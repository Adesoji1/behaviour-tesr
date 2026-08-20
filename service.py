#!/usr/bin/env python3
"""
Adhere Behavioural Anti-Fraud Service (FastAPI) — the ONE production scoring service.

For every transaction, adhere calls POST /score. There is a single scoring path:
  1. load the customer's profile / recent history,
  2. build behavioural features,
  3. run the trained behavioural ML model (ml.serve) — safe / review / unsafe,
  4. return risk + status + activity code + description, persist bp_decision, send the webhook.
/score does NOT re-learn the customer (§8 Behaviour Stability — no per-request profile mutation);
re-learning is the offline, gated batch retrain (ml.retrain_trigger -> ml.train) on new-data / age /
drift (§4). NO AML/rule scoring lives here — AML is a separate service.

Endpoints:
  GET  /health                      liveness
  POST /score                       score one transaction (the model decides)
  GET  /profile/{entity_key}        inspect a stored profile
  GET  /customer/{entity_key}       full customer status (eligibility, learned, retrain state)
  POST /retrain/{entity_key}        force a per-customer statistical profile retrain
  POST /reload                      pick up a newly-promoted model (registry handoff)

Run:  uvicorn service:app --host 0.0.0.0 --port 8080
"""
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Security
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import audit
import config
import db
import retrain
import webhooks
from eligibility import profile_is_trusted
from ml.config import normalize_transaction_type   # shared canonicaliser (train == score)

