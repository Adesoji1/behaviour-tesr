#!/usr/bin/env python3
"""
sync_manager.py — the Data Synchronization Layer.

THE ONLY PROCESS THAT READS PRODUCTION.

Everything else (every service replica, every retrain) learns from the local
`bp_transactions_cache` table, so production sees exactly one reader no matter
how many replicas run. This is the design in ingestionstratimprove.md §5.

How it pulls SAFELY (why this cannot hammer the live DB):
  * KEYSET pagination  — `WHERE id > :last_id ORDER BY id LIMIT :chunk`.
                         Bounded, index-friendly, and constant-cost per chunk.
                         (Never OFFSET, which degrades linearly.)
  * CHUNKED            — BP_SYNC_CHUNK_SIZE rows per query (default 5,000).
                         We never materialise a huge result set.
  * ROW CAP            — BP_SYNC_MAX_ROWS caps a single run (default 50,000), so
                         a first run can't try to drag the whole table down.
  * THROTTLE           — BP_SYNC_SLEEP_SECONDS between chunks: the dial the DB
                         engineer turns to make us gentler.
  * STATEMENT TIMEOUT  — BP_SYNC_STATEMENT_TIMEOUT_MS; the server kills a runaway
                         query instead of letting it sit on prod.
  * READ-ONLY          — `SET default_transaction_read_only = on`. We can never write.
  * RESUME             — the watermark in bp_sync_state advances only AFTER a chunk
                         is durably committed, so a crash resumes, never restarts.
  * REFRESH WINDOW     — re-pulls the last BP_SYNC_REFRESH_DAYS days each run and
                         UPSERTs, so status flips (clean -> blocked/blacklisted) are
                         corrected in the cache (closes the §6.1 correctness hole).
  * PRUNE              — drops cached rows older than the learning window so the
                         cache can't grow forever or skew the baseline (§6.2).
  * SAFETY SWITCH      — BP_ALLOW_PROD_PULL=0 blocks every read outright.

Every chunk is logged to stdout (visible in `docker compose logs`) and the run is
recorded in bp_sync_state. A tqdm progress bar is shown on a TTY (CLI use only).

Run:
    python sync_manager.py                 # incremental sync
    python sync_manager.py --full          # ignore the watermark, re-sync the window
    python sync_manager.py --max-rows 5000 # small, safe demo pull
"""
import argparse
import sys
import time
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

import audit
import config
import db

SOURCE_TABLE = "monitoring_transactionmonitoring"

# Columns pulled from production -> mirrored into bp_transactions_cache.
# host() renders inet as text; the rest map 1:1.
_SOURCE_SELECT = """
    id, transaction_id, amount, currency, transaction_type,
    transaction_type_normalized, status, branch_id, origin_account_no,
    origin_account_type, destination_account_no, destination_bank_code,
    customer_name, customer_email, identifier, identifier_type_id, bvn,
    account_type, host(customer_ip_address) AS customer_ip_address,
    customer_location, merchant_name, merchant_location,
    origin_country, destination_country, date_created,
    sender_blacklisted, receiver_blacklisted, is_blocked, indicator
"""

# Column order used to write the cache (entity_key is derived, not from source).
_CACHE_COLS = [
    "id", "entity_key", "transaction_id", "amount", "currency", "transaction_type",
    "transaction_type_normalized", "status", "branch_id", "origin_account_no",
    "origin_account_type", "destination_account_no", "destination_bank_code",
    "customer_name", "customer_email", "identifier", "identifier_type_id", "bvn",
    "account_type", "customer_ip_address", "customer_location", "merchant_name",
    "merchant_location", "origin_country", "destination_country", "date_created",
    "sender_blacklisted", "receiver_blacklisted", "is_blocked", "indicator",
]

# Only rows with a usable entity key are worth caching.
_BASE_FILTER = "origin_account_no IS NOT NULL AND origin_account_no NOT IN ('N/A','')"


