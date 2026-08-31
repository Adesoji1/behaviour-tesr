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

from . import codes, config, geo, geo_state, inference_log, live_velocity, registry
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
        # Schema-drift guard: score on exactly the features THIS model was trained on. Older models
        # (and today's) do not persist a "features" list -> assume they match the running FEATURES
        # (unchanged behaviour). If the model was trained on a strict PREFIX of the running features
        # (i.e. new code appended trailing features the model never saw), GRACEFULLY score on the
        # model's set so a rollback stays safe; any other divergence is a real mismatch -> fail fast
        # rather than silently mis-score.
        self.model_features = list(blob.get("features") or [])
        run_features = list(features.FEATURES)
        if not self.model_features or self.model_features == run_features:
            self.feat_cols = run_features
        elif run_features[:len(self.model_features)] == self.model_features:
            log.warning("schema guard: model %s trained on %d features, code has %d — scoring on the "
                        "model's set (ignoring trailing %s)", version, len(self.model_features),
                        len(run_features), run_features[len(self.model_features):])
            self.feat_cols = self.model_features
        else:
            raise RuntimeError(f"feature-schema mismatch for model {version}: model={self.model_features} "
                               f"vs code={run_features} — refusing to score (retrain or rollback needed)")
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
        # OPTIONAL first-party coordinates (best-effort geo enrichment only; never required)
        "latitude": add.get("latitude"),
        "longitude": add.get("longitude"),
    }


def score_payload(payload: dict, include_audit: bool = False) -> dict:
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
    feat_cols = m.feat_cols                       # the features THIS model was trained on (schema guard)
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
    # Decision-audit block (endpoint 3 only): the learned-vs-observed side-by-side, built from the
    # SAME feats/decision computed above so it can never disagree with /score. Off by default, so
    # /score's response is byte-identical.
    if include_audit:
        response["audit"] = _build_audit(payload, row, m, feats, hist, is_cold, det_scores, risk, conf, decision)
    # Geo-velocity SHADOW enrichment (Phase 1): compute + log ONLY. It does NOT touch X, the risk, the
    # decision, or the response — scoring above is unchanged. Best-effort, never raises.
    geo_tel = _geo_shadow(row, is_cold)
    inference_log.log_inference(response, geo=geo_tel)   # compliance history (+ internal geo telemetry)
    # Record THIS transaction into the live window AFTER scoring, so the NEXT /score for this
    # customer sees it (real-time velocity). Fail-safe no-op when Redis is off/unreachable.
    live_velocity.record(row["customer_key"], row["transaction_id"], row["amount"], row["date_created"])
    return response


def _geo_shadow(row: dict, is_cold: bool) -> dict | None:
    """Phase-1 SHADOW geo enrichment: resolve the transaction's coordinates via the first-party
    waterfall, compute geo-velocity (km/h) vs the customer's previous point, and store the current
    point for next time. Returns internal telemetry for the compliance log ONLY — it never affects the
    feature vector, the score, or the decision, and never raises into /score."""
    if not config.GEO_ENABLED:
        return None
    try:
        det = geo.resolve_detail(row.get("latitude"), row.get("longitude"),
                                 row.get("customer_ip_address"), row.get("customer_location"))
        tel = {"geo_source": det["source"], "geo_eligible": (not is_cold),
               "geo_enrichment_success": det["source"] != "unavailable",
               "geo_granularity": det["granularity"], "geo_matched": det["matched"],
               "geo_precision_m": det["precision_m"],
               "plus_code_present": det["plus_code_present"], "plus_code_decoded": det["plus_code_decoded"],
               "loc_present": det["loc_present"], "loc_resolved": det["loc_resolved"],
               "ip_present": det["ip_present"], "ip_public": det["ip_public"],
               "ip_resolved": det["ip_resolved"], "unresolved_reason": det["reason"],
               "geo_velocity_available": 0, "geo_velocity_kmh": None}
        # geo-velocity is meaningful only for ELIGIBLE (established) customers with a resolvable point.
        if is_cold or det["source"] == "unavailable":
            return tel
        ck = str(row["customer_key"])
        ts = row.get("date_created")
        cur_ep = float(ts.timestamp()) if ts is not None and ts == ts else None
        prev = geo_state.previous(ck)
        if prev is not None and cur_ep is not None:
            elapsed_h = (cur_ep - prev["epoch"]) / 3600.0
            kmh = geo.geo_velocity_kmh((prev["lat"], prev["lon"]), (det["lat"], det["lon"]), elapsed_h)
            tel["geo_velocity_available"] = 1
            tel["geo_velocity_kmh"] = round(kmh, 3)
        geo_state.record(ck, det["lat"], det["lon"], ts)   # store current AFTER reading prev
        return tel
    except Exception as e:                          # enrichment must never break scoring
        log.debug("geo shadow failed (non-fatal): %s", e)
        return {"geo_source": "error", "geo_enrichment_success": False,
                "geo_velocity_available": 0, "geo_velocity_kmh": None}