# Serve our own favicon (static/favicon.ico) so the browser tab shows it on Swagger /docs, ReDoc,
# and every page. We disable FastAPI's built-in /docs + /redoc and re-add them below pointing at it.
app = FastAPI(title="Adhere Behaviour-Profile Service", version="1.0",
              docs_url=None, redoc_url=None)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_FAVICON = os.path.join(_STATIC_DIR, "favicon.ico")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browser tab icon (also what /docs and /redoc reference)."""
    return FileResponse(_FAVICON)


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    """Swagger UI with our own favicon in the browser tab."""
    return get_swagger_ui_html(openapi_url=app.openapi_url, title=f"{app.title} — Swagger UI",
                               swagger_favicon_url="/static/favicon.ico")


@app.get("/redoc", include_in_schema=False)
def redoc_ui():
    """ReDoc with our own favicon in the browser tab."""
    return get_redoc_html(openapi_url=app.openapi_url, title=f"{app.title} — ReDoc",
                          redoc_favicon_url="/static/favicon.ico")


# ---------------------------------------------------------------------------
# API-KEY AUTH for POST /score — header `X-Adhere-Key`. Only the SHA-256 hash is
# ever compared/stored (see manage_api_key.py). Exactly ONE key is active at a
# time; rotating a new key invalidates the former. Declaring APIKeyHeader adds the
# 🔒 padlock + "Authorize" button to /score in Swagger.
#
# Standard HTTP auth semantics:
#   missing OR invalid credentials -> 401 (+ WWW-Authenticate challenge)
#   no key configured on the server -> 503 (server misconfiguration, not the caller's fault)
#   body validation errors          -> 422 (FastAPI default)
# (403 is reserved for "authenticated but not authorized" — not used here yet.)
# Set BP_API_KEY_DISABLED=1 to turn auth off (internal/dev only).
# ---------------------------------------------------------------------------
api_key_header = APIKeyHeader(name="X-Adhere-Key", auto_error=False,
                             description="Your Adhere API key (config.API_KEY / manage_api_key.py).")
_WWW_AUTH = {"WWW-Authenticate": 'ApiKey realm="adhere", header="X-Adhere-Key"'}
_key_cache = {"hash": None, "at": 0.0}


def _active_key_hash() -> str | None:
    """SHA-256 hash of the currently active key. `config.API_KEY` (BP_API_KEY in .env) overrides
    the DB (simple deploys); otherwise the single active row in bp_api_key. Cached for
    config.API_KEY_CACHE_TTL seconds (rotation is picked up within the TTL, or now via POST /reload)."""
    if config.API_KEY:
        return hashlib.sha256(config.API_KEY.encode()).hexdigest()
    now = time.time()
    if now - _key_cache["at"] < config.API_KEY_CACHE_TTL:
        return _key_cache["hash"]
    h = _key_cache["hash"]
    try:
        with db.pooled() as conn:
            cur = db.dict_cursor(conn)
            cur.execute("SELECT key_hash FROM bp_api_key WHERE active ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            h = row["key_hash"] if row else None
    except Exception as e:                       # keep last-known on a store hiccup
        audit.log.warning("api-key lookup failed (%s) — using cached", e)
    _key_cache.update(hash=h, at=now)
    return h


def require_api_key(x_adhere_key: str | None = Security(api_key_header)) -> None:
    """Enforce the X-Adhere-Key on /score (standard semantics): 401 for missing OR invalid
    credentials (with a WWW-Authenticate challenge). A missing SERVER key is caught at startup
    (see _require_api_key_configured), so the no-key 503 below is only a defensive last resort for
    a key revoked WHILE the service is running — it is not a normal/config response."""
    if config.API_KEY_DISABLED:
        return
    active = _active_key_hash()
    if not active:                                            # unexpected: key revoked post-startup
        raise HTTPException(503, "API key unavailable — the active key was revoked; rotate a new one")
    if not x_adhere_key or not x_adhere_key.strip():
        raise HTTPException(401, "Missing API key", headers=_WWW_AUTH)
    supplied = hashlib.sha256(x_adhere_key.strip().encode()).hexdigest()
    if not secrets.compare_digest(supplied, active):     # constant-time compare
        raise HTTPException(401, "Invalid API key", headers=_WWW_AUTH)


def _require_api_key_configured() -> None:
    """Fail-fast at startup if `/score` would have no key to check — a REQUIRED server config, not a
    per-request failure. Accepts either source: the `BP_API_KEY` env (config.API_KEY) or an active
    row in `bp_api_key` (the rotating key). Set `BP_API_KEY_DISABLED=1` to intentionally run without
    auth in dev. The actual secret is NEVER read into the message or logged — only its presence."""
    if config.API_KEY_DISABLED:
        audit.log.warning("startup: API-key auth is DISABLED (BP_API_KEY_DISABLED) — /score is open")
        return
    if config.API_KEY:                                        # present via env; never log the value
        audit.log.info("startup: /score API-key auth enabled (source: BP_API_KEY env)")
        return
    try:                                                      # else require an active DB (rotating) key
        with db.pooled() as conn:
            cur = db.dict_cursor(conn)
            cur.execute("SELECT 1 FROM bp_api_key WHERE active LIMIT 1")
            has_db_key = cur.fetchone() is not None
    except Exception as e:                                    # cannot verify -> fail (don't start blind)
        raise RuntimeError(
            "BP_API_KEY is required but could not be verified: the profile store was unreachable at "
            f"startup ({e}). Set BP_API_KEY in the environment, or bring the store up and create a "
            "key with `python manage_api_key.py rotate`.") from None
    if not has_db_key:
        raise RuntimeError(
            "BP_API_KEY is required and no active API key is configured. Set BP_API_KEY in the "
            "environment, or create one with `python manage_api_key.py rotate`. "
            "(Set BP_API_KEY_DISABLED=1 to run without auth in development.)")
    audit.log.info("startup: /score API-key auth enabled (source: active bp_api_key row)")


@app.on_event("startup")
def _startup():
    """Self-migrate: ensure the store schema (incl. bp_decision) exists. Additive,
    idempotent — reaches an existing volume that the one-time init script missed.
    Never fatal: if the store is briefly unreachable at boot, /health still comes up.
    THEN validate that an API key is configured — fatal if not (see _require_api_key_configured)."""
    try:
        db.ensure_schema()
        audit.log.info("startup: schema ensured")
    except Exception as e:
        audit.log.warning("startup: could not ensure schema (%s) — continuing", e)
    _require_api_key_configured()                             # fatal (raises) when no key is configured
    # Warm the behavioural model ONCE per worker so the first /score isn't paying the
    # (multi-second) model-load cost on the request path.
    if config.USE_MODEL:
        try:
            from ml import serve as ml_serve
            ml_serve._active()
            audit.log.info("startup: behavioural model warmed")
        except Exception as e:
            audit.log.warning("startup: model warm failed (%s) — loads on first request", e)


@app.on_event("shutdown")
def _shutdown():
    db.close_pool()


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

# ---------------------------------------------------------------------------
# /score request schema — the Adhere transaction payload (drives the Swagger form).
# Required fields use Field(...) (Swagger marks them with a red *); optional ones default to
# None. Empty strings are accepted (they never break validation). The model reads the customer's
# identifier + context from this body and from the store; it aligns 1:1 with the trained model's
# feature inputs (amount, type, time, location, IP, beneficiary, country, velocity).
# ---------------------------------------------------------------------------
class Account(BaseModel):
    account_number: Optional[str] = Field(None, example="9876543219")
    bank_code: Optional[str] = Field(None, example="001")
    account_type: Optional[str] = Field(None, example="individual")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CustomerDetails(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=200, description="Customer's full name",
                               example="Muhammad Ibrahim Isah")
    customer_email: str = Field(..., max_length=254, description="Customer's email address",
                                example="user@example.com")

    @field_validator("customer_email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = (v or "").strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("customer_email must be a valid email address")
        return v
    customer_phone: Optional[str] = Field(None, description="e.g. +2347012345678", example="+2347012345678")
    identifier: Optional[str] = Field(None, description="BVN (NG) / national_id (KE) / ghana_card (GH) …",
                                      example="22430372151")
    identifier_type: Optional[str] = Field(None, description="bvn / national_id / kra_pin / ghana_card … "
                                           "(required when identifier is provided)", example="bvn")
    country: Optional[str] = Field(None, example="Nigeria")


class AdditionalInfo(BaseModel):
    ip_address: str = Field(..., description="IP address during the transaction", example="192.168.1.1")
    location: str = Field(..., description='Human-readable location, e.g. "Lagos, Nigeria". '
                          "Free text — NOT parsed for coordinates; send lat/long in the fields below.",
                          example="Lagos, Nigeria")
    # OPTIONAL first-party coordinates of the CUSTOMER's actual location for this transaction. When
    # supplied they are the highest-priority geo source for the (best-effort, non-scoring) geo-velocity
    # enrichment. Omitting them keeps the existing contract intact — existing clients are unaffected.
    # See the "Geo-velocity data requirement" disclaimer in the /score endpoint description.
    latitude: Optional[float] = Field(None, example=6.5244,
                                      description="OPTIONAL. The CUSTOMER's actual latitude (WGS84, "
                                      "-90..90) for this transaction — not an agent/terminal location. "
                                      "Enables the optional geo-velocity signal; a missing/invalid/out-of-"
                                      "range value is simply IGNORED (geo unavailable) and never fails the "
                                      "request or affects the fraud decision. See the /score disclaimer.")
    longitude: Optional[float] = Field(None, example=3.3792,
                                       description="OPTIONAL. The CUSTOMER's actual longitude (WGS84, "
                                       "-180..180) for this transaction — not an agent/terminal location. "
                                       "Invalid values are ignored, never rejected. See latitude.")
    transaction_description: Optional[str] = Field(None, example="Payment for order #789")

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _optional_coord(cls, v):
        """Optional geo coordinate: coerce to float; anything missing / empty / non-numeric becomes None
        so a bad coordinate simply makes geo UNAVAILABLE and NEVER fails the request (3.md §15 fail-safe).
        Range (-90..90 / -180..180) is validated downstream by the geo resolver, so an out-of-range value
        is dropped there rather than returned as a 422 here — location is never enforced at the API."""
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


class Txn(BaseModel):
    """One transaction to score. * = required."""
    transaction_id: str = Field(..., min_length=1, max_length=100,
                                description="Unique identifier for the transaction", example="12345678")
    amount: float = Field(..., gt=0, le=1e15,
                          description="Transaction amount — must be > 0 (e.g. 99.13 or 14000000)",
                          example=14000000)
    currency: str = Field(..., description="ISO-4217 currency code, 3 letters (e.g. NGN, USD)", example="NGN")
    transaction_type: str = Field(..., description="The transaction channel/type as the platform "
                                  "records it (e.g. transfer, ussd, card, bank_transfer, vas, "
                                  "ussd_session). Sent RAW — the service canonicalises it (trim + "
                                  "lowercase) to match the vocabulary the model learned.",
                                  example="transfer")
    account_type: str = Field(..., description="individual or corporate", example="individual")

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency must be a 3-letter ISO-4217 code, e.g. NGN, USD, KES")
        return v

    @field_validator("transaction_type")
    @classmethod
    def _ttype(cls, v: str) -> str:
        # transaction_type is a MODEL FEATURE (type_rare): the model learns each customer's channel
        # vocabulary from the RAW training values, so /score must accept the SAME raw values the
        # platform emits (inward_transfer, vas, ussd_session, …) — NOT a fixed {transfer,ussd,web,
        # card} enum, which would 422 real traffic and, if callers normalised to it, corrupt
        # type_rare (the model compares against the raw learned set). We only CANONICALISE, using the
        # exact same normaliser the feature builder applies at train time (ml.config).
        v = normalize_transaction_type(v)
        if not v:
            raise ValueError("transaction_type is required")
        if len(v) > 60:
            raise ValueError("transaction_type is too long (max 60 characters)")
        return v

    @field_validator("account_type")
    @classmethod
    def _atype(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"individual", "corporate"}:
            raise ValueError("account_type must be 'individual' or 'corporate'")
        return v
    customer_details: CustomerDetails
    additional_info: AdditionalInfo
    timestamp: Optional[str] = Field(None, description="ISO 8601; defaults to now if omitted",
                                     example="2026-08-02T14:00:00Z")
    origin_account: Optional[Account] = None
    destination_account: Optional[Account] = None
    run_kyc: Optional[bool] = Field(False, description="(reserved) run KYC checks")

    class Config:
        json_schema_extra = {
            "example": {
                "transaction_id": "12345678", "amount": 14000000, "currency": "NGN",
                "transaction_type": "transfer", "account_type": "individual",
                "timestamp": "2026-08-02T14:00:00Z",
                "origin_account": {"account_number": "9876543219", "bank_code": "001"},
                "destination_account": {"account_number": "123456789", "bank_code": "002"},
                "customer_details": {"customer_name": "Muhammad Ibrahim Isah",
                                     "customer_email": "user@example.com",
                                     "identifier": "22430372151", "identifier_type": "bvn",
                                     "country": "Nigeria"},
                "additional_info": {"ip_address": "192.168.1.1", "location": "Lagos, Nigeria",
                                    "transaction_description": "Payment for order #789"},
                "run_kyc": False,
            }
        }


@app.get("/health")
def health():
    return {"status": "ok", "service": "behaviour-profile", "time": datetime.utcnow().isoformat()}


def _score_with_model(t: Txn, background: BackgroundTasks) -> dict:
    """The adhere hook, MODEL edition — the behavioural anti-fraud ensemble (ml/) is the
    decision engine. It reads THIS customer's learned baseline + recent history from the store
    (read-only) and compares the incoming transaction against it. Inference is CPU; the GPU is
    only used by the offline training job. The decision is persisted to bp_decision for audit
    and delivered by webhook, exactly like the rule path — so nothing downstream changes."""
    from ml import serve as ml_serve
    payload = t.model_dump(exclude_none=True)                # the Txn IS the model payload shape
    result = ml_serve.score_payload(payload)                 # the MODEL decides (+ compliance log)
    # Key the customer the same way the model does: on the stable identifier; fall back to the
    # origin account number, then the transaction id, so there is always a stable entity_key.
    ident = (t.customer_details.identifier if t.customer_details else None) \
        or (t.origin_account.account_number if t.origin_account else None) \
        or t.transaction_id
    ek = str(ident)
    result["entity_key"] = ek
    # Analyst-facing baseline provenance (derived, not stored): a cold-start customer is judged
    # against the POPULATION baseline; an established one against their PERSONAL learned profile.
    _cold = bool(result.get("result", {}).get("is_cold_start"))
    result["baseline_info"] = {"baseline_type": "population" if _cold else "personal",
                               "is_cold_start": _cold}
    decision = result["status"]                              # safe | review | unsafe

    # Persist to bp_decision (audit + webhook outbox) — best-effort so a store hiccup never
    # blocks a live decision (the model has already logged its own compliance record).
    decision_id = None
    try:
        with db.pooled() as conn:
            if config.SCORE_WEBHOOK_URL:
                webhook_status = "pending"
                webhook_next = datetime.utcnow() + timedelta(
                    seconds=config.WEBHOOK_RELAY_GRACE_SECONDS)
            else:
                webhook_status, webhook_next = "disabled", None
            dc = conn.cursor()
            dc.execute(
                "INSERT INTO bp_decision (entity_key, transaction_id, decision, fired_rules, "
                "rules_fired_n, judged_against, trust_reason, own_profile_anomaly, amount, "
                "currency, latency_ms, webhook_status, webhook_next_attempt_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (ek, t.transaction_id, decision,
                 json.dumps(result.get("triggered_signals", [])),
                 len(result.get("triggered_signals", [])),
                 "behavioural_model",                     # judged_against (varchar(32))
                 (f"{result.get('model_version')} | {result.get('activity_code')}: "
                  f"{result.get('description')}"),        # trust_reason (text — full detail)
                 decision != "safe", t.amount, t.currency,
                 result.get("inference_ms"), webhook_status, webhook_next),
            )
            decision_id = dc.fetchone()[0]
            # NOTE: /score intentionally does NOT mutate the learned profile. Per-request profile
            # updates violate Practical Rules §8 (Behaviour Stability: don't shift a profile on one
            # event). Re-learning is the offline, gated batch retrain (ml.retrain_trigger -> ml.train),
            # which re-derives every customer's baseline from the cache and fires on new-data / age /
            # amount-drift (§4). The live monitor (ml.monitor --live) watches for drift between runs.
            audit.log_event(ek, "score", decision,
                            {"model_version": result.get("model_version"),
                             "activity_code": result.get("activity_code"),
                             "risk_score": result.get("risk_score"),
                             "inference_ms": result.get("inference_ms")},
                            transaction_id=t.transaction_id, conn=conn)
            conn.commit()
    except Exception as e:                                   # never fail scoring on persistence
        audit.log.warning("model-score persist failed (decision still returned): %s", e)

    # AFTER responding (off the hot path): deliver the webhook. No per-request retrain is scheduled
    # here — re-learning is the offline batch retrain (§8 stability; see the note above).
    if config.SCORE_WEBHOOK_URL and decision_id is not None:
        background.add_task(webhooks.deliver_and_record, decision_id, dict(result))
        result["webhook"] = "queued"
    return result


class ErrorDetail(BaseModel):
    """FastAPI's standard error envelope — the JSON body of an `HTTPException` (e.g. 401)."""
    detail: str = Field(..., examples=["Invalid API key"])


