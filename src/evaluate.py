"""Evaluate — walk-forward backtest of the team-Elo moneyline.

Elo is inherently as-of (each game is predicted from ratings *before* it is played),
so we replay results chronologically and score every game from
``backtest.holdout_start_season`` onward: log-loss, Brier, accuracy — against two
baselines (always-home, and a coin-flip). Writes ``reports/backtest.json``.
"""
from __future__ import annotations

import math
import os
import sys

from . import ingest, util


def _logloss(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def evaluate(cfg: dict) -> dict:
    e = cfg["elo"]
    init, k, hfa = e["initial"], e["k"], e["home_field"]
    holdout = cfg["backtest"]["holdout_start_season"]
    ratings: dict[str, float] = {}

    ll = brier = 0.0
    correct = n = 0
    home_wins = 0
    base_ll = 0.0

    seasons = sorted(cfg["data"]["history_seasons"])
    # First pass to estimate the home-win base rate over the full history.
    all_games = [(yr, g) for yr in seasons for g in ingest.load_results(cfg, yr)]
    if not all_games:
        return {"n": 0, "note": "no results available"}
    base_rate = sum(1 for _, g in all_games if g["homeRuns"] > g["awayRuns"]) / len(all_games)

    for si, yr in enumerate(seasons):
        if si > 0 and e.get("season_regression"):
            r = e["season_regression"]
            for tid in ratings:
                ratings[tid] = init + (ratings[tid] - init) * (1 - r)
        for g in ingest.load_results(cfg, yr):
            h, a = str(g["homeId"]), str(g["awayId"])
            rh, ra = ratings.get(h, init), ratings.get(a, init)
            p_home = 1.0 / (1.0 + 10 ** ((ra - (rh + hfa)) / 400.0))
            y = 1 if g["homeRuns"] > g["awayRuns"] else 0

            if yr >= holdout:
                ll += _logloss(p_home, y)
                brier += (p_home - y) ** 2
                base_ll += _logloss(base_rate, y)
                correct += 1 if (p_home >= 0.5) == (y == 1) else 0
                home_wins += y
                n += 1

            kf = k * (math.log(abs(g["homeRuns"] - g["awayRuns"]) + 1) if e.get("mov_mult") else 1.0)
            delta = kf * (y - p_home)
            ratings[h], ratings[a] = rh + delta, ra - delta

    if n == 0:
        return {"n": 0, "note": "no holdout games"}
    return {
        "holdout_start_season": holdout,
        "n": n,
        "log_loss": round(ll / n, 4),
        "brier": round(brier / n, 4),
        "accuracy": round(correct / n, 4),
        "home_win_rate": round(home_wins / n, 4),
        "baseline_log_loss": round(base_ll / n, 4),
        "beats_baseline": (ll / n) < (base_ll / n),
    }


def main(argv) -> int:
    cfg = util.load_config()
    report = evaluate(cfg)
    util.write_json(util.abspath(os.path.join(cfg["paths"]["reports_dir"], "backtest.json")), report)
    util.log(f"evaluate: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
