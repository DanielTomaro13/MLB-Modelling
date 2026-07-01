"""Features — build per-team, per-pitcher and per-batter rate profiles.

Reads the raw caches from ``ingest`` and writes ``models/profiles.json``:

    {
      "season": 2025,
      "league": {... league-average rates ...},
      "teams":    {teamId: {name, abbr, league, division, venueId, park,
                            gp, rs_pg, ra_pg, off, def, hr_pg}},
      "pitchers": {personId: {name, teamId, bf, ip, gs, starter,
                              k_rate, bb_rate, hr9, ra9, prevent}},
      "batters":  {personId: {name, teamId, pa, hits_pa, hr_pa, tb_pa,
                              k_pa, bb_pa, sb_pg, avg, obp, slg}}
    }

* ``off`` / ``def`` are runs scored / allowed per game divided by the league
  average (1.00 = league average), shrunk toward 1.00 for small samples.
* ``prevent`` is a starter's run-prevention multiplier (league RA9 / pitcher RA9,
  shrunk) — >1 means the pitcher suppresses runs better than average.
"""
from __future__ import annotations

import csv
import os
import sys

from . import ingest, util


def _raw(cfg: dict, name: str) -> str:
    return util.abspath(os.path.join(cfg["data"]["raw_dir"], name))


def _shrink(rate: float, prior: float, n: float, prior_n: float) -> float:
    """Empirical-Bayes shrinkage toward ``prior``."""
    if n <= 0:
        return prior
    return (rate * n + prior * prior_n) / (n + prior_n)


# --------------------------------------------------------------------------- #
# Park factors
# --------------------------------------------------------------------------- #
def load_park_factors(cfg: dict) -> dict[str, float]:
    path = util.abspath(cfg["data"]["park_factors_file"])
    out: dict[str, float] = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[str(r["team_id"])] = util.num(r["park_factor"], 1.0)
    return out


def derive_park_factors(cfg: dict, meta: dict) -> dict[str, float]:
    """Park factor per team from its own results: total runs/game at the current
    home venue vs on the road (team quality cancels — the same roster plays both
    halves), shrunk toward 1.0 and normalised to a league mean of 1.0.

    Home games are filtered to the team's *current* venue so relocations (the
    Athletics' 2025 move to Sacramento) don't drag stale-park seasons in.
    """
    home: dict[str, list[int]] = {}
    road: dict[str, list[int]] = {}
    for yr in cfg["data"]["history_seasons"]:
        for g in ingest.load_results(cfg, yr):
            tot = g["homeRuns"] + g["awayRuns"]
            h, a = str(g["homeId"]), str(g["awayId"])
            if str(g["venueId"]) == str(meta.get(h, {}).get("venueId", "")):
                home.setdefault(h, []).append(tot)
            road.setdefault(a, []).append(tot)
    prior_n = 60.0  # ~a third of a season of home games of "league average" prior
    pf = {}
    for tid in meta:
        ht, rt = home.get(tid, []), road.get(tid, [])
        if ht and rt:
            raw = (sum(ht) / len(ht)) / (sum(rt) / len(rt))
            pf[tid] = 1.0 + (raw - 1.0) * len(ht) / (len(ht) + prior_n)
    if not pf:
        return {}
    m = sum(pf.values()) / len(pf)
    return {tid: round(v / m, 4) for tid, v in pf.items()}


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #
def _team_meta(cfg: dict) -> dict[str, dict]:
    teams = util.read_json(_raw(cfg, "teams.json")).get("teams", [])
    out = {}
    for t in teams:
        out[str(t["id"])] = {
            "name": t["name"],
            "abbr": t.get("abbreviation", ""),
            "league": t.get("league", {}).get("id"),
            "division": t.get("division", {}).get("name", ""),
            "venueId": t.get("venue", {}).get("id"),
        }
    return out


