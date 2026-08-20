"""
Geo prev-observation store (Redis) — the customer's LAST resolved (lat, lon, time), for geo-velocity.

One tiny key per customer, `geo:{customer_key}` = "lat|lon|epoch", written AFTER a /score resolves a
current location and READ before the next one, so geo-velocity compares the current transaction with
the customer's previous known point (impossible-travel). Independent of the batch cache.

Design guarantees (identical to live_velocity):
  * ENRICHMENT ONLY — supplies a previous point; makes no decision, is not a rule.
  * FAIL-SAFE — disabled (`BP_REDIS_URL` empty) or Redis unreachable -> previous()/record() are quiet
    no-ops (previous() returns None) and scoring continues unchanged. Never raises into /score.
  * ATOMIC / concurrency-safe — a single `SET key value EX ttl`; concurrent same-customer writes are
    last-writer-wins, which is exactly "the most recent point" and can only SHRINK a later elapsed
    (conservative — never inflates velocity).
  * TTL-based — the key expires after BF_GEO_PREV_RETAIN_HOURS; nothing accumulates forever.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from . import config

log = logging.getLogger("ml.geo_state")

_client = None
_next_try = 0.0
_warned = False


def _fail(e) -> None:
    global _client, _next_try
    _client = None
    _next_try = time.time() + 30.0
    log.debug("geo-state: op failed, backing off 30s (%s)", e)


def _epoch(ts):
    try:
        t = pd.to_datetime(ts, utc=True)
        if t is None or pd.isna(t):
            return None
        return float(t.timestamp())
    except Exception:
        return None


def _redis():
    """A live client, or None if disabled/unreachable (short cooldown so a down Redis adds no
    per-request latency). Never raises. Reuses config.REDIS_URL (the same instance as live-velocity)."""
    global _client, _next_try, _warned
    if not config.REDIS_URL:
        return None
    if _client is not None:
        return _client
    if time.time() < _next_try:
        return None
    try:
        import redis
        c = redis.Redis.from_url(config.REDIS_URL, socket_timeout=0.25,
                                 socket_connect_timeout=0.25, decode_responses=True)
        c.ping()
        _client = c
        return _client
    except Exception as e:
        _next_try = time.time() + 30.0
        if not _warned:
            log.warning("geo-state: Redis unavailable (%s) — geo prev-observation disabled", e)
            _warned = True
        return None


def previous(customer_key):
    """Return the customer's last stored {lat, lon, epoch}, or None. Empty on any failure/disabled."""
    r = _redis()
    if r is None or customer_key in (None, "", "unknown"):
        return None
    try:
        raw = r.get(f"geo:{customer_key}")
    except Exception as e:
        _fail(e)
        return None
    if not raw:
        return None
    try:
        lat, lon, ep = raw.split("|")
        return {"lat": float(lat), "lon": float(lon), "epoch": float(ep)}
    except Exception:
        return None


def record(customer_key, lat, lon, ts) -> None:
    """Store the customer's current resolved point as the new 'previous'. Call AFTER reading previous()
    (and after scoring). No-op / never raises on any failure or when disabled or coords/ts are bad."""
    r = _redis()
    if r is None or customer_key in (None, "", "unknown") or lat is None or lon is None:
        return
    epoch = _epoch(ts)
    if epoch is None:
        return
    try:
        retain = int(config.GEO_PREV_RETAIN_HOURS * 3600)
        r.set(f"geo:{customer_key}", f"{float(lat)}|{float(lon)}|{epoch}", ex=retain)
    except Exception as e:
        _fail(e)
