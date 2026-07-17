#!/usr/bin/env python3
"""
Webhook OUTBOX relay — guaranteed, retried delivery of scoring decisions.

Why this exists
---------------
POST /score persists the decision AND a webhook_status='pending' marker in the SAME
Postgres transaction, then tries to deliver the webhook inline (a background task) for
speed. If that inline attempt is lost — the API process crashes between committing the
decision and finishing the POST — the decision is safe in bp_decision but its delivery
would never happen. This relay closes that gap: it periodically redelivers every
'pending' decision that is due, with exponential backoff, until it succeeds ('sent') or
exhausts the retry budget ('dead' / dead-letter). At-least-once delivery, and because
the outbox marker lives in Postgres (already our source of truth) it needs NO extra
infrastructure — no Redis, no queue.

Where it runs
-------------
Inside the existing `sync` service (see sync_manager.run_forever), on its own cadence.
It is safe to run exactly one instance; row-level FOR UPDATE SKIP LOCKED means even if
two ran, no decision would be delivered twice from the relay.

Run standalone (testing/ops):
    python webhook_relay.py --once     # one sweep, print counts
    python webhook_relay.py --loop     # forever, every BP_WEBHOOK_RELAY_INTERVAL_SECONDS
"""
import argparse
import json
import time

import audit
import config
import db
import webhooks


def _payload_from_row(r: dict) -> dict:
    """Rebuild the exact lean webhook payload /score sends, from the stored decision.
    bp_decision.fired_rules is JSON [{rule, severity, details}, ...]; the webhook carries
    only {rule, severity} (same shape as the /score HTTP response)."""
    fired = json.loads(r["fired_rules"] or "[]")
    lean = [{"rule": f.get("rule"), "severity": f.get("severity")} for f in fired]
    return {
        "entity_key": r["entity_key"],
        "transaction_id": r["transaction_id"],
        "decision": r["decision"],
        "fired_rules": lean,
        "judged_against": r["judged_against"],
        "latency_ms": r["latency_ms"],
    }


def relay_once(limit: int | None = None) -> dict:
    """One sweep: deliver every 'pending' decision whose next_attempt_at is due.

    Candidates are gathered in one short read, then each row is processed in its OWN
    short transaction (lock -> re-check due -> deliver -> advance lifecycle -> commit).
    Per-row transactions keep locks short despite network I/O, and FOR UPDATE SKIP
    LOCKED means a row currently held by the inline /score delivery is simply skipped."""
    if not config.SCORE_WEBHOOK_URL:
        return {"picked": 0, "sent": 0, "retry": 0, "dead": 0, "reason": "no webhook url"}
    limit = limit or config.WEBHOOK_RELAY_BATCH
    counts = {"picked": 0, "sent": 0, "retry": 0, "dead": 0}

    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM bp_decision "
                    " WHERE webhook_status='pending' AND webhook_next_attempt_at <= now() "
                    " ORDER BY webhook_next_attempt_at LIMIT %s", (limit,))
        ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    for decision_id in ids:
        conn = db.connect()
        try:
            cur = db.dict_cursor(conn)
            # Re-check under a row lock: another sweep or the inline task may have taken
            # or advanced it since we listed the ids. SKIP LOCKED avoids blocking.
            cur.execute(
                "SELECT id, entity_key, transaction_id, decision, fired_rules, "
                "judged_against, latency_ms, webhook_attempts "
                "FROM bp_decision "
                " WHERE id=%s AND webhook_status='pending' AND webhook_next_attempt_at <= now() "
                " FOR UPDATE SKIP LOCKED", (decision_id,))
            r = cur.fetchone()
            if r is None:
                conn.rollback()
                continue
            counts["picked"] += 1
            payload = _payload_from_row(r)
            rec = webhooks.deliver(payload)
            new_status = webhooks.apply_outcome(conn, r["id"], payload, rec,
                                                r["webhook_attempts"] or 0)
            conn.commit()
            counts["sent" if new_status == "sent"
                   else "dead" if new_status == "dead" else "retry"] += 1
        except Exception as e:                       # never let one bad row stop the sweep
            audit.log.warning("webhook relay: decision %s failed mid-sweep: %s", decision_id, e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    return counts


def run_forever() -> None:
    """Sweep on a fixed interval, forever. Catches every error so the loop never dies."""
    interval = max(float(config.WEBHOOK_RELAY_INTERVAL_SECONDS), 1.0)
    audit.log.info("webhook relay START — every %.0fs, batch=%s, max_attempts=%s, "
                   "backoff base=%.0fs cap=%.0fs, grace=%.0fs",
                   interval, config.WEBHOOK_RELAY_BATCH, config.WEBHOOK_MAX_ATTEMPTS,
                   config.WEBHOOK_BACKOFF_BASE_SECONDS, config.WEBHOOK_BACKOFF_CAP_SECONDS,
                   config.WEBHOOK_RELAY_GRACE_SECONDS)
    while True:
        t0 = time.monotonic()
        try:
            out = relay_once()
            if out.get("picked"):
                audit.log.info("webhook relay sweep: %s", out)
        except Exception as e:                        # never let the loop die
            audit.log.exception("webhook relay sweep FAILED (will retry next interval): %s", e)
        time.sleep(max(interval - (time.monotonic() - t0), 0.5))


def main() -> int:
    ap = argparse.ArgumentParser(description="Webhook outbox relay: guaranteed, retried "
                                             "delivery of scoring decisions.")
    ap.add_argument("--loop", action="store_true", help="run forever on a fixed interval")
    ap.add_argument("--once", action="store_true", help="run one sweep and print counts")
    ap.add_argument("--limit", type=int, default=None, help="max rows for this sweep")
    a = ap.parse_args()
    if a.loop:
        run_forever()
        return 0
    out = relay_once(limit=a.limit)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
