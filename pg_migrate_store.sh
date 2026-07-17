#!/usr/bin/env bash
# ============================================================================
# Carry the LEARNT behaviour from THIS profile store into a PRODUCTION store,
# so production does not have to re-learn from scratch. The profiles were built
# from real production transaction data, so they are valid in production.
#
# What moves (data-only — the target's schema is created by the app's
# ensure_schema() / schema_pg.sql, so we ship ROWS, not DDL):
#     bp_user_behaviour_profile   the learnt profiles           (THE behaviour)
#     bp_peer_baseline            cold-start peer baselines
#     bp_rule_definition          AML rule catalog + thresholds  (so scoring matches)
#     bp_rule_settings            per-branch/per-client overrides
#     bp_sync_state               ingestion watermark (resume point)
#   + optional:  bp_profile_history (--with-history), bp_transactions_cache (--with-cache)
#
# What is NOT moved: the dev audit/test rows (bp_decision, bp_webhook_delivery,
# bp_event_log, bp_rule_event) — those are this environment's, not production's.
#
# ---------------------------------------------------------------------------
# USAGE
#   ./pg_migrate_store.sh dump [--with-history] [--with-cache]
#       -> writes a timestamped custom-format dump under ./migrate_dumps/
#
#   DEST_PG_HOST=... DEST_PG_PORT=5432 DEST_PG_USER=... DEST_PG_PASSWORD=... \
#   DEST_PG_DB=... DEST_PG_SSLMODE=require \
#   ./pg_migrate_store.sh restore <dumpfile> --yes [--truncate]
#       -> restores the dump into the production store named by DEST_PG_*.
#          Target schema must already exist (start the prod app once, or apply
#          schema_pg.sql). --truncate empties the carried tables first (guarded).
#
#   ./pg_migrate_store.sh verify           # row counts in the LOCAL store
#   DEST_PG_*=... ./pg_migrate_store.sh verify --dest   # counts in the TARGET
# ============================================================================
set -euo pipefail

# ---- SOURCE (this environment's store) — the compose db container -----------
SRC_CONTAINER="${SRC_CONTAINER:-behaviour-profile-db}"
SRC_DB="${SRC_PG_DB:-behaviour}"
SRC_USER="${SRC_PG_USER:-behaviour}"

CORE_TABLES=(bp_user_behaviour_profile bp_peer_baseline bp_rule_definition
             bp_rule_settings bp_sync_state)

HERE="$(cd "$(dirname "$0")" && pwd)"
DUMPDIR="$HERE/migrate_dumps"

die() { echo "ERROR: $*" >&2; exit 1; }

# Build the -t table flags for the requested set.
table_flags() {
  local tabs=("${CORE_TABLES[@]}")
  [[ "${WITH_HISTORY:-0}" == "1" ]] && tabs+=(bp_profile_history)
  [[ "${WITH_CACHE:-0}"   == "1" ]] && tabs+=(bp_transactions_cache)
  for t in "${tabs[@]}"; do printf ' -t %s' "$t"; done
}

# Build a libpq DSN for the DESTINATION from DEST_PG_* env vars.
dest_dsn() {
  : "${DEST_PG_HOST:?set DEST_PG_HOST}" "${DEST_PG_USER:?set DEST_PG_USER}" \
    "${DEST_PG_DB:?set DEST_PG_DB}"
  local port="${DEST_PG_PORT:-5432}" ssl="${DEST_PG_SSLMODE:-require}"
  # password via env (PGPASSWORD) so it never lands in the process list
  printf 'host=%s port=%s user=%s dbname=%s sslmode=%s' \
         "$DEST_PG_HOST" "$port" "$DEST_PG_USER" "$DEST_PG_DB" "$ssl"
}

