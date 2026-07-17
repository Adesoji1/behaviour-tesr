#!/usr/bin/env python3
"""
Step 1 — Extract the FRESH transaction dataset from production (READ ONLY).

We do NOT use monitoring_customerbranchprofile_export.csv (its data is wrong /
pre-computed the wrong way). We also do NOT reuse the stale
monitoring_transactionmonitoring_202606240748.csv (a partial 600k-row export).
Instead we pull the last N months (default 6) straight from the source of truth,
`monitoring_transactionmonitoring`, selecting only the columns the profile needs.

Read-only guarantees:
  * We only issue a single SELECT inside psql's \copy.
  * No INSERT/UPDATE/DELETE/DDL is ever sent to production.

Usage:
    python extract_transactions.py                 # full 6-month pull
    python extract_transactions.py --sample-limit 200000   # quick slice for testing
    python extract_transactions.py --branch 231            # one branch only
"""
import argparse
import os
import subprocess
import sys

import config

# Columns needed for behaviour profiling (kept lean on purpose).
SELECT_COLS = """
    id,
    transaction_id,
    amount,
    currency,
    transaction_type,
    transaction_type_normalized,
    status,
    branch_id,
    origin_account_no,
    origin_account_type,
    destination_account_no,
    destination_bank_code,
    customer_name,
    customer_email,
    identifier,
    identifier_type_id,
    bvn,
    account_type,
    host(customer_ip_address) AS customer_ip_address,
    customer_location,
    merchant_name,
    merchant_location,
    origin_country,
    destination_country,
    date_created,
    sender_blacklisted,
    receiver_blacklisted,
    is_blocked,
    indicator
"""


def build_inner_select(months: int, branch: str | None, limit: int | None) -> str:
    where = [f"date_created >= (now() - interval '{months} months')"]
    if branch:
        where.append(f"branch_id = {int(branch)}")
    # only rows with a usable entity key
    where.append("origin_account_no IS NOT NULL")
    where.append("origin_account_no NOT IN ('N/A','')")
    sql = f"SELECT {SELECT_COLS} FROM monitoring_transactionmonitoring WHERE {' AND '.join(where)}"
    sql += " ORDER BY branch_id, origin_account_no, date_created"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=config.LOOKBACK_MONTHS)
    ap.add_argument("--branch", default=None, help="restrict to a single branch_id")
    ap.add_argument("--sample-limit", type=int, default=None, dest="limit")
    ap.add_argument("--out", default=os.path.join(config.DATA_DIR, "transactions.csv"))
    args = ap.parse_args()

    if not config.ALLOW_PROD_PULL:
        print("[transactions] BLOCKED — BP_ALLOW_PROD_PULL=0; refusing to read "
              "production (safety switch is ON to shield the live DB). Set "
              "BP_ALLOW_PROD_PULL=1 to allow this extract.", file=sys.stderr)
        return 2

    inner = build_inner_select(args.months, args.branch, args.limit)
    # \copy runs client-side; the SELECT is the only thing sent to prod.
    copy_cmd = f"\\copy ({inner}) TO '{args.out}' WITH (FORMAT csv, HEADER true)"

    env = dict(os.environ)
    env["PGPASSWORD"] = config.PROD_PG["password"]
    env["PGSSLMODE"] = config.PROD_PG["sslmode"]
    # POOLER-SAFE (PgBouncer on port 25061): --single-transaction wraps every -c in one
    # BEGIN..COMMIT, so SET LOCAL is scoped to that transaction and reset on COMMIT — it
    # never leaks to the next pooled client. A plain session SET would corrupt the pool.
    psql = [
        "psql",
        "-h", config.PROD_PG["host"],
        "-p", str(config.PROD_PG["port"]),
        "-U", config.PROD_PG["user"],
        "-d", config.PROD_PG["dbname"],
        "--single-transaction",
        "-c", f"SET LOCAL statement_timeout = {int(config.SYNC_STATEMENT_TIMEOUT_MS)}",
        "-c", "SET LOCAL transaction_read_only = on",
        "-c", copy_cmd,
    ]
    print(f"[extract] months={args.months} branch={args.branch} limit={args.limit}")
    print(f"[extract] -> {args.out}")
    r = subprocess.run(psql, env=env)
    if r.returncode != 0:
        print("[extract] FAILED", file=sys.stderr)
        return r.returncode

    # report row count
    try:
        with open(args.out, "rb") as f:
            rows = sum(1 for _ in f) - 1
        size_mb = os.path.getsize(args.out) / 1e6
        print(f"[extract] done: {rows:,} rows, {size_mb:,.1f} MB")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
