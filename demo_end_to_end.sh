#!/usr/bin/env bash
# ============================================================================
# END-TO-END DEMO of the customer behaviour-profile system.
# Narrates every stage with timestamps so a CTO / client can follow what happens
# "per time". Everything is echoed to the screen AND saved to logs/demo_<ts>.log.
#
#   ./demo_end_to_end.sh            # fast: uses the profiles already built
#   BUILD_SLICE=1 ./demo_end_to_end.sh   # also learn a fresh small slice live
#
# Safe: production is READ-ONLY; all writes go to the PostgreSQL profile store.
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
source "$HERE/../.venv/bin/activate" 2>/dev/null || true
mkdir -p logs
LOG="logs/demo_$(date +%Y%m%d_%H%M%S).log"

# echo to screen and log, with a timestamp on section headers
exec > >(tee -a "$LOG") 2>&1
ts()   { date '+%F %T'; }
step() { echo; echo "==================================================================";
         echo "[$(ts)]  $1"; echo "=================================================================="; }
run()  { echo "  \$ $*"; "$@"; }

echo "###################################################################"
echo "#   CUSTOMER BEHAVIOUR-PROFILE SYSTEM — END-TO-END DEMO            #"
echo "#   started: $(ts)"
echo "#   log:     $LOG"
echo "###################################################################"

step "STEP 1/9 — Configuration (what the system is set to)"
python demo_helpers.py config

step "STEP 2/9 — Source of truth: production is READ-ONLY"
echo "  A single read-only query to production (we never write there):"
# all connection details come from config/.env — nothing hard-coded here
export PGPASSWORD="$(python -c 'import config;print(config.PROD_PG["password"])')"
PGHOST="$(python -c 'import config;print(config.PROD_PG["host"])')"
PGPORT="$(python -c 'import config;print(config.PROD_PG["port"])')"
PGUSER="$(python -c 'import config;print(config.PROD_PG["user"])')"
PGDB="$(python -c 'import config;print(config.PROD_PG["dbname"])')"
run psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDB" --set=sslmode=require -t -A \
    -c "SELECT 'transactions in last quarter = ' || count(*) FROM monitoring_transactionmonitoring WHERE date_created >= now() - interval '3 months';"

if [ "${BUILD_SLICE:-0}" = "1" ]; then
  step "STEP 3/9 — Learn profiles LIVE from a fresh slice (read-only extract → build)"
  run python extract_transactions.py --branch 231 --sample-limit 20000 --out data/_demo_slice.csv
  run python build_profiles.py --in data/_demo_slice.csv
  rm -f data/_demo_slice.csv
else
  step "STEP 3/9 — Profiles already learned (full quarterly build)"
  echo "  (run with BUILD_SLICE=1 to watch a fresh slice being learned live)"
  python demo_helpers.py counts
fi

step "STEP 3b/9 — GOVERNANCE: Active (trusted) vs Warming-Up + confidence"
python demo_helpers.py governance
echo
echo "  A Warming-Up account (not trusted yet):"
python demo_helpers.py warming

step "STEP 4/9 — What the system LEARNED about a real TRUSTED customer"
python demo_helpers.py show_profile

step "STEP 5/9 — Load the AML rules + blacklist"
run python load_rules.py

step "STEP 6/9 — A CLIENT sets its own thresholds (tier-1/2/3 differ)"
echo "  Client (branch 231) lowers its hard cap to 250,000,000 and turns a rule off:"
run python client_thresholds.py --branch 231 --rule block_above_hard_cap --set hard_cap=250000000
run python client_thresholds.py --branch 231 --rule detect_unusual_country --disable
run python client_thresholds.py --list --branch 231

step "STEP 7/9 — Rules firing: NORMAL passes, ABNORMAL is flagged"
python demo_helpers.py rules_demo

step "STEP 8/9 — LIVE velocity: catching a burst the daily profile can't see"
python demo_helpers.py velocity_demo

step "STEP 8b/9 — COLD START: a brand-new account judged against its peers"
python demo_helpers.py coldstart_demo

# tidy the demo threshold overrides so the DB is left clean
python client_thresholds.py --branch 231 --rule block_above_hard_cap --set >/dev/null 2>&1 || true
python - <<'PY' 2>/dev/null || true
import db
c=db.connect();cur=c.cursor();cur.execute("DELETE FROM bp_rule_settings WHERE branch_id=231");c.commit();c.close()
PY

step "STEP 9/9 — Self-updating: event-driven per-customer retraining (NO cron)"
echo "  There is no nightly cron. Each customer is refreshed only when their own"
echo "  activity meets a trigger:  >=100 new txns  OR  30 days  OR  sustained drift."
echo "  It rides on the transaction the app already sends, via the microservice."
echo "  Retrain triggers (tunable):"
echo "    BP_RETRAIN_MIN_NEW_TXNS=${BP_RETRAIN_MIN_NEW_TXNS:-100}  BP_RETRAIN_MAX_AGE_DAYS=${BP_RETRAIN_MAX_AGE_DAYS:-30}  BP_DRIFT_SIGNAL_THRESHOLD=${BP_DRIFT_SIGNAL_THRESHOLD:-5}"
echo "  Microservice:  uvicorn service:app --port 8080   (endpoints: /score /profile /retrain /health /docs)"
echo "  See SERVICE.md for how the adhere app plugs in."

step "DEMO COMPLETE"
python demo_helpers.py counts
echo
echo "[$(ts)]  full transcript saved to: $LOG"
