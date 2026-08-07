#!/usr/bin/env python3
"""
MODEL DECISION SANITY AUDIT
===========================
Prove the behavioural model isn't guessing: for ANY /score payload, show side-by-side what the model
LEARNED for that customer (historical baseline) vs the REAL-TIME payload, the features that fall out
of that comparison, the three detector scores, the blend, the thresholds and the final decision.

It runs the SAME code path /score uses (feature builder + detectors + escalate blend + tiering), so
the verdict matches the API. Nothing about the payload is hardcoded — you pipe it in.

Run inside the API container (it has the active model):
    docker exec -i adhere-behaviour python demo/decision_audit.py < payload.json
    # or:  ... python demo/decision_audit.py --payload /app/demo/my_payload.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # ensure /app on sys.path

import pandas as pd

from ml import config, serve
from ml.pipeline import features as F


def _canon_type(v):
    return config.normalize_transaction_type(v)


def load_payload() -> dict:
    ap = argparse.ArgumentParser(description="Model decision sanity audit")
    ap.add_argument("--payload", help="path to a payload JSON file (default: read stdin)")
    a = ap.parse_args()
    raw = open(a.payload).read() if a.payload else sys.stdin.read()
    return json.loads(raw)


def main() -> int:
    p = load_payload()
    m = serve._active()
    row = serve._row_from_payload(p)
    key = str(row["customer_key"])
    b = m.fb.baselines.get(key)
    cold = b is None

    # --- reproduce /score's history (batch cache + live Redis window), so velocity matches ---
    hist = serve._recent_history(key, row["date_created"])
    live = serve.live_velocity.recent(key, row["date_created"])
    if not live.empty:
        hist = live if hist.empty else pd.concat([hist, live], ignore_index=True)
        hist = hist.drop_duplicates(subset=["transaction_id"], keep="last")
    gf = m.gfeat.get(key)
    graph = (pd.DataFrame([{"customer_key": row["customer_key"], **gf}]) if gf else None)
    if not hist.empty:
        X = m.fb.transform(pd.concat([hist, pd.DataFrame([row])], ignore_index=True), graph).iloc[[-1]]
    else:
        X = m.fb.transform(pd.DataFrame([row]), graph)
    X = X.reset_index(drop=True)
    fc = F.FEATURES

    scores = {"isoforest": m.iso.score(X[fc].to_numpy())}
    if m.ae.available and m.ae.model is not None:
        scores["autoencoder"] = m.ae.score(X[fc].to_numpy())
    if m.gnn.available and m.gnn.customer_score_:
        scores["gnn"] = m.gnn.score(X["customer_key"].to_numpy())
    blended = m.ens.blend(scores)
    risk = float(blended["risk_score"][0])
    feats = {c: float(X.iloc[0][c]) for c in fc}
    det = {k: round(float(v[0]), 3) for k, v in scores.items()}
    dec = m.tiering.decide(risk, feats, det, cold)

    cust = p.get("customer_details", {}) or {}
    dest = (p.get("destination_account", {}) or {}).get("account_number")
    pay_type = _canon_type(p.get("transaction_type"))
    W = 92
    line = "=" * W

    def yn(x):
        return "YES" if x else "no"

    print(line)
    print("  MODEL DECISION SANITY AUDIT")
    print(f"  model: {m.version}   customer: {key}   txn: {p.get('transaction_id')}")
    print(line)

    print("\nSTEP 1 — WHAT THE MODEL LEARNED (historical baseline)")
    if cold:
        print("  COLD-START: no personal profile for this customer — judged vs the POPULATION baseline.")
    else:
        print("  personal profile: YES (this customer HAS a learned behavioural baseline)")
        print("  amount    : median={:,.0f}  mean={:,.0f}  p95={:,.0f}  max={:,.0f}  std={:,.0f}".format(
            b["amt_median"], b["amt_mean"], b.get("amt_p95", 0), b["amt_max"], b["amt_std"]))
        print("  known     : %d beneficiaries | %d locations | %d IP subnets | types=%s" % (
            len(b["benefs"]), len(b["locs"]), len(b["ips"]), sorted(b["types"])[:6]))

    print("\nSTEP 2 — THE REAL-TIME PAYLOAD")
    print("  amount=%s  currency=%s  type=%s(canon)  beneficiary=%s  location=%s  ip=%s" % (
        f"{p.get('amount'):,}", p.get("currency"), pay_type, dest,
        (p.get("additional_info", {}) or {}).get("location"),
        (p.get("additional_info", {}) or {}).get("ip_address")))

    print("\nSTEP 3 — HISTORICAL vs REAL-TIME  (this is the model comparing, not guessing)")
    amt_x = feats.get("amt_over_median", 0)
    amt_desc = f"{amt_x:.2f}x" if amt_x < 2 else f"~{amt_x:.0f}x"
    print("  Amount      : {} the {} (amt_z={:+.2f}, above_historical_max={})".format(
        amt_desc, "population median" if cold else "customer's median", feats.get("amt_z", 0),
        yn(feats.get("above_max", 0) >= 1)))
    if not cold:
        print("  Beneficiary : {}  -> {}".format(
            f"{dest} " + ("IS in" if dest in b["benefs"] else "NOT in") + " the learned set",
            "known" if feats.get("beneficiary_new", 1) < 1 else "NEW (beneficiary_new=1)"))
        print("  Location    : {} learned {}  -> {}".format(
            "in" if not feats.get("location_new", 0) else "NOT in", sorted(b["locs"])[:3],
            "known" if not feats.get("location_new", 0) else "NEW (location_new=1)"))
        print("  IP subnet   : {}  -> {}".format(
            "in learned subnets" if not feats.get("ip_new", 0) else "NOT in learned subnets",
            "known" if not feats.get("ip_new", 0) else "NEW (ip_new=1)"))
        print("  Txn type    : '{}' {} learned {}  -> {}".format(
            pay_type, "in" if not feats.get("type_rare", 0) else "NOT in", sorted(b["types"])[:4],
            "known" if not feats.get("type_rare", 0) else "UNUSUAL (type_rare=1)"))
    print("  Velocity    : vel_1m={:.2f} vel_3m={:.2f} vel_1h={:.2f} vel_24h={:.2f} amt_1h_ratio={:.2f}".format(
        feats.get("vel_1m", 0), feats.get("vel_3m", 0), feats.get("vel_1h", 0),
        feats.get("vel_24h", 0), feats.get("amt_1h_ratio", 0)))
    print("                (from {} recent txns in the window: batch cache + live Redis)".format(
        0 if hist is None or hist.empty else len(hist)))

    print("\nSTEP 4 — DETECTORS, BLEND & DECISION")
    print("  detector scores : %s" % det)
    print("  blend (%s)  -> risk = %.4f   confidence = %.4f" % (
        getattr(config, "BLEND_MODE", "escalate"), risk, float(blended["confidence"][0])))
    print("  thresholds      : review >= %.4f   unsafe >= %.4f" % (
        m.tiering.cuts.get("review", 0), m.tiering.cuts.get("unsafe", 0)))
    print("  DECISION        : %s  (%s, %s)" % (dec["status"].upper(), dec["activity_code"],
                                                dec["zone_label"]))

    print("\nSTEP 5 — WHY (grounded in the comparison above)")
    print("  " + dec["description"])
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
