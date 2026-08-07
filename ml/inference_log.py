"""
Compliance audit log — an immutable, append-only record of every inference and delivery.

Every /score decision is written here (customer reference, decision, risk, model version,
timestamp, and how long it took) so there is a defensible history for compliance, plus a line
for each webhook delivery. Written as daily JSONL under artifacts/inference_log/. The model has
READ-ONLY database access, so this file — not an Adhere table — is the model's own record; the
decision itself is delivered to Adhere by webhook (which then writes behavioral_analysis).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from . import config

log = logging.getLogger("ml.inference_log")
_lock = threading.Lock()


def _append(record: dict) -> None:
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        p = config.DIR_INFERENCE_LOG / f"inference-{day}.jsonl"
        line = json.dumps(record, default=str)
        with _lock:
            with open(p, "a") as f:
                f.write(line + "\n")
    except Exception as e:                      # auditing must never break scoring
        log.warning("audit log write failed: %s", e)


def log_inference(response: dict) -> None:
    """One line per scored transaction: who, what decision, how risky, which model, how long."""
    _append({
        "event": "inference",
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": response.get("timestamp"),
        "transaction_id": response.get("transaction_id"),
        "customer_ref": response.get("customer_ref"),            # masked identifier
        "status": response.get("status"),
        "activity_code": response.get("activity_code"),
        "risk_score": response.get("risk_score"),
        "confidence_score": response.get("confidence_score"),
        "triggered_signals": response.get("triggered_signals"),
        "is_cold_start": (response.get("result") or {}).get("is_cold_start"),
        "model_version": response.get("model_version"),
        "inference_ms": response.get("inference_ms"),
    })


def log_delivery(transaction_id, target: str, ok: bool, detail: str = "") -> None:
    """One line per webhook delivery attempt (the hand-off that writes behavioral_analysis)."""
    _append({
        "event": "webhook_delivery",
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "transaction_id": transaction_id,
        "target": target,
        "delivered": bool(ok),
        "detail": detail,
    })
