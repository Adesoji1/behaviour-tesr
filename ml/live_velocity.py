"""
Live-velocity feed (Redis) — real-time short-window transaction history for the ML velocity
features, INDEPENDENT of the batch sync.

Why: `bp_transactions_cache` is only written by the (batch) sync, so a customer's transactions since
the last pull are not visible at score time — real-time bursts across separate /score calls are
missed. This module lets every /score **record** its transaction in Redis and **read back** the
customer's very recent transactions, so `vel_*` / `amt_1h_ratio` / `recency` reflect live activity.

Design guarantees:
  * ENRICHMENT ONLY — it just supplies extra rows to the EXISTING feature builder. It is NOT a
    scoring engine and NOT a rule system; it makes no decisions.
  * FAIL-SAFE — if Redis is disabled (`BP_REDIS_URL` empty) or unreachable, record()/recent() are
    quiet no-ops and scoring continues exactly as before (cache-only). Never raises into /score.
  * TTL-based — each customer key expires after BP_VELOCITY_RETAIN_HOURS and old members are pruned,
    so the window self-cleans; nothing accumulates forever.
  * CONCURRENCY-safe & SHARED — Redis sorted-set ops are atomic and shared across all API workers /
    containers (unlike a per-process in-memory dict).
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from . import config

log = logging.getLogger("ml.live_velocity")

# Same columns _recent_history returns, so a live frame concats cleanly with the cache frame.
_COLS = ["transaction_id", "customer_key", "amount", "transaction_type", "date_created",
         "customer_ip_address", "customer_location", "origin_country", "destination_country",
         "destination_account_no"]

_client = None
_next_try = 0.0          # cooldown clock: after a failure, don't hammer Redis every request
_warned = False


def _fail(e) -> None:
    """A Redis op failed: drop the client and arm the 30s cooldown, so a sustained outage stops
    costing every /score a socket timeout (it skips Redis fast until the cooldown elapses)."""
    global _client, _next_try
    _client = None
    _next_try = time.time() + 30.0
    log.debug("live-velocity: op failed, backing off 30s (%s)", e)


def _epoch(ts) -> float | None:
    try:
        t = pd.to_datetime(ts, utc=True)
        if t is None or pd.isna(t):
            return None
        return float(t.timestamp())
    except Exception:
        return None


def _redis():
    """Return a live client, or None if disabled/unreachable (with a short cooldown so a down Redis
    never adds per-request latency). Never raises."""
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
        log.info("live-velocity: Redis connected (%s)", config.REDIS_URL)
        return _client
    except Exception as e:
        _next_try = time.time() + 30.0          # back off; retry in 30s
        if not _warned:
            log.warning("live-velocity: Redis unavailable (%s) — velocity uses the cache only", e)
            _warned = True
        return None


def record(customer_key, transaction_id, amount, ts) -> None:
    """Record one scored transaction into the customer's live window. No-op / never raises on any
    failure or when disabled. Call this AFTER scoring so a transaction is not counted in its own
    velocity."""
    r = _redis()
    if r is None or customer_key in (None, "", "unknown"):
        return
    epoch = _epoch(ts)
    if epoch is None:
        return
    try:
        key = f"vel:{customer_key}"
        retain = int(config.VELOCITY_RETAIN_HOURS * 3600)
        member = f"{transaction_id}|{float(amount or 0.0)}|{epoch}"
        pipe = r.pipeline()
        pipe.zadd(key, {member: epoch})                       # atomic add
        pipe.zremrangebyscore(key, 0, epoch - retain)         # prune anything older than the window
        pipe.expire(key, retain)                              # TTL so an idle customer self-cleans
        pipe.execute()
    except Exception as e:                                    # enrichment must never break scoring
        _fail(e)


def recent(customer_key, before_ts, hours: float = 24.0) -> pd.DataFrame:
    """Return the customer's recent transactions from the live window as a DataFrame shaped like
    _recent_history (velocity only needs date_created + amount; other columns are None). Empty on
    any failure or when disabled."""
    r = _redis()
    if r is None or customer_key in (None, "", "unknown"):
        return pd.DataFrame()
    before = _epoch(before_ts)
    if before is None:
        return pd.DataFrame()
    try:
        members = r.zrangebyscore(f"vel:{customer_key}", before - hours * 3600, before)
    except Exception as e:
        _fail(e)
        return pd.DataFrame()
    recs = []
    for m in members:
        parts = m.split("|")
        if len(parts) != 3:
            continue
        txid, amt, ep = parts
        try:
            recs.append({"transaction_id": txid, "customer_key": str(customer_key),
                         "amount": float(amt), "transaction_type": None,
                         "date_created": pd.to_datetime(float(ep), unit="s", utc=True),
                         "customer_ip_address": None, "customer_location": None,
                         "origin_country": None, "destination_country": None,
                         "destination_account_no": None})
        except Exception:
            continue
    return pd.DataFrame(recs, columns=_COLS) if recs else pd.DataFrame()