# A representative 200 body so Swagger shows the ACTUAL fields /score returns. There is no strict
# response_model because the decision carries dynamic nested detail (result/detector_scores); this
# example mirrors a real response verbatim so the Schema/Example Value are accurate, not invented.
_SCORE_200_EXAMPLE = {
    "transaction_id": "12345678", "status": "unsafe", "activity_code": "BF-301",
    "zone": "priority_1_unsafe", "zone_label": "Priority-1 Unsafe",
    "recommended_queue": "auto-block or priority analyst verification",
    "description": "Strong multi-signal behavioural anomaly — high risk; immediate review "
                   "recommended. An amount 1,727.3x the typical amount was detected, which is "
                   "unusual relative to the population baseline. The customer is in cold-start, so "
                   "the transaction was evaluated against the population baseline rather than an "
                   "established personal behavioural profile. Isolation Forest and Autoencoder also "
                   "produced very high anomaly scores.",
    "risk_score": 0.8406, "confidence_score": 0.611,
    "detection_reason": ["transaction amount is far above the population baseline",
                         "Autoencoder anomaly score 1.00", "Isolation Forest anomaly score 0.97",
                         "no learned profile for this customer yet (cold-start) — judged against "
                         "the population baseline"],
    "triggered_signals": ["amount_far_above_usual", "detector:autoencoder", "detector:isoforest",
                          "cold_start"],
    "baseline_info": {"baseline_type": "population", "is_cold_start": True},
    "result": {"risk_score": 0.8406, "confidence": 0.611,
               "detectors": ["isoforest", "autoencoder", "gnn"],
               "detector_scores": {"isoforest": 0.974, "autoencoder": 0.999, "gnn": 0.5},
               "is_cold_start": True},
    "recommended_actions": ["manual_review"], "customer_ref": "id:***2151",
    "model_version": "bf-ensemble-2026.08.03-094608",
    "timestamp": "2026-08-04T16:44:25.437073+00:00", "inference_ms": 131.23,
    "entity_key": "22430372151", "webhook": "queued",
}

