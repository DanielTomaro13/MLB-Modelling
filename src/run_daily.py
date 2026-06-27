"""Orchestrator — run the full pipeline end to end.

    python -m src.run_daily          # full build (ingest + results + Elo + backtest)
    python -m src.run_daily --quick  # skip results derivation + backtest (faster)

Stages: ingest -> features -> ratings -> (evaluate) -> scrape -> predict ->
players -> build_site -> odds (best-effort; AU-geo books).
"""
from __future__ import annotations

import sys
import time

from . import (build_site, evaluate, features, ingest, odds, players,
               predict, ratings, scrape_schedule, util)


def run(quick: bool = False, no_odds: bool = False) -> int:
    cfg = util.load_config()
    t0 = time.time()

    util.log("=== 1/9 ingest ===")
    ingest.download_core(cfg)
    if not quick:
        ingest.derive_results(cfg)

    util.log("=== 2/9 features ===")
    features.main([])

    util.log("=== 3/9 ratings (Elo) ===")
    try:
        ratings.main([])
    except Exception as exc:  # noqa: BLE001
        util.log(f"run_daily: ratings skipped ({exc})")

    if not quick:
        util.log("=== 4/9 evaluate (backtest) ===")
        try:
            evaluate.main([])
        except Exception as exc:  # noqa: BLE001
            util.log(f"run_daily: backtest skipped ({exc})")

    util.log("=== 5/9 scrape schedule ===")
    scrape_schedule.main([])

    util.log("=== 6/9 predict ===")
    predict.main([])

    util.log("=== 7/9 players ===")
    try:
        players.main([])
    except Exception as exc:  # noqa: BLE001
        util.log(f"run_daily: players skipped ({exc})")

    util.log("=== 8/9 build site ===")
    build_site.main([])

    if no_odds:
        util.log("=== 9/9 odds skipped (--no-odds) — left to the local AU cron ===")
    else:
        util.log("=== 9/9 odds (best-effort; AU-geo books) ===")
        try:
            odds.run(cfg)
        except Exception as exc:  # noqa: BLE001
            util.log(f"run_daily: odds skipped ({exc})")

    util.log(f"run_daily: done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(run(quick="--quick" in args, no_odds="--no-odds" in args))
