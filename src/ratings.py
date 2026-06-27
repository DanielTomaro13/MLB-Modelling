"""Ratings — chronological team Elo from derived results.

Walks every game in ``data/processed/results-{season}.csv`` in date order, updating
each club's Elo (with a home-field bonus and an optional margin-of-victory multiplier),
regressing toward the mean between seasons. Writes:

* ``models/elo.json``     — {teamId: {elo, played}} + ``_meta``
* ``models/ratings.json`` — a ranked leaderboard joined with team names
"""
from __future__ import annotations

import math
import os
import sys

from . import ingest, util


def elo_win_prob(elo: dict, home_id, away_id, home_field: float) -> float:
    rh = elo.get(str(home_id), {}).get("elo", 1500.0) + home_field
    ra = elo.get(str(away_id), {}).get("elo", 1500.0)
    return 1.0 / (1.0 + 10 ** ((ra - rh) / 400.0))


def compute_elo(cfg: dict) -> dict:
    e = cfg["elo"]
    init, k, hfa = e["initial"], e["k"], e["home_field"]
    ratings: dict[str, float] = {}
    played: dict[str, int] = {}

    seasons = sorted(cfg["data"]["history_seasons"])
    for si, yr in enumerate(seasons):
        if si > 0 and e.get("season_regression"):
            r = e["season_regression"]
            for tid in ratings:
                ratings[tid] = init + (ratings[tid] - init) * (1 - r)
        for g in ingest.load_results(cfg, yr):
            h, a = str(g["homeId"]), str(g["awayId"])
            rh = ratings.get(h, init)
            ra = ratings.get(a, init)
            exp_h = 1.0 / (1.0 + 10 ** ((ra - (rh + hfa)) / 400.0))
            home_won = 1.0 if g["homeRuns"] > g["awayRuns"] else 0.0
            kf = k
            if e.get("mov_mult"):
                margin = abs(g["homeRuns"] - g["awayRuns"])
                kf *= math.log(margin + 1)
            delta = kf * (home_won - exp_h)
            ratings[h] = rh + delta
            ratings[a] = ra - delta
            played[h] = played.get(h, 0) + 1
            played[a] = played.get(a, 0) + 1

    out = {tid: {"elo": round(r, 1), "played": played.get(tid, 0)} for tid, r in ratings.items()}
    out["_meta"] = {"home_field": hfa, "seasons": seasons}
    return out


def build_leaderboard(cfg: dict, elo: dict) -> list[dict]:
    teams = util.read_json(util.abspath(os.path.join(cfg["data"]["raw_dir"], "teams.json"))).get("teams", [])
    meta = {str(t["id"]): t for t in teams}
    rows = []
    for tid, r in elo.items():
        if tid == "_meta":
            continue
        t = meta.get(tid, {})
        rows.append({
            "teamId": int(tid),
            "name": t.get("name", tid),
            "abbr": t.get("abbreviation", ""),
            "division": t.get("division", {}).get("name", ""),
            "elo": r["elo"],
            "played": r["played"],
        })
    rows.sort(key=lambda x: -x["elo"])
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def main(argv) -> int:
    cfg = util.load_config()
    elo = compute_elo(cfg)
    util.write_json(util.abspath(os.path.join(cfg["paths"]["models_dir"], "elo.json")), elo)
    board = build_leaderboard(cfg, elo)
    util.write_json(util.abspath(os.path.join(cfg["paths"]["models_dir"], "ratings.json")), {"teams": board})
    util.log(f"ratings: Elo over {len(board)} teams")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