cmd_dump() {
  mkdir -p "$DUMPDIR"
  local out="$DUMPDIR/store_$(date +%Y%m%d_%H%M%S).dump"
  echo "dumping data-only [$( [[ ${WITH_HISTORY:-0} == 1 ]] && echo +history )$( [[ ${WITH_CACHE:-0} == 1 ]] && echo ' +cache')] from ${SRC_CONTAINER}:${SRC_DB} ..."
  # -Fc custom format (compressed, selective restore); --data-only ships rows only.
  # shellcheck disable=SC2046
  docker exec "$SRC_CONTAINER" pg_dump -U "$SRC_USER" -d "$SRC_DB" \
      --data-only --no-owner --no-privileges -Fc $(table_flags) > "$out"
  echo "wrote $out ($(du -h "$out" | cut -f1))"
  echo "next: DEST_PG_*=... ./pg_migrate_store.sh restore $out --yes"
}

cmd_restore() {
  local dump="${1:?usage: restore <dumpfile> --yes [--truncate]}"
  [[ -f "$dump" ]] || die "no such dump file: $dump"
  [[ "${CONFIRM:-0}" == "1" ]] || die "refusing to write to production without --yes"
  local dsn; dsn="$(dest_dsn)"
  echo "RESTORE -> $DEST_PG_HOST:${DEST_PG_PORT:-5432}/$DEST_PG_DB (data-only)"

  if [[ "${TRUNCATE:-0}" == "1" ]]; then
    echo "  --truncate: emptying carried tables in the TARGET first"
    local tabs; tabs="$(printf '%s,' "${CORE_TABLES[@]}")"; tabs="${tabs%,}"
    PGPASSWORD="${DEST_PG_PASSWORD:-}" docker exec -i -e PGPASSWORD="${DEST_PG_PASSWORD:-}" \
      "$SRC_CONTAINER" psql "$dsn" -v ON_ERROR_STOP=1 \
      -c "TRUNCATE ${tabs} RESTART IDENTITY;"
  fi

  # pg_restore reads the custom-format archive from stdin; --disable-triggers so FK/order
  # never blocks a data-only load into an existing (empty) schema.
  PGPASSWORD="${DEST_PG_PASSWORD:-}" docker exec -i -e PGPASSWORD="${DEST_PG_PASSWORD:-}" \
    "$SRC_CONTAINER" pg_restore --no-owner --no-privileges --data-only \
    --disable-triggers --exit-on-error --dbname="$dsn" < "$dump"
  echo "restore complete. verify with: DEST_PG_*=... ./pg_migrate_store.sh verify --dest"
}

cmd_verify() {
  local q="SELECT 'bp_user_behaviour_profile' t, count(*) n FROM bp_user_behaviour_profile
    UNION ALL SELECT 'bp_peer_baseline', count(*) FROM bp_peer_baseline
    UNION ALL SELECT 'bp_rule_definition', count(*) FROM bp_rule_definition
    UNION ALL SELECT 'bp_rule_settings', count(*) FROM bp_rule_settings
    UNION ALL SELECT 'bp_sync_state', count(*) FROM bp_sync_state ORDER BY 1;"
  if [[ "${1:-}" == "--dest" ]]; then
    PGPASSWORD="${DEST_PG_PASSWORD:-}" docker exec -i -e PGPASSWORD="${DEST_PG_PASSWORD:-}" \
      "$SRC_CONTAINER" psql "$(dest_dsn)" -c "$q"
  else
    docker exec "$SRC_CONTAINER" psql -U "$SRC_USER" -d "$SRC_DB" -c "$q"
  fi
}

# ---- arg parsing ------------------------------------------------------------
SUB="${1:-}"; shift || true
WITH_HISTORY=0; WITH_CACHE=0; CONFIRM=0; TRUNCATE=0; POS=()
for a in "$@"; do
  case "$a" in
    --with-history) WITH_HISTORY=1 ;;
    --with-cache)   WITH_CACHE=1 ;;
    --yes)          CONFIRM=1 ;;
    --truncate)     TRUNCATE=1 ;;
    --dest)         POS+=("--dest") ;;
    *)              POS+=("$a") ;;
  esac
done
export WITH_HISTORY WITH_CACHE CONFIRM TRUNCATE

case "$SUB" in
  dump)    cmd_dump ;;
  restore) cmd_restore "${POS[@]}" ;;
  verify)  cmd_verify "${POS[@]:-}" ;;
  *) echo "usage: $0 {dump|restore <dumpfile> --yes|verify [--dest]} [--with-history] [--with-cache] [--truncate]"; exit 1 ;;
esac
