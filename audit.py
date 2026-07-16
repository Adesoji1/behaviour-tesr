#!/usr/bin/env python3
"""
Accountability logging. Every meaningful step is written to BOTH:
  * stdout (structured, so Docker / k8s / log collectors capture it), and
  * the bp_event_log table (queryable per customer via GET /customer/{key}).

So nothing the system does is invisible: a transaction scored, why a customer
was or wasn't retrained, and any retrain failure — all leave a trail.
"""
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("behaviour")


def log_event(entity_key, event_type, outcome=None, detail=None,
              transaction_id=None, conn=None):
    """Record one event to stdout + bp_event_log. Reuses `conn` if given
    (caller commits); otherwise opens a short-lived connection. Never raises —
    logging must not break scoring."""
    log.info("%s entity=%s outcome=%s %s", event_type, entity_key, outcome,
             json.dumps(detail, default=str) if detail else "")
    try:
        # imported lazily so `audit` stays dependency-free at import time
        import db
        own = conn is None
        c = conn or db.connect()
        cur = c.cursor()
        cur.execute(
            "INSERT INTO bp_event_log (entity_key, transaction_id, event_type, outcome, detail) "
            "VALUES (%s,%s,%s,%s,%s)",
            (entity_key, transaction_id, event_type, outcome,
             json.dumps(detail, default=str) if detail is not None else None),
        )
        if own:
            c.commit()
            c.close()
    except Exception as e:
        log.warning("audit persist failed: %s", e)