# The HTTP responses Swagger advertises for /score, as real JSON bodies (schema + Example Value).
# 400 is intentionally NOT listed — missing credentials return standard 401 (see require_api_key).
# 422 is intentionally NOT overridden — FastAPI auto-documents it with its real HTTPValidationError
# schema (detail[].loc/msg/type), which names the offending field; overriding it would drop that.
_SCORE_RESPONSES = {
    200: {"description": "Behavioural decision (status, activity_code, zone, risk_score, "
                         "description, detection_reason, …).",
          "content": {"application/json": {"example": _SCORE_200_EXAMPLE}}},
    401: {"model": ErrorDetail,
          "description": "Missing or invalid `X-Adhere-Key`. Carries a `WWW-Authenticate: ApiKey` "
                         "challenge header.",
          "headers": {"WWW-Authenticate": {
              "description": "Authentication challenge for the X-Adhere-Key scheme.",
              "schema": {"type": "string",
                         "example": 'ApiKey realm="adhere", header="X-Adhere-Key"'}}},
          "content": {"application/json": {"examples": {
              "missing": {"summary": "Header not supplied",
                          "value": {"detail": "Missing API key"}},
              "invalid": {"summary": "Wrong key",
                          "value": {"detail": "Invalid API key"}}}}}},
}


