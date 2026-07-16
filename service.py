#!/usr/bin/env python3
"""
Adhere Behaviour-Profile microservice (FastAPI).

This is the plug-in point for the adhere application. For every transaction,
adhere calls POST /score. The service:
  1. scores the transaction against the customer's stored behaviour profile
     (rules fire if it breaks their pattern; live velocity catches bursts;
      new / Warming-Up customers are judged against their peer group),
  2. updates the customer's event-driven counters,
  3. retrains THAT customer's profile in place if a trigger is met
     (>=N new txns  OR  >=D days  OR  sustained drift) — no cron.

Endpoints:
  GET  /health                      liveness
  POST /score                       score one transaction (+ maybe retrain)
  GET  /profile/{entity_key}        inspect a stored profile
  POST /retrain/{entity_key}        force a retrain now
  POST /reload                      refresh rule/blacklist/peer caches

Run:  uvicorn service:app --host 0.0.0.0 --port 8080
"""
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

import audit
import config
import db
import retrain
from live_velocity import ProdVelocitySource
from rule_engine import RuleEngine, profile_is_trusted

app = FastAPI(title="Adhere Behaviour-Profile Service", version="1.0")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log EVERY request to stdout so each endpoint hit shows up in
    `docker compose logs` — method, path, status and duration — not just the
    container health-check. Guarantees visibility regardless of uvicorn's
    access-log settings."""
    start = time.perf_counter()
    response = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    audit.log.info("http %s %s -> %s (%.0f ms)",
                   request.method, request.url.path, response.status_code, dur_ms)
    return response

# Rules fired against the customer's OWN profile that signal their behaviour may
# be genuinely shifting (used to build the "sustained drift" retrain trigger).
ANOMALY_RULES = {"detect_unusual_city", "detect_unusual_country",
                 "outbound_exceeds_historical_max", "block_significantly_high_amount",
                 "unusual_time_for_user"}

# Rules that fire because of the AMOUNT specifically. Used by /demo so it can tell you
# honestly whether YOUR amount was the trigger, or whether the transaction was flagged
# on other grounds (city / hour / country / beneficiary) while the amount was fine.
AMOUNT_RULES = {"escalate_single_transfer_above_10m_ngn", "block_above_hard_cap",
                "block_significantly_high_amount", "outbound_exceeds_historical_max",
                "high_outbound_amount_15m"}

# ISO 3166 alpha-2 -> full name, for the few codes the demo shows. Purely cosmetic:
# so "KP" reads as "North Korea" in the response, not as a mystery code.
_COUNTRY_NAMES = {"NG": "Nigeria", "KP": "North Korea", "GB": "United Kingdom",
                  "US": "United States", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa",
                  "CN": "China", "RU": "Russia", "AE": "United Arab Emirates"}


def _country(code: str | None) -> str:
    """'KP' -> 'North Korea (KP)'. Unknown codes are shown as-is."""
    if not code:
        return "?"
    name = _COUNTRY_NAMES.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()


class SafeVelocity:
    """Wrap the live velocity lookup so a production hiccup never breaks scoring.
    Also honours the BP_ALLOW_PROD_PULL safety switch: when live pulls are disabled,
    velocity falls back to empty features and never touches production."""
    def __init__(self):
        self._src = ProdVelocitySource() if (config.LIVE_VELOCITY and config.ALLOW_PROD_PULL) else None

    def features(self, *a, **k):
        if self._src is None:
            from live_velocity import _empty
            return _empty()
        try:
            return self._src.features(*a, **k)
        except Exception:
            from live_velocity import _empty
            return _empty()


VELO = SafeVelocity()


class Txn(BaseModel):
    branch_id: int
    origin_account_no: str
    amount: float
    currency: str = "NGN"
    destination_account_no: str | None = None
    customer_location: str | None = None
    origin_country: str | None = None
    destination_country: str | None = None
    identifier: str | None = None
    account_type: str | None = None
    transaction_id: str | None = None
    ts: datetime | None = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "behaviour-profile", "time": datetime.utcnow().isoformat()}


@app.post("/score")
def score(t: Txn):
    """The adhere hook. End-to-end, every step logged for accountability:
    1) look up the customer's profile (or peer group if new/Warming-Up),
    2) score the transaction against it (rules fire if it breaks their pattern),
    3) update the customer's event counters,
    4) retrain the customer iff a trigger is met — and log why / why not / if it failed."""
    txn = t.model_dump()
    txn["ts"] = txn.get("ts") or datetime.utcnow()
    ek = f"{t.branch_id}:{t.origin_account_no}"

    conn = db.connect()
    try:
        # 1) which profile is being used?
        pc = db.dict_cursor(conn)
        pc.execute("SELECT profile_status, confidence_score, tenure_days, "
                   "lifetime_clean_txns, suspicious_tx_count "
                   "FROM bp_user_behaviour_profile WHERE entity_key=%s", (ek,))
        prof = pc.fetchone()
        # same gate the engine applies — one helper, so the two can never diverge
        trusted, trust_reason = profile_is_trusted(prof)
        judged_against = "own_profile" if trusted else ("peer_group" if prof else "peer_group(new)")

        # 2) score
        eng = RuleEngine(conn, velocity=VELO)
        fired = eng.evaluate(txn)
        decision = "review" if fired else "allow"
        anomaly = any(f["rule_code"] in ANOMALY_RULES and "peer_baseline" not in str(f["details"])
                      for f in fired)

        # 3) update the customer's counters (only if they have a profile row)
        if prof:
            cur = conn.cursor()
            cur.execute(
                "UPDATE bp_user_behaviour_profile SET txns_since_build = txns_since_build + 1, "
                "drift_signal_count = CASE WHEN %s THEN drift_signal_count + 1 ELSE 0 END "
                "WHERE entity_key=%s",
                (bool(anomaly), ek),   # Postgres CASE/WHEN needs a real boolean, not 1/0
            )
            conn.commit()

        audit.log_event(ek, "score", decision,
                        {"judged_against": judged_against, "trust_reason": trust_reason,
                         "own_profile_anomaly": anomaly,
                         "fired": [f["rule_code"] for f in fired]},
                        transaction_id=t.transaction_id, conn=conn)
        conn.commit()
    finally:
        conn.close()

    # 4) event-driven retrain (structured: retrained, or skipped-with-reason, or failed)
    retrained = retrain.maybe_retrain(ek)

    return {
        "entity_key": ek,
        "decision": decision,
        "judged_against": judged_against,
        "trust_reason": trust_reason,   # WHY it was (or was not) judged on its own profile
        "fired_rules": fired,
        "own_profile_anomaly": anomaly,
        "retrain": retrained,      # {retrained:true,...} | {retrained:false,reason:"not due — needs..."}
    }


@app.get("/customer/{entity_key}")
def customer_status(entity_key: str):
    """Everything about a customer at this moment: identity, eligibility (met/not),
    what was learned, the retrain state (why it will/won't retrain next), and their
    recent event trail. This is the single 'status of that customer' view."""
    import json
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT * FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
    p = cur.fetchone()

    cur.execute("SELECT event_type, outcome, detail, created_at FROM bp_event_log "
                "WHERE entity_key=%s ORDER BY created_at DESC LIMIT 10", (entity_key,))
    events = [{"type": e["event_type"], "outcome": e["outcome"],
               "detail": json.loads(e["detail"]) if e["detail"] else None,
               "at": e["created_at"].isoformat() if e["created_at"] else None}
              for e in cur.fetchall()]
    conn.close()

    if not p:
        return {"entity_key": entity_key, "has_profile": False,
                "status": "new / Warming-Up — no trusted profile yet; judged against peer group",
                "recent_events": events}

    eligible = {
        "tenure_days": {"value": p["tenure_days"], "required": config.ELIGIBLE_MIN_TENURE_DAYS,
                        "met": (p["tenure_days"] or 0) >= config.ELIGIBLE_MIN_TENURE_DAYS},
        "clean_lifetime_txns": {"value": p["lifetime_clean_txns"], "required": config.ELIGIBLE_MIN_TXNS,
                                "met": (p["lifetime_clean_txns"] or 0) >= config.ELIGIBLE_MIN_TXNS},
        # "Practical rules" §1: "No confirmed fraud cases"
        "no_confirmed_fraud": {"value": p["suspicious_tx_count"],
                               "max_allowed": config.ELIGIBLE_MAX_FRAUD_TXNS,
                               "met": (p["suspicious_tx_count"] or 0) <= config.ELIGIBLE_MAX_FRAUD_TXNS},
    }
    return {
        "entity_key": entity_key, "has_profile": True,
        "customer_name": p["customer_name"], "account_type": p["account_type"],
        "profile_status": p["profile_status"], "confidence_score": p["confidence_score"],
        # the SAME gate the engine applies at decision time (not the stored flag alone)
        "trusted_by_engine": profile_is_trusted(p)[0],
        "trust_reason": profile_is_trusted(p)[1],
        "eligibility": eligible,
        "learned": {
            # learned_from_txn_count is a COUNT of transactions (not money, not days)
            "learned_from_txn_count": p["total_tx_count"], "usual_avg_amount_ngn": float(p["avg_amount"] or 0),
            "biggest_ever_ngn": float(p["max_amount"] or 0), "recency_weighted_avg_ngn": float(p["decayed_avg_amount"] or 0),
            "usual_cities": list(json.loads(p["usual_cities"] or "{}"))[:6],
            "busiest_day": p["top_day_of_week"], "distinct_beneficiaries": p["distinct_beneficiaries"],
            "drift_status": p["drift_status"], "drift_reason": p["drift_reason"],
        },
        "retrain_state": retrain.retrain_decision(entity_key),
        "profile_version": p["profile_version"],
        "last_retrained_at": p["last_retrained_at"].isoformat() if p["last_retrained_at"] else None,
        "recent_events": events,
    }


@app.get("/profile/{entity_key}")
def get_profile(entity_key: str):
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT entity_key, customer_name, profile_status, confidence_score, "
                "drift_status, tenure_days, total_tx_count, txns_since_build, drift_signal_count, "
                "avg_amount, decayed_avg_amount, max_amount, p95_amount, usual_cities, "
                "top_day_of_week, profile_version, last_retrained_at "
                "FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"no profile for {entity_key} (new/Warming-Up — judged by peers)")
    return row


@app.post("/retrain/{entity_key}")
def force_retrain(entity_key: str):
    return retrain.retrain_customer(entity_key)


@app.get("/sync/status")
def sync_status():
    """What the local cache holds and where the ingestion watermark sits.
    Reads only the store — never production."""
    import sync_manager
    return sync_manager.status()


@app.post("/sync")
def run_sync(max_rows: int | None = None, chunk_size: int | None = None,
             full: bool = False):
    """Pull fresh transactions from production INTO THE LOCAL CACHE — safely.

    This is the only endpoint that touches production, and it does so in bounded
    chunks (keyset pagination), capped, throttled and statement-timed-out. Every
    chunk is logged. Everything else in the service reads the cache.

    Query params override the env defaults for one run, e.g.
    `POST /sync?max_rows=2000` for a small, obviously-light demo pull.
    """
    import sync_manager
    return sync_manager.sync(max_rows=max_rows, chunk_size=chunk_size, full=full)


@app.post("/reload")
def reload_caches():
    # RuleEngine caches are per-request in this build, so nothing global to reload;
    # kept for API compatibility with a pooled/cached production deployment.
    return {"status": "ok", "note": "rule caches are loaded per request in this build"}


@app.get("/")
def root():
    return {
        "service": "Adhere Behaviour-Profile",
        "try": {"end_to_end_demo": "GET /demo", "sample_customers": "GET /examples",
                "system_stats": "GET /stats", "score_a_txn": "POST /score", "api_docs": "GET /docs"},
    }


@app.get("/examples")
def examples():
    """Real customers you can copy-paste to test /score, /customer and /retrain —
    so you don't have to know any account number in advance."""
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cols = ("entity_key, branch_id, origin_account_no, customer_name, profile_status, "
            "confidence_score, tenure_days, lifetime_clean_txns, suspicious_tx_count")
    # only customers the engine actually trusts right now (the live §1/§2/§10 gate)
    cur.execute(f"SELECT {cols} FROM bp_user_behaviour_profile "
                " WHERE profile_status='active' AND usual_cities <> '{}' "
                "   AND coalesce(confidence_score,0) >= %s AND coalesce(tenure_days,0) >= %s "
                "   AND coalesce(lifetime_clean_txns,0) >= %s AND coalesce(suspicious_tx_count,0) <= %s "
                " ORDER BY total_tx_count DESC LIMIT 3",
                (config.CONFIDENCE_TRUST_THRESHOLD, config.ELIGIBLE_MIN_TENURE_DAYS,
                 config.ELIGIBLE_MIN_TXNS, config.ELIGIBLE_MAX_FRAUD_TXNS))
    active = cur.fetchall()
    cur.execute(f"SELECT {cols} FROM bp_user_behaviour_profile WHERE profile_status='warming_up' "
                "ORDER BY total_tx_count DESC LIMIT 2")
    warming = cur.fetchall()
    conn.close()
    return {
        "note": "Pick any entity_key below. For POST /score use its branch_id + origin_account_no.",
        "active_trusted": active,        # judged against their own profile
        "warming_up": warming,           # judged against peers (thin history)
    }


@app.get("/customers")
def customers(limit: int = 10, trusted: bool | None = None, q: str | None = None):
    """Browse real customers and their entity keys — then run the demo for any of them.

    Every row includes a ready-made `demo` URL you can paste straight into the browser.

      GET /customers                 -> a mixed sample (trusted + not)
      GET /customers?trusted=true    -> only customers the engine trusts right now
      GET /customers?trusted=false   -> only Warming-Up / untrusted (judged by peers)
      GET /customers?q=OLABUNMI      -> search by name or account number
      GET /customers?limit=25

    `trusted` reflects the LIVE §1/§2/§10 gate, not just the stored flag.
    """
    conn = db.connect()
    cur = db.dict_cursor(conn)
    limit = max(1, min(int(limit), 100))
    cols = ("entity_key, branch_id, origin_account_no, customer_name, profile_status, "
            "confidence_score, tenure_days, lifetime_clean_txns, suspicious_tx_count, "
            "total_tx_count, avg_amount, max_amount")
    where, params = ["usual_cities <> '{}'"], []
    if q:
        where.append("(customer_name ILIKE %s OR origin_account_no ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    gate = ("coalesce(confidence_score,0) >= %s AND coalesce(tenure_days,0) >= %s "
            "AND coalesce(lifetime_clean_txns,0) >= %s AND coalesce(suspicious_tx_count,0) <= %s "
            "AND profile_status='active'")
    gate_params = [config.CONFIDENCE_TRUST_THRESHOLD, config.ELIGIBLE_MIN_TENURE_DAYS,
                   config.ELIGIBLE_MIN_TXNS, config.ELIGIBLE_MAX_FRAUD_TXNS]
    if trusted is True:
        where.append(gate); params += gate_params
    elif trusted is False:
        where.append(f"NOT ({gate})"); params += gate_params
    cur.execute(f"SELECT {cols} FROM bp_user_behaviour_profile WHERE {' AND '.join(where)} "
                f"ORDER BY total_tx_count DESC LIMIT %s", (*params, limit))
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        ok, why = profile_is_trusted(r)
        out.append({
            "entity_key": r["entity_key"], "name": r["customer_name"],
            "trusted_by_engine": ok, "trust_reason": why,
            "profile_status": r["profile_status"], "confidence": r["confidence_score"],
            "tenure_days": r["tenure_days"], "clean_lifetime_txns": r["lifetime_clean_txns"],
            "confirmed_fraud_txns": r["suspicious_tx_count"],
            "usual_spend_avg_ngn": float(r["avg_amount"] or 0),
            "biggest_ever_ngn": float(r["max_amount"] or 0),
            "demo": f"/demo?entity_key={r['entity_key']}",
            "detail": f"/customer/{r['entity_key']}",
        })
    return {
        "note": ("Pick any entity_key and open its `demo` URL to run the FULL end-to-end "
                 "demo for that customer. `trusted_by_engine` is the live §1 gate: false "
                 "means the engine judges them against their peer group instead."),
        "gate": (f"trusted needs: confidence >= {config.CONFIDENCE_TRUST_THRESHOLD}, tenure >= "
                 f"{config.ELIGIBLE_MIN_TENURE_DAYS}d, clean txns >= {config.ELIGIBLE_MIN_TXNS}, "
                 f"confirmed fraud <= {config.ELIGIBLE_MAX_FRAUD_TXNS}"),
        "filters": {"trusted": "?trusted=true|false", "search": "?q=<name or account no>",
                    "limit": "?limit=1..100"},
        "count": len(out), "customers": out,
    }


@app.get("/stats")
def stats():
    conn = db.connect()
    cur = db.dict_cursor(conn)
    out = {}
    cur.execute("SELECT COUNT(*) n FROM bp_user_behaviour_profile")
    out["profiles"] = cur.fetchone()["n"]
    cur.execute("SELECT profile_status, COUNT(*) n FROM bp_user_behaviour_profile GROUP BY profile_status")
    out["by_status"] = {r["profile_status"]: r["n"] for r in cur.fetchall()}
    cur.execute("SELECT drift_status, COUNT(*) n FROM bp_user_behaviour_profile GROUP BY drift_status")
    out["by_drift"] = {r["drift_status"]: r["n"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) n FROM bp_rule_definition")
    out["rules"] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM bp_peer_baseline")
    out["peer_baselines"] = cur.fetchone()["n"]
    conn.close()
    return out


class _BurstVelocity:
    """A fake recent-window feed (5 txns in the last minute) so /demo can show the
    live velocity rules firing without needing a real burst in production."""
    def features(self, *a, **k):
        return {"n_1m": 5, "amt_1m": 10_000_000.0, "n_10m": 5, "n_15m": 5,
                "amt_15m": 10_000_000.0, "n_1h": 5, "recip_1h": 5, "countries_1h": 1,
                "n_24h": 5, "benef_24h": 5}


# Placeholder values that production's `customer_location` sometimes carries. A city
# learned from these is not a real city, so it must not be presented as one.
_JUNK_CITY = {"-", "", "n/a", "na", "null", "none", "unknown", ".", "nil"}


def _real_cities(cities: dict) -> list[str]:
    """The learned cities that are actually place names (not source placeholders)."""
    return [c for c in cities if str(c).strip().lower() not in _JUNK_CITY]


def _why_ordinary(name, r6, amt, vs_median, median, biggest, city_v, their_cities,
                  hour_v, hour_label, hour_hist, country_v, benef_v, their_benefs,
                  amt_supplied) -> str:
    """Explain the ordinary-day stage HONESTLY.

    The trap this fixes: the stage used to assume that if it reviewed and an amount had
    been supplied, the AMOUNT must be why. That is false the moment the caller can also
    supply a city/hour/country/beneficiary. Sending a customer's exact median amount
    from a city they never use fires ONLY detect_unusual_city — blaming the amount there
    ("10,000 NGN is 1.0x their median, so this fired on the AMOUNT") is nonsense and
    would destroy trust in the demo. So: look at which rules ACTUALLY fired and name the
    real culprit.
    """
    fired = [f["rule"] for f in r6.get("fired_rules", [])]
    if not fired:
        return (f"the amount is fine for {name} and the context is their own: "
                f"{amt:,.0f} NGN is "
                + (f"{vs_median:.2f}x their median ({median:,.0f}) — inside their normal range"
                   if amt_supplied and vs_median else "their typical spend (their own median)")
                + f", '{city_v}' is a location they use, the beneficiary is one they already "
                  f"pay, and {hour_label} is an hour they transact in. Nothing to flag.")

    # Which supplied field is each fired rule actually about?
    culprits, ok = [], []
    if set(fired) & AMOUNT_RULES:
        culprits.append(f"the AMOUNT ({amt:,.0f} NGN is "
                        + (f"{vs_median:.1f}x their median {median:,.0f}" if vs_median else "unusual")
                        + (f" and ABOVE their biggest-ever {biggest:,.0f}" if biggest and amt > biggest else "")
                        + ")")
    elif median:
        ok.append(f"the amount ({amt:,.0f} = {vs_median:.2f}x their median)")
    if "detect_unusual_city" in fired:
        culprits.append(f"the CITY ('{city_v}' is not in their usual cities "
                        f"{list(their_cities)[:4]})")
    elif city_v in their_cities:
        ok.append(f"the city ('{city_v}' is one they use)")
    if "unusual_time_for_user" in fired:
        culprits.append(f"the HOUR ({hour_label} is not an hour they transact in)")
    elif hour_v in hour_hist:
        ok.append(f"the hour ({hour_label} is one they use)")
    if {"flag_cross_border_transfer", "detect_unusual_country"} & set(fired):
        culprits.append(f"the COUNTRY (destination {country_v})")
    elif country_v == "NG":
        ok.append("the country (domestic)")
    if "first_time_beneficiary" in fired:
        culprits.append(f"the BENEFICIARY ({benef_v} is one they have never paid)")
    elif benef_v in their_benefs:
        ok.append("the beneficiary (one they already pay)")

    other = sorted(set(fired) - AMOUNT_RULES - {"detect_unusual_city", "unusual_time_for_user",
                                                "flag_cross_border_transfer",
                                                "detect_unusual_country",
                                                "first_time_beneficiary"})
    txt = f"flagged on {' and '.join(culprits) if culprits else 'rules: ' + ', '.join(fired)}"
    if ok:
        txt += f". What was FINE: {', '.join(ok)}"
    if other:
        txt += f". Also fired: {', '.join(other)}"
    return (txt + f". Each value was judged against what {name} has actually done — "
                  f"see transaction_sent")


def _why_abnormal(name, r7, amt, vs_max, biggest, hard_cap, city, hour_label, same_amount,
                  country="KP") -> str:
    """Explain the suspicious-context stage HONESTLY.

    The trap: this stage reviews for TWO independent reasons — the amount, and the
    context (city / country / hour / new beneficiary). If only the context is wrong,
    saying "100 NGN is 0.0x their biggest-ever" next to "breaks their pattern" implies
    the amount contributed when no amount rule fired at all. So we check which rules
    ACTUALLY fired and describe only what really happened.

    `same_amount` = the caller entered one amount, so stage 6 and 7 used the identical
    figure and ONLY the context differs — which is the whole lesson of the pair.
    """
    fired = [f["rule"] for f in r7.get("fired_rules", [])]
    amount_fired = sorted(set(fired) & AMOUNT_RULES)
    other_fired = sorted(set(fired) - AMOUNT_RULES)
    others = (f"{city} is a city they have never used, "
              + (f"it is cross-border (Nigeria -> {_country(country)}), " if country != "NG"
                 else "it is domestic, ")
              + f"the beneficiary is brand-new, and {hour_label} is an hour they have never "
                f"transacted in")
    lead = ("the amount is IDENTICAL to stage 6 — only the context changed. "
            if same_amount else "")

    if not fired:
        return (f"nothing fired: {amt:,.0f} NGN is not abnormal for {name} (biggest-ever "
                f"{biggest:,.0f} NGN"
                + (f", AML hard cap {hard_cap:,.0f}" if hard_cap else "")
                + "), and nothing in the context broke their pattern either. The engine "
                  "will not flag a transaction just because a stage is labelled suspicious")
    if amount_fired and other_fired:
        return (lead + f"flagged on BOTH counts. The AMOUNT: {amt:,.0f} NGN is "
                + (f"{vs_max:.1f}x their biggest-ever ({biggest:,.0f} NGN)" if vs_max
                   else "far above their usual")
                + (f" and over the AML hard cap ({hard_cap:,.0f})"
                   if hard_cap and amt > hard_cap else "")
                + f" -> {', '.join(amount_fired)}. The CONTEXT: {others} -> "
                  f"{', '.join(other_fired)}. Each rule names exactly which line was crossed")
    if amount_fired:
        return (lead + f"flagged on the AMOUNT alone: {amt:,.0f} NGN is "
                + (f"{vs_max:.1f}x their biggest-ever ({biggest:,.0f} NGN)" if vs_max
                   else "far above their usual")
                + (f" and over the AML hard cap ({hard_cap:,.0f})" if hard_cap and amt > hard_cap
                   else "")
                + f" -> {', '.join(amount_fired)}")
    # Reviewed, but NOT because of the amount — say so plainly.
    return (lead + f"flagged on the CONTEXT, NOT the amount. {amt:,.0f} NGN is "
            + (f"only {vs_max:.2f}x their biggest-ever ({biggest:,.0f} NGN)" if vs_max
               else "within their usual range")
            + ", so no amount rule fired — that same figure passed in stage 6. What changed "
              f"is where and how it was sent: {others}. Fired: {', '.join(other_fired)}. "
              f"This is the lesson of the pair: an ordinary amount is still flagged when the "
              f"surroundings break the customer's pattern"
            + (f". Raise the amount above their biggest-ever ({biggest:,.0f})" if biggest else "")
            + (f" or the hard cap ({hard_cap:,.0f})" if hard_cap else "")
            + " to see the amount rules bite too")


def _hard_cap_for(conn, branch_id) -> float | None:
    """Read the AML `block_above_hard_cap` threshold (global rule + any per-branch
    override) from the rules tables — so the demo derives its 'abnormal' amount from
    the real configured hard cap instead of a hard-coded literal."""
    import json
    cur = conn.cursor()
    cur.execute("SELECT params FROM bp_rule_definition WHERE rule_code='block_above_hard_cap' AND enabled=1")
    row = cur.fetchone()
    cap = json.loads(row[0] or "{}").get("hard_cap") if row else None
    cur.execute("SELECT params FROM bp_rule_settings WHERE rule_code='block_above_hard_cap' AND branch_id=%s",
                (int(branch_id),))
    r2 = cur.fetchone()
    if r2:
        cap = json.loads(r2[0] or "{}").get("hard_cap", cap)
    return float(cap) if cap else None


@app.get("/demo")
def demo(
    entity_key: str | None = Query(
        None, examples=["231:7064038214"],
        description="WHO. The customer every stage is run against. Omit to auto-pick a "
                    "trusted one. Browse keys with GET /customers."),
    amount: float | None = Query(
        None, examples=[5000],
        description="HOW MUCH (NGN). Omit -> stage 6 uses their own median spend and "
                    "stage 7 uses 10x their biggest-ever."),
    city: str | None = Query(
        None, examples=["Pyongyang"],
        description="WHERE. Judged against the cities THIS customer has actually used "
                    "(read from their profile). Try one of their own (see "
                    "GET /customer/{entity_key} -> learned.usual_cities) and no city "
                    "rule fires; try 'Pyongyang' or 'Kano' and detect_unusual_city "
                    "fires — because it is not in THEIR list, not because it is on any "
                    "list of ours."),
    destination_country: str | None = Query(
        None, examples=["KP"],
        description="TO WHICH COUNTRY (2-letter ISO code). 'NG' = Nigeria (domestic); "
                    "anything else is cross-border, e.g. 'KP' = North Korea, "
                    "'GB' = United Kingdom, 'US' = United States. Omit -> stage 6 uses "
                    "NG (Nigeria), stage 7 uses KP (North Korea)."),
    hour: int | None = Query(
        None, ge=0, le=23, examples=[3],
        description="WHAT TIME (0-23). Judged against the hours THIS customer actually "
                    "transacts in. Omit -> stage 6 uses their busiest hour, stage 7 "
                    "uses an hour they have never used."),
    destination_account_no: str | None = Query(
        None, examples=["NEW-ACCT-9999"],
        description="TO WHOM. A beneficiary they already pay fires nothing; an unknown "
                    "one fires first_time_beneficiary. Omit -> stage 6 uses one of "
                    "their known beneficiaries, stage 7 uses a brand-new account."),
):
    """Run the whole story end-to-end THROUGH the service and return each stage
    with its real result — the microservice version of demo_end_to_end.sh.

    EVERY field is optional. Anything you do not supply is filled in from THAT
    CUSTOMER'S OWN learned profile (read from Postgres) — never from a fixed script.

    HOW THE TWO SCORING STAGES USE YOUR INPUT
      * stage 6 - "an ordinary day": fields you omit are taken from their profile
        (their city, their busiest hour, a beneficiary they already pay, their median).
      * stage 7 - "a suspicious day": fields you omit are chosen to BREAK their
        pattern (a city they have never used, cross-border, an hour they never use, a
        brand-new account).
      * A field you DO supply is used in BOTH stages, unchanged. So if you pass
        city=Pyongyang, stage 6 uses Pyongyang too — and the city rule will fire there
        as well. That is the point: the engine reacts to YOUR input, judged against
        THEIR learned history.

    WHY A CITY IS "UNUSUAL" — there is no blocklist of bad cities. The engine compares
    the city you send against `usual_cities` learned for THIS customer. 'Kano' is
    perfectly normal for a Kano customer and unusual for a Lagos one. Every stage
    returns `transaction_sent`, which shows each field, where the value came from, and
    whether it matches that customer's learned data — so you can see the comparison,
    not just the verdict.

        GET /demo?entity_key=231:7064038214&amount=5000&city=Pyongyang&hour=3

    Browse customers with GET /customers. Inspect what one has actually learned with
    GET /customer/{entity_key}.

    Every value below is either read from the CHOSEN CUSTOMER's learned profile or
    from the env-driven config / AML rules. Nothing behavioural is hard-coded: the
    thresholds come from `.env`/`config.py` and the hard cap comes from the AML rules.

    Stage 1 pulls FRESH data live from production — safely (bounded chunks, capped,
    throttled) — so the whole story is visible from ingestion through to decision.

    Every stage is also narrated to stdout, so `docker compose logs -f` tells the same
    story with the reason for each outcome.
    """
    import json
    import sync_manager
    stages = []
    TOTAL = 10

    def add(title, description, result, outcome="", why=""):
        """Record a stage AND narrate it to the logs.

        `description` = what this stage demonstrates (plain words).
        `outcome`     = what actually happened, in one line.
        `why`         = WHY it happened — the reason, not just the value.
        """
        n = len(stages) + 1
        audit.log.info("demo %d/%d | %s", n, TOTAL, title)
        audit.log.info("demo %d/%d |   what : %s", n, TOTAL, description.split(". ")[0] + ".")
        if outcome:
            audit.log.info("demo %d/%d |   result: %s", n, TOTAL, outcome)
        if why:
            audit.log.info("demo %d/%d |   why  : %s", n, TOTAL, why)
        stages.append({"stage": n, "of": TOTAL, "title": title,
                       "description": description,
                       "outcome": outcome, "why": why, "result": result})

    def _rules(r):
        """'allow (no rules fired)' / 'review (3 rules: a, b, c)'"""
        fired = [f["rule"] if isinstance(f, dict) and "rule" in f else f.get("rule_code")
                 for f in r.get("fired_rules", [])]
        if r.get("decision") == "allow":
            return "allow — no rules fired"
        return f"review — {len(fired)} rule(s) fired: {', '.join(fired)}"

    audit.log.info("demo ================ START (entity_key=%s) ================",
                   entity_key or "auto-pick")

    # 1) SAFE INGESTION — pull fresh data from production without hammering it.
    #    Deliberately small for the demo: one capped, chunked, throttled run.
    s1 = sync_manager.sync(max_rows=config.DEMO_SYNC_MAX_ROWS, progress=False)
    if s1.get("synced"):
        o1 = (f"pulled {s1.get('new_rows', 0):,} new + {s1.get('refreshed_rows', 0):,} "
              f"refreshed rows in {s1.get('elapsed_seconds')}s; cache now holds "
              f"{s1.get('cache_rows_total', 0):,} rows")
        w1 = (f"production was read READ-ONLY in bounded chunks of "
              f"{config.SYNC_CHUNK_SIZE:,}, capped at {config.DEMO_SYNC_MAX_ROWS:,} rows for "
              f"this demo, with a {config.SYNC_SLEEP_SECONDS}s pause between chunks — so the "
              f"live DB is never hammered")
    elif s1.get("reason") == "prod_pull_disabled":
        o1 = "SKIPPED — no production read was attempted"
        w1 = ("the BP_ALLOW_PROD_PULL safety switch is set to 0, so the service refuses to "
              "touch production at all. Everything below still works because it reads the "
              "LOCAL cache. Set BP_ALLOW_PROD_PULL=1 (and have your IP allowlisted) to see "
              "a real pull")
    else:
        o1 = f"FAILED — {s1.get('reason')}"
        w1 = str(s1.get("error") or s1.get("detail") or "see logs above")
    add("Pull FRESH data from production — safely",
        "The ingestion layer (sync_manager) is the ONLY thing that reads production. "
        "It pages with a keyset cursor (WHERE id > last ORDER BY id LIMIT n) in small "
        "bounded chunks, caps how much one run may pull, sleeps between chunks, and "
        "runs READ-ONLY with a server-side statement timeout — so the live DB is never "
        "hammered. It resumes from a watermark after any failure, and re-pulls the last "
        "few days so status flips (clean -> blocked) are corrected. Everything after "
        "this stage reads the LOCAL cache, never production.",
        s1, outcome=o1, why=w1)

    # 2) ingestion state — what the cache now holds
    s2 = sync_manager.status()
    add("Ingestion state (the local cache)",
        "Where the watermark sits and what the local cache holds after that pull. "
        "This cache is what every service replica learns from, so production sees "
        "exactly one reader no matter how many replicas run.",
        s2,
        outcome=(f"cache holds {s2.get('cache_rows', 0):,} transactions for "
                 f"{s2.get('cache_customers', 0):,} customers; watermark at id="
                 f"{s2.get('watermark_last_id', 0)}"),
        why=("the watermark is the resume point — a future sync starts from there instead "
             "of re-reading everything. cache_rows=0 simply means no successful pull has "
             "happened yet" if not s2.get("cache_rows") else
             "the sync advances the watermark only AFTER a chunk is safely committed, so a "
             "crash resumes rather than restarts"))

    # 3) configuration — every knob here is env-driven (see .env / config.py)
    add("Configuration",
        "The env-driven settings the whole system runs by (from .env / config.py): the "
        "learning window, time-decay, who earns a trusted profile, the confidence "
        "threshold, when a customer is retrained, and the safe-ingestion dials. None "
        "of these are hard-coded.",
        {
            "ingestion_safety": {
                "chunk_size": config.SYNC_CHUNK_SIZE,
                "row_cap_per_run": config.SYNC_MAX_ROWS,
                "throttle_seconds": config.SYNC_SLEEP_SECONDS,
                "statement_timeout_ms": config.SYNC_STATEMENT_TIMEOUT_MS,
                "refresh_days": config.SYNC_REFRESH_DAYS,
                "prod_reads_allowed": config.ALLOW_PROD_PULL,
                "production_access": "READ-ONLY, sync job only",
            },
            "learning_window_months": config.LOOKBACK_MONTHS,
            "decay_half_life_days": config.DECAY_HALF_LIFE_DAYS,
            "eligibility_§1": (f">= {config.ELIGIBLE_MIN_TENURE_DAYS} days tenure AND "
                               f">= {config.ELIGIBLE_MIN_TXNS} clean txns AND "
                               f"<= {config.ELIGIBLE_MAX_FRAUD_TXNS} confirmed-fraud txns"),
            "confidence_trust_threshold": config.CONFIDENCE_TRUST_THRESHOLD,
            "learn_from_clean_only": config.LEARN_FROM_CLEAN_ONLY,
            "retrain_triggers": {"new_txns": config.RETRAIN_MIN_NEW_TXNS,
                                 "max_age_days": config.RETRAIN_MAX_AGE_DAYS,
                                 "drift_signals": config.DRIFT_SIGNAL_THRESHOLD},
        },
        outcome=(f"learning window {config.LOOKBACK_MONTHS} months; trusted needs "
                 f">={config.ELIGIBLE_MIN_TENURE_DAYS}d tenure, >={config.ELIGIBLE_MIN_TXNS} clean txns, "
                 f"<={config.ELIGIBLE_MAX_FRAUD_TXNS} fraud, confidence >={config.CONFIDENCE_TRUST_THRESHOLD}"),
        why=("every one of these is an environment variable — compliance can retune the "
             "system without a code change or redeploy"))

    # 4) system stats
    st = stats()
    add("What the system has learned",
        "Totals across every learned profile: how many customers, the Active (trusted) "
        "vs Warming-Up split, drift status, and how many AML rules are loaded.",
        st,
        outcome=(f"{st.get('profiles', 0):,} profiles learned — "
                 f"{st.get('by_status', {}).get('active', 0):,} Active, "
                 f"{st.get('by_status', {}).get('warming_up', 0):,} Warming-Up; "
                 f"{st.get('rules', 0)} AML rules, {st.get('peer_baselines', 0)} peer baselines"),
        why=("most customers are Warming-Up because they have not yet earned trust under "
             "§1 — they are judged against their peer group, never on thin history"))

    # 5) choose the customer — either the caller's entity_key, or auto-pick.
    # Trim stray whitespace so a copy-pasted " 231:..." (a leading space is easy to add
    # in Swagger or a URL) still resolves instead of failing as "no profile".
    entity_key = entity_key.strip() if entity_key else entity_key
    city = city.strip() if city else city
    destination_country = destination_country.strip().upper() if destination_country else destination_country
    destination_account_no = destination_account_no.strip() if destination_account_no else destination_account_no
    conn = db.connect()
    cur = db.dict_cursor(conn)
    gate = (config.CONFIDENCE_TRUST_THRESHOLD, config.ELIGIBLE_MIN_TENURE_DAYS,
            config.ELIGIBLE_MIN_TXNS, config.ELIGIBLE_MAX_FRAUD_TXNS)
    picked_by = "auto-picked"
    if entity_key:
        # Caller chose the customer — run the whole demo against THEM, whatever their state.
        cur.execute("SELECT * FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
        p = cur.fetchone()
        picked_by = "requested via ?entity_key="
        if p is None:
            conn.close()
            audit.log.warning("demo ABORT — entity_key=%s has no profile", entity_key)
            raise HTTPException(404, {
                "error": f"no profile for entity_key '{entity_key}'",
                "hint": "browse valid keys with GET /customers (or GET /examples)",
                "entity_key_format": "{branch_id}:{origin_account_no}, e.g. 231:5510027882",
            })
    else:
        # Pick someone the engine ACTUALLY trusts right now — i.e. who passes the live
        # §1/§2/§10 gate, not merely someone whose stored flag says 'active'. Otherwise
        # the stage claims "TRUSTED" while scoring would fall back to the peer baseline.
        # Take several candidates and prefer one whose learned cities are REAL place
        # names — the most active accounts often have placeholder location data, which
        # would make the city part of the story meaningless.
        cur.execute("SELECT * FROM bp_user_behaviour_profile "
                    " WHERE profile_status='active' AND usual_cities <> '{}' AND max_amount > 0 "
                    "   AND coalesce(confidence_score,0) >= %s "
                    "   AND coalesce(tenure_days,0) >= %s "
                    "   AND coalesce(lifetime_clean_txns,0) >= %s "
                    "   AND coalesce(suspicious_tx_count,0) <= %s "
                    " ORDER BY total_tx_count DESC LIMIT 50", gate)
        candidates = cur.fetchall()
        p = next((c for c in candidates
                  if _real_cities(json.loads(c["usual_cities"] or "{}"))), None)
        if p is None:
            p = candidates[0] if candidates else None
        if p is None:
            conn.close()
            return {"error": "no customer currently passes the trust gate",
                    "detail": (f"needs confidence >= {config.CONFIDENCE_TRUST_THRESHOLD}, tenure >= "
                               f"{config.ELIGIBLE_MIN_TENURE_DAYS}d, clean txns >= {config.ELIGIBLE_MIN_TXNS}, "
                               f"confirmed fraud <= {config.ELIGIBLE_MAX_FRAUD_TXNS}"),
                    "hint": "browse customers with GET /customers",
                    "stages": stages}
    hard_cap = _hard_cap_for(conn, p["branch_id"])
    # The unusual-time rule fires on the SHARE of a customer's activity at an hour, not
    # on mere presence — so the receipt must report the share, using the rule's own
    # threshold. Read it here rather than assuming the default.
    _utp = conn.cursor()
    _utp.execute("SELECT params FROM bp_rule_definition WHERE rule_code='unusual_time_for_user'")
    _utrow = _utp.fetchone()
    min_share = float(json.loads((_utrow[0] if _utrow else None) or "{}").get("min_share", 0.02))
    conn.close()
    ek = p["entity_key"]
    name = p["customer_name"]
    cities = json.loads(p["usual_cities"] or "{}")
    real = _real_cities(cities)
    # DATA QUALITY: some accounts' customer_location in production is a placeholder,
    # so their learned "usual cities" are literally {"-", "N/A"}. The city rule cannot
    # mean anything for them — say so rather than claiming '- is a city they use'.
    their_city = next(iter(real), None)
    city_note = ""
    if their_city is None:
        their_city = next(iter(cities), "Lagos")
        city_note = (f" NOTE: this customer's location data in production is a placeholder "
                     f"({list(cities)[:3]}), so 'usual city' is not meaningful for them and "
                     f"the unusual-city rule carries no signal here — a data-quality issue "
                     f"in the source, not the model.")
    their_benefs = json.loads(p["beneficiaries"] or "{}")
    benef = next(iter(their_benefs), "KNOWN1")
    biggest = float(p["max_amount"] or 0)
    is_trusted, trust_why = profile_is_trusted(p)
    add("The customer under test",
        f"The customer every later stage is scored against ({picked_by}). You can run this "
        f"whole demo for anyone: GET /demo?entity_key=<key> — browse keys with GET /customers. "
        f"Stages 6, 7 and 9 all use THIS person's learned profile, so you see a normal "
        f"transaction pass and an abnormal one flagged for the same human being.",
        {
            "entity_key": ek, "name": name, "picked_by": picked_by,
            "status": p["profile_status"], "confidence": p["confidence_score"],
            "trusted_by_engine": is_trusted, "trust_reason": trust_why,
            "tenure_days": p["tenure_days"], "clean_lifetime_txns": p["lifetime_clean_txns"],
            "confirmed_fraud_txns": p["suspicious_tx_count"],
            "usual_spend_avg_ngn": float(p["avg_amount"] or 0), "biggest_ever_ngn": biggest,
            "usual_cities": list(cities)[:5], "busiest_day": p["top_day_of_week"],
        },
        outcome=(f"{name} ({ek}) — {'TRUSTED on their own profile' if is_trusted else 'NOT trusted'}; "
                 f"tenure {p['tenure_days']}d, {p['lifetime_clean_txns']} clean txns, "
                 f"confidence {p['confidence_score']}, usual spend "
                 f"{float(p['avg_amount'] or 0):,.0f} NGN, biggest ever {biggest:,.0f} NGN"),
        why=(f"they pass the §1 gate, so the engine judges them on their OWN learned history"
             if is_trusted else
             f"{trust_why} — so the engine will judge them against their PEER GROUP, not "
             f"their own history. That is the anti-poisoning rule working as designed"))

    def score(txn, velocity=None):
        """Score a transaction exactly as POST /score does — same RuleEngine, same
        profile read from Postgres, same trust gate — but WITHOUT the side effects.

        POST /score additionally: bumps txns_since_build / drift_signal_count, writes a
        bp_event_log row, and calls maybe_retrain(). The demo deliberately skips those
        three so it is repeatable and does not mutate the customer's counters every time
        someone clicks it (running it 100 times would otherwise fake a retrain trigger).
        Stage 10 performs the retrain explicitly, so the full path is still demonstrated
        — just not silently, and not once per stage.
        """
        c = db.connect()
        try:
            fired = RuleEngine(c, velocity=velocity).evaluate(txn)
        finally:
            c.close()
        return {"decision": "review" if fired else "allow",
                "fired_rules": [{"rule": f["rule_code"], "severity": f["severity"]} for f in fired]}

    base = {"branch_id": p["branch_id"], "origin_account_no": p["origin_account_no"],
            "currency": "NGN", "identifier": p["identifier"], "account_type": p["account_type"]}

    # Hours come from THIS customer's own learned hour histogram — never hard-coded.
    # normal_hour  = the hour they transact in most (guaranteed inside their pattern)
    # odd_hour     = an hour they have NEVER used (falls back to their least-used)
    hour_hist = {int(h): int(c) for h, c in json.loads(p["peak_transaction_hours"] or "{}").items()}
    normal_hour = (int(p["top_hour"]) if p["top_hour"] is not None
                   else (max(hour_hist, key=hour_hist.get) if hour_hist else 13))
    unused_hours = [h for h in range(24) if h not in hour_hist]
    odd_hour = next((h for h in unused_hours if h in (0, 1, 2, 3, 4, 5)),      # prefer night
                    unused_hours[0] if unused_hours else
                    (min(hour_hist, key=hour_hist.get) if hour_hist else 3))
    _h = lambda h: f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"                 # 13 -> '1pm'

    their_median = float(p["median_amount"] or p["avg_amount"] or 0)
    amt_supplied = amount is not None
    # A city they have never used. The candidate pool is only a source of NAMES — what
    # makes one "unusual" is that it is absent from THIS customer's learned usual_cities.
    unusual_city = next((c for c in ["Pyongyang", "Kano", "Maiduguri", "Sokoto", "Yola"]
                         if c not in cities), "Pyongyang")

    def _sent(amt, city_v, country_v, hour_v, benef_v, derived: str):
        """Exactly what we sent, where each value came from, and how it compares to what
        THIS customer has actually done. This is the anti-hard-coding receipt: you can
        see the comparison the engine made, not just its verdict."""
        return {
            "amount_ngn": {
                "value": amt,
                "source": "you supplied" if amt_supplied else derived,
                "their_median": their_median, "their_biggest_ever": biggest,
                "times_their_median": round(amt / their_median, 2) if their_median else None,
                "above_their_biggest_ever": bool(biggest and amt > biggest),
            },
            "city": {
                "value": city_v,
                "source": "you supplied" if city else derived,
                "their_usual_cities": list(cities)[:6],
                "is_one_of_their_usual_cities": city_v in cities,
            },
            "destination_country": {
                "value": country_v,
                "country_name": _COUNTRY_NAMES.get((country_v or "").upper(), "(unknown code)"),
                "source": "you supplied" if destination_country else derived,
                "cross_border": country_v != "NG",
            },
            "hour": {
                "value": hour_v,
                "source": "you supplied" if hour is not None else derived,
                "their_busiest_hour": p["top_hour"],
                "hours_they_actually_use": sorted(hour_hist),
                # The rule judges the SHARE of their activity at this hour, not mere
                # presence. e.g. 3am may exist in their history but be <2% of it, which
                # still fires. So we report the real metric the rule uses.
                "share_of_their_activity_at_this_hour":
                    round(hour_hist.get(hour_v, 0) / (sum(hour_hist.values()) or 1), 4),
                "flags_as_unusual_time_below_share": min_share,
                "is_a_normal_hour_for_them":
                    (hour_hist.get(hour_v, 0) / (sum(hour_hist.values()) or 1)) >= min_share,
            },
            "destination_account_no": {
                "value": benef_v,
                "source": "you supplied" if destination_account_no else derived,
                "is_a_beneficiary_they_already_pay": benef_v in their_benefs,
            },
        }

    # ---- stage 6: an ORDINARY day. Anything you did not supply comes from THEIR profile.
    normal_amt = float(amount) if amt_supplied else (their_median or 5000)
    s6_city = city or their_city
    s6_country = destination_country or "NG"
    s6_hour = hour if hour is not None else normal_hour
    s6_benef = destination_account_no or benef
    vs_median = (normal_amt / their_median) if their_median else None
    r6 = score({**base, "amount": normal_amt, "destination_account_no": s6_benef,
                "customer_location": f"street, {s6_city}, State", "origin_country": "NG",
                "destination_country": s6_country,
                "ts": datetime.utcnow().replace(hour=s6_hour)})
    _supplied = [n for n, v in (("amount", amount), ("city", city),
                                ("destination_country", destination_country),
                                ("hour", hour), ("destination_account_no", destination_account_no))
                 if v is not None]
    add("The customer pays — an ORDINARY day",
        f"{name} sends {normal_amt:,.0f} NGN to {s6_benef} in {s6_city} at {_h(s6_hour)} "
        f"({s6_country}). "
        + (f"You supplied: {', '.join(_supplied)}. Everything else is taken from THEIR "
           f"own profile. " if _supplied else
           "You supplied nothing, so every field is taken from THEIR own profile — their "
           "median spend, a city they use, their busiest hour, a beneficiary they pay. ")
        + "`transaction_sent` below shows each field, where the value came from, and "
          "whether it matches what this customer has actually done.",
        {"customer": name, "entity_key": ek,
         "transaction_sent": _sent(normal_amt, s6_city, s6_country, s6_hour, s6_benef,
                                   derived="from their own profile"),
         "you_supplied": _supplied or None, **r6},
        outcome=_rules(r6),
        why=_why_ordinary(name, r6, normal_amt, vs_median, their_median, biggest,
                          s6_city, cities, s6_hour, _h(s6_hour), hour_hist,
                          s6_country, s6_benef, their_benefs, amt_supplied) + city_note)

    # 7) abnormal transaction -> review. All values DERIVED from this customer + the
    #    AML hard cap — no hard-coded amount. 10x their biggest-ever AND above the cap.
    # The SAME amount the customer paid in stage 6 — only the CONTEXT changes. A real
    # customer never says "this one is abnormal"; they pay an amount and the engine
    # judges the whole transaction. When no amount is given we derive an obviously
    # extreme one so the out-of-the-box demo also exercises the amount rules.
    abn_amt = normal_amt if amt_supplied else max(biggest * 10, (hard_cap or 1e9) * 2)
    abn_vs_max = (abn_amt / biggest) if biggest else None
    s7_city = city or unusual_city
    s7_country = destination_country or "KP"
    s7_hour = hour if hour is not None else odd_hour
    s7_benef = destination_account_no or "NEW-BENEFICIARY-ACCT"
    r7 = score({**base, "amount": abn_amt, "destination_account_no": s7_benef,
                "customer_location": f"road, {s7_city}, region", "origin_country": "NG",
                "destination_country": s7_country,
                "ts": datetime.utcnow().replace(hour=s7_hour)})
    # what actually differs between the two stages — computed, not asserted
    changed = [n for n, a, b in (("amount", normal_amt, abn_amt), ("city", s6_city, s7_city),
                                 ("destination_country", s6_country, s7_country),
                                 ("hour", s6_hour, s7_hour),
                                 ("destination_account_no", s6_benef, s7_benef)) if a != b]
    add("The SAME transaction — a SUSPICIOUS day",
        f"{name} sends {abn_amt:,.0f} NGN to {s7_benef} in {s7_city} at {_h(s7_hour)} "
        f"({s7_country}). "
        + (f"Only these changed from stage 6: {', '.join(changed)}. "
           if changed else "NOTHING changed from stage 6 — you supplied every field, so "
                           "both stages scored the identical transaction. ")
        + (f"Fields you supplied ({', '.join(_supplied)}) are used UNCHANGED here too; "
           f"the rest are chosen to break their pattern. " if _supplied else
           "You supplied nothing, so every field here is chosen to break their pattern — "
           "a city they have never used, cross-border, an hour they never use, a new "
           "account, and an amount 10x their biggest-ever. ")
        + "`transaction_sent` shows how each value compares to their learned history.",
        {"customer": name, "entity_key": ek,
         "transaction_sent": _sent(abn_amt, s7_city, s7_country, s7_hour, s7_benef,
                                   derived="chosen to break their pattern"),
         "you_supplied": _supplied or None,
         "changed_from_stage_6": changed or None,
         "aml_hard_cap_ngn": hard_cap, **r7},
        outcome=_rules(r7),
        why=_why_abnormal(name, r7, abn_amt, abn_vs_max, biggest, hard_cap,
                          s7_city, _h(s7_hour), amt_supplied and "amount" not in changed,
                          country=s7_country))

    # 8) COLD START — verdict for THIS customer. Every stage is about the customer under
    #    test, so this stage answers: "is THIS person a cold start, or have they earned
    #    the right to be judged on their own history?" We never invent another account.
    peer_group = {"branch_id": p["branch_id"], "account_type": p["account_type"] or "unknown"}
    gate_checks = {
        "tenure_days": {"value": p["tenure_days"], "required": config.ELIGIBLE_MIN_TENURE_DAYS,
                        "met": (p["tenure_days"] or 0) >= config.ELIGIBLE_MIN_TENURE_DAYS},
        "clean_lifetime_txns": {"value": p["lifetime_clean_txns"], "required": config.ELIGIBLE_MIN_TXNS,
                                "met": (p["lifetime_clean_txns"] or 0) >= config.ELIGIBLE_MIN_TXNS},
        "confirmed_fraud_txns": {"value": p["suspicious_tx_count"], "max_allowed": config.ELIGIBLE_MAX_FRAUD_TXNS,
                                 "met": (p["suspicious_tx_count"] or 0) <= config.ELIGIBLE_MAX_FRAUD_TXNS},
        "confidence": {"value": p["confidence_score"], "required": config.CONFIDENCE_TRUST_THRESHOLD,
                       "met": (p["confidence_score"] or 0) >= config.CONFIDENCE_TRUST_THRESHOLD},
    }
    failed = [k for k, v in gate_checks.items() if not v["met"]]
    if is_trusted:
        o8 = f"NOT a cold start — {name} is judged on their OWN learned profile"
        w8 = (f"{name} has earned trust: they pass every §1 condition "
              f"(tenure {p['tenure_days']}d >= {config.ELIGIBLE_MIN_TENURE_DAYS}, "
              f"{p['lifetime_clean_txns']} clean txns >= {config.ELIGIBLE_MIN_TXNS}, "
              f"{p['suspicious_tx_count']} confirmed fraud <= {config.ELIGIBLE_MAX_FRAUD_TXNS}, "
              f"confidence {p['confidence_score']} >= {config.CONFIDENCE_TRUST_THRESHOLD}). "
              f"Cold start applies to accounts that have NOT earned trust — so it does not "
              f"apply to this person, which is exactly what stages 6 and 7 demonstrated. "
              f"Had they failed any condition, the engine would fall back to their peer "
              f"group {peer_group}. To watch the cold-start path for real, run this demo "
              f"for an untrusted customer: GET /customers?trusted=false")
    else:
        o8 = f"COLD START — {name} is judged against their PEER GROUP, not their own history"
        w8 = (f"{trust_why}. Failing conditions: {failed or 'none (status/confidence)'}. "
              f"Because they have not earned trust, the engine ignores their own thin "
              f"history and compares them to peers in the same branch "
              f"({peer_group['branch_id']}) and account type ({peer_group['account_type']}) — "
              f"rules fired against them are tagged peer_baseline. This is what stops a "
              f"fraudster opening an account, running a little fake activity, and having it "
              f"accepted as 'normal'")
    add("COLD START — is THIS customer trusted, or judged against peers?",
        f"Every other stage scored {name}; this stage states the verdict for THEM. An "
        f"account that has not earned trust under §1 (too new, too few clean transactions, "
        f"any confirmed fraud, or low confidence) is judged against its PEER GROUP baseline "
        f"instead of its own history — that is the cold-start path. A customer who HAS "
        f"earned trust is not a cold start, and is judged on their own profile. Run the "
        f"demo for a Warming-Up customer (GET /customers?trusted=false) to see the "
        f"cold-start path light up with real data.",
        {"entity_key": ek, "customer": name,
         "is_cold_start": not is_trusted,
         "judged_against": "own_profile" if is_trusted else "peer_group",
         "trust_reason": trust_why,
         "gate_§1": gate_checks,
         "failing_conditions": failed,
         "peer_group_they_fall_back_to": peer_group},
        outcome=o8, why=w8)

    # 9) live velocity: a burst the daily profile can't see
    r9 = score({**base, "amount": 2_000_000, "destination_account_no": "V6",
                "customer_location": f"x, {their_city}, y", "origin_country": "NG",
                "destination_country": "NG", "ts": datetime.utcnow()},
               velocity=_BurstVelocity())
    add("LIVE velocity (a burst)",
        f"{name}: a burst of small transfers within ~1 minute (a card-testing pattern) "
        "that the daily profile alone can't see. The live recent-window velocity rules "
        "catch it. Expected result: REVIEW on the velocity rules.",
        {"customer": name, "entity_key": ek, **r9},
        outcome=_rules(r9),
        why=("each transfer on its own looks unremarkable — it is the RATE that betrays it. "
             "A profile rebuilt daily could never see a burst that happens inside one "
             "minute, so a live recent-window counter runs alongside the stored profile"))

    # 10) event-driven retrain (force, to show it working)
    r10 = retrain.retrain_customer(ek)
    if r10.get("retrained"):
        lr = r10.get("learned", {})
        o10 = (f"retrained — version {r10.get('version')}, status {r10.get('status')}, "
               f"confidence {r10.get('confidence')}, learned from "
               f"{lr.get('learned_from_txn_count', 0):,} transactions")
        w10 = ("recomputed from the LOCAL cache (one indexed lookup) and saved in place — "
               "production was not touched. This is what replaced the unbounded per-retrain "
               "production query that loaded the live DB")
    elif r10.get("reason") in ("cache_not_populated", "customer_not_in_cache",
                               "all_cached_txns_excluded"):
        # Say precisely WHICH of the three causes it is — "no clean history" alone would
        # wrongly imply this customer's every transaction is dirty.
        about = ("This IS about the customer." if r10.get("about_this_customer")
                 else f"This is NOT a statement about {name} — it is about the cache.")
        o10 = f"SKIPPED ({r10.get('reason')}) — {r10.get('meaning')}"
        w10 = f"{about} {r10.get('note')} Production is never read here."
    else:
        o10 = f"not retrained — {r10.get('reason')}"
        w10 = str(r10.get("note") or r10.get("error") or "see the result for detail")
    add("Event-driven retrain",
        f"Recompute {name}'s profile FROM THE LOCAL CACHE (production is not touched) "
        "and save it in place; the version bumps. One small indexed lookup — this "
        "replaced the unbounded per-retrain production query that used to load the "
        "live DB. This is how each customer stays fresh — event-driven, no cron. "
        "In the result's 'learned', learned_from_txn_count is a COUNT of transactions the "
        "profile was learned from (not a money amount and not days); *_ngn fields are money.",
        r10, outcome=o10, why=w10)

    audit.log.info("demo ================ DONE (%s / %s) ================", ek, name)
    return {
        "customer_used": {"entity_key": ek, "name": name, "picked_by": picked_by,
                          "trusted_by_engine": is_trusted, "trust_reason": trust_why},
        "run_for_another_customer": "GET /demo?entity_key=<key>  — list keys with GET /customers",
        "read_the_logs": "docker compose logs -f  — every stage is narrated with its reason",
        "stages": stages,
        "note": "The whole pipeline through the microservice, all against one real "
                "customer: a normal transaction passes, an abnormal one (derived from "
                "their own profile + the AML hard cap) is flagged, new accounts use "
                "peers, bursts are caught, and the profile retrains itself — no cron.",
    }
