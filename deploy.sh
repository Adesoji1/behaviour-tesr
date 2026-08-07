#!/usr/bin/env bash
# =============================================================================
# Behavioural Anti-Fraud — PRODUCTION deployment (operator-run).
#
# Carries the ALREADY-LEARNED state forward so production does not start from
# zero: the learnt customer behaviour profiles are promoted into the production
# behaviour store, and the validated MODEL artifacts + registry are promoted to
# the serving host. Everything is logged to ./logs/deploy_*.log (plain text).
#
# Interactive when a TTY is present (prompts for the IP confirmation); unattended
# otherwise (reads PROD_IP_ALLOWLISTED=yes). Either way it VERIFIES the production
# PostgreSQL connection directly — the real check, not just the confirmation.
#
# Required env (usually from .env):
#   PROD_PG_HOST/PORT/USER/PROD_PG_PASSWORD/PROD_PG_DB [/PROD_PG_SSLMODE]
#   SRC_STORE_DSN     libpq DSN of the store holding the learnt profiles (staging/local)
#   PROD_STORE_DSN    libpq DSN of the PRODUCTION behaviour store (target)
# Optional:
#   PROD_ARTIFACTS_DEST  rsync target for artifacts/ (skip if it's a shared volume)
#   SERVICE_URL          default http://localhost:8080
#   BP_SLACK_WEBHOOK_URL  Slack alerts (set later; no-op until then)
#   PROD_IP_ALLOWLISTED   yes/no (unattended path)
#   BP_API_KEY           /score API key. If set, it is used as-is. If UNSET and no active key
#                        exists in the store, deploy generates one and prints it ONCE to the
#                        console (never to the log). BP_API_KEY_DISABLED=1 runs /score open (dev).
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"

LOGDIR="${DEPLOY_LOG_DIR:-./logs}"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/deploy_$(date +%Y%m%d_%H%M%S).log"
exec 3>&1                              # fd3 = the real console, BYPASSING the log file (for secrets)
exec > >(tee -a "$LOG") 2>&1            # everything to the .log AND the console
log(){ echo "[$(date '+%F %T')] $*"; }
# Print a SECRET (e.g. a freshly generated API key) to the operator ONLY — fd3 bypasses the tee'd
# log, so it is never written to ./logs. (In unattended runs fd3 is the deploy's own stdout; capture
# that securely in CI. The secret still never touches the log file.)
say_secret(){ printf '%s\n' "$*" >&3; }
slack(){ [ -n "${BP_SLACK_WEBHOOK_URL:-}" ] && curl -s -X POST -H 'Content-type: application/json' \
           --data "{\"text\":\"$(printf '%s' "$1" | sed 's/"/\\"/g')\"}" "$BP_SLACK_WEBHOOK_URL" >/dev/null 2>&1 || true; }
fail(){ log "ERROR: $*"; slack ":x: Behavioural deploy FAILED — $*"; exit 1; }

# load .env if present (so PROD_* / *_DSN / BP_API_KEY come from there)
[ -f .env ] && { set -a; . ./.env; set +a; }

# Guarantee /score has a key to check, so the API's REQUIRED-key startup preflight passes and the
# container boots (instead of exiting). Precedence:
#   1) BP_API_KEY in the environment/.env -> used as-is (never printed; you already hold it).
#   2) an active row in bp_api_key        -> reused (only its hash is stored; nothing to print).
#   3) neither                            -> generate ONE now, show it ONCE on the console (fd3),
#                                            never in the log. This bootstraps auth on first deploy.
# BP_API_KEY_DISABLED=1 turns auth off (dev/internal) and skips all of this.
ensure_api_key(){
  case "$(printf '%s' "${BP_API_KEY_DISABLED:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) log "API key: auth DISABLED via BP_API_KEY_DISABLED — /score will be UNAUTHENTICATED."; return 0 ;;
  esac
  if [ -n "${BP_API_KEY:-}" ]; then
    log "API key: using BP_API_KEY from the environment (.env); the container receives it via env_file. Not logged."
    return 0
  fi
  log "API key: BP_API_KEY not set — checking the store for an active key ..."
  if docker compose run --rm -T behaviour-profile python manage_api_key.py show 2>/dev/null | grep -q 'active=True'; then
    log "API key: an active key already exists in the store — reusing it (hash only; nothing to print)."
    return 0
  fi
  log "API key: none configured — generating one now (shown ONCE on the console, NOT in this log) ..."
  local out key
  out="$(docker compose run --rm -T behaviour-profile python manage_api_key.py rotate --label "deploy $(date +%F)" 2>/dev/null)" \
    || fail "could not create the initial API key (is the store up?)"
  key="$(printf '%s\n' "$out" | grep -oE '[0-9a-f]{64}' | head -1)"
  [ -n "$key" ] || fail "API key generation produced no key"
  say_secret ""
  say_secret "  ============================================================"
  say_secret "  NEW X-Adhere-Key (store it NOW — shown once, never in the log):"
  say_secret "      $key"
  say_secret "  Send it on every /score call:  -H 'X-Adhere-Key: <key>'"
  say_secret "  ============================================================"
  say_secret ""
  log "API key: generated and activated (hash stored in bp_api_key; key shown on console only)."
}

