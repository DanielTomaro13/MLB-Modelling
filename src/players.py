"""Players — per-player season cards for the site directory.

Built from ``models/profiles.json`` (batters + pitchers). Writes:

* ``docs/data/players.json``        — {personId: {name, role, team, stats}}
* ``docs/data/players-index.json``  — lightweight [{id, name, role, team}] for search
"""
from __future__ import annotations

import os
import sys

from . import util


def build(cfg: dict) -> tuple[dict, list[dict]]:
    profiles = util.read_json(util.abspath(os.path.join(cfg["paths"]["models_dir"], "profiles.json")))
    teams = profiles.get("teams", {})

    def team_abbr(tid):
        return teams.get(str(tid), {}).get("abbr", "") if tid else ""

    players, index = {}, []

    for pid, b in profiles.get("batters", {}).items():
        if pid == "_league":
            continue
        players[pid] = {
            "name": b["name"], "role": "B", "team": team_abbr(b.get("teamId")),
            "stats": {"PA": b["pa"], "AVG": b["avg"], "OBP": b["obp"], "SLG": b["slg"],
                      "HR/PA": b["hr_pa"], "H/PA": b["hits_pa"], "SB/G": b["sb_pg"]},
        }
        index.append({"id": pid, "name": b["name"], "role": "B", "team": team_abbr(b.get("teamId"))})

    for pid, p in profiles.get("pitchers", {}).items():
        if pid == "_league":
            continue
        players[pid] = {
            "name": p["name"], "role": "P", "team": team_abbr(p.get("teamId")),
            "stats": {"IP": p["ip"], "GS": p["gs"], "RA9": p["ra9"], "K%": p["k_rate"],
                      "BB%": p["bb_rate"], "HR/9": p["hr9"], "starter": p["starter"]},
        }
        index.append({"id": pid, "name": p["name"], "role": "P", "team": team_abbr(p.get("teamId"))})

    index.sort(key=lambda x: x["name"])
    return players, index


def main(argv) -> int:
    cfg = util.load_config()
    players, index = build(cfg)
    util.write_json(util.abspath(os.path.join(cfg["paths"]["docs_data_dir"], "players.json")), players)
    util.write_json(util.abspath(os.path.join(cfg["paths"]["docs_data_dir"], "players-index.json")), index)
    util.log(f"players: {len(index)} cards -> players.json + players-index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
