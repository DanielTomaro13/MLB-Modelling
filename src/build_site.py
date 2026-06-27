"""Build site — publish the slim model artifacts the static site reads.

Copies/derives the public ``docs/data`` JSON from the ``models/`` outputs:

* ``profiles.json`` — slim team table (drops nothing large; teams only)
* ``ratings.json``  — Elo leaderboard (copied)
* ``meta.json``     — generation stamp, season, counts, backtest summary
"""
from __future__ import annotations

import os
import sys
import time

from . import util


def main(argv) -> int:
    cfg = util.load_config()
    models = cfg["paths"]["models_dir"]
    ddata = cfg["paths"]["docs_data_dir"]

    profiles = util.read_json(util.abspath(os.path.join(models, "profiles.json")))
    slim_teams = {
        tid: {k: t[k] for k in ("name", "abbr", "league", "division", "venueId",
                                "park", "rs_pg", "ra_pg", "off", "def", "gp") if k in t}
        for tid, t in profiles.get("teams", {}).items()
    }
    util.write_json(util.abspath(os.path.join(ddata, "profiles.json")),
                    {"season": profiles.get("season"), "teams": slim_teams})

    ratings_path = util.abspath(os.path.join(models, "ratings.json"))
    if os.path.exists(ratings_path):
        util.write_json(util.abspath(os.path.join(ddata, "ratings.json")), util.read_json(ratings_path))

    backtest = {}
    bt_path = util.abspath(os.path.join(cfg["paths"]["reports_dir"], "backtest.json"))
    if os.path.exists(bt_path):
        backtest = util.read_json(bt_path)

    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "season": profiles.get("season"),
        "n_teams": len(slim_teams),
        "n_pitchers": len(profiles.get("pitchers", {})) - 1,
        "n_batters": len(profiles.get("batters", {})) - 1,
        "backtest": backtest,
    }
    util.write_json(util.abspath(os.path.join(ddata, "meta.json")), meta)
    util.log("build_site: profiles.json, ratings.json, meta.json refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