log "================= Behavioural Anti-Fraud deployment ================="

# 1) Server-IP confirmation — prompt on a TTY, else read the env flag.
if [ -t 0 ]; then
  read -r -p "Has THIS server's IP been allowlisted on the PRODUCTION Postgres? [y/N] " ans
  case "$ans" in y|Y) : ;; *) log "IP not confirmed — stopping (nothing changed)."; exit 0 ;; esac
else
  [ "${PROD_IP_ALLOWLISTED:-no}" = "yes" ] || { log "PROD_IP_ALLOWLISTED != yes — stopping."; exit 0; }
fi

# 2) Verify the PRODUCTION Postgres connection DIRECTLY (the real gate).
: "${PROD_PG_HOST:?}"; : "${PROD_PG_USER:?}"; : "${PROD_PG_DB:?}"
log "Verifying production Postgres ${PROD_PG_HOST}:${PROD_PG_PORT:-5432}/${PROD_PG_DB} ..."
PGPASSWORD="${PROD_PG_PASSWORD:-}" PGSSLMODE="${PROD_PG_SSLMODE:-require}" \
  psql -h "$PROD_PG_HOST" -p "${PROD_PG_PORT:-5432}" -U "$PROD_PG_USER" -d "$PROD_PG_DB" \
       -tAc "select 1" >/dev/null || fail "cannot reach production Postgres (IP allowlist / credentials)"
log "Production Postgres reachable."

# 3) Promote the LEARNT PROFILES into the production behaviour store (data-only; the
#    target schema is created by the app's ensure_schema()/schema_pg.sql).
: "${SRC_STORE_DSN:?set SRC_STORE_DSN (store holding the learnt profiles)}"
: "${PROD_STORE_DSN:?set PROD_STORE_DSN (production behaviour store)}"
log "Promoting learnt behaviour profiles -> production behaviour store ..."
pg_dump "$SRC_STORE_DSN" --data-only --no-owner --on-conflict-do-nothing 2>/dev/null \
  -t bp_user_behaviour_profile -t bp_peer_baseline -t bp_sync_state \
  | psql "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 >/dev/null \
  || fail "profile promotion (pg_dump | psql) failed"
log "Profiles promoted (production starts from the learnt state, not from zero)."

# 3b) Unpack the MODEL BUNDLE if present. The trained model + registry are shipped OUT-OF-BAND
#     (never committed to git — the model files contain customer-derived identifiers, beneficiary
#     account numbers and IP subnets, so they must not enter the repo). Transfer the bundle to this
#     host next to deploy.sh (or point MODEL_BUNDLE at it) and it is extracted into ./artifacts so
#     the models land in artifacts/models/ EXACTLY as artifacts/registry/index.json expects — both
#     the ACTIVE and the PREVIOUS model, so registry.rollback() works out of the box. Idempotent;
#     skipped when no bundle is present (e.g. artifacts/ is already populated on this host).
MODEL_BUNDLE="${MODEL_BUNDLE:-$HERE/model-bundle.tar.gz}"
if [ -f "$MODEL_BUNDLE" ]; then
  log "Unpacking model bundle ${MODEL_BUNDLE} -> ./artifacts ..."
  tar xzf "$MODEL_BUNDLE" -C "$HERE" || fail "could not extract model bundle ${MODEL_BUNDLE}"
  log "Model bundle unpacked (active: $(grep -o '"active"[^,]*' artifacts/registry/index.json 2>/dev/null | head -1))."
elif [ -d artifacts/models ] && [ -n "$(ls -A artifacts/models 2>/dev/null)" ]; then
  log "No model bundle at ${MODEL_BUNDLE} — using the models already present in ./artifacts/models."
else
  log "WARNING: no model bundle at ${MODEL_BUNDLE} and ./artifacts/models is empty — /score would have"
  log "         no model to load. Transfer the out-of-band model bundle here (or set MODEL_BUNDLE), or"
  log "         run a training job:  docker compose --profile train run --rm trainer --promote"
fi

# 4) Promote the VALIDATED MODEL artifacts + registry to the serving host.
if [ -n "${PROD_ARTIFACTS_DEST:-}" ]; then
  log "Promoting model artifacts + registry -> ${PROD_ARTIFACTS_DEST} ..."
  rsync -a --delete artifacts/registry artifacts/models artifacts/metrics "$PROD_ARTIFACTS_DEST/" \
    || fail "artifact promotion (rsync) failed"
  log "Model artifacts + registry promoted (active version: $(cat artifacts/registry/index.json 2>/dev/null | grep -o '"active"[^,]*' | head -1))."
