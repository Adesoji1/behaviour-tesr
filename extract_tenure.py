#!/usr/bin/env python3
"""
Step 1b — Extract each account's FULL-LIFETIME stats from production (READ ONLY).

The eligibility gate ("Practical rules" §1/§2) asks a long-history question —
"has this account existed long enough and transacted enough to be trusted?" —
which must look at the account's whole lifetime, not just the recent 90 days we
learn features from. This one GROUP-BY query (over the full table, read-only)
gives us, per (branch, account):
  * first_seen_ever / last_seen_ever  -> account age (tenure)
  * lifetime_txns                     -> total transactions ever
  * lifetime_clean_txns               -> clean ones only (the trustworthy count)
  * lifetime_suspicious / blocked     -> for visibility

Output: data/tenure.csv, joined by build_profiles.py on (branch_id, origin_account_no).
"""
import os
import subprocess
import sys

import config

TENURE_SQL = """
SELECT branch_id,
       origin_account_no,
       min(date_created) AS first_seen_ever,
       max(date_created) AS last_seen_ever,
       count(*) AS lifetime_txns,
       count(*) FILTER (WHERE status = 'clean' AND is_blocked = false
                        AND sender_blacklisted = false) AS lifetime_clean_txns,
       count(*) FILTER (WHERE status = 'suspicious') AS lifetime_suspicious,
       count(*) FILTER (WHERE is_blocked = true) AS lifetime_blocked
FROM   monitoring_transactionmonitoring
WHERE  origin_account_no IS NOT NULL AND origin_account_no NOT IN ('N/A','')
GROUP  BY branch_id, origin_account_no
"""


def main() -> int:
    if not config.ALLOW_PROD_PULL:
        print("[tenure] BLOCKED — BP_ALLOW_PROD_PULL=0; refusing to read production "
              "(safety switch is ON to shield the live DB). Set BP_ALLOW_PROD_PULL=1 "
              "to allow this extract.", file=sys.stderr)
        return 2
    out = os.path.join(config.DATA_DIR, "tenure.csv")
    copy_cmd = f"\\copy ({TENURE_SQL}) TO '{out}' WITH (FORMAT csv, HEADER true)"
    env = dict(os.environ)
    env["PGPASSWORD"] = config.PROD_PG["password"]
    psql = [
        "psql", "-h", config.PROD_PG["host"], "-p", str(config.PROD_PG["port"]),
        "-U", config.PROD_PG["user"], "-d", config.PROD_PG["dbname"],
        "--set=sslmode=require",
        "-c", "SET default_transaction_read_only = on;",
        "-c", copy_cmd,
    ]
    print(f"[tenure] -> {out}")
    r = subprocess.run(psql, env=env)
    if r.returncode != 0:
        print("[tenure] FAILED", file=sys.stderr)
        return r.returncode
    try:
        with open(out, "rb") as f:
            rows = sum(1 for _ in f) - 1
        print(f"[tenure] done: {rows:,} accounts")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
