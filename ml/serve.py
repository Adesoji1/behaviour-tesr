"""
Stage 9 — INFERENCE. Score one live transaction with the ACTIVE model, hardware-aware.

Loads the active ensemble once (feature builder + detectors + tiering) and scores a transaction
payload (accepts BOTH payload shapes — see docs §4). Returns the behavioural response contract
(status, activity_code, description, risk_score, confidence, model_version, …). Behavioural
detection only — NO AML rules. The API layer delivers this to Adhere by webhook.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np
import pandas as pd

from . import codes, config, inference_log, live_velocity, registry
from .codes import Tiering
from .models.autoencoder import AutoencoderDetector
from .models.ensemble import Ensemble
from .models.gnn import GNNDetector
from .models.isoforest import IsoForestDetector
from .pipeline import features

log = logging.getLogger("ml.serve")


class _Model:
    def __init__(self, version: str):
        import joblib
        self.version = version
        d = registry.model_dir(version)
        blob = joblib.load(d / "featurebuilder.joblib")
        self.fb = features.FeatureBuilder(blob["feature_version"])
        self.fb.baselines, self.fb.global_ = blob["baselines"], blob["global"]
        try:
            self.gfeat = joblib.load(d / "graph_features.joblib")   # per-customer g_* for inference
        except Exception:
            self.gfeat = {}
        self.iso = IsoForestDetector().load(d)
        self.ae = AutoencoderDetector()
        try:
            self.ae.load(d, self.fb.n_features)
        except Exception:
            self.ae.available = False
        self.gnn = GNNDetector().load(d)
        import json
        self.tiering = Tiering.from_dict(json.loads((d / "tiering.json").read_text()))
        self.ens = Ensemble(json.loads((d / "ensemble.json").read_text())["weights"])


@lru_cache(maxsize=1)
def _active() -> _Model:
    v = registry.active()
    if not v:
        raise RuntimeError("no active behavioural model — train and promote one first")
    return _Model(v)


# Recent-history lookup so velocity/recency features match TRAINING semantics at inference.
# Without the customer's preceding transactions a single-row payload has vel=0/recency=0, which
# is out-of-distribution and inflates the reconstruction error. One reused read-only connection
# (per worker process) keeps this cheap. READ-ONLY — the model never writes Adhere tables.
_HIST_SQL = """
SELECT transaction_id, identifier AS customer_key, amount, transaction_type, date_created,
       customer_ip_address, customer_location, origin_country, destination_country,
       destination_account_no
