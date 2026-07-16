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

    def features(self, branch_id, account_no, as_of, current_amount: float = 0.0) -> dict:
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

    def features(self, branch_id, account_no, as_of, current_amount: float = 0.0) -> dict:
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
        out = subprocess.check_output(
            ["psql", "-h", config.PROD_PG["host"], "-p", str(config.PROD_PG["port"]),
             "-U", config.PROD_PG["user"], "-d", config.PROD_PG["dbname"],
             "--set=sslmode=require", "-t", "-A", "-F", "|",
             "-c", "SET default_transaction_read_only = on;", "-c", q],
            env=env, text=True,
        ).strip()
        vals = out.split("|")
        keys = ["n_1m", "amt_1m", "n_10m", "n_15m", "amt_15m", "n_1h", "recip_1h", "countries_1h", "n_24h", "benef_24h"]
        f = _empty()
        for k, v in zip(keys, vals):
            try:
                f[k] = float(v) if "amt" in k else int(v)
            except ValueError:
                pass
        return f