def _mask(payload: dict) -> str:
    cust = payload.get("customer_details", {}) or {}
    idv = cust.get("identifier") or cust.get("bvn") or ""
    return f"id:***{str(idv)[-4:]}" if idv else "id:unknown"


# =============================================================================
# ANALYST-FACING READ/COMPARE HELPERS (endpoints: learnt behaviour, re-learning
# diff, decision audit). These READ the SAME learned baselines the model scores
# with — no scoring change, no side effects (except the decision audit, which is
# a real scoring pass and mirrors /score, Redis included). All never raise.
# =============================================================================
_DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _top_hist(hist, labels=None, top=3):
    """The top-`top` buckets of a normalised histogram (busiest hours 0-23 or days)."""
    if not hist:
        return []
    order = sorted(range(len(hist)), key=lambda i: hist[i], reverse=True)
    return [(labels[i] if labels else i) for i in order[:top] if hist[i] > 0]


def _baseline_view(b: dict) -> dict:
    """Human-readable view of ONE learned baseline (m.fb.baselines[key])."""
    return {
        "amount": {"median": round(float(b.get("amt_median", 0)), 2),
                   "mean": round(float(b.get("amt_mean", 0)), 2),
                   "p95": round(float(b.get("amt_p95", 0)), 2),
                   "max": round(float(b.get("amt_max", 0)), 2),
                   "std": round(float(b.get("amt_std", 0)), 2)},
        "usual_hours_of_day": _top_hist(b.get("hour_hist")),
        "usual_days_of_week": _top_hist(b.get("dow_hist"), _DOW),
        "known_locations": sorted(str(x) for x in b.get("locs", set()))[:20],
        "known_beneficiaries_count": len(b.get("benefs", set())),
        "known_transaction_types": sorted(str(x) for x in b.get("types", set())),
        "known_ip_subnets_count": len(b.get("ips", set())),
    }


def learned_behaviour(identifier: str) -> dict:
    """Endpoint 1 core: the behaviour the ACTIVE model learned for this customer,
    or a cold-start notice when there is no personal profile yet."""
    m = _active()
    key = str(identifier)
    b = m.fb.baselines.get(key)
    if b is None:
        return {"identifier": key, "model_version": m.version, "eligibility": "cold_start",
                "is_cold_start": True, "baseline_type": "population",
                "note": ("No personal behavioural profile yet (cold-start). This customer is judged "
                         "against the POPULATION baseline until they accrue enough clean history."),
                "learned": None}
    return {"identifier": key, "model_version": m.version, "eligibility": "eligible",
            "is_cold_start": False, "baseline_type": "personal",
            "note": "This customer HAS a personal learned behavioural profile.",
            "learned": _baseline_view(b)}


@lru_cache(maxsize=4)
def _baselines_for_version(version: str) -> dict:
    """Load ONLY the baselines dict of a given model version (cheap — no detectors/torch)."""
    import joblib
    blob = joblib.load(registry.model_dir(version) / "featurebuilder.joblib")
    return blob.get("baselines", {})


