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
import logging.handlers
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("behaviour")

# Persist EVERYTHING to a plain-text audit file as well as stdout, so a Dockerised run leaves an
# on-disk trail we can grep after the fact. Attached to the ROOT logger, so the service AND the
# in-process model (ml.*) both land here. Path is bind-mounted to ./logs on the host; rotated so
# it never grows unbounded. Never fatal — if the dir isn't writable we just keep stdout.
_LOG_FILE = os.getenv("BP_LOG_FILE", "/app/logs/behaviour.log")
try:
    os.makedirs(os.path.dirname(_LOG_FILE) or ".", exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(_fh)
    log.info("audit: file logging -> %s", _LOG_FILE)
except Exception as e:                       # pragma: no cover
    log.warning("audit: file log handler not attached (%s) — stdout only", e)


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
