#!/usr/bin/env python3
"""
The "live feature factory" — the fast leg of the hybrid architecture.

The nightly profile captures a customer's LONG-TERM normal, but it is recomputed
only once a day, so it cannot see a burst happening right now. Velocity rules need
the RECENT window (last 1m / 10m / 15m / 1h / 24h) computed at scoring time.

This module answers: "for this account, as of this instant, how much has it done
in the last N minutes?" — using conditional aggregation over an indexed source
(`(origin_account_no, date_created)` already exists in production).

Two interchangeable sources (same interface `.features(...)`):
  * CsvVelocitySource  — reads recent activity from a CSV/extract. Fast, offline,
                         used for demos and tests.
  * ProdVelocitySource — one read-only SQL query against production. The real path.

A brand-new production system would point this at the live transaction table.
"""
from datetime import timedelta

import polars as pl

import config

WINDOWS_MIN = {"1m": 1, "10m": 10, "15m": 15, "1h": 60, "24h": 1440}


def _empty() -> dict:
    return {
        "n_1m": 0, "amt_1m": 0.0,
        "n_10m": 0,
        "n_15m": 0, "amt_15m": 0.0,
        "n_1h": 0, "recip_1h": 0, "countries_1h": 0,
        "n_24h": 0, "benef_24h": 0,
    }


class CsvVelocitySource:
    """Recent-activity lookup backed by an extracted CSV (offline / demo)."""

    def __init__(self, csv_path: str):
        self.df = pl.read_csv(csv_path, infer_schema_length=5000, ignore_errors=True).with_columns(
            pl.col("amount").cast(pl.Float64, strict=False),
            pl.col("date_created")
            .str.to_datetime(format="%Y-%m-%d %H:%M:%S%.f%#z", strict=False, time_unit="us")
            .dt.convert_time_zone("UTC").dt.replace_time_zone(None).alias("ts"),
        )

    def features(self, branch_id, account_no, as_of, current_amount: float = 0.0,
                 conn=None) -> dict:
        if as_of is None:
            return _empty()
        d = self.df.filter(
            (pl.col("branch_id") == int(branch_id))
            & (pl.col("origin_account_no").cast(pl.Utf8) == str(account_no))
            & (pl.col("ts") <= as_of)
        )
        f = _empty()

        def win(mins):
            return d.filter(pl.col("ts") >= (as_of - timedelta(minutes=mins)))

        w1, w10, w15, w60, w1440 = win(1), win(10), win(15), win(60), win(1440)
        f["n_1m"], f["amt_1m"] = w1.height, float(w1["amount"].sum() or 0)
        f["n_10m"] = w10.height
        f["n_15m"], f["amt_15m"] = w15.height, float(w15["amount"].sum() or 0)
        f["n_1h"] = w60.height
        f["recip_1h"] = int(w60["destination_account_no"].n_unique())
        f["countries_1h"] = int(w60["origin_country"].drop_nulls().n_unique())
        f["n_24h"] = w1440.height
        f["benef_24h"] = int(w1440["destination_account_no"].n_unique())
        return f


class ProdVelocitySource:
    """Recent-activity lookup via ONE read-only SQL query against production.
    This is the production path; it uses the existing (origin_account_no,
    date_created) index so the lookup stays sub-second even at billions of rows."""

    def features(self, branch_id, account_no, as_of, current_amount: float = 0.0,
                 conn=None) -> dict:
        import os
        import subprocess
        if as_of is None:
            return _empty()
        ts = as_of.strftime("%Y-%m-%d %H:%M:%S")
        q = f"""
        SELECT
          count(*) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '1 minute')  AS n_1m,
          coalesce(sum(amount) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '1 minute'),0) AS amt_1m,
          count(*) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '10 minutes') AS n_10m,
          count(*) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '15 minutes') AS n_15m,
          coalesce(sum(amount) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '15 minutes'),0) AS amt_15m,
          count(*) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '1 hour')     AS n_1h,
          count(DISTINCT destination_account_no) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '1 hour') AS recip_1h,
          count(DISTINCT origin_country) FILTER (WHERE date_created >= '{ts}'::timestamp - interval '1 hour') AS countries_1h,
          count(*) AS n_24h,
          count(DISTINCT destination_account_no) AS benef_24h
        FROM monitoring_transactionmonitoring
        WHERE branch_id = {int(branch_id)} AND origin_account_no = '{account_no}'
          AND date_created >= '{ts}'::timestamp - interval '24 hours'
          AND date_created <= '{ts}'::timestamp
        """
        env = dict(os.environ)
        env["PGPASSWORD"] = config.PROD_PG["password"]
        env["PGSSLMODE"] = config.PROD_PG["sslmode"]
        # POOLER-SAFE: --single-transaction + SET LOCAL (scoped, reset on COMMIT), -q so
        # BEGIN/SET/COMMIT tags are not printed. See sync_manager for the rationale.
        out = subprocess.check_output(
            ["psql", "-h", config.PROD_PG["host"], "-p", str(config.PROD_PG["port"]),
             "-U", config.PROD_PG["user"], "-d", config.PROD_PG["dbname"],
             "-q", "-t", "-A", "-F", "|", "--single-transaction",
             "-c", f"SET LOCAL statement_timeout = {int(config.SYNC_STATEMENT_TIMEOUT_MS)}",
             "-c", "SET LOCAL transaction_read_only = on", "-c", q],
            env=env, text=True,
        )
        # keep only the SELECT's data line (defensive against any stray command tag)
        _lines = [l for l in out.splitlines() if l.strip() and l.strip() not in ("BEGIN", "SET", "COMMIT")]
        vals = (_lines[-1] if _lines else "").split("|")
        keys = ["n_1m", "amt_1m", "n_10m", "n_15m", "amt_15m", "n_1h", "recip_1h", "countries_1h", "n_24h", "benef_24h"]
        f = _empty()
        for k, v in zip(keys, vals):
            try:
                f[k] = float(v) if "amt" in k else int(v)
            except ValueError:
                pass
        return f


