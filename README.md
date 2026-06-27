# MLB 162-0

Statistical modelling of Major League Baseball, published as a static
[GitHub Pages site](https://danieltomaro13.github.io/MLB-Modelling/) and rebuilt
automatically every few hours.

Part of the **0 Series** — the same modelling dashboard as the AFL, NRL and Tennis
sites. Every game is priced across a full market book: moneyline and fair odds, run
line ±1.5, total runs over/under, first-5-innings, team totals, correct score, plus
player props (batter hits / total bases / home runs / steals and pitcher strikeouts).

## How it works

A reproducible Python pipeline built entirely from public data:

| Stage | Module | Output |
|-------|--------|--------|
| Ingest | `src/ingest.py` | Cache teams, standings, season hitting/pitching lines; derive every final score |
| Profiles | `src/features.py` | Park- & sample-adjusted team offense/defense, per-pitcher run prevention, per-batter rates |
| Ratings | `src/ratings.py` | Home-field-adjusted, margin-weighted team Elo |
| Engine | `src/sim.py` | Negative-binomial run distributions → joint score grid → every market |
| Fixtures | `src/scrape_schedule.py`, `src/fixtures.py` | Upcoming games + probable starters |
| Predict | `src/predict.py` | Full market book + props per game |
| Backtest | `src/evaluate.py` | Walk-forward log-loss / Brier / accuracy vs baseline |
| Odds | `src/odds.py` | Bookmaker board → best price / fair price / EV |
| Site | `src/build_site.py` | Render `docs/` (HTML + JSON) |

`src/run_daily.py` chains these; `.github/workflows/daily.yml` runs it on a cron.

### Model summary

1. **Team & player profiles.** From season hitting/pitching lines we build each team's
   run-scoring and run-prevention rates (park- and sample-adjusted, shrunk toward the
   league mean), each starter's run-prevention multiplier, and each batter's per-PA rate
   profile for props.
2. **Team Elo.** A chronological, home-field-adjusted, margin-weighted Elo gives a
   calibrated baseline win probability.
3. **Run-scoring engine.** Each team's expected runs (offense vs the opposing starter,
   bullpen and ballpark) drive a negative-binomial run distribution; the two marginals
   form a joint score grid that every market is read off — anchored to the Elo blend so
   the headline number stays calibrated.

## Data sources

- [MLB Stats API](https://statsapi.mlb.com/api) — teams, standings, player & team stats,
  schedules, probable pitchers, final scores (public, no auth).

## Local run

```bash
pip install -r requirements.txt
python -m src.run_daily            # full pipeline -> docs/
python -m src.run_daily --quick    # skip results derivation + backtest
python -m http.server -d docs      # preview at http://localhost:8000
```

The odds stage targets Australian books and is best-effort — run it from a local
AU cron (`scripts/odds-cron.sh`); the rest of the pipeline runs anywhere.

## Disclaimer

For research and entertainment only. Model projections are not betting advice.