else
  log "PROD_ARTIFACTS_DEST unset — assuming artifacts/ is a shared volume; skipping copy."
fi

# 5) Bring up (or refresh) the running stack: the store, the API, and the scheduled ingestion.
#    `sync` runs `sync_manager.py --loop` — the ONLY production reader — pulling the daily delta
#    (BP_SYNC_AT_HOUR). All three use restart:unless-stopped so they self-heal after a crash.
#    Order matters: the API REQUIRES an API key at startup, and that key may live in the store, so
#    we build images, bring up the store, ensure a key exists, THEN start the API + ingestion.
if [ "${DEPLOY_COMPOSE_UP:-1}" = "1" ] && command -v docker >/dev/null 2>&1; then
  log "Building images (db + behaviour-profile) ..."
  docker compose build db behaviour-profile || fail "docker compose build failed"

  log "Starting the store (db) and waiting until it is healthy ..."
  docker compose up -d db || fail "docker compose up db failed"
  for i in $(seq 1 60); do
    [ "$(docker compose ps -q db | xargs -r docker inspect -f '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && break
    sleep 2
  done
  [ "$(docker compose ps -q db | xargs -r docker inspect -f '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] \
    || fail "store (db) did not become healthy — cannot continue"
  log "Store healthy."

  # The API's startup preflight refuses to boot without a key — make sure one is configured.
  ensure_api_key

  log "Starting/refreshing the API + ingestion (behaviour-profile + sync) ..."
  docker compose up -d behaviour-profile sync || fail "docker compose up (behaviour-profile + sync) failed"
fi

# 6) Verify the service is up and answering.
SVC="${SERVICE_URL:-http://localhost:8080}"
log "Verifying service health at ${SVC}/health ..."
for i in $(seq 1 30); do curl -sf "${SVC}/health" >/dev/null 2>&1 && break; sleep 2; done
curl -sf "${SVC}/health" >/dev/null || fail "service not healthy after deploy"
log "Service healthy — ready to receive /score requests."

# 7) Show the CURRENT dynamic risk zones the fraud team should use (from the promoted model).
curl -sf "${SVC}/thresholds" 2>/dev/null | sed 's/^/[thresholds] /' || true

# 8) ADVISORY (console only): tell the operator whether a MODEL retrain is due now (§4). This does
#    NOT retrain (retraining is MANUAL — see README "Manual model retraining"), and it does NOT Slack:
#    the always-on `sync` service (started above) owns the SINGLE, de-duplicated Slack alert, so deploy
#    and sync never double-post. Here we only print it to the deploy console/log for the operator.
if [ "${DEPLOY_COMPOSE_UP:-1}" = "1" ] && command -v docker >/dev/null 2>&1; then
  log "Checking whether a model retrain is due (advisory; does not retrain, does not Slack) ..."
  RT_JSON="$(docker compose run --rm -T behaviour-profile python -m ml.retrain_trigger 2>/dev/null || true)"
  DUE="$(printf '%s' "$RT_JSON" | python3 -c 'import sys,json
try: d=json.load(sys.stdin)
except Exception: d={}
print("; ".join(d.get("reasons",[])) if d.get("should_retrain") else "")' 2>/dev/null || true)"
  RETRAIN_CMD="docker compose --profile train run --rm --entrypoint python trainer -m ml.retrain_trigger --run"
  if [ -n "$DUE" ]; then
    log "MODEL RETRAIN DUE — ${DUE}. Retrain MANUALLY when ready:  ${RETRAIN_CMD}  then  curl -X POST ${SVC}/reload"
    log "(A single Slack alert for this is sent by the sync service — not duplicated here.)"
  else
    log "Model retrain not due (no §4 trigger fired) — nothing to do."
  fi
fi

log "================= deployment complete ================="
slack ":white_check_mark: Behavioural Anti-Fraud deployed — profiles + model promoted, stack up (db + API + sync --loop), service healthy."

# 9) REMINDER: the crash/health watcher is NOT started by this script (it is a long-running tail).
#    Start it once, after a successful deploy, so container crashes / ERROR & retrain-failure log
#    lines raise a Slack alert. It self-heals nothing — it only NOTIFIES.
chmod +x "$HERE/watchdog.sh" 2>/dev/null || true
log "-------------------------------------------------------------------"
log "NEXT STEP (do not forget): start the crash/health watcher —"
log "    nohup ./watchdog.sh >> logs/watchdog.log 2>&1 &"
log "(or install it as a systemd service; see DEPLOYMENT.md → 'After a successful deploy')."
log "-------------------------------------------------------------------"
