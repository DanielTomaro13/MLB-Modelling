"""Run-scoring engine — pure-Python, no I/O.

A team's expected runs come from its offense rating, the opposing starter's run
prevention (blended with the opponent's bullpen/defense for the back of the game),
the ballpark factor, and home-field advantage. Each team's runs are modelled as a
negative-binomial (runs are over-dispersed relative to Poisson), the two marginals
are combined into an independent joint score grid, and every market is read off the
grid: moneyline, run line ±1.5, totals O/U, first-5-innings, team totals,
correct-score. Player props are priced from rate profiles via Poisson tails.

This is a clean-room implementation built for the published site; it shares no code
with any private pricing engine.
"""
from __future__ import annotations

import math

PA_PER_GAME = 4.3          # plate appearances for a lineup regular
SP_INNINGS_FRACTION = 0.60  # share of a 9-inning game a starter typically covers


# --------------------------------------------------------------------------- #
# Distributions
# --------------------------------------------------------------------------- #
def nb_pmf(mu: float, phi: float, n: int) -> list[float]:
    """Negative-binomial pmf over 0..n with mean ``mu`` and variance ``phi*mu``.

    Falls back to Poisson when ``phi <= 1``. ``r`` (the dispersion) may be real.
    """
    mu = max(mu, 1e-6)
    if phi <= 1.0:
        return poisson_pmf(mu, n)
    r = mu / (phi - 1.0)
    p = r / (r + mu)            # P(success); pmf(k) ∝ (1-p)^k
    pmf = [0.0] * (n + 1)
    pmf[0] = p ** r
    for k in range(1, n + 1):
        pmf[k] = pmf[k - 1] * (k + r - 1) / k * (1 - p)
    s = sum(pmf)
    return [x / s for x in pmf] if s else pmf


def poisson_pmf(mu: float, n: int) -> list[float]:
    mu = max(mu, 1e-6)
    pmf = [0.0] * (n + 1)
    pmf[0] = math.exp(-mu)
    for k in range(1, n + 1):
        pmf[k] = pmf[k - 1] * mu / k
    s = sum(pmf)
    return [x / s for x in pmf] if s else pmf


def poisson_over(mu: float, line: float) -> float:
    """P(count > line) for a Poisson, line a half-integer (no push)."""
    k = int(math.floor(line)) + 1
    pmf = poisson_pmf(mu, max(k + 40, 20))
    return sum(pmf[k:])


def fair(prob: float) -> float | None:
    return round(1.0 / prob, 3) if prob and prob > 1e-9 else None


# --------------------------------------------------------------------------- #
# Expected runs
# --------------------------------------------------------------------------- #
def _allow_mult(starter: dict | None, team_def: float) -> float:
    """Opponent run-allow multiplier vs league (1.0 = average; <1 suppresses runs)."""
    if starter and starter.get("prevent"):
        sp_factor = 1.0 / starter["prevent"]      # pitcher RA9 / league RA9
    else:
        sp_factor = 1.0
    f = SP_INNINGS_FRACTION
    return f * sp_factor + (1 - f) * team_def


def team_means(home, away, home_sp, away_sp, park, cfg) -> dict:
    """Return expected runs for both teams, full game and first-5-innings."""
    lg = cfg["sim"]["league_runs_per_team"]
    hfa = cfg["sim"]["home_field_runs"]

    home_allow_opp = _allow_mult(away_sp, away.get("def", 1.0))   # how many runs HOME scores
    away_allow_opp = _allow_mult(home_sp, home.get("def", 1.0))

    mu_home = lg * home.get("off", 1.0) * home_allow_opp * park + hfa
    mu_away = lg * away.get("off", 1.0) * away_allow_opp * park

    # First-5: starter-dominated, so use the starter alone, scaled to ~5 innings.
    f5_scale = 5.0 / 9.0
    sp_home_allow = (1.0 / away_sp["prevent"]) if (away_sp and away_sp.get("prevent")) else 1.0
    sp_away_allow = (1.0 / home_sp["prevent"]) if (home_sp and home_sp.get("prevent")) else 1.0
    mu_home_f5 = lg * home.get("off", 1.0) * sp_home_allow * park * f5_scale + hfa * f5_scale
    mu_away_f5 = lg * away.get("off", 1.0) * sp_away_allow * park * f5_scale

    return {
        "home": max(mu_home, 0.2), "away": max(mu_away, 0.2),
        "home_f5": max(mu_home_f5, 0.1), "away_f5": max(mu_away_f5, 0.1),
    }


