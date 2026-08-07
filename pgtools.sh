#!/usr/bin/env bash
# =============================================================================
# pgtools.sh — resolve PostgreSQL CLIENT tools that are >= the store's major
# version, so dump/restore never fails on a version mismatch. Sourced by
# make_store_dump.sh, deploy.sh and backup_store.sh. Not run directly.
#
# Selection order (first that works wins):
#   1) HOST tools  — if `pg_dump` on PATH is >= PG_REQUIRED_MAJOR (default 17).
#   2) CONTAINER   — a running postgres:17 container used as the toolbox
#      (`docker exec`), so a host with an older/absent psql still works. This is
#      why a dev PC (e.g. PG15) can still build/restore against a v17 store: the
#      `db` container already ships psql/pg_dump/pg_restore/pg_basebackup/pg_waldump.
#   3) NEITHER     — advise ./install_pg_client.sh (OS-detected install).
#
# After pg_resolve_tools succeeds, call the wrappers pgdump / pgrestore / psqlc /
# pgdumpall exactly like the real binaries; they route to host or container.
# Override the container with PG_TOOLS_CONTAINER=<name>. Force host-only with
# PG_FORCE_HOST=1 (skips the container fallback).
# =============================================================================
PG_REQUIRED_MAJOR="${PG_REQUIRED_MAJOR:-17}"
PG_EXEC=()            # empty = host tools;  (docker exec -i <container>) = container
PG_TOOLS_SRC=""       # human description of what was chosen

_pg_host_major(){ command -v pg_dump >/dev/null 2>&1 || { echo 0; return; }
  pg_dump --version 2>/dev/null | grep -oE '[0-9]+' | head -1; }

_pg_container_major(){ # $1 = container name -> major version of its pg_dump (0 if none)
  docker exec "$1" sh -lc 'command -v pg_dump >/dev/null 2>&1 && pg_dump --version' 2>/dev/null \
    | grep -oE '[0-9]+' | head -1; }

_pg_find_container(){
  command -v docker >/dev/null 2>&1 || return 1
  local c m
  # explicit override, then the known store containers, then any postgres:17 container
  for c in "${PG_TOOLS_CONTAINER:-}" behaviour-profile-db adhere-behaviour-db; do
    [ -n "$c" ] || continue
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c" || continue
    m="$(_pg_container_major "$c")"; [ "${m:-0}" -ge "$PG_REQUIRED_MAJOR" ] && { echo "$c"; return 0; }
  done
  for c in $(docker ps --filter ancestor=postgres:17 --format '{{.Names}}' 2>/dev/null); do
    m="$(_pg_container_major "$c")"; [ "${m:-0}" -ge "$PG_REQUIRED_MAJOR" ] && { echo "$c"; return 0; }
  done
  return 1
}

pg_resolve_tools(){
  local hm; hm="$(_pg_host_major)"
  if [ "${hm:-0}" -ge "$PG_REQUIRED_MAJOR" ]; then
    PG_EXEC=(); PG_TOOLS_SRC="host PostgreSQL client v${hm}"; return 0
  fi
  if [ "${PG_FORCE_HOST:-0}" != "1" ]; then
    local c; c="$(_pg_find_container || true)"
    if [ -n "$c" ]; then
      PG_EXEC=(docker exec -i "$c"); PG_TOOLS_SRC="container '${c}' (PostgreSQL ${PG_REQUIRED_MAJOR} toolbox)"; return 0
    fi
  fi
  return 1                                   # caller prints the install hint
}

pg_tools_hint(){
  echo "No PostgreSQL ${PG_REQUIRED_MAJOR} client tools found (host pg_dump is $( _pg_host_major )) and no"
  echo "running postgres:${PG_REQUIRED_MAJOR} container to borrow them from. Fix either way:"
  echo "  • install the client tools:   ./install_pg_client.sh        (detects your OS)"
  echo "  • or start the store container: docker compose up -d db      (then re-run)"
}

# wrappers — use exactly like the real binaries (route to host or container)
pgdump(){     "${PG_EXEC[@]}" pg_dump "$@"; }
pgdumpall(){  "${PG_EXEC[@]}" pg_dumpall "$@"; }
pgrestore(){  "${PG_EXEC[@]}" pg_restore "$@"; }
psqlc(){      "${PG_EXEC[@]}" psql "$@"; }
