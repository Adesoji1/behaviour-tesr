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
#   PROD_STORE_DSN    libpq DSN of the PRODUCTION behaviour store (target)
# Learnt-state transport (step 3) — provide ONE of these (bundle wins if both exist):
#   SRC_STORE_DSN     libpq DSN of the store holding the learnt profiles + cache (DIRECT DB->DB pipe;
#                     needs the source reachable from THIS host), OR
#   STORE_BUNDLE      path to a store-bundle.tar.gz built by ./make_store_dump.sh (default
#                     ./store-bundle.tar.gz) — use this when the two DBs can't talk directly.
#   ALLOW_COLD_START=yes  (unattended only) proceed even if neither is set and the prod store is empty.
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
# shellcheck source=pgtools.sh
. "$HERE/pgtools.sh"          # pg_resolve_tools + pgdump/pgrestore/psqlc (host or postgres:17 container)

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

# Fetch an OUT-OF-BAND bundle from a private object store (NEVER git) when it isn't already local.
# Lets a disconnected deploy host auto-pull the model/store bundles so nothing must be scp'd by hand.
_fetch(){ # _fetch <url> <dest>
  local url="$1" dest="$2"
  case "$url" in
    s3://*)   command -v aws    >/dev/null 2>&1 || { log "aws cli not found for $url"; return 1; }; aws s3 cp "$url" "$dest" ;;
    gs://*)   command -v gsutil >/dev/null 2>&1 || { log "gsutil not found for $url"; return 1; }; gsutil cp "$url" "$dest" ;;
    http://*|https://*) command -v curl >/dev/null 2>&1 || { log "curl not found for $url"; return 1; }; curl -fSL -o "$dest" "$url" ;;
    file://*) cp "${url#file://}" "$dest" ;;
    *) log "unsupported bundle URL scheme: $url"; return 1 ;;
  esac
}

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

# Production pulls production data on a DAILY schedule (BP_SYNC_AT_HOUR, e.g. 4 = 04:00). If it is
# unset the sync falls back to INTERVAL mode (every BP_SYNC_INTERVAL_SECONDS) — the initial-backfill
# mode, NOT what production wants. Verify it so a prod deploy never SILENTLY runs interval pulls.
# (Pulling ALSO requires this host's IP to be allowlisted on the production Postgres — confirmed in
# step 1 and verified for real in step 2 below.)
verify_sync_schedule(){
  if [ -n "${BP_SYNC_AT_HOUR:-}" ]; then
    log "Ingestion schedule: DAILY at $(printf '%02d:%02d' "${BP_SYNC_AT_HOUR}" "${BP_SYNC_AT_MINUTE:-0}") ${BP_SYNC_TZ:-UTC} — the production daily pull (BP_SYNC_AT_HOUR set)."
    return 0
  fi
  log "WARNING: BP_SYNC_AT_HOUR is not set — the sync would run in INTERVAL mode (every ${BP_SYNC_INTERVAL_SECONDS:-300}s)."
  log "         That is the backfill mode, NOT the production daily pull. Production should set"
  log "         BP_SYNC_AT_HOUR=4 (04:00) in .env so it pulls once a day."
  if [ -t 0 ]; then
    read -r -p "Deploy with INTERVAL-mode pulls anyway (not the production schedule)? [y/N] " a
    case "$a" in
      y|Y) log "Operator confirmed interval-mode pulls — continuing." ;;
      *)   fail "stopped — set BP_SYNC_AT_HOUR=4 in .env for the production daily pull, then re-run." ;;
    esac
  else
    [ "${ALLOW_INTERVAL_SYNC:-no}" = "yes" ] \
      || fail "BP_SYNC_AT_HOUR unset (interval mode). Set it to 4 for production, or pass ALLOW_INTERVAL_SYNC=yes to override."
  fi
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

# 2b) Verify the production DAILY pull schedule is configured (else stop / require an override).
verify_sync_schedule

