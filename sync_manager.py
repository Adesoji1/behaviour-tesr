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
  * READ-ONLY USER     — production is reached as a DB user that has only SELECT
                         on monitoring_transactionmonitoring, so writes are
                         blocked at the GRANT level, not by a session GUC. We
                         deliberately do NOT `SET default_transaction_read_only`
                         because the source is a primary under live write load,
                         and flipping that GUC can leak via the connection pool
                         and freeze the production system.
  * RESUME             — the watermark in bp_sync_state advances only AFTER the
                         new-row pass is durably committed. A crash mid-pass
                         resumes from the last good watermark; rows already
                         cached are re-UPSERTed, which is idempotent.
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
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import datetime, timezone

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
    "id",
    "entity_key",
    "transaction_id",
    "amount",
    "currency",
    "transaction_type",
    "transaction_type_normalized",
    "status",
    "branch_id",
    "origin_account_no",
    "origin_account_type",
    "destination_account_no",
    "destination_bank_code",
    "customer_name",
    "customer_email",
    "identifier",
    "identifier_type_id",
    "bvn",
    "account_type",
    "customer_ip_address",
    "customer_location",
    "merchant_name",
    "merchant_location",
    "origin_country",
    "destination_country",
    "date_created",
    "sender_blacklisted",
    "receiver_blacklisted",
    "is_blocked",
    "indicator",
]

# Only rows with a usable entity key are worth caching.
_BASE_FILTER = "origin_account_no IS NOT NULL AND origin_account_no NOT IN ('N/A','')"


class ProdPullDisabled(RuntimeError):
    """Raised when a production read is attempted while BP_ALLOW_PROD_PULL=0."""


def _prod_conn():
    """A timeout-bounded connection to production.

    Read-only is enforced at the GRANT level: the sync DB user has only SELECT
    on monitoring_transactionmonitoring, so it cannot write regardless of
    session state. We deliberately do NOT set
    `default_transaction_read_only = on` because the source is a primary
    under live write load from other services, and a session-level GUC can
    leak through the connection pool and freeze the production system.
    """
    if not config.ALLOW_PROD_PULL:
        audit.log.warning(
            "prod_pull BLOCKED — BP_ALLOW_PROD_PULL=0; refusing to read "
            "production (safety switch is ON)"
        )
        raise ProdPullDisabled("production reads are disabled (BP_ALLOW_PROD_PULL=0)")
    conn = psycopg.connect(
        config.prod_pg_dsn() + " connect_timeout=30 application_name=behaviour-sync",
        autocommit=True,
        row_factory=dict_row,
    )
    with conn.cursor() as c:
        # Read-only is enforced by the sync DB user's GRANTs (see docstring).
        # Do NOT add `SET default_transaction_read_only = on` here: this is a
        # primary under live writes and a session-level GUC can leak via the
        # connection pool and freeze the production system.
        c.execute(f"SET statement_timeout = {int(config.SYNC_STATEMENT_TIMEOUT_MS)}")
    audit.log.info(
        "prod_pull connected host=%s timeout=%sms",
        config.PROD_PG["host"],
        config.SYNC_STATEMENT_TIMEOUT_MS,
    )
    return conn


@contextmanager
def _guarded_txn(prod):
    """Open a transaction on `prod` that is READ-ONLY and statement-timed-out for its
    duration only (SET LOCAL). Pooler-safe: both settings reset when the block exits."""
    with prod.transaction():
        with prod.cursor() as c:
            # integer from config (cast) — safe to inline; SET takes no bind params
            c.execute(f"SET LOCAL statement_timeout = {int(config.SYNC_STATEMENT_TIMEOUT_MS)}")
            c.execute("SET LOCAL transaction_read_only = on")
        yield


