#!/usr/bin/env bash
# =============================================================================
# Build a STORE BUNDLE for OUT-OF-BAND transfer to production.
#
# Use this when the deploy host CANNOT reach your behaviour store directly (so the
# DB->DB pipe in deploy.sh step 3 is not possible) — e.g. you build the bundle on
# your own PC and later scp it to the production host.
#
#   ./make_store_dump.sh                 # dumps SRC_STORE_DSN (or .env) -> ./store-bundle.tar.gz
#   ./make_store_dump.sh "postgresql://user:pw@host:5432/db"   # explicit source DSN
#   STORE_BUNDLE_OUT=/path/out.tar.gz ./make_store_dump.sh     # custom output path
#
# The bundle carries the LEARNT STATE (no schema — the target creates it):
#   store-learnt.sql   bp_user_behaviour_profile + bp_peer_baseline  (idempotent INSERTs)
#   store-cache.dump   bp_transactions_cache + bp_sync_state         (compressed custom format)
# deploy.sh (step 3) auto-detects ./store-bundle.tar.gz and restores it — profiles idempotently,
# the cache + watermark only when prod's cache is empty (re-run safe).
#
# TOOLS: pg_dump/pg_restore must be >= the store's major (17). This script resolves them
# automatically (pgtools.sh): host client if new enough, else a running postgres:17 container as
# the toolbox, else it points you at ./install_pg_client.sh. So a PG15 host still works if the
# `db` container is up.
#
# NOTE: this bundle contains CUSTOMER DATA — transfer it securely (scp / secure bucket) and NEVER
# commit it to git (it is git-ignored). It is the same class of data as the DB itself.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
[ -f .env ] && { set -a; . ./.env; set +a; }
# shellcheck source=pgtools.sh
. "$HERE/pgtools.sh"

SRC="${1:-${SRC_STORE_DSN:-}}"
: "${SRC:?set SRC_STORE_DSN in .env (or pass a libpq DSN as arg 1) — the store to dump}"
OUT="${STORE_BUNDLE_OUT:-$HERE/store-bundle.tar.gz}"

pg_resolve_tools || { pg_tools_hint; exit 1; }
echo "Using ${PG_TOOLS_SRC} for pg_dump."

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "[1/3] Dumping learnt profiles + peer baselines (idempotent SQL) ..."
pgdump "$SRC" --data-only --no-owner --inserts --on-conflict-do-nothing \
  -t bp_user_behaviour_profile -t bp_peer_baseline > "$WORK/store-learnt.sql" \
  || { echo "ERROR: profile dump failed (check SRC_STORE_DSN / connectivity)"; exit 1; }

echo "[2/3] Dumping transaction cache + sync watermark (compressed custom format; can be large) ..."
# -Fc writes to stdout (no -f) so it works whether the tool runs on the host or inside the container.
pgdump "$SRC" -Fc --no-owner -t bp_transactions_cache -t bp_sync_state > "$WORK/store-cache.dump" \
  || { echo "ERROR: cache dump failed"; exit 1; }

echo "[3/3] Packing -> $OUT ..."
tar czf "$OUT" -C "$WORK" store-learnt.sql store-cache.dump

echo
echo "Wrote $OUT  ($(du -h "$OUT" | cut -f1))"
( sha256sum "$OUT" 2>/dev/null || shasum -a 256 "$OUT" ) | awk '{print "sha256: "$1}'
echo
echo "Next:"
echo "  1) scp \"$OUT\" to the DEPLOY host, next to deploy.sh   (or set STORE_BUNDLE=/path/to/it)"
echo "  2) run ./deploy.sh on that host — step 3 auto-detects the bundle and restores it."
echo "  Keep this file OUT of git (it holds customer data); it is git-ignored by default."
