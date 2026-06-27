"""Predict — price every upcoming fixture's full market book + player props.

For each resolved fixture: blend the run-scoring sim moneyline with team Elo, price
the market book (``sim.project_game``), and attach player props for the two probable
starters (strikeouts) and each side's top batters (hits / total bases / HR / SB).

Outputs:
* ``reports/predictions.csv``      — flat headline rows
* ``docs/data/predictions.json``   — full nested book for the site
"""
from __future__ import annotations

import csv
import os
import sys
import time

from . import fixtures, ratings, sim, util

TOP_BATTERS = 5


def _top_batters(profiles: dict, team_id: int) -> list[dict]:
    bs = [b for pid, b in profiles.get("batters", {}).items()
          if pid != "_league" and b.get("teamId") == team_id]
    bs.sort(key=lambda b: -b.get("pa", 0))
    return bs[:TOP_BATTERS]


def project_fixture(cfg: dict, fx: dict, profiles: dict, elo: dict | None) -> dict:
    elo_wp = None
    if elo:
        elo_wp = ratings.elo_win_prob(elo, fx["homeId"], fx["awayId"], elo.get("_meta", {}).get("home_field", 24.0))

    proj = sim.project_game(fx["home_team"], fx["away_team"], fx["home_sp"], fx["away_sp"],
                            fx["park"], cfg, elo_home_wp=elo_wp)

    props = {"home": [], "away": []}
    for side, sp, tid in (("home", fx["home_sp"], fx["homeId"]), ("away", fx["away_sp"], fx["awayId"])):
        entry = []
        if sp:
            entry.append({"player": sp["name"], "role": "P", "props": sim.pitcher_props(sp)})
        for b in _top_batters(profiles, tid):
            entry.append({"player": b["name"], "role": "B", "props": sim.batter_props(b)})
        props[side] = entry

    return {
        "gamePk": fx["gamePk"], "date": fx["date"], "status": fx["status"],
        "home": fx["home"], "away": fx["away"],
        "homeAbbr": fx["homeAbbr"], "awayAbbr": fx["awayAbbr"],
        "homePitcher": fx["homePitcher"], "awayPitcher": fx["awayPitcher"],
        "park": fx["park"],
        "elo_win_home": round(elo_wp, 4) if elo_wp is not None else None,
        **proj,
        "props": props,
    }


def run(cfg: dict) -> list[dict]:
    profiles = util.read_json(util.abspath(os.path.join(cfg["paths"]["models_dir"], "profiles.json")))
    elo_path = util.abspath(os.path.join(cfg["paths"]["models_dir"], "elo.json"))
    elo = util.read_json(elo_path) if os.path.exists(elo_path) else None
    fxs = fixtures.load_fixtures(cfg, profiles)
    preds = [project_fixture(cfg, fx, profiles, elo) for fx in fxs]
    preds.sort(key=lambda p: (p["date"], -p["win_home"]))
    return preds


def main(argv) -> int:
    cfg = util.load_config()
    preds = run(cfg)

    # docs JSON (full book)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "season": cfg["data"]["season"], "count": len(preds), "games": preds}
    util.write_json(util.abspath(os.path.join(cfg["paths"]["docs_data_dir"], "predictions.json")), out)

    # flat CSV (headline)
    path = util.abspath(os.path.join(cfg["paths"]["reports_dir"], "predictions.csv"))
    util.ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "away", "home", "awayPitcher", "homePitcher",
                    "win_home", "fair_home", "fair_away", "total_mean"])
        for p in preds:
            w.writerow([p["date"], p["away"], p["home"], p["awayPitcher"], p["homePitcher"],
                        p["win_home"], p["fair_home"], p["fair_away"], p["total_mean"]])
    util.log(f"predict: {len(preds)} games -> predictions.json + predictions.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
