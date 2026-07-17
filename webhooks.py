#!/usr/bin/env python3
"""
Outbound webhook delivery for scoring decisions.

POST /score returns the decision in its HTTP response AND (if configured) delivers
the same decision to a webhook. The webhook is sent *after* the response is returned
(a FastAPI background task), so it never slows the caller down.

Design:
  * stdlib only (urllib) — no extra dependency for one small POST.
  * strictly timeout-bounded (config.SCORE_WEBHOOK_TIMEOUT).
  * NEVER raises — a webhook failure must not affect scoring or the audit record.
    It returns a status the caller records in bp_decision instead.
  * Optional HMAC-SHA256 signature (config.SCORE_WEBHOOK_SECRET) so the receiver can
    verify the payload came from us.
"""
import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import audit
import config


def deliver(payload: dict) -> dict:
    """POST `payload` as JSON to the configured webhook.

    Returns a delivery record (never raises — a webhook failure must not affect scoring):
      {status, http_status, detail, signed, latency_ms, url}
      status: disabled | sent | failed
    """
    url = config.SCORE_WEBHOOK_URL
    if not url:
        return {"status": "disabled", "http_status": None,
                "detail": "no BP_SCORE_WEBHOOK_URL configured",
                "signed": False, "latency_ms": None, "url": None}

    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": "behaviour-profile/score-webhook"}
    signed = bool(config.SCORE_WEBHOOK_SECRET)
    if signed:
        sig = hmac.new(config.SCORE_WEBHOOK_SECRET.encode("utf-8"),
                       body, hashlib.sha256).hexdigest()
        headers["X-Behaviour-Signature"] = f"sha256={sig}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=config.SCORE_WEBHOOK_TIMEOUT) as resp:
            code = resp.getcode()
        ms = round((time.perf_counter() - t0) * 1000, 2)
        ok = 200 <= code < 300
        (audit.log.info if ok else audit.log.warning)(
            "webhook %s entity=%s txn=%s -> HTTP %s (%sms)",
            "sent" if ok else "non-2xx", payload.get("entity_key"),
            payload.get("transaction_id"), code, ms)
        return {"status": "sent" if ok else "failed", "http_status": code,
                "detail": f"HTTP {code}", "signed": signed, "latency_ms": ms, "url": url}
    except urllib.error.HTTPError as e:
        ms = round((time.perf_counter() - t0) * 1000, 2)
        audit.log.warning("webhook HTTPError entity=%s -> %s (%sms)",
                          payload.get("entity_key"), e.code, ms)
        return {"status": "failed", "http_status": e.code, "detail": f"HTTP {e.code}",
                "signed": signed, "latency_ms": ms, "url": url}
    except Exception as e:  # timeout, DNS, connection refused, ...
        ms = round((time.perf_counter() - t0) * 1000, 2)
        audit.log.warning("webhook failed entity=%s -> %s (%sms)",
                          payload.get("entity_key"), e, ms)
        return {"status": "failed", "http_status": None, "detail": str(e)[:300],
                "signed": signed, "latency_ms": ms, "url": url}


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff for the Nth attempt (1-based): min(base*2^(n-1), cap) with
    +-20% jitter so many failing decisions don't retry in lockstep (thundering herd)."""
    base = config.WEBHOOK_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))
    base = min(base, config.WEBHOOK_BACKOFF_CAP_SECONDS)
    return round(base * (0.8 + 0.4 * random.random()), 2)


def apply_outcome(conn, decision_id: int, payload: dict, rec: dict, prev_attempts: int) -> str:
    """Record ONE delivery attempt and advance the decision's outbox lifecycle, in the
    caller's transaction (the caller commits). Appends an append-only bp_webhook_delivery
    row and updates bp_decision:
        sent               -> status='sent',    next_attempt_at=NULL          (done)
        failed, budget left -> status='pending', next_attempt_at=now()+backoff (retry)
        failed, budget spent-> status='dead',    next_attempt_at=NULL          (dead-letter)
    Returns the new bp_decision.webhook_status. Shared by the inline /score delivery and
    the relay, so both advance the lifecycle identically."""
    attempts = (prev_attempts or 0) + 1
    if rec["status"] == "sent":
        new_status, next_at = "sent", None
    elif attempts >= config.WEBHOOK_MAX_ATTEMPTS:
        new_status, next_at = "dead", None
    else:
        new_status = "pending"
        next_at = datetime.utcnow() + timedelta(seconds=backoff_seconds(attempts))
    cur = conn.cursor()
    cur.execute("UPDATE bp_decision SET webhook_status=%s, webhook_detail=%s, "
                "webhook_attempts=%s, webhook_next_attempt_at=%s WHERE id=%s",
                (new_status, rec["detail"], attempts, next_at, decision_id))
    cur.execute(
        "INSERT INTO bp_webhook_delivery (decision_id, entity_key, transaction_id, url, "
        "status, http_status, detail, signed, latency_ms) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (decision_id, payload.get("entity_key"), payload.get("transaction_id"),
         rec["url"], rec["status"], rec["http_status"], rec["detail"],
         rec["signed"], rec["latency_ms"]),
    )
    if new_status == "dead":
        audit.log.error("webhook DEAD-LETTER decision=%s entity=%s txn=%s after %s attempts: %s",
                        decision_id, payload.get("entity_key"),
                        payload.get("transaction_id"), attempts, rec["detail"])
    return new_status


def deliver_and_record(decision_id: int, payload: dict) -> None:
    """Inline (fast-path) delivery, run AFTER /score responds. Delivers once and advances
    the outbox lifecycle via apply_outcome — so a FAILURE here leaves the row 'pending'
    with a short backoff for the relay to retry, and a SUCCESS marks it 'sent'. Locks the
    row FOR UPDATE so it never races the relay. Uses its own connection; never raises."""
    import db
    rec = deliver(payload)
    try:
        conn = db.connect()
        try:
            cur = conn.cursor()
            # Lock the row so the relay's SKIP LOCKED sweep won't also grab it right now.
            cur.execute("SELECT webhook_attempts FROM bp_decision WHERE id=%s FOR UPDATE",
                        (decision_id,))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return
            apply_outcome(conn, decision_id, payload, rec, row[0] or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        audit.log.warning("could not record webhook delivery for decision %s: %s", decision_id, e)
