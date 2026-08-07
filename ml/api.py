"""
Behavioural anti-fraud microservice — FastAPI inference API (Stage 9, serving layer).

POST /score runs the ACTIVE ensemble (hardware-aware) on ONE transaction and returns the
behavioural response contract. This is the exact scoring Adhere calls in production; the same
result is delivered to Adhere by webhook (BF_WEBHOOK_URL) — the model never writes Adhere tables
(read-only DB access, per 1.md). Swagger UI is served at /docs.

    uvicorn ml.api:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, Field

from . import hardware, inference_log, registry, serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("ml.api")

WEBHOOK_URL = os.getenv("BF_WEBHOOK_URL", "").strip()

app = FastAPI(
    title="Adhere Behavioural Anti-Fraud Service",
    version="1.0",
    description="Unsupervised behavioural anomaly scoring per customer. Behavioural detection "
                "only — no AML rules (a separate service owns AML). Read-only DB; result "
                "delivered to Adhere by webhook.",
)


# ---- request schema (drives the Swagger form) -------------------------------
class Account(BaseModel):
    account_number: Optional[str] = Field(None, example="1234567890")
    bank_code: Optional[str] = Field(None, example="63")
    account_type: Optional[str] = Field(None, example="individual")


class CustomerDetails(BaseModel):
    customer_name: Optional[str] = Field(None, example="Barasa O.")
    customer_email: Optional[str] = Field(None, example="b@example.com")
    country: Optional[str] = Field(None, example="Nigeria")
    identifier_type: Optional[str] = Field(None, example="bvn", description="bvn / national_id / kra_pin")
    identifier: Optional[str] = Field(None, description="stable customer identifier (preferred key)")


class AdditionalInfo(BaseModel):
    ip_address: Optional[str] = Field(None, example="102.89.1.10")
    location: Optional[str] = Field(None, example="Lagos, NG")


class ScoreRequest(BaseModel):
    """Either shape works: pass customer_details.identifier (preferred) OR customer_details.bvn."""
    transaction_id: str = Field(..., example="TXN-1001")
    amount: float = Field(..., example=250000.0)
    currency: str = Field("NGN", example="NGN")
    transaction_type: Optional[str] = Field("transfer", example="transfer")
    account_type: Optional[str] = Field(None, example="individual")
    timestamp: Optional[str] = Field(None, example="2026-07-20T14:00:00Z", description="ISO 8601; defaults to now")
    origin_account: Optional[Account] = None
    destination_account: Optional[Account] = None
    customer_details: Optional[CustomerDetails] = None
    additional_info: Optional[AdditionalInfo] = None
    # alternate flat shape
    bvn: Optional[str] = Field(None, description="alternate to customer_details.identifier")

    class Config:
        json_schema_extra = {
            "example": {
                "transaction_id": "TXN-1001",
                "amount": 250000.0, "currency": "NGN",
                "transaction_type": "transfer", "account_type": "individual",
                "timestamp": "2026-07-20T14:00:00Z",
                "origin_account": {"account_number": "1234567890", "bank_code": "63", "account_type": "individual"},
                "destination_account": {"account_number": "0987654321", "bank_code": "43", "account_type": "individual"},
                "customer_details": {"customer_name": "Barasa O.", "customer_email": "b@example.com",
                                     "country": "Nigeria", "identifier_type": "bvn",
                                     "identifier": "22190000001"},
                "additional_info": {"ip_address": "102.89.1.10", "location": "Lagos, NG"},
            }
        }


@app.on_event("startup")
def _warm():
    hardware.log_summary(log)
    try:
        serve._active()            # load + cache the active model once (avoids a cold first request)
        log.info("api: active model loaded — %s", registry.active())
    except Exception as e:         # no model yet — /score will report it per request
        log.warning("api: no active model at startup (%s)", e)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "active_model": registry.active(), "device": hardware.device(),
            "webhook_configured": bool(WEBHOOK_URL)}


def _post_webhook(body: dict):
    tid = body.get("transaction_id")
    try:
        r = httpx.post(WEBHOOK_URL, json=body, timeout=5.0)
        inference_log.log_delivery(tid, WEBHOOK_URL, ok=r.is_success, detail=f"HTTP {r.status_code}")
    except Exception as e:
        log.warning("webhook delivery failed: %s", e)
        inference_log.log_delivery(tid, WEBHOOK_URL, ok=False, detail=str(e))


@app.post("/score")
def score(req: ScoreRequest, background: BackgroundTasks):
    """Score one transaction against the customer's learned behaviour. Returns the behavioural
    contract (already timed + audit-logged by serve) and, if BF_WEBHOOK_URL is set, also delivers
    it to Adhere by webhook — which is what writes behavioral_analysis on Adhere's side."""
    result = serve.score_payload(req.model_dump(exclude_none=True))
    if WEBHOOK_URL:
        background.add_task(_post_webhook, result)   # async delivery, off the response path
        result["webhook"] = "queued"
    log.info("score %s -> %s %s risk=%.3f in %.2fms", result["transaction_id"],
             result["status"], result["activity_code"], result["risk_score"], result["inference_ms"])
    return result
