#!/usr/bin/env python3
"""
Profile-store access layer — PostgreSQL (psycopg v3).

One module owns the SQL dialect so the rest of the code stays plain SQL. This is
the MySQL -> PostgreSQL alignment described in `ingestionstratimprove.md` §7: the
transaction source is already Postgres, so we now run one engine, one driver and
one dialect end-to-end.

What lives here:
  * connect()                 -> connection to the profile store
  * dict_cursor(conn)         -> cursor yielding dict rows (like pymysql DictCursor)
  * upsert_sql(...)           -> INSERT ... ON CONFLICT ... DO UPDATE  (was ON DUPLICATE KEY)
  * try_lock() / unlock()     -> per-customer advisory lock (was MySQL GET_LOCK)

Placeholders stay `%s` in psycopg exactly as in pymysql, so existing SQL carries
over unchanged; only the dialect-specific pieces above needed replacing.
"""
import hashlib

import psycopg
from psycopg.rows import dict_row

import config


def connect():
    """A connection to the PROFILE STORE (PostgreSQL).

    Mirrors the previous `config.mysql_connect()` lifecycle: caller closes it.
    Connections are cheap here because the store is local/co-located; a pool
    (PgBouncer) is the documented next step, not needed for correctness.
    """
    return psycopg.connect(config.pg_store_dsn(), autocommit=False)


def dict_cursor(conn):
    """Cursor that returns dict rows — the psycopg equivalent of
    `conn.cursor(pymysql.cursors.DictCursor)`."""
    return conn.cursor(row_factory=dict_row)


def upsert_sql(table: str, cols: list[str], conflict_cols: list[str],
               update_cols: list[str] | None = None) -> str:
    """Build `INSERT ... ON CONFLICT (keys) DO UPDATE SET c=EXCLUDED.c`.

    Postgres' equivalent of MySQL's `ON DUPLICATE KEY UPDATE c=VALUES(c)`.
    `update_cols` defaults to every column that is not part of the conflict key.
    """
    if update_cols is None:
        update_cols = [c for c in cols if c not in conflict_cols]
    placeholders = ",".join(["%s"] * len(cols))
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({','.join(conflict_cols)}) ")
    if update_cols:
        sets = ",".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
        sql += f"DO UPDATE SET {sets}"
    else:
        sql += "DO NOTHING"
    return sql


def _lock_key(entity_key: str) -> int:
    """A stable signed 64-bit key for pg_advisory_lock, derived from entity_key."""
    h = hashlib.blake2b(entity_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def try_lock(cur, entity_key: str) -> bool:
    """Take the per-customer advisory lock without blocking.

    Replaces MySQL `GET_LOCK`. Different customers never contend; the SAME
    customer can only be retrained by one worker at a time. Non-blocking, so a
    concurrent retrain is reported as 'busy' and skipped rather than queueing.
    The lock is session-scoped: released by unlock() or when the connection closes.
    """
    cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (_lock_key(entity_key),))
    row = cur.fetchone()
    got = row["got"] if isinstance(row, dict) else row[0]
    return bool(got)


def unlock(cur, entity_key: str) -> None:
    """Release the per-customer advisory lock (replaces MySQL RELEASE_LOCK)."""
    cur.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(entity_key),))