# 3) Promote the LEARNT STATE into the production behaviour store, so production CONTINUES from the
#    current learnt behaviour instead of starting cold — and the daily 04:00 pull then only ADDS the
#    delta on top. Everything is OUT-OF-BAND (NOTHING here is committed to git). The target schema is
#    created by the app's ensure_schema()/schema_pg.sql. In BOTH transports the promotion is two parts:
#      (a) the SMALL learnt tables (profiles + peer baselines) — idempotent (INSERT ... ON CONFLICT DO
#          NOTHING), safe on re-run and it never clobbers profiles production has since learned itself;
#      (b) the LARGE transaction cache + the sync WATERMARK — seeded ONLY when the prod cache is empty
#          (so a re-run never duplicates ~1.3 GB), and TOGETHER so the watermark stays consistent with
#          the history and the next pull continues cleanly (new rows only, not a full re-fetch). The
#          cache is what the model uses for velocity features and retraining, so without it prod is cold.
#
#    TWO TRANSPORTS — pick whichever your network allows (deploy.sh auto-detects):
#      • DIRECT DB->DB pipe — set SRC_STORE_DSN (reachable) + PROD_STORE_DSN. deploy.sh pipes it live.
#      • STORE BUNDLE FILE — when the two DBs CANNOT talk directly (e.g. you build the bundle on your PC
#        and the prod host cannot reach your store). Build it once with `./make_store_dump.sh`, scp the
#        resulting store-bundle.tar.gz next to deploy.sh (or point STORE_BUNDLE at it), and deploy.sh
#        restores from the file. If BOTH are available the bundle file wins (explicit beats live).
: "${PROD_STORE_DSN:?set PROD_STORE_DSN (production behaviour store)}"
STORE_BUNDLE="${STORE_BUNDLE:-$HERE/store-bundle.tar.gz}"

# Resolve pg_dump/pg_restore/psql that are >= the store's major (17): host tools if new enough, else a
# running postgres:17 container as the toolbox (so a PG15 host still works), else advise installing.
pg_resolve_tools || { pg_tools_hint; fail "no PostgreSQL 17 client tools available for the store promotion"; }
log "Store tools: ${PG_TOOLS_SRC}."

# helpers -------------------------------------------------------------------------------------------
# NOTE: file inputs go via STDIN (not -f/file-arg) so the tool works the same whether it runs on the
# host or inside the container (a host file path is not visible inside the container).
_prod_cache_count(){ psqlc "$PROD_STORE_DSN" -tAc 'SELECT count(*) FROM bp_transactions_cache' 2>/dev/null | tr -dc '0-9'; }

promote_from_dsn(){                                    # transport 1: live DB -> DB
  : "${SRC_STORE_DSN:?set SRC_STORE_DSN (the store holding the learnt profiles + cache)}"
  log "Learnt-state transport: DIRECT DB->DB (SRC_STORE_DSN -> PROD_STORE_DSN)."
  log "Promoting learnt profiles + peer baselines (idempotent; won't overwrite prod's own) ..."
  pgdump "$SRC_STORE_DSN" --data-only --no-owner --inserts --on-conflict-do-nothing \
    -t bp_user_behaviour_profile -t bp_peer_baseline 2>/dev/null \
    | psqlc "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 >/dev/null \
    || fail "learnt-profile promotion (pg_dump | psql) failed"
  log "Learnt profiles + peer baselines promoted."
  local n; n="$(_prod_cache_count)"; n="${n:-0}"
  if [ "$n" = "0" ]; then
    log "Seeding transaction cache + sync watermark via COPY (large — the current history) ..."
    psqlc "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 -qc 'TRUNCATE bp_sync_state' >/dev/null 2>&1 || true
    pgdump "$SRC_STORE_DSN" --data-only --no-owner -t bp_transactions_cache -t bp_sync_state 2>/dev/null \
      | psqlc "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 >/dev/null \
      || fail "cache + watermark seed (pg_dump | psql) failed"
    log "Transaction cache seeded ($(_prod_cache_count) rows) — prod continues from the current history."
  else
    log "Production cache already has $n rows — leaving cache + watermark untouched (re-run safe)."
  fi
}