@app.post("/score", responses=_SCORE_RESPONSES)
def score(t: Txn, background: BackgroundTasks, _key: None = Depends(require_api_key)):
    """The adhere hook — the ONE behavioural scoring path. **Requires header `X-Adhere-Key`.**

    **Responses:** `200` decision · `401` missing/invalid key (JSON `{"detail": …}` + `WWW-Authenticate`)
    · `422` request validation error (FastAPI's `HTTPValidationError`, naming the offending fields).
    (There is no "no key configured" 503: a missing server key fails at **startup**, not per request.)

    transaction -> load the customer's profile/history -> build behavioural features ->
    run the trained ML model -> risk + status + activity code + description -> return.

    **Required (\\*):** `transaction_id`, `amount`, `currency`, `transaction_type`, `account_type`,
    `customer_details` (`customer_name`, `customer_email`), `additional_info` (`ip_address`,
    `location`). Optional: `customer_details.identifier`(+`identifier_type`), `country`,
    `customer_phone`, `origin_account`, `destination_account`, `timestamp`,
    `additional_info.transaction_description`, `run_kyc`. (The `*` markers show under the **Schema**
    tab of the request body; "Example Value" never shows them — that's normal Swagger behaviour.)

    The behavioural anti-fraud MODEL is the only decision engine (no rule/AML scoring lives
    here — AML is a separate service). The decision is persisted to bp_decision for audit and
    delivered by webhook; the customer's statistical profile is updated event-driven off the hot
    path (see _score_with_model).

    **Geo-velocity data requirement (disclaimer):** Geo-velocity is an OPTIONAL behavioural signal and
    is only available when the client provides a valid representation of the customer's ACTUAL location
    for the transaction. Where supported, provide accurate customer `additional_info.latitude` /
    `additional_info.longitude` in the documented format. If location data is missing, malformed,
    invalid, or represents an agent/terminal location rather than the customer's actual location,
    geo-velocity may not be calculated and the system should not be expected to produce a geographic
    anomaly flag. **Missing or invalid location data will not cause the transaction to fail and will not
    affect the existing behavioural fraud decision.** Providing location is never required and is never
    enforced."""
    return _score_with_model(t, background)