def _read_watermark(store) -> dict:
    cur = db.dict_cursor(store)
    cur.execute("SELECT * FROM bp_sync_state WHERE source=%s", (SOURCE_TABLE,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO bp_sync_state (source, last_id, rows_synced, last_status) "
            "VALUES (%s, 0, 0, 'new') ON CONFLICT (source) DO NOTHING",
            (SOURCE_TABLE,),
        )
        store.commit()
        return {"source": SOURCE_TABLE, "last_id": 0, "rows_synced": 0}
    return row


def _save_watermark(
    store, last_id: int, added: int, status: str, detail: str = ""
) -> None:
    """Advance the resume point. Called only AFTER the chunk is committed."""
    cur = store.cursor()
    cur.execute(
        "UPDATE bp_sync_state SET last_id=%s, rows_synced=rows_synced+%s, "
        "last_synced_at=now(), last_status=%s, last_detail=%s WHERE source=%s",
        (last_id, added, status, detail[:1000], SOURCE_TABLE),
    )
    store.commit()


def _mark_failed(store, detail: str) -> None:
    """Record a FAILED sync WITHOUT touching last_id — a transient prod outage (e.g. a
    connection timeout) must never reset the resume point, or the next successful run
    would re-scan the whole window from id 0. Only status/detail/time change."""
    try:
        cur = store.cursor()
        cur.execute(
            "UPDATE bp_sync_state SET last_synced_at=now(), last_status='failed', "
            "last_detail=%s WHERE source=%s", (detail[:1000], SOURCE_TABLE))
        store.commit()
    except Exception:
        pass


def _entity_key(row: dict) -> str:
    return f"{row['branch_id']}:{row['origin_account_no']}"


def _write_chunk(store, rows: list[dict]) -> tuple[int, int]:
    """UPSERT a chunk into the cache. Conflict on the production id, so a
    re-synced row UPDATES in place — that is how status flips get corrected.

    Rows are written ONE AT A TIME so that a single bad row (e.g. a value
    too long for a VARCHAR column, an enum mismatch, a NOT NULL violation)
    does not poison the whole `executemany` batch. The offending row is
    logged with its production id and a one-line error, and skipped —
    the run continues. Returns (written, skipped).
    """
    sql = db.upsert_sql("bp_transactions_cache", _CACHE_COLS, ["id"])
    written, skipped = 0, 0
    cur = store.cursor()
    for r in rows:
        r = dict(r)
        r["entity_key"] = _entity_key(r)
        payload = [r.get(c) for c in _CACHE_COLS]
        try:
            cur.execute(sql, payload)
            written += 1
        except Exception as e:
            skipped += 1
            # First line of the psycopg error is the most useful (e.g.
            # "value too long for type character varying(64)").
            msg = str(e).strip().splitlines()[0] if str(e) else type(e).__name__
            audit.log.warning(
                "sync cache write SKIPPED id=%s: %s (row left out of cache; "
                "the rest of the chunk continues)",
                r.get("id"),
                msg,
            )
    return written, skipped


def _pull_pass(
    prod,
    store,
    where_sql: str,
    params: tuple,
    label: str,
    start_id: int,
    budget: int,
    chunk: int,
    sleep_s: float,
    progress=None,
) -> tuple[int, int]:
    """One chunked keyset pass. Returns (rows_written, last_id_reached).

    Each iteration is ONE bounded query against production; we commit the chunk,
    log it, then sleep. Nothing here can produce an unbounded scan.
    """
    last_id, total = start_id, 0
    while True:
        if budget and total >= budget:
            audit.log.info(
                "sync[%s] row cap reached (%s) — stopping this run cleanly",
                label,
                budget,
            )
            break
        limit = min(chunk, budget - total) if budget else chunk
        sql = (
            f"SELECT {_SOURCE_SELECT} FROM {SOURCE_TABLE} "
            f"WHERE id > %s AND {_BASE_FILTER} AND {where_sql} "
            f"ORDER BY id LIMIT %s"
        )
        t0 = time.perf_counter()
        # read-only + statement timeout are scoped to THIS transaction (pooler-safe)
        with _guarded_txn(prod), prod.cursor() as pc:
            pc.execute(sql, (last_id, *params, limit))
            rows = pc.fetchall()
        fetch_ms = (time.perf_counter() - t0) * 1000
        if not rows:
            break

        written, skipped = _write_chunk(store, rows)
        store.commit()  # durable BEFORE the watermark moves
        last_id = int(rows[-1]["id"])
        total += len(rows)
        audit.log.info(
            "sync[%s] chunk rows=%d written=%d skipped=%d fetch=%.0fms "
            "last_id=%d total=%d | DB WRITE ok -> "
            "postgres://%s:%s/%s table bp_transactions_cache (upsert on production id)",
            label,
            len(rows),
            written,
            skipped,
            fetch_ms,
            last_id,
            total,
            config.STORE_PG["host"],
            config.STORE_PG["port"],
            config.STORE_PG["dbname"],
        )
        if progress is not None:
            progress.update(len(rows))
        if len(rows) < limit:  # drained
            break
        if sleep_s:
            time.sleep(sleep_s)  # throttle — be kind to production
    return total, last_id


def _remaining_rows(prod, start_id: int) -> int | None:
    """Count how many production rows are still to pull (id > watermark, in the learning
    window) so the progress bar can show a real percentage. One bounded, pooler-safe,
    read-only COUNT. Returns None if it fails (the bar then just counts up)."""
    try:
        sql = (
            f"SELECT count(*) AS n FROM {SOURCE_TABLE} "
            f"WHERE id > %s AND {_BASE_FILTER} "
            f"AND date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months')"
        )
        with _guarded_txn(prod), prod.cursor() as pc:
            pc.execute(sql, (start_id,))
            row = pc.fetchone()  # prod cursor yields dict rows
            return int(row["n"] if isinstance(row, dict) else row[0])
    except Exception as e:  # never let the progress total break the sync
        audit.log.warning("could not count remaining rows for the progress bar: %s", e)
        return None


def _prune(store) -> int:
    """Drop cached rows older than the learning window (§6.2)."""
    cur = store.cursor()
    cur.execute(
        "DELETE FROM bp_transactions_cache WHERE date_created < "
        f"(now() - interval '{int(config.LOOKBACK_MONTHS)} months')"
    )
    n = cur.rowcount or 0
    store.commit()
    if n:
        audit.log.info(
            "sync prune removed %d rows older than %d months", n, config.LOOKBACK_MONTHS
        )
    return n


def sync(
    chunk_size: int | None = None,
    max_rows: int | None = None,
    sleep_s: float | None = None,
    refresh_days: int | None = None,
    full: bool = False,
    progress: bool = False,
) -> dict:
    """Run one safe incremental sync. Returns a summary dict (used by /sync and /demo)."""
    chunk = chunk_size or config.SYNC_CHUNK_SIZE
    budget = config.SYNC_MAX_ROWS if max_rows is None else max_rows
    sleep_v = config.SYNC_SLEEP_SECONDS if sleep_s is None else sleep_s
    refresh = config.SYNC_REFRESH_DAYS if refresh_days is None else refresh_days
    started = datetime.now(timezone.utc)

    store = db.connect()
    # Track the last id durably committed to bp_sync_state. The error handler
    # uses this so a failure NEVER rewinds the watermark (which would force a
    # full re-pull of the learning window next run). Default to 0 so it is
    # always defined even if db.connect() or the watermark read itself fails.
    last_committed_id = 0
    try:
        wm = _read_watermark(store)
        start_id = 0 if full else int(wm.get("last_id") or 0)
        last_committed_id = start_id
        _save_watermark(store, start_id, 0, "running", "sync started")
        audit.log.info(
            "sync START from id>%d window=%dmonths chunk=%d cap=%s "
            "throttle=%.2fs refresh=%dd",
            start_id,
            config.LOOKBACK_MONTHS,
            chunk,
            budget or "none",
            sleep_v,
            refresh,
        )

        try:
            prod = _prod_conn()
        except ProdPullDisabled as e:
            _save_watermark(store, start_id, 0, "blocked", str(e))
            return {
                "synced": False,
                "reason": "prod_pull_disabled",
                "detail": str(e),
                "note": "live reads from production are disabled (BP_ALLOW_PROD_PULL=0)",
            }

        # Progress bar. Shown whenever progress=True (NOT gated on a TTY, so it also renders
        # in a background run's log — watch it with `tail -f`). When there is no row cap we
        # count the remaining rows so the bar shows a real % complete + ETA.
        bar = None
        if progress:
            try:
                from tqdm import tqdm

                total = budget or _remaining_rows(prod, start_id)
                bar = tqdm(
                    desc="backfill", unit="row", total=total, file=sys.stdout,
                    mininterval=2.0, dynamic_ncols=True, smoothing=0.05,
                )
            except Exception:
                bar = None

        try:
            # Pass 1 — NEW rows since the watermark, inside the learning window.
            new_rows, last_id = _pull_pass(
                prod,
                store,
                f"date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months')",
                (),
                "new",
                start_id,
                budget,
                chunk,
                sleep_v,
                bar,
            )
            if new_rows:
                _save_watermark(store, last_id, new_rows, "ok", f"{new_rows} new rows")
                last_committed_id = last_id

            # Pass 2 — REFRESH the recent window so status flips (clean -> blocked /
            # blacklisted) are corrected in the cache. Watermark is NOT advanced here.
            refreshed = 0
            if refresh > 0:
                remaining = max(budget - new_rows, 0) if budget else 0
                if not budget or remaining > 0:
                    refreshed, _ = _pull_pass(
                        prod,
                        store,
                        f"date_created >= (now() - interval '{int(refresh)} days')",
                        (),
                        "refresh",
                        0,
                        remaining,
                        chunk,
                        sleep_v,
                        bar,
                    )
        finally:
            prod.close()
            if bar is not None:
                bar.close()

        pruned = _prune(store) if config.SYNC_PRUNE else 0

        cur = db.dict_cursor(store)
        cur.execute(
            "SELECT count(*) AS n, min(date_created) AS oldest, "
            "max(date_created) AS newest FROM bp_transactions_cache"
        )
        c = cur.fetchone()
        _save_watermark(
            store,
            last_id,
            0,
            "ok",
            f"new={new_rows} refreshed={refreshed} pruned={pruned}",
        )
        last_committed_id = last_id

        out = {
            "synced": True,
            "new_rows": new_rows,
            "refreshed_rows": refreshed,
            "pruned_rows": pruned,
            "watermark_last_id": last_id,
            "cache_rows_total": c["n"],
            "cache_oldest": c["oldest"].isoformat() if c["oldest"] else None,
            "cache_newest": c["newest"].isoformat() if c["newest"] else None,
            "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
            "safety": {
                "chunk_size": chunk,
                "row_cap": budget,
                "throttle_seconds": sleep_v,
                "refresh_days": refresh,
                "statement_timeout_ms": config.SYNC_STATEMENT_TIMEOUT_MS,
                "read_only": True,                        # sync DB user has only SELECT on source
                "read_only_enforcement": "db_grant",      # NOT a session GUC (see _prod_conn)
            },
        }
        audit.log.info("sync DONE %s", out)
        audit.log_event("sync", "sync", "completed", out)
        return out
    except Exception as e:
        try:
            # Preserve the last durably-committed watermark — never rewind to 0
            # on a transient failure (would force a full re-pull next run).
            _save_watermark(store, last_committed_id, 0, "failed", str(e))
        except Exception:
            pass
        audit.log.exception("sync FAILED: %s", e)
        audit.log_event("sync", "sync_fail", "failed", {"error": str(e)})
        return {"synced": False, "reason": "error", "error": str(e)}
    finally:
        store.close()


def schedule_description() -> str:
    """One human line describing HOW the scheduler runs — so the log, GET /sync/status
    and the demo all say the same thing. Daily when BP_SYNC_AT_HOUR is set, else interval."""
    if config.SYNC_AT_HOUR is not None:
        return (f"daily at {config.SYNC_AT_HOUR:02d}:{config.SYNC_AT_MINUTE:02d} "
                f"{config.SYNC_TZ}")
    return f"every {max(int(config.SYNC_INTERVAL_SECONDS), 30)}s"


def _seconds_until_next(hour: int, minute: int, tzname: str) -> float:
    """Seconds from now until the next occurrence of hour:minute in timezone `tzname`."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tzname)
    except Exception:                       # bad/missing tz db -> fall back to UTC, logged
        audit.log.warning("sync: unknown timezone %r, using UTC", tzname)
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def status() -> dict:
    """Current ingestion state — what the cache holds and where the watermark is."""
    store = db.connect()
    try:
        cur = db.dict_cursor(store)
        cur.execute("SELECT * FROM bp_sync_state WHERE source=%s", (SOURCE_TABLE,))
        wm = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS n, count(DISTINCT entity_key) AS customers, "
            "min(date_created) AS oldest, max(date_created) AS newest "
            "FROM bp_transactions_cache"
        )
        c = cur.fetchone()
        return {
            "source": SOURCE_TABLE,
            "watermark_last_id": (wm or {}).get("last_id", 0),
            "last_synced_at": (wm or {}).get("last_synced_at").isoformat()
            if (wm or {}).get("last_synced_at")
            else None,
            "last_status": (wm or {}).get("last_status"),
            "last_detail": (wm or {}).get("last_detail"),
            "cache_rows": c["n"],
            "cache_customers": c["customers"],
            "cache_oldest": c["oldest"].isoformat() if c["oldest"] else None,
            "cache_newest": c["newest"].isoformat() if c["newest"] else None,
            "prod_pull_allowed": config.ALLOW_PROD_PULL,
            "schedule": schedule_description(),
        }
    finally:
        store.close()


def _post_slack(text: str) -> None:
    """Post one message to Slack via stdlib urllib (like webhooks.py — no extra dependency).
    Logs and returns quietly when no webhook is configured. Never raises."""
    from ml import config as mlcfg
    url = getattr(mlcfg, "SLACK_WEBHOOK_URL", "")
    if not url:
        audit.log.warning("Slack not configured (BF_/BP_SLACK_WEBHOOK_URL) — alert logged only: %s", text)
        return
    import json as _json
    import urllib.request
    req = urllib.request.Request(url, data=_json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).close()


def _alert_once(alert_key: str, signature: str, text: str) -> None:
    """Slack `text` ONLY when this alert's state CHANGED (edge-triggered de-dup) — so a standing
    condition is announced ONCE, not on every pull, and deploy.sh + the sync never double-post. The
    last signature per alert_key lives in bp_alert_state (survives restarts). An empty `text` means a
    'cleared' state: the signature is updated silently (no Slack) so the alert re-fires if it returns.
    Never raises — alerting must not affect ingestion."""
    try:
        with db.pooled() as conn:
            cur = db.dict_cursor(conn)
            cur.execute("SELECT signature FROM bp_alert_state WHERE alert_key=%s", (alert_key,))
            row = cur.fetchone()
            if row and row["signature"] == signature:
                return                      # same state already announced — stay quiet
            conn.cursor().execute(
                "INSERT INTO bp_alert_state (alert_key, signature, updated_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (alert_key) DO UPDATE SET signature=EXCLUDED.signature, updated_at=now()",
                (alert_key, signature))
            conn.commit()
        if text:                            # empty = a cleared state -> update silently, no Slack
            _post_slack(text)
    except Exception as e:
        audit.log.warning("alert-once (%s) failed (non-fatal): %s", alert_key, e)


def _check_retrain_due() -> None:
    """After each pull, evaluate the MODEL-retrain triggers (§4: new-data / age / amount drift) and
    Slack — ONCE per state change — if a retrain is now due. Detection only (read-only, CPU); the
    retrain stays the gated MANUAL GPU job. Never raises."""
    try:
        from ml import retrain_trigger
        d = retrain_trigger.evaluate()
        if d.get("should_retrain"):
            reasons = "; ".join(d.get("reasons", []))
            audit.log.warning("retrain DUE — %s", reasons)
            _alert_once("retrain_due", "due:" + reasons,
                        ":arrows_counterclockwise: *Behavioural model retrain DUE* — " + reasons +
                        "\nRetraining is *manual* for now. When ready, on the GPU host run "
                        "`docker compose --profile train run --rm --entrypoint python trainer "
                        "-m ml.retrain_trigger --run` (promotes only if it beats the active model), "
                        "then `curl -X POST http://<service>:8080/reload`.")
        else:
            audit.log.info("retrain check: not due — signals=%s", d.get("signals"))
            _alert_once("retrain_due", "notdue", "")     # clear silently -> re-alerts if it returns
    except Exception as e:                      # detection must never break ingestion
        audit.log.warning("retrain check failed (non-fatal): %s", e)


def _check_live_drift() -> None:
    """Automatic live drift / health watch (§9): read the decisions /score wrote to bp_decision and
    Slack — ONCE per state change — if the flagged rate drifts out of band (over/under-flagging). We
    call check_live(alert=False) and do our own de-duped Slack (its built-in alert needs httpx, which
    isn't in this image). Detection only; never retrains, never raises."""
    try:
        from ml import monitor
        st = monitor.check_live(alert=False)
        problems = st.get("problems") or []
        if problems:
            audit.log.warning("live monitor UNHEALTHY — %s", "; ".join(problems))
            _alert_once("live_drift", "unhealthy:" + "; ".join(sorted(problems)),
                        ":rotating_light: *Behavioural model looks UNHEALTHY in production* "
                        f"(flag rate {st.get('flag_rate')}, {st.get('total_decisions')} decisions/"
                        f"{st.get('window_hours')}h)\n" + "\n".join("• " + p for p in problems) +
                        "\nLikely data/behaviour drift or a stale model — consider a manual retrain.")
        else:
            audit.log.info("live monitor: %s", st.get("note"))
            _alert_once("live_drift", "healthy", "")     # clear silently
    except Exception as e:
        audit.log.warning("live monitor failed (non-fatal): %s", e)


def _run_one_sync() -> None:
    """One scheduled ingestion, with the outcome logged. Never raises — the scheduler
    must survive any single failure and try again at the next scheduled time."""
    try:
        out = sync()   # uses the full env-configured caps (not the small demo cap)
        audit.log.info("sync scheduler run done: %s", {k: out.get(k) for k in
                       ("synced", "new_rows", "refreshed_rows", "pruned_rows",
                        "cache_rows_total", "reason")})
        _check_retrain_due()   # fresh data landed — is the MODEL now due for a retrain? (§4, de-duped alert)
        _check_live_drift()    # is the LIVE decision mix drifting out of band? (§9, de-duped alert)
    except Exception as e:                      # never let the loop die
        audit.log.exception("sync scheduler run FAILED (will retry at next scheduled time): %s", e)


def run_forever() -> None:
    """The PRODUCTION scheduler. This is the ONLY production reader and is NOT triggered
    by any HTTP request — it runs as its own background service (`python sync_manager.py
    --loop`, one instance).

    Two modes, chosen by config (see schedule_description()):
      * DAILY  — when BP_SYNC_AT_HOUR is set: sync once a day at that wall-clock time in
                 BP_SYNC_TZ (the production default, e.g. 04:00 Africa/Lagos). Equivalent
                 to a crontab `0 4 * * *`, but self-contained (no host crond).
      * INTERVAL — otherwise: sync every BP_SYNC_INTERVAL_SECONDS (useful for the initial
                 backfill and for demos).

    Each run is bounded/throttled (see sync()) and every error is caught so the loop
    never dies. If BP_ALLOW_PROD_PULL=0 the sync self-skips (logged) and we simply wait
    for the next time — so ingestion can be paused without stopping the service.
    """
    daily = config.SYNC_AT_HOUR is not None
    try:                                    # make sure our tables exist (incl. bp_alert_state for
        db.ensure_schema()                  # de-duped alerts); idempotent + advisory-locked, safe
    except Exception as e:                  # alongside the API's own ensure_schema.
        audit.log.warning("sync: ensure_schema failed at start (%s) — continuing", e)
    audit.log.info("sync scheduler START — %s (run_on_start=%s, cap=%s/run, chunk=%s, "
                   "throttle=%.2fs). This is the only production reader.",
                   schedule_description(), config.SYNC_RUN_ON_START,
                   config.SYNC_MAX_ROWS or "none", config.SYNC_CHUNK_SIZE,
                   config.SYNC_SLEEP_SECONDS)

    # Webhook OUTBOX relay rides along in this same container (its own thread + cadence),
    # so a decision whose fast inline delivery was lost (e.g. an API crash) is still
    # delivered with retries. Decoupled from the sync cadence: it sweeps every few
    # seconds, not every sync run. Only started when a webhook URL is configured.
    if config.WEBHOOK_RELAY_ENABLED and config.SCORE_WEBHOOK_URL:
        import threading

        import webhook_relay
        threading.Thread(target=webhook_relay.run_forever, name="webhook-relay",
                         daemon=True).start()
    elif config.WEBHOOK_RELAY_ENABLED:
        audit.log.info("webhook relay idle — no BP_SCORE_WEBHOOK_URL configured")

    if config.SYNC_RUN_ON_START:
        audit.log.info("sync: run_on_start — one catch-up ingestion now")
        _run_one_sync()

    # After any run_on_start catch-up, both loops SLEEP first, then sync — so the daily
    # run lands at the scheduled time and the interval run waits a full interval.
    if daily:
        while True:
            secs = _seconds_until_next(config.SYNC_AT_HOUR, config.SYNC_AT_MINUTE, config.SYNC_TZ)
            audit.log.info("sync: next scheduled ingestion in %.1fh (at %s)",
                           secs / 3600.0, schedule_description())
            time.sleep(secs)
            _run_one_sync()
    else:
        interval = max(int(config.SYNC_INTERVAL_SECONDS), 30)
        while True:
            time.sleep(interval)
            _run_one_sync()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Safe incremental sync of production "
        "transactions into the local cache."
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="ignore the watermark and re-sync the whole learning window",
    )
    ap.add_argument("--chunk-size", type=int, default=None)
    ap.add_argument(
        "--max-rows", type=int, default=None, help="cap rows for this run (0 = no cap)"
    )
    ap.add_argument("--sleep", type=float, default=None, help="throttle between chunks")
    ap.add_argument("--refresh-days", type=int, default=None)
    ap.add_argument("--status", action="store_true", help="show sync state and exit")
    ap.add_argument(
        "--loop",
        action="store_true",
        help="PRODUCTION scheduler: sync forever on the configured schedule "
        "(daily at BP_SYNC_AT_HOUR, or every BP_SYNC_INTERVAL_SECONDS)",
    )
    a = ap.parse_args()

    if a.status:
        import json

        print(json.dumps(status(), indent=2, default=str))
        return 0

    if a.loop:
        run_forever()
        return 0

    out = sync(
        chunk_size=a.chunk_size,
        max_rows=a.max_rows,
        sleep_s=a.sleep,
        refresh_days=a.refresh_days,
        full=a.full,
        progress=True,
    )
    import json

    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("synced") else 1


if __name__ == "__main__":
    raise SystemExit(main())
