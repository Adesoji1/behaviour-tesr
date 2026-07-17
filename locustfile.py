"""
Load test for POST /score — the behaviour-scoring hook.

Everything is env-driven; results (CSV + HTML + a JSON summary + a run log) are written
under logs/loadtest/ (bind-mounted to the host). Run it with the compose `loadtest`
profile — see docker-compose.yaml / README.

  Measured metric: the /score HTTP round-trip = "transaction received -> decision returned"
  (the synchronous work our system controls). The webhook ("send the event") is delivered
  AFTER the response as a background task + outbox relay, so it does not add to this number;
  its delivery time is recorded separately in bp_webhook_delivery.

Env knobs (with defaults):
  SCORE_SLA_MS            600    the target ceiling; requests slower than this are counted
                                 as SLA breaches (reported, not treated as HTTP failures).
  SCORE_LOAD_ENTITY_KEYS  ""     comma-separated real entity keys to score. If empty, the
                                 file fetches a sample from GET /customers at start.
  SCORE_WAIT_MIN / _MAX   0/0.05 per-user think time (seconds) between requests.
  SCORE_ABNORMAL_PCT      0.10   fraction of transactions made deliberately large/foreign
                                 so the amount / cross-border rules are exercised too.
Locust's own knobs (LOCUST_USERS, LOCUST_SPAWN_RATE, LOCUST_RUN_TIME, --host) come from
the compose command / env.
"""
import json
import os
import random
import uuid
from datetime import datetime

from locust import HttpUser, between, events, task

SLA_MS = float(os.getenv("SCORE_SLA_MS", "600"))
ABNORMAL_PCT = float(os.getenv("SCORE_ABNORMAL_PCT", "0.10"))
LOG_DIR = os.getenv("LOADTEST_LOG_DIR", "/mnt/logs/loadtest")

ENTITY_KEYS = [k.strip() for k in os.getenv("SCORE_LOAD_ENTITY_KEYS", "").split(",") if k.strip()]
_FALLBACK_KEYS = ["231:5510027677", "232:9077799070"]

_sla_breaches = 0
_counted = 0


@events.test_start.add_listener
def _resolve_keys(environment, **_):
    """Use env-provided keys, else fetch a live sample from GET /customers, else fall back."""
    global ENTITY_KEYS
    if ENTITY_KEYS:
        print(f"[loadtest] {len(ENTITY_KEYS)} entity keys from SCORE_LOAD_ENTITY_KEYS")
        return
    try:
        import requests
        host = (environment.host or "http://behaviour-profile:8080").rstrip("/")
        d = requests.get(f"{host}/customers?limit=100", timeout=15).json()
        rows = d.get("customers", []) if isinstance(d, dict) else (d or [])
        ENTITY_KEYS = [c["entity_key"] for c in rows if c.get("entity_key")]
    except Exception as e:                    # pragma: no cover
        print(f"[loadtest] key fetch failed ({e})")
    if not ENTITY_KEYS:
        ENTITY_KEYS = _FALLBACK_KEYS
    print(f"[loadtest] {len(ENTITY_KEYS)} entity keys | SLA {SLA_MS:.0f}ms")


@events.request.add_listener
def _track_sla(request_type, name, response_time, response_length, exception, **_):
    global _sla_breaches, _counted
    if exception is None:                     # only successful HTTP responses count toward SLA
        _counted += 1
        if response_time > SLA_MS:
            _sla_breaches += 1


@events.test_stop.add_listener
def _write_summary(environment, **_):
    s = environment.stats.total
    p95 = s.get_response_time_percentile(0.95)
    summary = {
        "requests": s.num_requests,
        "http_failures": s.num_failures,
        "rps": round(s.total_rps, 1),
        "latency_ms": {
            "p50": s.get_response_time_percentile(0.50),
            "p95": p95,
            "p99": s.get_response_time_percentile(0.99),
            "max": s.max_response_time,
            "avg": round(s.avg_response_time, 1),
        },
        "sla_ms": SLA_MS,
        "sla_breaches": _sla_breaches,
        "sla_breach_pct": round(100 * _sla_breaches / max(_counted, 1), 2),
        "sla_met_at_p95": (p95 or 0) <= SLA_MS,
        "finished_at": datetime.utcnow().isoformat() + "Z",
    }
    print("[loadtest] SUMMARY:", json.dumps(summary))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        print(f"[loadtest] wrote {LOG_DIR}/summary.json")
    except Exception as e:                    # pragma: no cover
        print(f"[loadtest] could not write summary: {e}")


class ScoreUser(HttpUser):
    wait_time = between(float(os.getenv("SCORE_WAIT_MIN", "0.0")),
                        float(os.getenv("SCORE_WAIT_MAX", "0.05")))

    @task
    def score(self):
        ek = random.choice(ENTITY_KEYS)
        branch_id, acct = ek.split(":", 1)
        abnormal = random.random() < ABNORMAL_PCT
        if abnormal:                          # exercise amount / cross-border / new-beneficiary
            amount = random.randint(1_000_000, 20_000_000)
            dest_country = random.choice(["US", "GB", "KP"])
            benef = f"NEW-{uuid.uuid4().hex[:8]}"
        else:                                 # ordinary in-pattern-ish transaction
            amount = random.choice([2000, 5000, 8000, 12000, 25000])
            dest_country = "NG"
            benef = random.choice(["0123965972", "BENE-A", "BENE-B"])
        payload = {
            "branch_id": int(branch_id), "origin_account_no": acct,
            "amount": amount, "currency": "NGN",
            "destination_account_no": benef,
            "customer_location": "street, Lagos, State",
            "origin_country": "NG", "destination_country": dest_country,
            "transaction_id": f"LOAD-{uuid.uuid4().hex[:12]}",
            "ts": datetime.utcnow().isoformat(),
        }
        with self.client.post("/score", json=payload, name="POST /score",
                              catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