def _diff_baselines(prev: dict, cur: dict) -> dict:
    """What changed between two learned baselines: amounts, usual time, and the sets that were
    ADDED or dropped (invalidated/decayed out)."""
    def amt(b):
        return {"median": round(float(b.get("amt_median", 0)), 2), "mean": round(float(b.get("amt_mean", 0)), 2),
                "p95": round(float(b.get("amt_p95", 0)), 2), "max": round(float(b.get("amt_max", 0)), 2)}
    p_loc, c_loc = {str(x) for x in prev.get("locs", set())}, {str(x) for x in cur.get("locs", set())}
    p_ben, c_ben = {str(x) for x in prev.get("benefs", set())}, {str(x) for x in cur.get("benefs", set())}
    p_typ, c_typ = {str(x) for x in prev.get("types", set())}, {str(x) for x in cur.get("types", set())}
    return {
        "amount": {"previous": amt(prev), "current": amt(cur),
                   "median_shift": round(float(cur.get("amt_median", 0)) - float(prev.get("amt_median", 0)), 2)},
        "usual_hours_of_day": {"previous": _top_hist(prev.get("hour_hist")),
                               "current": _top_hist(cur.get("hour_hist"))},
        "usual_days_of_week": {"previous": _top_hist(prev.get("dow_hist"), _DOW),
                               "current": _top_hist(cur.get("dow_hist"), _DOW)},
        "locations": {"added": sorted(c_loc - p_loc)[:20],
                      "removed_invalidated": sorted(p_loc - c_loc)[:20],
                      "still_known": sorted(c_loc & p_loc)[:20]},
        "beneficiaries": {"added": len(c_ben - p_ben), "removed_invalidated": len(p_ben - c_ben)},
        "transaction_types": {"added": sorted(c_typ - p_typ), "removed_invalidated": sorted(p_typ - c_typ)},
        "decay": {"half_life_days": config.DECAY_HALF_LIFE_DAYS,
                  "note": ("Learning is recency-weighted (half-life %g days): stale locations/beneficiaries "
                           "decay out of the learned set and the amount stats track recent behaviour — so a "
                           "'removed_invalidated' entry is old behaviour the new model no longer treats as normal."
                           % config.DECAY_HALF_LIFE_DAYS)},
    }


def _model_built_at(version: str | None) -> str | None:
    """The build timestamp encoded in a model version (bf-ensemble-YYYY.MM.DD-HHMMSS)."""
    if not version:
        return None
    import re
    mm = re.search(r"(\d{4})\.(\d{2})\.(\d{2})-(\d{2})(\d{2})(\d{2})", version)
    if not mm:
        return None
    y, mo, d, h, mi, s = mm.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}"


def behaviour_change(identifier: str) -> dict:
    """Endpoint 2 core: how the model's learned behaviour for this customer changed between the
    PREVIOUS and the CURRENT active model (i.e. after re-learning). Works from the registry, so it
    is available wherever the model is (no store required)."""
    m = _active()
    key = str(identifier)
    prev_v = registry._load().get("previous_active")
    cur_b = m.fb.baselines.get(key)
    out = {"identifier": key,
           "current_model": m.version, "current_model_built": _model_built_at(m.version),
           "previous_model": prev_v, "previous_model_built": _model_built_at(prev_v)}
    if not prev_v:
        out.update({"changed": None,
                    "note": "Only one model version exists — no previous model to compare against yet."})
        return out
    prev_b = _baselines_for_version(prev_v).get(key)
    if prev_b is None and cur_b is None:
        out.update({"changed": None,
                    "note": "Cold-start in BOTH models — no personal profile has been learned yet."})
    elif prev_b is None and cur_b is not None:
        out.update({"transition": "newly_learned", "current": _baseline_view(cur_b),
                    "note": "Cold-start in the previous model; the current model has now learned a personal profile."})
    elif prev_b is not None and cur_b is None:
        out.update({"transition": "invalidated_to_coldstart", "previous": _baseline_view(prev_b),
                    "note": "Had a personal profile before; the current model reverted this customer to cold-start "
                            "(prior behaviour aged/decayed out and did not meet eligibility)."})
    else:
        out.update({"transition": "relearned", "changed": _diff_baselines(prev_b, cur_b),
                    "note": "Behaviour was re-learned between the two models — see the per-dimension changes."})
    return out


