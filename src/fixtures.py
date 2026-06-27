"""Fixtures — load upcoming games and attach model inputs.

Reads ``data/fixtures.csv`` (falling back to ``data/manual_fixtures.csv``), joins
each side to its team profile, resolves the probable starters against the pitcher
profiles, and attaches the home ballpark factor. Output is a list of dicts ready
for ``predict.project_fixture``.
"""
from __future__ import annotations

import csv
import os
import sys

from . import util


def _read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_fixtures(cfg: dict, profiles: dict) -> list[dict]:
    rows = _read_csv(util.abspath("data/fixtures.csv"))
    if not rows:
        rows = _read_csv(util.abspath(cfg["fixtures"]["manual_file"]))
    teams = profiles.get("teams", {})
    pitchers = profiles.get("pitchers", {})

    out = []
    for r in rows:
        h, a = str(r.get("homeTeamId", "")), str(r.get("awayTeamId", ""))
        if h not in teams or a not in teams:
            continue
        home_team, away_team = teams[h], teams[a]
        out.append({
            "gamePk": r.get("gamePk", ""),
            "date": r.get("date", ""),
            "status": r.get("status", ""),
            "homeId": int(h), "awayId": int(a),
            "home": home_team["name"], "away": away_team["name"],
            "homeAbbr": home_team["abbr"], "awayAbbr": away_team["abbr"],
            "home_team": home_team, "away_team": away_team,
            "home_sp": pitchers.get(str(r.get("homePitcherId", "")), None),
            "away_sp": pitchers.get(str(r.get("awayPitcherId", "")), None),
            "homePitcher": r.get("homePitcher", "") or (pitchers.get(str(r.get("homePitcherId", "")), {}) or {}).get("name", "TBD"),
            "awayPitcher": r.get("awayPitcher", "") or (pitchers.get(str(r.get("awayPitcherId", "")), {}) or {}).get("name", "TBD"),
            "park": home_team.get("park", 1.0),
        })
    return out


def main(argv) -> int:
    cfg = util.load_config()
    profiles = util.read_json(util.abspath(os.path.join(cfg["paths"]["models_dir"], "profiles.json")))
    fixtures = load_fixtures(cfg, profiles)
    for f in fixtures:
        util.log(f"{f['date']}  {f['awayAbbr']} @ {f['homeAbbr']}  ({f['awayPitcher']} vs {f['homePitcher']})")
    util.log(f"fixtures: {len(fixtures)} games resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
