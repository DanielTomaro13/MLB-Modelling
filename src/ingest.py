"""Ingest — cache the public MLB Stats API into ``data/raw`` and derive results.

All data is the official, no-auth MLB Stats API (``statsapi.mlb.com``):

* ``teams.json``               — the 30 clubs (id, abbr, league, division, venue)
* ``standings-{season}.json``  — W/L + runs scored/allowed per team
* ``team-stats-{season}.json`` — team hitting + pitching season aggregates
* ``pitching-{season}.json``   — every pitcher's season line (for starters)
* ``hitting-{season}.json``    — every batter's season line (for props)

``derive_results`` streams each season's regular-season schedule (with linescore)
into ``data/processed/results-{season}.csv`` — one row per final game — which feeds
Elo (``ratings.py``) and the backtest (``evaluate.py``).
"""
from __future__ import annotations

import csv
import os
import sys

from . import util


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _raw(cfg: dict, name: str) -> str:
    return util.abspath(os.path.join(cfg["data"]["raw_dir"], name))


def _processed(cfg: dict, name: str) -> str:
    return util.abspath(os.path.join(cfg["data"]["processed_dir"], name))


def _api(cfg: dict, path: str) -> str:
    return f"{cfg['data']['api_base']}{path}"


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _fetch_all_player_stats(cfg: dict, group: str, season: int) -> list[dict]:
    """Page through the league-wide ``/stats`` endpoint for a stat group."""
    rows, offset, limit = [], 0, 1000
    while True:
        url = _api(
            cfg,
            f"/stats?stats=season&group={group}&season={season}"
            f"&sportId={cfg['sport_id']}&playerPool=all&limit={limit}&offset={offset}",
        )
        data = util.http_get_json(url)
        if not data or not data.get("stats"):
            break
        splits = data["stats"][0].get("splits", [])
        if not splits:
            break
        rows.extend(splits)
        total = data["stats"][0].get("totalSplits", len(rows))
        offset += limit
        if offset >= total:
            break
    return rows


def download_core(cfg: dict) -> None:
    seasons = cfg["data"]["history_seasons"]
    season = cfg["data"]["season"]

    util.log("ingest: teams")
    teams = util.http_get_json(_api(cfg, f"/teams?sportId={cfg['sport_id']}"))
    util.write_json(_raw(cfg, "teams.json"), teams)

    for yr in seasons:
        util.log(f"ingest: standings {yr}")
        st = util.http_get_json(
            _api(cfg, f"/standings?leagueId={cfg['leagues']['al']},{cfg['leagues']['nl']}&season={yr}")
        )
        util.write_json(_raw(cfg, f"standings-{yr}.json"), st)

    util.log(f"ingest: team stats {season}")
    tstats = util.http_get_json(
        _api(cfg, f"/teams/stats?season={season}&sportId={cfg['sport_id']}&stats=season&group=hitting,pitching")
    )
    util.write_json(_raw(cfg, f"team-stats-{season}.json"), tstats)

    util.log(f"ingest: player pitching {season}")
    util.write_json(_raw(cfg, f"pitching-{season}.json"), _fetch_all_player_stats(cfg, "pitching", season))
    util.log(f"ingest: player hitting {season}")
    util.write_json(_raw(cfg, f"hitting-{season}.json"), _fetch_all_player_stats(cfg, "hitting", season))


# --------------------------------------------------------------------------- #
# Results derivation (for Elo + backtest)
# --------------------------------------------------------------------------- #
RESULT_COLS = ["gamePk", "date", "homeId", "awayId", "home", "away", "homeRuns", "awayRuns", "venueId", "winnerId"]


def derive_results(cfg: dict) -> None:
    for yr in cfg["data"]["history_seasons"]:
        url = _api(
            cfg,
            f"/schedule?sportId={cfg['sport_id']}&season={yr}&gameType=R"
            f"&startDate={yr}-03-01&endDate={yr}-11-30&hydrate=linescore",
        )
        data = util.http_get_json(url, timeout=240)
        rows = []
        for day in (data or {}).get("dates", []):
            for g in day.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                home, away = g["teams"]["home"], g["teams"]["away"]
                hr, ar = home.get("score"), away.get("score")
                if hr is None or ar is None:
                    continue
                rows.append({
                    "gamePk": g["gamePk"],
                    "date": (g.get("officialDate") or g.get("gameDate", "")[:10]),
                    "homeId": home["team"]["id"],
                    "awayId": away["team"]["id"],
                    "home": home["team"]["name"],
                    "away": away["team"]["name"],
                    "homeRuns": hr,
                    "awayRuns": ar,
                    "venueId": g.get("venue", {}).get("id", ""),
                    "winnerId": home["team"]["id"] if hr > ar else away["team"]["id"],
                })
        rows.sort(key=lambda r: (r["date"], r["gamePk"]))
        path = _processed(cfg, f"results-{yr}.csv")
        util.ensure_dir(os.path.dirname(path))
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=RESULT_COLS)
            w.writeheader()
            w.writerows(rows)
        util.log(f"ingest: results {yr} -> {len(rows)} games")


def load_results(cfg: dict, season: int) -> list[dict]:
    path = _processed(cfg, f"results-{season}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        out = []
        for r in csv.DictReader(fh):
            r["homeRuns"], r["awayRuns"] = int(r["homeRuns"]), int(r["awayRuns"])
            r["homeId"], r["awayId"], r["winnerId"] = int(r["homeId"]), int(r["awayId"]), int(r["winnerId"])
            out.append(r)
        return out


# --------------------------------------------------------------------------- #
def main(argv) -> int:
    cfg = util.load_config()
    download_core(cfg)
    if "--no-results" not in argv:
        derive_results(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