def _build_audit(payload, row, m, feats, hist, is_cold, det_scores, risk, conf, decision) -> dict:
    """Endpoint 3 core: the learned-vs-observed side-by-side (JSON form of demo/decision_audit.py),
    proving the model COMPARED rather than guessed. Built from the SAME computation /score used, and
    laid out to mirror that tool's STEP 1..5 sanity audit."""
    key = str(row["customer_key"])
    b = m.fb.baselines.get(key)
    add = payload.get("additional_info", {}) or {}
    dest = (payload.get("destination_account", {}) or {}).get("account_number")
    ptype = config.normalize_transaction_type(payload.get("transaction_type"))

    # STEP 1 — what the model learned (historical baseline): amount stats + the known sets.
    if is_cold or b is None:
        learned = {"has_personal_profile": False, "baseline_type": "population",
                   "note": "Cold-start — judged against the population baseline (no personal history yet)."}
    else:
        learned = {
            "has_personal_profile": True, "baseline_type": "personal",
            "amount": {"median": round(float(b.get("amt_median", 0)), 2),
                       "mean": round(float(b.get("amt_mean", 0)), 2),
                       "p95": round(float(b.get("amt_p95", 0)), 2),
                       "max": round(float(b.get("amt_max", 0)), 2),
                       "std": round(float(b.get("amt_std", 0)), 2)},
            "known": {"beneficiaries": len(b.get("benefs", set())), "locations": len(b.get("locs", set())),
                      "ip_subnets": len(b.get("ips", set())),
                      "transaction_types": sorted(str(x) for x in b.get("types", set()))},
            "usual_hours_of_day": _top_hist(b.get("hour_hist")),
            "usual_days_of_week": _top_hist(b.get("dow_hist"), _DOW),
            "known_locations_sample": sorted(str(x) for x in b.get("locs", set()))[:20],
        }

    # STEP 3 — historical vs real-time (the model comparing, not guessing).
    comparison = {
        "amount": {"x_baseline_median": round(feats.get("amt_over_median", 0), 2),
                   "amt_z": round(feats.get("amt_z", 0), 3),
                   "above_historical_max": bool(feats.get("above_max", 0) >= 1)},
        "velocity": {"vel_1m": round(feats.get("vel_1m", 0), 3), "vel_3m": round(feats.get("vel_3m", 0), 3),
                     "vel_1h": round(feats.get("vel_1h", 0), 3), "vel_24h": round(feats.get("vel_24h", 0), 3),
                     "amt_1h_ratio": round(feats.get("amt_1h_ratio", 0), 3),
                     "recent_txns_in_window": 0 if hist is None or hist.empty else int(len(hist))},
    }
    if not is_cold and b is not None:
        comparison["beneficiary"] = {"in_learned_set": bool(feats.get("beneficiary_new", 1) < 1),
                                     "verdict": "new" if feats.get("beneficiary_new", 0) >= 1 else "known"}
        comparison["location"] = {"in_learned_set": not bool(feats.get("location_new", 0)),
                                  "verdict": "new" if feats.get("location_new", 0) else "known",
                                  "learned_sample": sorted(str(x) for x in b.get("locs", set()))[:5]}
        comparison["ip_subnet"] = {"in_learned_subnets": not bool(feats.get("ip_new", 0)),
                                   "verdict": "new" if feats.get("ip_new", 0) else "known"}
        comparison["transaction_type"] = {"in_learned_types": not bool(feats.get("type_rare", 0)),
                                          "verdict": "unusual" if feats.get("type_rare", 0) else "known"}

    return {
        # STEP 1
        "what_the_model_learned": learned,
        # STEP 2 — the real-time transaction
        "the_real_time_transaction": {"amount": payload.get("amount"), "currency": payload.get("currency"),
                                      "transaction_type": ptype, "beneficiary": dest,
                                      "location": add.get("location"), "ip_address": add.get("ip_address"),
                                      "timestamp": payload.get("timestamp")},
        # STEP 3
        "historical_vs_real_time": comparison,
        # STEP 4 — detectors, blend, thresholds, decision
        "detectors_blend_decision": {"detector_scores": det_scores,
                                     "blend_mode": getattr(config, "BLEND_MODE", "escalate"),
                                     "risk_score": round(risk, 4), "confidence": round(conf, 4),
                                     "thresholds": {"review": round(m.tiering.cuts.get("review", 0), 4),
                                                    "unsafe": round(m.tiering.cuts.get("unsafe", 0), 4)},
                                     "decision": {"status": decision["status"],
                                                  "activity_code": decision["activity_code"],
                                                  "zone_label": decision["zone_label"]}},
        # STEP 5
        "why": decision["description"],
        "live_velocity": "enabled : mirrors /score",
        "note": "Side-by-side proof the model compared learned vs observed — it is not guessing.",
    }
