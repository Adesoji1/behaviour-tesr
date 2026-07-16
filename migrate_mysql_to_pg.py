#!/usr/bin/env python3
"""
ONE-TIME migration: copy the already-learned profiles from the old MySQL store
into the new PostgreSQL store.

Why this exists
---------------
The ~99k profiles were learned over months. Re-seeding them from scratch would
mean a huge read against PRODUCTION — exactly what we are trying to stop. MySQL
is still reachable and already holds the answer, so we copy store->store and
production is never touched.

    MySQL (old store)  ──copy──▶  PostgreSQL (new store)      [prod untouched]

Safe to re-run: every table is UPSERTed on its primary key, and it is chunked so
a big table never lands in memory all at once.

Usage:
    python migrate_mysql_to_pg.py                 # migrate everything
    python migrate_mysql_to_pg.py --tables bp_user_behaviour_profile
    python migrate_mysql_to_pg.py --verify        # just compare row counts
"""
import argparse
import sys

import config
import db

# table -> primary key columns (for the ON CONFLICT upsert)
TABLES = {
    "bp_user_behaviour_profile": ["entity_key"],
    "bp_rule_definition":        ["rule_code"],
    "bp_rule_settings":          ["branch_id", "rule_code"],
    "bp_peer_baseline":          ["branch_id", "account_type"],
    "bp_blacklist":              ["id"],
    "bp_incremental_state":      ["entity_key"],
    "bp_build_run":              ["run_id"],
}

CHUNK = 2000


def _mysql_cols(mcur, table: str) -> list[str]:
    mcur.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d[0] for d in mcur.description]


def _pg_cols(pconn, table: str) -> set[str]:
    cur = pconn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,))
    return {r[0] for r in cur.fetchall()}


def migrate_table(mconn, pconn, table: str, keys: list[str]) -> int:
    """Copy one table in chunks. Returns rows written."""
    mcur = mconn.cursor()
    # only migrate columns that exist on BOTH sides, so a schema drift never aborts
    src = _mysql_cols(mcur, table)
    dst = _pg_cols(pconn, table)
    cols = [c for c in src if c in dst]
    missing = [c for c in src if c not in dst]
    if missing:
        print(f"  [{table}] skipping columns absent in Postgres: {missing}")
    if not cols:
        print(f"  [{table}] no common columns — skipped")
        return 0

    mcur.execute(f"SELECT COUNT(*) FROM {table}")
    total = mcur.fetchone()[0]
    if total == 0:
        print(f"  [{table}] empty — nothing to copy")
        return 0

    sql = db.upsert_sql(table, cols, keys)
    pcur = pconn.cursor()
    written = 0
    mcur.execute(f"SELECT {','.join(cols)} FROM {table}")
    while True:
        rows = mcur.fetchmany(CHUNK)
        if not rows:
            break
        pcur.executemany(sql, [list(r) for r in rows])
        pconn.commit()                 # commit per chunk: progress survives a crash
        written += len(rows)
        pct = (written / total) * 100 if total else 100
        print(f"  [{table}] {written:,}/{total:,} ({pct:.0f}%)", flush=True)
    return written


def verify(mconn, pconn) -> int:
    """Compare row counts on both sides. Returns non-zero if any table differs."""
    mcur, pcur = mconn.cursor(), pconn.cursor()
    bad = 0
    print(f"{'table':<32}{'mysql':>12}{'postgres':>12}   status")
    for t in TABLES:
        try:
            mcur.execute(f"SELECT COUNT(*) FROM {t}")
            m = mcur.fetchone()[0]
        except Exception:
            m = -1
        try:
            pcur.execute(f"SELECT COUNT(*) FROM {t}")
            p = pcur.fetchone()[0]
        except Exception:
            p = -1
        ok = (m == p)
        bad += 0 if ok else 1
        print(f"{t:<32}{m:>12,}{p:>12,}   {'OK' if ok else 'MISMATCH'}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy the learned profiles from MySQL "
                                             "into PostgreSQL (production untouched).")
    ap.add_argument("--tables", nargs="*", default=None, help="subset of tables to migrate")
    ap.add_argument("--verify", action="store_true", help="only compare row counts")
    a = ap.parse_args()

    print(f"[migrate] MySQL   {config.TEST_MYSQL['host']}/{config.TEST_MYSQL['database']}")
    print(f"[migrate] Postgres {config.STORE_PG['host']}:{config.STORE_PG['port']}/{config.STORE_PG['dbname']}")
    print("[migrate] production is NOT touched by this script")

    mconn = config.mysql_connect()
    pconn = db.connect()
    try:
        if a.verify:
            return 0 if verify(mconn, pconn) == 0 else 1

        picked = a.tables or list(TABLES)
        grand = 0
        for t in picked:
            if t not in TABLES:
                print(f"  [{t}] unknown table — skipped", file=sys.stderr)
                continue
            print(f"[migrate] {t} ...")
            try:
                grand += migrate_table(mconn, pconn, t, TABLES[t])
            except Exception as e:
                pconn.rollback()
                print(f"  [{t}] FAILED: {e}", file=sys.stderr)
        print(f"\n[migrate] done — {grand:,} rows copied into PostgreSQL\n")
        print("[migrate] verifying row counts:")
        return 0 if verify(mconn, pconn) == 0 else 1
    finally:
        mconn.close()
        pconn.close()


if __name__ == "__main__":
    raise SystemExit(main())