def build_team_profiles(cfg: dict) -> tuple[dict, float]:
    season = cfg["data"]["season"]
    meta = _team_meta(cfg)
    # Results-derived park factors; the static CSV is the fallback for teams
    # without enough games at their current venue.
    park_csv = load_park_factors(cfg)
    park = {**park_csv, **derive_park_factors(cfg, meta)}
    standings = util.read_json(_raw(cfg, f"standings-{season}.json")).get("records", [])

    raw = {}
    total_rs = total_g = 0.0
    for rec in standings:
        for tr in rec.get("teamRecords", []):
            tid = str(tr["team"]["id"])
            w, l = tr.get("wins", 0), tr.get("losses", 0)
            gp = max(w + l, 1)
            rs, ra = tr.get("runsScored", 0), tr.get("runsAllowed", 0)
            raw[tid] = {"gp": gp, "rs": rs, "ra": ra}
            total_rs += rs
            total_g += gp
    league_rpg = (total_rs / total_g) if total_g else cfg["sim"]["league_runs_per_team"]

    prior_g = cfg["features"]["team_prior_games"]
    teams = {}
    for tid, m in meta.items():
        r = raw.get(tid, {"gp": 0, "rs": 0, "ra": 0})
        gp = r["gp"]
        rs_pg = r["rs"] / gp if gp else league_rpg
        ra_pg = r["ra"] / gp if gp else league_rpg
        # Half a team's games are at its home park, so raw run rates carry half
        # the park effect — divide it out so ``off``/``def`` are park-neutral
        # (the game park is re-applied at pricing time; see sim.team_means).
        exposure = (1.0 + park.get(tid, 1.0)) / 2.0
        off = _shrink(rs_pg / league_rpg / exposure, 1.0, gp, prior_g)
        dfn = _shrink(ra_pg / league_rpg / exposure, 1.0, gp, prior_g)
        teams[tid] = {
            **m,
            "park": park.get(tid, 1.0),
            "gp": gp,
            "rs_pg": round(rs_pg, 3),
            "ra_pg": round(ra_pg, 3),
            "off": round(off, 4),
            "def": round(dfn, 4),
        }
    return teams, league_rpg


# --------------------------------------------------------------------------- #
# Pitchers
# --------------------------------------------------------------------------- #
def build_pitcher_profiles(cfg: dict, parks: dict[str, float] | None = None) -> dict:
    season = cfg["data"]["season"]
    splits = util.read_json(_raw(cfg, f"pitching-{season}.json"))
    parks = parks or {}

    # League RA9 / rates across pitchers with meaningful samples.
    tot_r = tot_er = tot_ip = tot_bf = tot_k = tot_bb = tot_hr = 0.0
    for sp in splits:
        s = sp["stat"]
        ip = util.innings_to_float(s.get("inningsPitched"))
        if ip < 10:
            continue
        tot_ip += ip
        tot_r += util.num(s.get("runs"))
        tot_er += util.num(s.get("earnedRuns"))
        tot_bf += util.num(s.get("battersFaced"))
        tot_k += util.num(s.get("strikeOuts"))
        tot_bb += util.num(s.get("baseOnBalls"))
        tot_hr += util.num(s.get("homeRuns"))
    league_ra9 = (tot_r / tot_ip * 9) if tot_ip else 4.45
    league_er9 = (tot_er / tot_ip * 9) if tot_ip else 4.1
    lg_k = tot_k / tot_bf if tot_bf else 0.22
    lg_bb = tot_bb / tot_bf if tot_bf else 0.08
    lg_hr9 = tot_hr / tot_ip * 9 if tot_ip else 1.1

    prior_bf = cfg["features"]["pitcher_prior_bf"]
    out = {}
    for sp in splits:
        s = sp["stat"]
        pid = str(sp["player"]["id"])
        bf = util.num(s.get("battersFaced"))
        ip = util.innings_to_float(s.get("inningsPitched"))
        gs = int(util.num(s.get("gamesStarted")))
        if bf < 1:
            continue
        ra9 = (util.num(s.get("runs")) / ip * 9) if ip > 0 else league_ra9
        er9 = (util.num(s.get("earnedRuns")) / ip * 9) if ip > 0 else league_er9
        ra9_sh = _shrink(ra9, league_ra9, bf, prior_bf)
        # Half a pitcher's innings come at his home park — neutralise it before
        # comparing to league (the game park is re-applied at pricing time).
        exposure = (1.0 + parks.get(str((sp.get("team") or {}).get("id")), 1.0)) / 2.0
        ra9_neutral = _shrink(ra9 / exposure, league_ra9, bf, prior_bf)
        out[pid] = {
            "name": sp["player"]["fullName"],
            "teamId": (sp.get("team") or {}).get("id"),
            "bf": bf,
            "ip": round(ip, 1),
            "gs": gs,
            "starter": gs >= 5,
            "k_rate": round(_shrink(util.num(s.get("strikeOuts")) / bf, lg_k, bf, prior_bf), 4),
            "bb_rate": round(_shrink(util.num(s.get("baseOnBalls")) / bf, lg_bb, bf, prior_bf), 4),
            "hr9": round(_shrink(util.num(s.get("homeRuns")) / ip * 9 if ip else lg_hr9, lg_hr9, bf, prior_bf), 3),
            "ra9": round(ra9_sh, 3),
            # Earned runs only (the ER prop settles on earned, not total, runs).
            "er9": round(_shrink(er9, league_er9, bf, prior_bf), 3),
            # >1 suppresses runs better than league average (used as a defensive multiplier).
            "prevent": round(league_ra9 / ra9_neutral if ra9_neutral > 0 else 1.0, 4),
        }
    out["_league"] = {"ra9": round(league_ra9, 3), "er9": round(league_er9, 3),
                      "k_rate": round(lg_k, 4), "bb_rate": round(lg_bb, 4), "hr9": round(lg_hr9, 3)}
    return out