FROM bp_transactions_cache
WHERE identifier = %s AND date_created < %s AND date_created >= %s
ORDER BY date_created
"""
_hist_lock = threading.Lock()
_hist_conn = None


def _recent_history(customer_key, before_ts, hours: int = 24) -> pd.DataFrame:
    global _hist_conn
    if customer_key in (None, "", "unknown") or before_ts is None or before_ts != before_ts:
        return pd.DataFrame()
    try:
        start = (before_ts - pd.Timedelta(hours=hours))
        b_naive = before_ts.tz_convert("UTC").tz_localize(None) if before_ts.tzinfo else before_ts
        s_naive = start.tz_convert("UTC").tz_localize(None) if start.tzinfo else start
        import psycopg
        from psycopg.rows import dict_row
        with _hist_lock:
            if _hist_conn is None or _hist_conn.closed:
                _hist_conn = psycopg.connect(config.pg_dsn(), autocommit=True, row_factory=dict_row)
            with _hist_conn.cursor() as cur:
                cur.execute(_HIST_SQL, (str(customer_key), b_naive, s_naive))
                rows = cur.fetchall()
        df = pd.DataFrame(rows)
        if not df.empty:
            df["date_created"] = pd.to_datetime(df["date_created"], utc=True)
        return df
    except Exception as e:                     # scoring must never fail on a history miss
        log.warning("recent-history lookup failed (scoring without velocity): %s", e)
        _hist_conn = None
        return pd.DataFrame()


def _row_from_payload(p: dict) -> dict:
    """Normalise either payload shape into the columns the feature builder expects."""
    cust = p.get("customer_details", {}) or {}
    origin = p.get("origin_account", {}) or {}
    dest = p.get("destination_account", {}) or {}
    add = p.get("additional_info", {}) or {}
    identifier = cust.get("identifier") or cust.get("bvn")
    ts = p.get("timestamp") or datetime.now(timezone.utc).isoformat()
    return {
        "customer_key": str(identifier) if identifier else "unknown",
        "transaction_id": p.get("transaction_id"),
        "amount": p.get("amount"),
        "transaction_type": p.get("transaction_type"),
        "date_created": pd.to_datetime(ts, utc=True, errors="coerce"),
        "customer_ip_address": add.get("ip_address"),
        "customer_location": add.get("location"),
        "origin_country": cust.get("country") or p.get("origin_country"),
        "destination_country": p.get("destination_country"),
        "destination_account_no": dest.get("account_number"),
    }


def score_payload(payload: dict) -> dict:
    t0 = time.perf_counter()
    m = _active()
    row = _row_from_payload(payload)
    # prepend the customer's recent transactions so velocity/recency are computed as in training
    gf = m.gfeat.get(str(row["customer_key"]))
    graph_feats = (pd.DataFrame([{"customer_key": row["customer_key"], **gf}]) if gf else None)
    hist = _recent_history(row["customer_key"], row["date_created"])
    # Enrich with the LIVE window (Redis): transactions since the last sync — real-time bursts across
    # separate /score calls. Fail-safe: empty when Redis is off/unreachable (velocity uses cache only).
    live = live_velocity.recent(row["customer_key"], row["date_created"])
    if not live.empty:
        hist = live if hist.empty else pd.concat([hist, live], ignore_index=True)
        hist = hist.drop_duplicates(subset=["transaction_id"], keep="last")  # a synced txn may be in both
    if not hist.empty:
        df = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
        X = m.fb.transform(df, graph_feats).iloc[[-1]].reset_index(drop=True)  # scored txn is last
    else:
        X = m.fb.transform(pd.DataFrame([row]), graph_feats)
    feat_cols = features.FEATURES
    scores = {"isoforest": m.iso.score(X[feat_cols].to_numpy())}
    if m.ae.available and m.ae.model is not None:
        scores["autoencoder"] = m.ae.score(X[feat_cols].to_numpy())
    if m.gnn.available and m.gnn.customer_score_:
        scores["gnn"] = m.gnn.score(X["customer_key"].to_numpy())
    blended = m.ens.blend(scores)
    risk = float(blended["risk_score"][0])
    conf = float(blended["confidence"][0])
    feats = {c: float(X.iloc[0][c]) for c in feat_cols}
    det_scores = {k: round(float(v[0]), 4) for k, v in scores.items()}
    is_cold = str(X.iloc[0]["customer_key"]) not in m.fb.baselines
    decision = m.tiering.decide(risk, feats, det_scores, is_cold)
    # Populate the explanation dynamically whenever the transaction is NOT safe, so an analyst
    # always sees WHY (behavioural signals + which detector(s) fired). Safe -> nothing triggered.
    if decision["status"] != "safe":
        signals, reasons = codes.explain(feats, det_scores, is_cold)
    else:
        signals, reasons = [], []
    response = {
        "transaction_id": payload.get("transaction_id"),
        "status": decision["status"],
        "activity_code": decision["activity_code"],
        "zone": decision["zone"],                       # fraud-team queue (aligned to the 3 tiers)
        "zone_label": decision["zone_label"],
        "recommended_queue": decision["recommended_queue"],
        "description": decision["description"],
        "risk_score": round(risk, 4),
        "confidence_score": round(conf, 4),
        "detection_reason": reasons,
        "triggered_signals": signals,
        "result": {"risk_score": round(risk, 4), "confidence": round(conf, 4),
                   "detectors": blended["detectors"],
                   "detector_scores": det_scores,
                   "is_cold_start": is_cold},
        "recommended_actions": (["manual_review"] if decision["status"] == "unsafe"
                                else ["monitor"] if decision["status"] == "review" else []),
        "customer_ref": _mask(payload),
        "model_version": m.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inference_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
    inference_log.log_inference(response)   # compliance history: customer, decision, time taken
    # Record THIS transaction into the live window AFTER scoring, so the NEXT /score for this
    # customer sees it (real-time velocity). Fail-safe no-op when Redis is off/unreachable.
    live_velocity.record(row["customer_key"], row["transaction_id"], row["amount"], row["date_created"])
    return response


def _mask(payload: dict) -> str:
    cust = payload.get("customer_details", {}) or {}
    idv = cust.get("identifier") or cust.get("bvn") or ""
    return f"id:***{str(idv)[-4:]}" if idv else "id:unknown"
