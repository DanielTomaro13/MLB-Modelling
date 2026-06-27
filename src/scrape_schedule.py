"""Schedule — upcoming fixtures from the official MLB schedule endpoint.

Pulls today + ``fixtures.days_ahead`` days from
``/schedule?sportId=1&hydrate=probablePitcher,team`` and writes ``data/fixtures.csv``
(one row per game, with probable starters and venue). ``data/manual_fixtures.csv``
is the guaranteed fallback when the API is unreachable.
"""
from __future__ import annotations

import csv
import datetime
import os
import sys

from . import util

COLS = ["gamePk", "date", "homeTeamId", "awayTeamId", "home", "away",
        "homePitcherId", "awayPitcherId", "homePitcher", "awayPitcher", "venueId", "status"]


def scrape(cfg: dict) -> list[dict]:
    start = datetime.date.today()
    end = start + datetime.timedelta(days=cfg["fixtures"]["days_ahead"])
    url = (
        f"{cfg['data']['api_base']}/schedule?sportId={cfg['sport_id']}"
        f"&startDate={start.isoformat()}&endDate={end.isoformat()}"
        f"&hydrate=probablePitcher,team"
    )
    data = util.http_get_json(url)
    rows = []
    for day in (data or {}).get("dates", []):
        for g in day.get("games", []):
            state = g.get("status", {}).get("abstractGameState")
            if state == "Final":
                continue
            home, away = g["teams"]["home"], g["teams"]["away"]
            hp, ap = home.get("probablePitcher", {}), away.get("probablePitcher", {})
            rows.append({
                "gamePk": g["gamePk"],
                "date": (g.get("officialDate") or g.get("gameDate", "")[:10]),
                "homeTeamId": home["team"]["id"],
                "awayTeamId": away["team"]["id"],
                "home": home["team"]["name"],
                "away": away["team"]["name"],
                "homePitcherId": hp.get("id", ""),
                "awayPitcherId": ap.get("id", ""),
                "homePitcher": hp.get("fullName", ""),
                "awayPitcher": ap.get("fullName", ""),
                "venueId": g.get("venue", {}).get("id", ""),
                "status": g.get("status", {}).get("detailedState", ""),
            })
    return rows


def write_csv(path: str, rows: list[dict]) -> None:
    util.ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def main(argv) -> int:
    cfg = util.load_config()
    path = util.abspath("data/fixtures.csv")
    try:
        rows = scrape(cfg)
        if not rows:
            raise RuntimeError("no upcoming games returned")
        write_csv(path, rows)
        util.log(f"scrape_schedule: {len(rows)} upcoming games -> {path}")
    except Exception as exc:  # noqa: BLE001
        util.log(f"scrape_schedule: failed ({exc}); manual fixtures remain the source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
