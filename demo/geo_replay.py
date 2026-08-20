#!/usr/bin/env python3
"""
Replay a sample of ELIGIBLE customers' REAL cached transactions through /score, to measure geo
resolution coverage on realistic traffic (phase2 shadow). Sends the real customer_location /
customer_ip_address so the geo shadow resolves against production-shaped data. Ordered by
(customer, time) so geo-velocity can accumulate per customer. Telemetry-only; changes nothing.

    docker exec adhere-behaviour python demo/geo_replay.py --n 800
Then:
    docker exec adhere-behaviour python demo/geo_coverage.py
"""
from __future__ import annotations

import argparse
import json
import os
import random
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800, help="max transactions to replay")
    ap.add_argument("--mode", choices=["random", "pluscode"], default="random",
                    help="random eligible sample, or bias toward customers with Plus-Code locations")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    if a.seed is not None:
        random.seed(a.seed)

    from ml import registry, db
    import joblib
    v = registry.active()
    fb = joblib.load(registry.model_dir(v) / "featurebuilder.joblib")
    eligible = list(fb["baselines"].keys())
    print(f"active model {v}: {len(eligible)} eligible customers")

    cols = """transaction_id, identifier, amount, currency, transaction_type, account_type,
              customer_name, customer_email, customer_ip_address, customer_location, date_created"""
    if a.mode == "pluscode":
        # eligible customers who have Plus-Code-bearing locations (>=2 such txns), most first
        idf = db.read_sql(
            "SELECT identifier, count(*) n FROM bp_transactions_cache "
            "WHERE identifier = ANY(%s) AND customer_location LIKE '%%+%%' "
            "GROUP BY identifier HAVING count(*) >= 2 ORDER BY n DESC LIMIT 400", (eligible,))
        sample = idf["identifier"].astype(str).tolist()
        print(f"pluscode mode: {len(sample)} eligible customers with >=2 Plus-Code locations")
        q = (f"SELECT {cols} FROM bp_transactions_cache WHERE identifier = ANY(%s) "
             "AND customer_location LIKE '%%+%%' ORDER BY identifier, date_created LIMIT %s")
    else:
        sample = random.sample(eligible, min(1000, len(eligible)))   # representative
        q = (f"SELECT {cols} FROM bp_transactions_cache WHERE identifier = ANY(%s) AND amount > 0 "
             "ORDER BY identifier, date_created LIMIT %s")
    df = db.read_sql(q, (sample, a.n))
    print(f"replaying {len(df)} real transactions ...")

    key = os.getenv("BP_API_KEY", "")
    url = "http://localhost:8080/score"
    ok = err = 0
    for _, r in df.iterrows():
        payload = {
            "transaction_id": str(r["transaction_id"] or ""),
            "amount": float(r["amount"]),
            "currency": r["currency"] or "NGN",
            "transaction_type": r["transaction_type"] or "transfer",
            "account_type": (r["account_type"] or "individual"),
            "customer_details": {"identifier": str(r["identifier"]),
                                 "customer_name": r["customer_name"] or "Customer",
                                 "customer_email": r["customer_email"] or "c@example.com"},
            "additional_info": {"ip_address": r["customer_ip_address"] or "0.0.0.0",
                                "location": r["customer_location"] or "-"},
            "timestamp": (r["date_created"].isoformat() if r["date_created"] is not None else None),
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "X-Adhere-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            ok += 1
        except Exception:
            err += 1
    print(f"done: {ok} scored, {err} errors")


if __name__ == "__main__":
    main()
