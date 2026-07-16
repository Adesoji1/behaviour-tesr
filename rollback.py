#!/usr/bin/env python3
"""
§12 Profile rollback — revert a profile (or a whole build run) to an earlier
version if a retraining run degraded it.

Every build appends a snapshot to bp_profile_history (version + scalars, and the
full JSON when BP_STORE_HISTORY_JSON=1). Rollback restores from those snapshots.

  * With full JSON (production setting) -> restores the complete profile.
  * With scalars only (test default)    -> restores the numeric baseline
    (counts/amounts/version) and flags that features need a rebuild.

Usage:
    python rollback.py --list --entity 231:1100716290       # show versions
    python rollback.py --entity 231:1100716290 --to-version 3
    python rollback.py --entity 231:1100716290 --to-run bp_2026...
    python rollback.py --to-run bp_2026...                   # roll the WHOLE run back
"""
import argparse
import json


import config
import db

# Columns we can restore directly from a full JSON snapshot.
_RESTORE_COLS = [
    "total_tx_count", "total_tx_amount", "avg_amount", "max_amount", "min_amount",
    "std_amount", "median_amount", "p95_amount", "decayed_avg_amount",
    "usual_transaction_types", "usual_merchants", "usual_cities", "usual_countries",
    "known_ip_addresses", "known_ip_subnets", "beneficiaries",
    "peak_transaction_hours", "top_hour", "peak_transaction_days", "top_day_of_week",
    "profile_status", "confidence_score", "tenure_days",
]


def list_versions(entity_key: str):
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT profile_version, build_run_id, snapshot_ts, total_tx_count, "
                "avg_amount, decayed_avg_amount, (profile_json IS NOT NULL) AS has_full "
                "FROM bp_profile_history WHERE entity_key=%s ORDER BY snapshot_ts", (entity_key,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print(f"(no history for {entity_key})")
        return
    print(f"versions for {entity_key}:")
    for r in rows:
        full = "full" if r["has_full"] else "scalars-only"
        print(f"  v{r['profile_version']}  {r['snapshot_ts']}  run={r['build_run_id']}  "
              f"txns={r['total_tx_count']} avg={r['avg_amount']}  [{full}]")


def _restore_row(cur, snap: dict) -> str:
    """Restore one profile from a history snapshot row. Returns a status string."""
    ek = snap["entity_key"]
    if snap.get("profile_json"):
        p = json.loads(snap["profile_json"])
        sets, vals = [], []
        for c in _RESTORE_COLS:
            if c in p:
                sets.append(f"{c}=%s")
                vals.append(p[c])
        sets.append("profile_version=%s")
        vals.append(snap["profile_version"])
        vals.append(ek)
        cur.execute(f"UPDATE bp_user_behaviour_profile SET {','.join(sets)} WHERE entity_key=%s", vals)
        return "full"
    # scalars-only snapshot
    cur.execute("UPDATE bp_user_behaviour_profile SET total_tx_count=%s, total_tx_amount=%s, "
                "avg_amount=%s, decayed_avg_amount=%s, profile_version=%s, "
                "retrain_reason='rolled_back' WHERE entity_key=%s",
                (snap["total_tx_count"], snap["total_tx_amount"], snap["avg_amount"],
                 snap["decayed_avg_amount"], snap["profile_version"], ek))
    return "scalars"


def rollback_entity(entity_key: str, to_version=None, to_run=None):
    conn = db.connect()
    cur = db.dict_cursor(conn)
    if to_version is not None:
        cur.execute("SELECT * FROM bp_profile_history WHERE entity_key=%s AND profile_version=%s "
                    "ORDER BY snapshot_ts DESC LIMIT 1", (entity_key, to_version))
    else:
        cur.execute("SELECT * FROM bp_profile_history WHERE entity_key=%s AND build_run_id=%s "
                    "ORDER BY snapshot_ts DESC LIMIT 1", (entity_key, to_run))
    snap = cur.fetchone()
    if not snap:
        print("no matching snapshot")
        conn.close()
        return
    kind = _restore_row(cur, snap)
    conn.commit()
    conn.close()
    print(f"[rollback] {entity_key} -> v{snap['profile_version']} ({kind} restore)"
          + ("" if kind == "full" else "  (features need a rebuild — only scalars were stored)"))


def rollback_run(to_run: str):
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT * FROM bp_profile_history WHERE build_run_id=%s", (to_run,))
    snaps = cur.fetchall()
    if not snaps:
        print(f"(no snapshots for run {to_run})")
        conn.close()
        return
    full = scal = 0
    for i, snap in enumerate(snaps, 1):
        kind = _restore_row(cur, snap)
        full += kind == "full"
        scal += kind == "scalars"
        if i % 1000 == 0:
            conn.commit()
    conn.commit()
    conn.close()
    print(f"[rollback] restored {len(snaps):,} profiles to run {to_run} "
          f"({full:,} full / {scal:,} scalars-only)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity")
    ap.add_argument("--to-version", type=int)
    ap.add_argument("--to-run")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list and args.entity:
        list_versions(args.entity)
    elif args.entity and (args.to_version is not None or args.to_run):
        rollback_entity(args.entity, args.to_version, args.to_run)
    elif args.to_run:
        rollback_run(args.to_run)
    else:
        ap.error("use --list --entity E, or --entity E --to-version N/--to-run R, or --to-run R")