class Feedback(BaseModel):
    """A fraud-analyst verdict on a decision the model already made (the §11 feedback loop).

    After the analyst team reviews a `review`/`unsafe` (or auto-blocked) decision, they confirm the
    ground truth here so the model can learn from it at the next retrain and so live precision can be
    measured. Keyed by the `transaction_id` we scored."""
    transaction_id: str = Field(..., examples=["68665786"],
                                description="The transaction_id that was scored (matches bp_decision).")
    verdict: str = Field(..., examples=["genuine"],
                         description="Analyst ground truth: 'genuine' (legitimate) or 'fraud' (confirmed fraud).")
    analyst: str | None = Field(None, examples=["anita"], description="Who gave the verdict (optional).")
    note: str | None = Field(None, examples=["Customer confirmed the transfer by phone."],
                             description="Free-text reason (optional).")

    @field_validator("transaction_id")
    @classmethod
    def _tid(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("transaction_id is required")
        if len(v) > 100:
            raise ValueError("transaction_id too long (max 100)")
        return v

    @field_validator("verdict")
    @classmethod
    def _verdict(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v in ("genuine", "legit", "legitimate", "clean", "ok", "false_positive"):
            return "genuine"
        if v in ("fraud", "fraudulent", "bad", "confirmed_fraud", "true_positive"):
            return "fraud"
        raise ValueError("verdict must be 'genuine' or 'fraud'")


_FEEDBACK_RESPONSES = {
    200: {"description": "Verdict stored (or updated).",
          "content": {"application/json": {"example": {
              "stored": True, "transaction_id": "68665786", "verdict": "genuine",
              "entity_key": "21200336604", "model_decision": "review", "was_update": False,
              "note": "Feeds the next retrain (clean/fraud split) and live precision (ml.monitor --live)."}}}},
    401: _SCORE_RESPONSES[401],
}


@app.post("/feedback", responses=_FEEDBACK_RESPONSES)
def feedback(fb: Feedback, _key: None = Depends(require_api_key)):
    """**Analyst feedback loop (closes PDF §11). Requires header `X-Adhere-Key`.**

    Record the fraud team's confirmed ground truth for a transaction the model scored:
    `verdict='genuine'` (a legitimate transaction — a false positive if we flagged it) or
    `verdict='fraud'` (truly fraudulent). The verdict is stored in `bp_decision_feedback`, keyed by
    `transaction_id` (re-submitting UPDATEs it — latest wins), and drives two things:

    * **Retraining** — at the next retrain, a `genuine` verdict FORCES the transaction into the CLEAN
      training set (so the model learns it as normal even if the source status looked suspicious); a
      `fraud` verdict FORCES it OUT (§7 "never learn confirmed fraud").
    * **Live precision** — `ml.monitor --live` now reports REAL precision (flagged decisions analysts
      later confirmed fraud vs genuine), not just the flag-rate drift proxy.

    We link the verdict to the model's own decision (`entity_key`, `decision`) by looking up the most
    recent `bp_decision` row for that `transaction_id`; feedback for an unknown transaction is still
    stored (entity_key/decision left null) so nothing is lost.
    """
    entity_key = None
    model_decision = None
    try:
        with db.pooled() as conn:
            cur = db.dict_cursor(conn)
            cur.execute(
                "SELECT entity_key, decision FROM bp_decision WHERE transaction_id = %s "
                "ORDER BY scored_at DESC LIMIT 1", (fb.transaction_id,))
            row = cur.fetchone()
            if row:
                entity_key, model_decision = row["entity_key"], row["decision"]
            wc = conn.cursor()
            wc.execute(
                "INSERT INTO bp_decision_feedback "
                "(transaction_id, entity_key, decision, verdict, analyst, note) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (transaction_id) DO UPDATE SET "
                "  entity_key = COALESCE(EXCLUDED.entity_key, bp_decision_feedback.entity_key), "
                "  decision   = COALESCE(EXCLUDED.decision, bp_decision_feedback.decision), "
                "  verdict = EXCLUDED.verdict, analyst = EXCLUDED.analyst, note = EXCLUDED.note, "
                "  updated_at = now() "
                "RETURNING (xmax <> 0) AS was_update",
                (fb.transaction_id, entity_key, model_decision, fb.verdict, fb.analyst, fb.note))
            was_update = bool(wc.fetchone()[0])
            audit.log_event(entity_key or fb.transaction_id, "feedback", fb.verdict,
                            {"analyst": fb.analyst, "model_decision": model_decision},
                            transaction_id=fb.transaction_id, conn=conn)
            conn.commit()
    except Exception as e:
        audit.log.warning("feedback persist failed: %s", e)
        raise HTTPException(503, "could not store feedback (store unavailable) — retry")
    return {"stored": True, "transaction_id": fb.transaction_id, "verdict": fb.verdict,
            "entity_key": entity_key, "model_decision": model_decision, "was_update": was_update,
            "note": "Feeds the next retrain (clean/fraud split) and live precision (ml.monitor --live)."}


@app.get("/thresholds")
def thresholds():
    """The CURRENT dynamic risk zones the fraud team should use — read live from the ACTIVE
    model's calibrated quantile cuts, so they are always up to date and never hard-coded.
    (Same three tiers the /score response returns as `zone`.)"""
    try:
        from ml import codes, registry
        v = registry.active()
        cuts = json.loads((registry.model_dir(v) / "tiering.json").read_text())
        c = cuts.get("cuts", {})
        review, unsafe = float(c["review"]), float(c["unsafe"])
        return {
            "model": v,
            "percentile_levels": cuts.get("percentiles"),
            "zones": {
                "priority_1_unsafe": {"color_code": "🟥", "range": f"score >= {unsafe:.4f}",
                                      "lower": round(unsafe, 4), "status": "unsafe",
                                      "action": codes.ZONES["unsafe"][2]},
                "review_grey": {"color_code": "🟨", "range": f"{review:.4f} <= score < {unsafe:.4f}",
                                "lower": round(review, 4), "upper": round(unsafe, 4),
                                "status": "review", "action": codes.ZONES["review"][2]},
                "clear_normal": {"color_code": "🟩", "range": f"score < {review:.4f}",
                                 "upper": round(review, 4), "status": "safe",
                                 "action": codes.ZONES["safe"][2]},
            },
            "note": "Dynamic quantile boundaries recomputed on every retrain — do not hard-code.",
        }
    except Exception as e:
        raise HTTPException(503, f"thresholds unavailable (no active model?): {e}")


@app.get("/customer/{entity_key}")
def customer_status(entity_key: str):
    """Everything about a customer at this moment: identity, eligibility (met/not),
    what was learned, the retrain state (why it will/won't retrain next), and their
    recent event trail. This is the single 'status of that customer' view."""
    import json
    conn = db.connect()
    cur = db.dict_cursor(conn)
    # One row per currency now — the main view shows the DOMINANT currency, with a compact
    # per-currency breakdown alongside.
    cur.execute("SELECT * FROM bp_user_behaviour_profile WHERE entity_key=%s "
                "ORDER BY total_tx_count DESC", (entity_key,))
    prows = cur.fetchall()
    p = prows[0] if prows else None
    currencies = [{"currency": r["currency"], "profile_status": r["profile_status"],
                   "trusted_by_engine": profile_is_trusted(r)[0],
                   "learned_from_txn_count": r["total_tx_count"],
                   "usual_avg_amount": float(r["avg_amount"] or 0),
                   "biggest_ever": float(r["max_amount"] or 0)} for r in prows]

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
        "currency": p["currency"], "currencies": currencies,   # per-currency breakdown
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
    cur.execute("SELECT entity_key, currency, customer_name, profile_status, confidence_score, "
                "drift_status, tenure_days, total_tx_count, txns_since_build, drift_signal_count, "
                "avg_amount, decayed_avg_amount, max_amount, p95_amount, usual_cities, "
                "top_day_of_week, profile_version, last_retrained_at "
                "FROM bp_user_behaviour_profile WHERE entity_key=%s "
                "ORDER BY total_tx_count DESC", (entity_key,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        raise HTTPException(404, f"no profile for {entity_key} (new/Warming-Up — judged by peers)")
    # One profile per currency; return them all (dominant first).
    return {"entity_key": entity_key, "currencies": [r["currency"] for r in rows], "profiles": rows}


@app.post("/retrain/{entity_key}")
def force_retrain(entity_key: str):
    return retrain.retrain_customer(entity_key)


@app.get("/sync/status")
def sync_status():
    """What the local cache holds and where the ingestion watermark sits.
    Reads only the store — never production."""
    import sync_manager
    return sync_manager.status()


# NOTE: POST /sync (a MANUAL, on-demand production pull) is intentionally DISABLED.
# In production, ingestion is NOT triggered by an HTTP request — it runs as a dedicated
# scheduled background service (`sync_manager.py --loop`, the `sync` compose service /
# a k8s CronJob), the only production reader, on BP_SYNC_INTERVAL_SECONDS. Keeping a
# manual trigger would let anyone pull from production on demand, which does not mirror
# how the system actually runs. Observe ingestion read-only via GET /sync/status.
#
# @app.post("/sync")
# def run_sync(max_rows: int | None = None, chunk_size: int | None = None,
#              full: bool = False):
#     import sync_manager
#     return sync_manager.sync(max_rows=max_rows, chunk_size=chunk_size, full=full)


@app.post("/reload")
def reload_caches():
    """Pick up a newly-PROMOTED model from the registry WITHOUT a restart: drop the cached active
    model so the next /score loads whatever the MLOps side just promoted (the registry handoff)."""
    model_reloaded = None
    try:
        from ml import serve as ml_serve
        ml_serve._active.cache_clear()
        model_reloaded = ml_serve._active().version   # warm the new one now
        audit.log.info("reload: active behavioural model -> %s", model_reloaded)
    except Exception as e:
        audit.log.warning("reload: model reload failed (%s)", e)
    _key_cache["at"] = 0.0                             # force re-read of the active API key
    return {"status": "ok", "active_model": model_reloaded, "api_key_cache": "cleared"}


@app.get("/")
def root():
    return {
        "service": "Adhere Behaviour-Profile",
        "try": {"score_a_txn": "POST /score", "sample_customers": "GET /examples",
                "system_stats": "GET /stats", "inspect_customer": "GET /customer/{entity_key}",
                "api_docs": "GET /docs"},
    }


@app.get("/examples")
def examples():
    """Real customers you can copy-paste to test /score, /customer and /retrain —
    so you don't have to know any account number in advance."""
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cols = ("entity_key, currency, branch_id, origin_account_no, customer_name, profile_status, "
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
    cols = ("entity_key, currency, branch_id, origin_account_no, customer_name, profile_status, "
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
            "entity_key": r["entity_key"], "currency": r["currency"], "name": r["customer_name"],
            "trusted_by_engine": ok, "trust_reason": why,
            "profile_status": r["profile_status"], "confidence": r["confidence_score"],
            "tenure_days": r["tenure_days"], "clean_lifetime_txns": r["lifetime_clean_txns"],
            "confirmed_fraud_txns": r["suspicious_tx_count"],
            "usual_spend_avg_ngn": float(r["avg_amount"] or 0),
            "biggest_ever_ngn": float(r["max_amount"] or 0),
            "detail": f"/customer/{r['entity_key']}",
        })
    return {
        "note": ("Pick any entity_key to inspect (GET /customer/{key}) or score (POST /score). "
                 "`trusted_by_engine` is the live §1 gate: false means the customer is judged "
                 "against their peer group instead of their own profile."),
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
    # "profiles" = distinct CUSTOMERS (the profile is now one row per customer PER
    # CURRENCY, so a raw COUNT(*) would over-count multi-currency customers).
    cur.execute("SELECT COUNT(DISTINCT entity_key) n FROM bp_user_behaviour_profile")
    out["profiles"] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM bp_user_behaviour_profile")
    out["profile_rows"] = cur.fetchone()["n"]          # rows across all currencies
    cur.execute("SELECT currency, COUNT(*) n FROM bp_user_behaviour_profile "
                "GROUP BY currency ORDER BY 2 DESC")
    out["by_currency"] = {r["currency"]: r["n"] for r in cur.fetchall()}
    cur.execute("SELECT profile_status, COUNT(*) n FROM bp_user_behaviour_profile GROUP BY profile_status")
    out["by_status"] = {r["profile_status"]: r["n"] for r in cur.fetchall()}
    cur.execute("SELECT drift_status, COUNT(*) n FROM bp_user_behaviour_profile GROUP BY drift_status")
    out["by_drift"] = {r["drift_status"]: r["n"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) n FROM bp_peer_baseline")
    out["peer_baselines"] = cur.fetchone()["n"]
    conn.close()
    return out

