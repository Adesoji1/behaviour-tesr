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


# --- ERROR-and-above -> Slack (channel: behavioural-analysis-errorlogs). ------------------------
# Anita's requirement: the behaviour-analysis service's error logs must reach Slack. This forwarder
# runs IN-PROCESS, so it works on Render where watchdog.sh and the sync worker are NOT deployed.
# It is: dependency-free (stdlib urllib, like sync_manager._post_slack), NON-BLOCKING (a bounded
# queue drained by one daemon thread — the request/log path never waits on the network), THROTTLED
# (the same message is not re-sent within BP_SLACK_ERROR_COOLDOWN seconds), and utterly FAIL-SAFE
# (never raises, never loops). It NO-OPS when no webhook is configured, so nothing changes locally
# until BP_/BF_SLACK_WEBHOOK_URL is set. Attached to the ROOT logger, so behaviour + ml.* both feed it.
import queue as _queue
import threading as _threading
import time as _time
import traceback as _traceback
import urllib.request as _urlreq

_SLACK_Q: "_queue.Queue" = _queue.Queue(maxsize=200)   # bounded: a storm drops, never blocks/floods
_SLACK_LAST: dict = {}                                  # message signature -> last-sent epoch (throttle)
_SLACK_COOLDOWN = float(os.getenv("BP_SLACK_ERROR_COOLDOWN", "300"))


def _slack_url() -> str:
    """The configured webhook, or '' when unset. Reads the SAME var everywhere (BF_/BP_)."""
    try:
        from ml import config as _cfg
        return getattr(_cfg, "SLACK_WEBHOOK_URL", "") or ""
    except Exception:
        return (os.getenv("BF_SLACK_WEBHOOK_URL") or os.getenv("BP_SLACK_WEBHOOK_URL", "") or "").strip()


def _slack_worker() -> None:
    """Drain the queue and POST each message. Swallows everything — must never raise or log at
    ERROR (that would loop back into this handler)."""
    while True:
        text = _SLACK_Q.get()
        try:
            url = _slack_url()
            if url:
                req = _urlreq.Request(url, data=json.dumps({"text": text}).encode(),
                                      headers={"Content-Type": "application/json"}, method="POST")
                _urlreq.urlopen(req, timeout=5).close()
        except Exception:
            pass
        finally:
            _SLACK_Q.task_done()


class _SlackErrorHandler(logging.Handler):
    """Forward ERROR/CRITICAL records to Slack, throttled and off the hot path."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR or not _slack_url():
                return
            msg = record.getMessage()
            sig = record.name + ":" + msg[:200]
            now = _time.time()
            if now - _SLACK_LAST.get(sig, 0.0) < _SLACK_COOLDOWN:
                return
            if len(_SLACK_LAST) > 1000:                 # bound the throttle map
                _SLACK_LAST.clear()
            _SLACK_LAST[sig] = now
            loc = os.path.basename(record.pathname) + ":" + str(record.lineno)
            text = (":rotating_light: *behaviour-profile " + record.levelname + "*\n"
                    "`" + record.name + "` (" + loc + ") — " + msg[:800])
            if record.exc_info:
                tb = "".join(_traceback.format_exception(*record.exc_info))
                text += "\n```" + tb[-1500:] + "```"
            _SLACK_Q.put_nowait(text)                   # non-blocking; drops if the queue is full
        except Exception:
            pass                                        # logging must never raise


try:
    _slack_handler = _SlackErrorHandler()
    _slack_handler.setLevel(logging.ERROR)
    _threading.Thread(target=_slack_worker, name="slack-error-relay", daemon=True).start()
    logging.getLogger().addHandler(_slack_handler)
    log.info("audit: ERROR-log -> Slack handler attached (%s)",
             "webhook configured" if _slack_url() else "no webhook yet — no-op until set")
except Exception as e:                        # pragma: no cover
    log.warning("audit: Slack error handler not attached (%s)", e)


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
