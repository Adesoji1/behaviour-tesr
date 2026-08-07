#!/usr/bin/env bash
# =============================================================================
# backup_store.sh — full LOGICAL backup of the behaviour store (customer data)
# to ./backups/store_<timestamp>.dump  (compressed pg_dump custom format).
#
#   ./backup_store.sh                       # dumps SRC_STORE_DSN / STORE_PG_DSN from .env
#   ./backup_store.sh "postgresql://user:pw@host:5432/db"
#   BACKUP_DIR=/mnt/backups ./backup_store.sh
#
# Uses PostgreSQL 17 tools automatically — host client if new enough, else the running
# postgres:17 container as the toolbox (pgtools.sh). So the backup can be "written from the
# container to the host" even if the host has no/old psql.
#
# Restore later (whole store):
#   pg_restore --no-owner --clean --if-exists -d "$TARGET_DSN"  backups/store_XXXX.dump
# or data only, non-destructive:
#   pg_restore --no-owner --data-only        -d "$TARGET_DSN"  backups/store_XXXX.dump
#
# The backup holds CUSTOMER DATA — keep ./backups OUT of git (it is git-ignored) and ship securely.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
[ -f .env ] && { set -a; . ./.env; set +a; }
# shellcheck source=pgtools.sh
. "$HERE/pgtools.sh"

SRC="${1:-${SRC_STORE_DSN:-${STORE_PG_DSN:-}}}"
: "${SRC:?set SRC_STORE_DSN or STORE_PG_DSN in .env (or pass a libpq DSN as arg 1)}"
DIR="${BACKUP_DIR:-$HERE/backups}"; mkdir -p "$DIR"
OUT="$DIR/store_$(date +%Y%m%d_%H%M%S).dump"

pg_resolve_tools || { pg_tools_hint; exit 1; }
echo "Backing up the behaviour store with ${PG_TOOLS_SRC} ..."
# -Fc to stdout (no -f) so it works host-or-container; redirect to the host file.
pgdump "$SRC" -Fc --no-owner > "$OUT" || { echo "ERROR: backup failed"; rm -f "$OUT"; exit 1; }

echo "Wrote $OUT  ($(du -h "$OUT" | cut -f1))"
( sha256sum "$OUT" 2>/dev/null || shasum -a 256 "$OUT" ) | awk '{print "sha256: "$1}'
echo "Restore:  pg_restore --no-owner --data-only -d \"\$TARGET_DSN\"  $OUT"
