"""Score a few representative payloads against a SPECIFIC (candidate) model version, using the
current code (escalate blend + new thresholds), WITHOUT promoting it. For evaluating a retrain."""
import sys
import numpy as np
import pandas as pd
from ml import serve
from ml.pipeline import features as F

VERSION = sys.argv[1]
m = serve._Model(VERSION)
print("model:", VERSION, "| thresholds:", m.tiering.cuts)


def score(tag, amount, ident="21200336604", location="Lagos, Nigeria", country=None):
    payload = {
        "transaction_id": "V", "amount": float(amount), "currency": "NGN",
        "transaction_type": "transfer", "account_type": "individual",
        "customer_details": {"customer_name": "T", "customer_email": "e@example.com",
                             "identifier": ident, "identifier_type": "bvn", "country": country},
        "additional_info": {"ip_address": "102.89.1.1", "location": location},
    }
    row = serve._row_from_payload(payload)
    gf = m.gfeat.get(str(row["customer_key"]))
    graph_feats = (pd.DataFrame([{"customer_key": row["customer_key"], **gf}]) if gf else None)
    X = m.fb.transform(pd.DataFrame([row]), graph_feats)
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
    cold = str(X.iloc[0]["customer_key"]) not in m.fb.baselines
    dec = m.tiering.decide(risk, feats, det, cold)
    print("  %-32s -> %-6s %-7s risk=%.4f cold=%-5s det=%s" % (
        tag, dec["status"], dec["activity_code"], risk, cold, det))


print("--- established customer 21200336604 (median ~3M, NOT cold-start) ---")
score("normal 3,000,000",        3000000)
score("small 5,000",             5000)
score("HUGE 100,000,000 +London", 100000000, location="London, United Kingdom", country="United Kingdom")
print("--- a cold-start id (new) ---")
score("cold-start 5,000",        5000, ident="99999999001")
score("cold-start 50,000,000",   50000000, ident="99999999001")
