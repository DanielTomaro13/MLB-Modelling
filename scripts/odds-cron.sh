#!/bin/bash
# odds-cron.sh — refresh bookmaker odds from a local AU machine and push.
#
# The Australian books in src/odds.py geo-restrict to AU IPs, so the odds stage
# is best run from a local AU cron (the GitHub Actions runner is offshore). This
# script regenerates predictions + odds and commits the refreshed docs/data.
#
# Schedule it via scripts/com.danieltomaro.mlb-odds.plist (launchd).
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"

LOG="scripts/odds-cron.log"
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) odds refresh ===" >>"$LOG"

# Pull the CI's model refresh first so the odds push rebases cleanly on top.
git pull --rebase --autostash >>"$LOG" 2>&1 || true

# Predictions must exist/are current before odds prices against them.
python3 -m src.predict   >>"$LOG" 2>&1 || true
python3 -m src.odds      >>"$LOG" 2>&1 || true

if [[ -n "$(git status --porcelain docs/data/odds.json docs/data/predictions.json)" ]]; then
  git add docs/data/odds.json docs/data/predictions.json
  git commit -m "chore: odds refresh ($(date -u +%Y-%m-%dT%H:%MZ))" >>"$LOG" 2>&1
  git push >>"$LOG" 2>&1 || echo "push failed (will retry next run)" >>"$LOG"
fi