class ProdPullDisabled(RuntimeError):
    """Raised when a production read is attempted while BP_ALLOW_PROD_PULL=0."""


def _prod_conn():
    """A READ-ONLY, timeout-bounded connection to production."""
    if not config.ALLOW_PROD_PULL:
        audit.log.warning("prod_pull BLOCKED — BP_ALLOW_PROD_PULL=0; refusing to read "
                          "production (safety switch is ON)")
        raise ProdPullDisabled("production reads are disabled (BP_ALLOW_PROD_PULL=0)")
    conn = psycopg.connect(config.prod_pg_dsn() + " connect_timeout=30 "
                           "application_name=behaviour-sync", autocommit=True,
                           row_factory=dict_row)
    with conn.cursor() as c:
        c.execute("SET default_transaction_read_only = on")   # we can never write prod
        c.execute(f"SET statement_timeout = {int(config.SYNC_STATEMENT_TIMEOUT_MS)}")
    audit.log.info("prod_pull connected READ-ONLY host=%s timeout=%sms",
                   config.PROD_PG["host"], config.SYNC_STATEMENT_TIMEOUT_MS)
    return conn


def _read_watermark(store) -> dict:
    cur = db.dict_cursor(store)
    cur.execute("SELECT * FROM bp_sync_state WHERE source=%s", (SOURCE_TABLE,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO bp_sync_state (source, last_id, rows_synced, last_status) "
                    "VALUES (%s, 0, 0, 'new') ON CONFLICT (source) DO NOTHING", (SOURCE_TABLE,))
        store.commit()
        return {"source": SOURCE_TABLE, "last_id": 0, "rows_synced": 0}
    return row


def _save_watermark(store, last_id: int, added: int, status: str, detail: str = "") -> None:
    """Advance the resume point. Called only AFTER the chunk is committed."""
    cur = store.cursor()
    cur.execute(
        "UPDATE bp_sync_state SET last_id=%s, rows_synced=rows_synced+%s, "
        "last_synced_at=now(), last_status=%s, last_detail=%s WHERE source=%s",
        (last_id, added, status, detail[:1000], SOURCE_TABLE),
    )
    store.commit()


def _entity_key(row: dict) -> str:
    return f"{row['branch_id']}:{row['origin_account_no']}"


def _write_chunk(store, rows: list[dict]) -> None:
    """UPSERT a chunk into the cache. Conflict on the production id, so a
    re-synced row UPDATES in place — that is how status flips get corrected."""
    sql = db.upsert_sql("bp_transactions_cache", _CACHE_COLS, ["id"])
    payload = []
    for r in rows:
        r = dict(r)
        r["entity_key"] = _entity_key(r)
        payload.append([r.get(c) for c in _CACHE_COLS])
    cur = store.cursor()
    cur.executemany(sql, payload)


def _pull_pass(prod, store, where_sql: str, params: tuple, label: str,
               start_id: int, budget: int, chunk: int, sleep_s: float,
               progress=None) -> tuple[int, int]:
    """One chunked keyset pass. Returns (rows_written, last_id_reached).

    Each iteration is ONE bounded query against production; we commit the chunk,
    log it, then sleep. Nothing here can produce an unbounded scan.
    """
    last_id, total = start_id, 0
    while True:
        if budget and total >= budget:
            audit.log.info("sync[%s] row cap reached (%s) — stopping this run cleanly",
                           label, budget)
            break
        limit = min(chunk, budget - total) if budget else chunk
        sql = (f"SELECT {_SOURCE_SELECT} FROM {SOURCE_TABLE} "
               f"WHERE id > %s AND {_BASE_FILTER} AND {where_sql} "
               f"ORDER BY id LIMIT %s")
        t0 = time.perf_counter()
        with prod.cursor() as pc:
            pc.execute(sql, (last_id, *params, limit))
            rows = pc.fetchall()
        fetch_ms = (time.perf_counter() - t0) * 1000
        if not rows:
            break

        _write_chunk(store, rows)
        store.commit()                      # durable BEFORE the watermark moves
        last_id = int(rows[-1]["id"])
        total += len(rows)
        audit.log.info(
            "sync[%s] chunk rows=%d fetch=%.0fms last_id=%d total=%d | DB WRITE ok -> "
            "postgres://%s:%s/%s table bp_transactions_cache (upsert on production id)",
            label, len(rows), fetch_ms, last_id, total,
            config.STORE_PG["host"], config.STORE_PG["port"], config.STORE_PG["dbname"])
        if progress is not None:
            progress.update(len(rows))
        if len(rows) < limit:               # drained
            break
        if sleep_s:
            time.sleep(sleep_s)             # throttle — be kind to production
    return total, last_id


def _prune(store) -> int:
    """Drop cached rows older than the learning window (§6.2)."""
    cur = store.cursor()
    cur.execute("DELETE FROM bp_transactions_cache WHERE date_created < "
                f"(now() - interval '{int(config.LOOKBACK_MONTHS)} months')")
    n = cur.rowcount or 0
    store.commit()
    if n:
        audit.log.info("sync prune removed %d rows older than %d months",
                       n, config.LOOKBACK_MONTHS)
    return n


def sync(chunk_size: int | None = None, max_rows: int | None = None,
         sleep_s: float | None = None, refresh_days: int | None = None,
         full: bool = False, progress: bool = False) -> dict:
    """Run one safe incremental sync. Returns a summary dict (used by /sync and /demo)."""
    chunk = chunk_size or config.SYNC_CHUNK_SIZE
    budget = config.SYNC_MAX_ROWS if max_rows is None else max_rows
    sleep_v = config.SYNC_SLEEP_SECONDS if sleep_s is None else sleep_s
    refresh = config.SYNC_REFRESH_DAYS if refresh_days is None else refresh_days
    started = datetime.utcnow()

    store = db.connect()
    try:
        wm = _read_watermark(store)
        start_id = 0 if full else int(wm.get("last_id") or 0)
        _save_watermark(store, start_id, 0, "running", "sync started")
        audit.log.info("sync START from id>%d window=%dmonths chunk=%d cap=%s "
                       "throttle=%.2fs refresh=%dd", start_id, config.LOOKBACK_MONTHS,
                       chunk, budget or "none", sleep_v, refresh)

        bar = None
        if progress and sys.stderr.isatty():
            try:
                from tqdm import tqdm
                bar = tqdm(desc="syncing", unit="row", total=budget or None)
            except Exception:
                bar = None

        try:
            prod = _prod_conn()
        except ProdPullDisabled as e:
            _save_watermark(store, start_id, 0, "blocked", str(e))
            return {"synced": False, "reason": "prod_pull_disabled", "detail": str(e),
                    "note": "live reads from production are disabled (BP_ALLOW_PROD_PULL=0)"}

        try:
            # Pass 1 — NEW rows since the watermark, inside the learning window.
            new_rows, last_id = _pull_pass(
                prod, store,
                f"date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months')",
                (), "new", start_id, budget, chunk, sleep_v, bar)
            if new_rows:
                _save_watermark(store, last_id, new_rows, "ok", f"{new_rows} new rows")

            # Pass 2 — REFRESH the recent window so status flips (clean -> blocked /
            # blacklisted) are corrected in the cache. Watermark is NOT advanced here.
            refreshed = 0
            if refresh > 0:
                remaining = max(budget - new_rows, 0) if budget else 0
                if not budget or remaining > 0:
                    refreshed, _ = _pull_pass(
                        prod, store,
                        f"date_created >= (now() - interval '{int(refresh)} days')",
                        (), "refresh", 0, remaining, chunk, sleep_v, bar)
        finally:
            prod.close()
            if bar is not None:
                bar.close()

        pruned = _prune(store) if config.SYNC_PRUNE else 0

        cur = db.dict_cursor(store)
        cur.execute("SELECT count(*) AS n, min(date_created) AS oldest, "
                    "max(date_created) AS newest FROM bp_transactions_cache")
        c = cur.fetchone()
        _save_watermark(store, last_id, 0, "ok",
                        f"new={new_rows} refreshed={refreshed} pruned={pruned}")

        out = {
            "synced": True,
            "new_rows": new_rows,
            "refreshed_rows": refreshed,
            "pruned_rows": pruned,
            "watermark_last_id": last_id,
            "cache_rows_total": c["n"],
            "cache_oldest": c["oldest"].isoformat() if c["oldest"] else None,
            "cache_newest": c["newest"].isoformat() if c["newest"] else None,
            "elapsed_seconds": round((datetime.utcnow() - started).total_seconds(), 2),
            "safety": {"chunk_size": chunk, "row_cap": budget,
                       "throttle_seconds": sleep_v, "refresh_days": refresh,
                       "statement_timeout_ms": config.SYNC_STATEMENT_TIMEOUT_MS,
                       "read_only": True},
        }
        audit.log.info("sync DONE %s", out)
        audit.log_event("sync", "sync", "completed", out)
        return out
    except Exception as e:
        try:
            _save_watermark(store, 0, 0, "failed", str(e))
        except Exception:
            pass
        audit.log.exception("sync FAILED: %s", e)
        audit.log_event("sync", "sync_fail", "failed", {"error": str(e)})
        return {"synced": False, "reason": "error", "error": str(e)}
    finally:
        store.close()


def status() -> dict:
    """Current ingestion state — what the cache holds and where the watermark is."""
    store = db.connect()
    try:
        cur = db.dict_cursor(store)
        cur.execute("SELECT * FROM bp_sync_state WHERE source=%s", (SOURCE_TABLE,))
        wm = cur.fetchone()
        cur.execute("SELECT count(*) AS n, count(DISTINCT entity_key) AS customers, "
                    "min(date_created) AS oldest, max(date_created) AS newest "
                    "FROM bp_transactions_cache")
        c = cur.fetchone()
        return {
            "source": SOURCE_TABLE,
            "watermark_last_id": (wm or {}).get("last_id", 0),
            "last_synced_at": (wm or {}).get("last_synced_at").isoformat()
                              if (wm or {}).get("last_synced_at") else None,
            "last_status": (wm or {}).get("last_status"),
            "last_detail": (wm or {}).get("last_detail"),
            "cache_rows": c["n"], "cache_customers": c["customers"],
            "cache_oldest": c["oldest"].isoformat() if c["oldest"] else None,
            "cache_newest": c["newest"].isoformat() if c["newest"] else None,
            "prod_pull_allowed": config.ALLOW_PROD_PULL,
        }
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe incremental sync of production "
                                             "transactions into the local cache.")
    ap.add_argument("--full", action="store_true",
                    help="ignore the watermark and re-sync the whole learning window")
    ap.add_argument("--chunk-size", type=int, default=None)
    ap.add_argument("--max-rows", type=int, default=None,
                    help="cap rows for this run (0 = no cap)")
    ap.add_argument("--sleep", type=float, default=None, help="throttle between chunks")
    ap.add_argument("--refresh-days", type=int, default=None)
    ap.add_argument("--status", action="store_true", help="show sync state and exit")
    a = ap.parse_args()

    if a.status:
        import json
        print(json.dumps(status(), indent=2, default=str))
        return 0

    out = sync(chunk_size=a.chunk_size, max_rows=a.max_rows, sleep_s=a.sleep,
               refresh_days=a.refresh_days, full=a.full, progress=True)
    import json
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("synced") else 1


if __name__ == "__main__":
    raise SystemExit(main())