# --------------------------------------------------------------------------- #
# Markets from a joint grid
# --------------------------------------------------------------------------- #
def _grid_markets(ph: list[float], pa: list[float], mu_h: float, mu_a: float, cfg) -> dict:
    n = len(ph) - 1
    p_home = p_away = p_tie = 0.0
    p_over = {ln: 0.0 for ln in cfg["sim"]["totals_lines"]}
    rl = cfg["sim"]["run_line"]
    p_home_cover = p_away_cover = 0.0
    cs = []  # correct-score cells

    for i in range(n + 1):
        if ph[i] < 1e-12:
            continue
        for j in range(n + 1):
            p = ph[i] * pa[j]
            if p < 1e-12:
                continue
            tot = i + j
            for ln in p_over:
                if tot > ln:
                    p_over[ln] += p
            if i - j >= math.ceil(rl):
                p_home_cover += p
            else:
                p_away_cover += p
            if i > j:
                p_home += p
            elif j > i:
                p_away += p
            else:
                p_tie += p
            cs.append((p, i, j))

    # Resolve regulation ties (extra innings) toward the higher-scoring profile.
    split = mu_h / (mu_h + mu_a) if (mu_h + mu_a) else 0.5
    win_home = p_home + p_tie * split
    win_away = p_away + p_tie * (1 - split)
    tot = win_home + win_away or 1.0
    win_home, win_away = win_home / tot, win_away / tot

    cs.sort(reverse=True)
    correct_score = [{"home": i, "away": j, "prob": round(p, 4), "fair": fair(p)} for p, i, j in cs[:8]]

    return {
        "win_home": win_home, "win_away": win_away,
        "p_over": p_over, "p_home_cover": p_home_cover, "p_away_cover": p_away_cover,
        "correct_score": correct_score,
    }


def _selection(label, prob):
    return {"label": label, "prob": round(prob, 4), "fair": fair(prob)}


