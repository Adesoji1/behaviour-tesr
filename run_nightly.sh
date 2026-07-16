#!/usr/bin/env bash
# ============================================================================
# Nightly behaviour-profile refresh (the "self-updating" batch job).
# Cron calls this once a night (02:00). It re-extracts the latest quarterly
# window read-only from production and rebuilds every profile, so the baseline
# naturally adapts to changing customer habits without touching live traffic.
# Idempotent: profiles are UPSERTed, so a re-run just re-derives the baseline.
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
VENV="$HERE/../.venv/bin/activate"
LOGDIR="$HERE/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/nightly_$(date +%Y%m%d_%H%M%S).log"

# shellcheck disable=SC1090
source "$VENV" 2>/dev/null || true

{
  echo "=================================================================="
  echo "[$(date '+%F %T')] NIGHTLY BEHAVIOUR-PROFILE REFRESH — start"
  echo "=================================================================="

  echo "[$(date '+%F %T')] step 1/3  refreshing lifetime tenure (READ ONLY from prod)"
  python extract_tenure.py

  echo "[$(date '+%F %T')] step 2/3  extracting latest quarterly window (READ ONLY from prod)"
  python extract_transactions.py --out data/transactions.csv

  echo "[$(date '+%F %T')] step 3/3  rebuilding all profiles + peer baselines (with eligibility gate)"
  python build_profiles.py --in data/transactions.csv

  echo "[$(date '+%F %T')] NIGHTLY REFRESH — done OK"
} >>"$LOG" 2>&1

# keep only the last 30 nightly logs
ls -1t "$LOGDIR"/nightly_*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "nightly refresh complete — log: $LOG"