class LocalVelocitySource:
    """Recent-activity lookup from the LOCAL `bp_recent_txn` table — the transactions this
    service has ITSELF seen at POST /score. Real-time, ~ms, and NO production read (this is
    what replaced the per-score production query). One conditional-aggregation query over
    the indexed (entity_key, ts). Reads its own pooled connection so it sees only committed
    prior transactions — the current one is recorded AFTER scoring, so it is never
    double-counted (the rules add the +1 for the current transaction themselves)."""

    def features(self, branch_id, account_no, as_of, current_amount: float = 0.0,
                 conn=None) -> dict:
        import db
        if as_of is None:
            return _empty()
        entity_key = f"{int(branch_id)}:{account_no}"
        sql = """
        SELECT
          count(*) FILTER (WHERE ts > %(t)s - interval '1 minute')  AS n_1m,
          coalesce(sum(amount) FILTER (WHERE ts > %(t)s - interval '1 minute'),0) AS amt_1m,
          count(*) FILTER (WHERE ts > %(t)s - interval '10 minutes') AS n_10m,
          count(*) FILTER (WHERE ts > %(t)s - interval '15 minutes') AS n_15m,
          coalesce(sum(amount) FILTER (WHERE ts > %(t)s - interval '15 minutes'),0) AS amt_15m,
          count(*) FILTER (WHERE ts > %(t)s - interval '1 hour')     AS n_1h,
          count(DISTINCT destination_account_no) FILTER (WHERE ts > %(t)s - interval '1 hour') AS recip_1h,
          count(DISTINCT origin_country) FILTER (WHERE ts > %(t)s - interval '1 hour') AS countries_1h,
          count(*) AS n_24h,
          count(DISTINCT destination_account_no) AS benef_24h
        FROM bp_recent_txn
        WHERE entity_key = %(ek)s AND ts > %(t)s - interval '24 hours' AND ts <= %(t)s
        """
        keys = ["n_1m", "amt_1m", "n_10m", "n_15m", "amt_15m", "n_1h", "recip_1h",
                "countries_1h", "n_24h", "benef_24h"]
        try:
            if conn is not None:                 # reuse the engine's connection (no 2nd conn)
                cur = conn.cursor()
                cur.execute(sql, {"ek": entity_key, "t": as_of})
                row = cur.fetchone()
            else:
                with db.pooled() as c:
                    cur = c.cursor()
                    cur.execute(sql, {"ek": entity_key, "t": as_of})
                    row = cur.fetchone()
        except Exception:                       # velocity must never break scoring
            return _empty()
        f = _empty()
        if row:
            for k, v in zip(keys, row):
                f[k] = float(v or 0) if "amt" in k else int(v or 0)
        return f


def record_txn(conn, entity_key, ts, amount, destination_account_no,
               origin_country, destination_country, currency) -> None:
    """Record ONE scored transaction into the recent-window table, on the caller's
    connection (so it commits with the decision). This is what makes the NEXT
    transactions' velocity rules see it. Never raises — recording must not break scoring."""
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bp_recent_txn (entity_key, ts, amount, destination_account_no, "
            "origin_country, destination_country, currency) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (entity_key, ts, amount, destination_account_no, origin_country,
             destination_country, currency))
    except Exception as e:
        import audit
        audit.log.warning("recent-txn record failed for %s: %s", entity_key, e)


def prune_recent(retain_hours: float | None = None) -> int:
    """Delete recent-window rows older than the retention window (default from config).
    Runs off the hot path (a /score background task, occasionally). Returns rows removed."""
    import db
    hours = retain_hours if retain_hours is not None else config.VELOCITY_RETAIN_HOURS
    try:
        conn = db.connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM bp_recent_txn WHERE ts < now() - (%s || ' hours')::interval",
                        (str(int(hours)),))
            n = cur.rowcount
            conn.commit()
            return n or 0
        finally:
            conn.close()
    except Exception as e:
        import audit
        audit.log.warning("recent-txn prune failed: %s", e)
        return 0