promote_from_bundle(){                                 # transport 2: store-bundle.tar.gz (out-of-band)
  log "Learnt-state transport: STORE BUNDLE ${STORE_BUNDLE} (out-of-band file)."
  local work; work="$(mktemp -d)"
  tar xzf "$STORE_BUNDLE" -C "$work" || { rm -rf "$work"; fail "could not extract ${STORE_BUNDLE}"; }
  if [ -f "$work/store-learnt.sql" ]; then
    log "Restoring learnt profiles + peer baselines from bundle (idempotent) ..."
    psqlc "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 -q < "$work/store-learnt.sql" >/dev/null \
      || { rm -rf "$work"; fail "learnt-profile restore (psql) failed"; }
    log "Learnt profiles + peer baselines restored."
  else
    log "WARNING: store-learnt.sql not in the bundle — skipping profiles."
  fi
  local n; n="$(_prod_cache_count)"; n="${n:-0}"
  if [ "$n" = "0" ]; then
    if [ -f "$work/store-cache.dump" ]; then
      log "Restoring transaction cache + sync watermark from bundle (COPY) ..."
      psqlc "$PROD_STORE_DSN" -v ON_ERROR_STOP=1 -qc 'TRUNCATE bp_sync_state' >/dev/null 2>&1 || true
      pgrestore --no-owner --data-only -d "$PROD_STORE_DSN" < "$work/store-cache.dump" >/dev/null 2>&1 \
        || { rm -rf "$work"; fail "cache restore (pg_restore) failed"; }
      log "Transaction cache restored ($(_prod_cache_count) rows) — prod continues from the current history."
    else
      log "WARNING: store-cache.dump not in the bundle — cache NOT seeded (prod would be cold)."
    fi
  else
    log "Production cache already has $n rows — leaving cache + watermark untouched (re-run safe)."
  fi
  rm -rf "$work"
}

# If the bundle isn't local but a URL is configured (private object store), fetch it — so a
# disconnected host needs no manual scp. Build+upload with ./prepare_release.sh (STORE_BUNDLE_URL).
if [ ! -f "$STORE_BUNDLE" ] && [ -n "${STORE_BUNDLE_URL:-}" ]; then
  log "Store bundle not local — fetching from ${STORE_BUNDLE_URL} ..."
  _fetch "$STORE_BUNDLE_URL" "$STORE_BUNDLE" || fail "could not fetch store bundle from ${STORE_BUNDLE_URL}"
fi

if [ -f "$STORE_BUNDLE" ]; then
  promote_from_bundle
elif [ -n "${SRC_STORE_DSN:-}" ]; then
  promote_from_dsn
else
  # Neither transport available. Don't hard-fail (prod store may already be seeded / a shared DB), but
  # do NOT let production silently start with an EMPTY store — gate it like the other prerequisites.
  n="$(_prod_cache_count)"; n="${n:-0}"
  if [ "$n" != "0" ]; then
    log "No SRC_STORE_DSN and no store bundle, but production already has ${n} cached rows — continuing."
  else
    log "No learnt-state transport: SRC_STORE_DSN is unset AND no store bundle at ${STORE_BUNDLE},"
    log "and production's store is EMPTY. Production would start COLD (no history for velocity/retrain)."
    log "Fix: build a bundle on a host that can reach your store — ./make_store_dump.sh — scp"
    log "store-bundle.tar.gz next to deploy.sh, and re-run; OR set SRC_STORE_DSN for a direct DB->DB pipe."
    if [ -t 0 ]; then
      read -r -p "Continue with a COLD production store anyway? [y/N] " a
      case "$a" in y|Y) log "Operator confirmed a cold start — continuing." ;;
                   *) fail "stopped — provide a store bundle or SRC_STORE_DSN, then re-run." ;; esac
    else
      [ "${ALLOW_COLD_START:-no}" = "yes" ] || fail "no learnt-state transport and empty prod store. Provide a bundle / SRC_STORE_DSN, or pass ALLOW_COLD_START=yes to override."
    fi
  fi
fi
log "Learnt state promotion done (production starts warm, not from zero)."

# 3b) Unpack the MODEL BUNDLE if present. The trained model + registry are shipped OUT-OF-BAND
#     (never committed to git — the model files contain customer-derived identifiers, beneficiary
#     account numbers and IP subnets, so they must not enter the repo). Transfer the bundle to this
#     host next to deploy.sh (or point MODEL_BUNDLE at it) and it is extracted into ./artifacts so
#     the models land in artifacts/models/ EXACTLY as artifacts/registry/index.json expects — both
#     the ACTIVE and the PREVIOUS model, so registry.rollback() works out of the box. Idempotent;
#     skipped when no bundle is present (e.g. artifacts/ is already populated on this host).
MODEL_BUNDLE="${MODEL_BUNDLE:-$HERE/model-bundle.tar.gz}"
if [ ! -f "$MODEL_BUNDLE" ] && [ -n "${MODEL_BUNDLE_URL:-}" ]; then
  log "Model bundle not local — fetching from ${MODEL_BUNDLE_URL} ..."
  _fetch "$MODEL_BUNDLE_URL" "$MODEL_BUNDLE" || fail "could not fetch model bundle from ${MODEL_BUNDLE_URL}"
fi
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
