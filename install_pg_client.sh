#!/usr/bin/env bash
# =============================================================================
# install_pg_client.sh — install the PostgreSQL 17 CLIENT tools for THIS OS.
#
# The behaviour store is PostgreSQL 17, and pg_dump/pg_restore must be >= the
# server's major version. If your host has an older psql (or none), run this.
# It installs the CLIENT package only (psql, pg_dump, pg_restore, pg_basebackup,
# pg_waldump) — NOT a server — using the official PGDG repositories.
#
#   ./install_pg_client.sh              # detect OS + install (prompts before changes)
#   ./install_pg_client.sh --dry-run    # print exactly what it WOULD run, change nothing
#   ./install_pg_client.sh --yes        # non-interactive (assume yes)
#
# Alternative that needs NO install: if you run Docker, `docker compose up -d db`
# gives you a postgres:17 container, and make_store_dump.sh / deploy.sh will borrow
# its tools automatically (see pgtools.sh). This script is for hosts without Docker.
# =============================================================================
set -euo pipefail
PG_MAJOR="${PG_MAJOR:-17}"
DRY=0; ASSUME_YES=0
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;; --yes|-y) ASSUME_YES=1 ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "unknown arg: $a"; exit 2 ;;
esac; done

run(){ echo "  + $*"; [ "$DRY" = "1" ] || eval "$@"; }
have(){ command -v "$1" >/dev/null 2>&1; }
_client_major(){ have pg_dump && pg_dump --version | grep -oE '[0-9]+' | head -1 || echo 0; }

# 0) already good?
cm="$(_client_major)"
if [ "${cm:-0}" -ge "$PG_MAJOR" ]; then
  echo "PostgreSQL client v${cm} already present (>= ${PG_MAJOR}) — nothing to do."
  exit 0
fi
[ "$cm" != "0" ] && echo "Found pg_dump v${cm} (< ${PG_MAJOR}) — will install the v${PG_MAJOR} client alongside it."

# sudo helper (root -> none; else sudo if available)
SUDO=""; if [ "$(id -u)" != "0" ]; then have sudo && SUDO="sudo" || { echo "ERROR: need root or sudo to install packages"; exit 1; }; fi

# 1) detect OS
OS=""; LIKE=""; CODENAME=""; UNAME="$(uname -s)"
if [ -f /etc/os-release ]; then . /etc/os-release; OS="${ID:-}"; LIKE="${ID_LIKE:-}"; CODENAME="${VERSION_CODENAME:-}"; fi
echo "Detected: uname=${UNAME} os=${OS:-?} like=${LIKE:-?} codename=${CODENAME:-?}  (target: PostgreSQL ${PG_MAJOR} client)"

confirm(){ [ "$ASSUME_YES" = "1" ] || [ "$DRY" = "1" ] && return 0
  read -r -p "Proceed with the install above? [y/N] " a; case "$a" in y|Y) return 0;; *) echo "aborted."; exit 1;; esac; }

case "${UNAME}:${OS}:${LIKE}" in
  Darwin:*:*)
    if have brew; then
      confirm
      run "brew install postgresql@${PG_MAJOR}"
      echo "Add to PATH:  export PATH=\"\$(brew --prefix)/opt/postgresql@${PG_MAJOR}/bin:\$PATH\""
    else
      echo "Homebrew not found. Install it (https://brew.sh) then re-run, or use the EDB installer:"
      echo "  https://www.postgresql.org/download/macosx/"
      exit 1
    fi
    ;;
  Linux:ubuntu:*|Linux:debian:*|Linux:*:*debian*|Linux:*:*ubuntu*)
    : "${CODENAME:?could not detect the Debian/Ubuntu codename (VERSION_CODENAME) — set CODENAME=... and re-run}"
    echo "Plan (Debian/Ubuntu, PGDG apt repo):"
    confirm
    run "$SUDO apt-get update -y"
    run "$SUDO apt-get install -y curl ca-certificates gnupg"
    run "$SUDO install -d /usr/share/postgresql-common/pgdg"
    run "$SUDO curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc"
    run "echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${CODENAME}-pgdg main' | $SUDO tee /etc/apt/sources.list.d/pgdg.list >/dev/null"
    run "$SUDO apt-get update -y"
    run "$SUDO apt-get install -y postgresql-client-${PG_MAJOR}"
    ;;
  Linux:rhel:*|Linux:centos:*|Linux:almalinux:*|Linux:rocky:*|Linux:fedora:*|Linux:*:*rhel*|Linux:*:*fedora*)
    echo "Plan (RHEL/Fedora family, PGDG dnf repo):"
    confirm
    if have rpm && rpm -E %rhel >/dev/null 2>&1 && [ -n "$(rpm -E %rhel 2>/dev/null)" ]; then
      EL="$(rpm -E %rhel)"
      run "$SUDO dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-${EL}-x86_64/pgdg-redhat-repo-latest.noarch.rpm"
      run "$SUDO dnf -qy module disable postgresql || true"
    fi
    run "$SUDO dnf install -y postgresql${PG_MAJOR}"
    ;;
  *)
    echo "Unrecognised OS. Install the PostgreSQL ${PG_MAJOR} client manually:"
    echo "  https://www.postgresql.org/download/   (client tools: psql, pg_dump, pg_restore)"
    exit 1
    ;;
esac

if [ "$DRY" = "1" ]; then echo "(dry-run — nothing was changed)"; exit 0; fi
nm="$(_client_major)"
if [ "${nm:-0}" -ge "$PG_MAJOR" ]; then echo "Done — pg_dump is now v${nm}."; else
  echo "WARNING: pg_dump still reports v${nm:-none}. You may need to open a new shell or fix your PATH."; fi