def project_game(home, away, home_sp, away_sp, park, cfg, elo_home_wp: float | None = None) -> dict:
    """Full market book for one game. ``home``/``away`` are team profiles."""
    phi = cfg["sim"]["overdispersion"]
    n = cfg["sim"]["max_team_runs"]
    mu = team_means(home, away, home_sp, away_sp, park, cfg)

    ph, pa = nb_pmf(mu["home"], phi, n), nb_pmf(mu["away"], phi, n)
    g = _grid_markets(ph, pa, mu["home"], mu["away"], cfg)

    # Blend the sim moneyline with the Elo win prob (logit space) for the headline.
    win_home = g["win_home"]
    if elo_home_wp is not None:
        w = cfg["sim"]["elo_weight"]
        win_home = _logit_blend(g["win_home"], elo_home_wp, w)
    win_away = 1 - win_home

    markets = []
    markets.append({"key": "ml", "label": "Moneyline", "selections": [
        _selection("home", win_home), _selection("away", win_away)]})

    markets.append({"key": "rl", "label": f"Run line ±{cfg['sim']['run_line']}", "selections": [
        _selection(f"home -{cfg['sim']['run_line']}", g["p_home_cover"]),
        _selection(f"away +{cfg['sim']['run_line']}", g["p_away_cover"])]})

    totals = []
    for ln in cfg["sim"]["totals_lines"]:
        ov = g["p_over"][ln]
        totals.append({"line": ln, "over": round(ov, 4), "under": round(1 - ov, 4),
                       "over_fair": fair(ov), "under_fair": fair(1 - ov)})
    markets.append({"key": "total", "label": "Total runs", "lines": totals})

    # Team totals from marginals.
    tt = []
    for side, pmf in (("home", ph), ("away", pa)):
        for ln in (3.5, 4.5, 5.5):
            ov = sum(pmf[int(math.floor(ln)) + 1:])
            tt.append({"side": side, "line": ln, "over": round(ov, 4), "under": round(1 - ov, 4),
                       "over_fair": fair(ov), "under_fair": fair(1 - ov)})
    markets.append({"key": "team_total", "label": "Team totals", "lines": tt})

    markets.append({"key": "cs", "label": "Correct score", "cells": g["correct_score"]})

    # First five innings.
    if cfg["sim"].get("first_five"):
        ph5, pa5 = nb_pmf(mu["home_f5"], phi, 12), nb_pmf(mu["away_f5"], phi, 12)
        g5 = _grid_markets(ph5, pa5, mu["home_f5"], mu["away_f5"],
                           {"sim": {"totals_lines": [4.5, 5.5], "run_line": 1.5}})
        # Raw three-way (ties stay as a draw selection for F5).
        ph_win = pa_win = ptie = 0.0
        for i in range(13):
            for j in range(13):
                p = ph5[i] * pa5[j]
                if i > j: ph_win += p
                elif j > i: pa_win += p
                else: ptie += p
        markets.append({"key": "f5_ml", "label": "First 5 — winner", "selections": [
            _selection("home", ph_win), _selection("away", pa_win), _selection("tie", ptie)]})
        f5t = []
        for ln in (4.5, 5.5):
            ov = g5["p_over"][ln]
            f5t.append({"line": ln, "over": round(ov, 4), "under": round(1 - ov, 4),
                        "over_fair": fair(ov), "under_fair": fair(1 - ov)})
        markets.append({"key": "f5_total", "label": "First 5 — total", "lines": f5t})

    return {
        "mu_home": round(mu["home"], 3), "mu_away": round(mu["away"], 3),
        "total_mean": round(mu["home"] + mu["away"], 3),
        "win_home": round(win_home, 4), "win_away": round(win_away, 4),
        "fair_home": fair(win_home), "fair_away": fair(win_away),
        "markets": markets,
    }


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _logit_blend(p_sim: float, p_elo: float, w_elo: float) -> float:
    z = (1 - w_elo) * _logit(p_sim) + w_elo * _logit(p_elo)
    return 1.0 / (1.0 + math.exp(-z))


# --------------------------------------------------------------------------- #
# Player props
# --------------------------------------------------------------------------- #
def batter_props(batter: dict) -> list[dict]:
    pa = PA_PER_GAME
    out = []
    for stat, rate_key, lines in (
        ("Hits", "hits_pa", [0.5, 1.5, 2.5]),
        ("Total bases", "tb_pa", [1.5, 2.5, 3.5]),
        ("Home run", "hr_pa", [0.5]),
    ):
        mu = batter.get(rate_key, 0.0) * pa
        sels = [{"line": ln, "over": round(poisson_over(mu, ln), 4),
                 "over_fair": fair(poisson_over(mu, ln))} for ln in lines]
        out.append({"stat": stat, "mu": round(mu, 3), "lines": sels})
    # Stolen base is a per-game rate already.
    sb_mu = batter.get("sb_pg", 0.0)
    out.append({"stat": "Stolen base", "mu": round(sb_mu, 3),
                "lines": [{"line": 0.5, "over": round(poisson_over(sb_mu, 0.5), 4),
                           "over_fair": fair(poisson_over(sb_mu, 0.5))}]})
    return out


def pitcher_props(pitcher: dict) -> list[dict]:
    # Estimate batters faced this start from innings/start (or a 5.5 IP default).
    ip_start = (pitcher["ip"] / pitcher["gs"]) if pitcher.get("gs") else 5.5
    bf = max(ip_start, 3.0) * 4.3
    mu_k = pitcher.get("k_rate", 0.22) * bf
    lines = [4.5, 5.5, 6.5, 7.5]
    sels = [{"line": ln, "over": round(poisson_over(mu_k, ln), 4),
             "over_fair": fair(poisson_over(mu_k, ln))} for ln in lines]
    return [{"stat": "Strikeouts", "mu": round(mu_k, 3), "lines": sels}]