# --------------------------------------------------------------------------- #
# Batters
# --------------------------------------------------------------------------- #
def build_batter_profiles(cfg: dict) -> dict:
    season = cfg["data"]["season"]
    splits = util.read_json(_raw(cfg, f"hitting-{season}.json"))
    prior_pa = cfg["features"]["batter_prior_pa"]
    min_pa = cfg["features"]["min_batter_pa"]

    # League per-PA rates over qualified-ish hitters.
    tot_pa = tot_h = tot_hr = tot_tb = tot_k = tot_bb = 0.0
    for sp in splits:
        s = sp["stat"]
        pa = util.num(s.get("plateAppearances"))
        if pa < 30:
            continue
        tot_pa += pa
        tot_h += util.num(s.get("hits"))
        tot_hr += util.num(s.get("homeRuns"))
        tot_tb += util.num(s.get("totalBases"))
        tot_k += util.num(s.get("strikeOuts"))
        tot_bb += util.num(s.get("baseOnBalls"))
    lg = {
        "hits_pa": tot_h / tot_pa if tot_pa else 0.23,
        "hr_pa": tot_hr / tot_pa if tot_pa else 0.03,
        "tb_pa": tot_tb / tot_pa if tot_pa else 0.37,
        "k_pa": tot_k / tot_pa if tot_pa else 0.22,
        "bb_pa": tot_bb / tot_pa if tot_pa else 0.08,
    }

    out = {}
    for sp in splits:
        s = sp["stat"]
        pid = str(sp["player"]["id"])
        pa = util.num(s.get("plateAppearances"))
        if pa < min_pa:
            continue
        gp = max(util.num(s.get("gamesPlayed")), 1)
        out[pid] = {
            "name": sp["player"]["fullName"],
            "teamId": (sp.get("team") or {}).get("id"),
            "pa": pa,
            "gp": int(gp),
            "hits_pa": round(_shrink(util.num(s.get("hits")) / pa, lg["hits_pa"], pa, prior_pa), 4),
            "hr_pa": round(_shrink(util.num(s.get("homeRuns")) / pa, lg["hr_pa"], pa, prior_pa), 4),
            "tb_pa": round(_shrink(util.num(s.get("totalBases")) / pa, lg["tb_pa"], pa, prior_pa), 4),
            "k_pa": round(_shrink(util.num(s.get("strikeOuts")) / pa, lg["k_pa"], pa, prior_pa), 4),
            "bb_pa": round(_shrink(util.num(s.get("baseOnBalls")) / pa, lg["bb_pa"], pa, prior_pa), 4),
            "sb_pg": round(util.num(s.get("stolenBases")) / gp, 4),
            "rbi_pg": round(util.num(s.get("rbi")) / gp, 4),
            "runs_pg": round(util.num(s.get("runs")) / gp, 4),
            "doubles_pg": round(util.num(s.get("doubles")) / gp, 4),
            "avg": s.get("avg", ".000"),
            "obp": s.get("obp", ".000"),
            "slg": s.get("slg", ".000"),
        }
    out["_league"] = {k: round(v, 4) for k, v in lg.items()}
    return out


# --------------------------------------------------------------------------- #
def build(cfg: dict) -> dict:
    teams, league_rpg = build_team_profiles(cfg)
    parks = {tid: t["park"] for tid, t in teams.items()}
    pitchers = build_pitcher_profiles(cfg, parks)
    batters = build_batter_profiles(cfg)
    return {
        "season": cfg["data"]["season"],
        "league": {"runs_per_team_game": round(league_rpg, 3)},
        "teams": teams,
        "pitchers": pitchers,
        "batters": batters,
    }


def main(argv) -> int:
    cfg = util.load_config()
    profiles = build(cfg)
    path = util.abspath(os.path.join(cfg["paths"]["models_dir"], "profiles.json"))
    util.write_json(path, profiles)
    util.log(
        f"features: {len(profiles['teams'])} teams, "
        f"{len(profiles['pitchers']) - 1} pitchers, {len(profiles['batters']) - 1} batters -> {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
