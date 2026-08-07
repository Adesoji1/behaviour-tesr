#!/usr/bin/env python3
"""
Behavioural Anti-Fraud — demo / light load runner.

Sends REAL transactions from the dataset to the EXISTING Docker-hosted `POST /score` over HTTP
(the actual production scoring path). It does NOT duplicate scoring, does NOT compute any behavioural
features (amt_z, velocity, risk_score, …) — it sends RAW transaction fields only; the service
computes everything and returns the observed result. No expected label is assumed.

It:
  * reads the real CSV, writes a derived `demo_transactions.csv` (the payloads it will send) WITHOUT
    modifying the original file,
  * maps CSV fields to the real FastAPI `Txn` schema (normalising channel/account_type to the
    values the validator accepts — see NOTE below),
  * sends sequentially in CSV order (customer-grouped) so repeated same-customer transactions
    exercise the Redis live-velocity feature,
  * captures each response (status, activity_code, zone, risk_score, confidence_score,
    detection_reason, triggered_signals, model_version, inference_ms, HTTP status) to
    `demo_results.csv`,
  * samples `docker stats` + container restart counts in the background,
  * prints a final summary (counts, decisions, latency percentiles, RPS, errors, container health).

NOTE on normalisation (payload plumbing, NOT feature fabrication): the real data has ~20
transaction_type values and several account_type values, but `/score` accepts only
{transfer,ussd,web,card} and {individual,corporate}. We map the channel/account_type to the nearest
accepted value so rows reach the model instead of 422-ing. The mapping is transparent (below) and
written into demo_transactions.csv so you can see exactly what was sent.

Usage (see demo/README for the safe run procedure):
    python demo/demo_runner.py --limit 1000 --key "$BP_API_KEY"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_CSV = "/home/adesoji/adhere-backend/behaviour_profile_build/data/transactions.csv"
DEFAULT_URL = "http://localhost:8080/score"

# --- field normalisation (to the values the /score validator accepts) ------------------------------
def norm_ttype(v: str) -> str:
    # /score now accepts the RAW channel value and canonicalises it internally (ml.config), so send
    # it raw — the model's type_rare then compares against the customer's REAL learned vocabulary.
    # (Mapping to a fixed {transfer,ussd,web,card} enum would corrupt type_rare.) Just trim+lower.
    return (v or "").strip().lower()


def norm_atype(v: str) -> str:
    return "corporate" if (v or "").strip().lower() == "corporate" else "individual"


def build_payload(row: dict) -> dict | None:
    """Map one CSV row to the real Txn schema. Returns None if a HARD-required field is missing/invalid
    (we skip those rather than fabricate). Sends RAW fields only — no computed features."""
    try:
        amount = float(row.get("amount") or 0)
    except ValueError:
        return None
    name = (row.get("customer_name") or "").strip()
    email = (row.get("customer_email") or "").strip()
    ip = (row.get("customer_ip_address") or "").strip()
    loc = (row.get("customer_location") or "").strip()
    txid = (row.get("transaction_id") or "").strip()
    if not (txid and amount > 0 and name and email and ip and loc):
        return None
    ident = (row.get("identifier") or row.get("bvn") or "").strip() or None
    payload = {
        "transaction_id": txid[:100],
        "amount": amount,
        "currency": (row.get("currency") or "NGN").strip().upper()[:3],
        "transaction_type": norm_ttype(row.get("transaction_type")),
        "account_type": norm_atype(row.get("account_type")),
        "customer_details": {
            "customer_name": name[:200],
            "customer_email": email[:254],
            "identifier": ident,
            "identifier_type": "bvn" if ident else None,
            "country": (row.get("origin_country") or "").strip() or None,
        },
        "additional_info": {"ip_address": ip, "location": loc},
    }
    ts = (row.get("date_created") or "").strip()
    if ts:
        payload["timestamp"] = ts        # real transaction time → realistic velocity windows
    on = (row.get("origin_account_no") or "").strip()
    if on:
        payload["origin_account"] = {"account_number": on,
                                     "account_type": norm_atype(row.get("origin_account_type"))}
    dn = (row.get("destination_account_no") or "").strip()
    if dn:
        payload["destination_account"] = {"account_number": dn,
                                          "bank_code": (row.get("destination_bank_code") or "").strip() or None}
    return payload


# --- background docker monitor ---------------------------------------------------------------------
class DockerMonitor(threading.Thread):
    def __init__(self, containers: list[str], out_path: str, interval: float = 5.0):
        super().__init__(daemon=True)
        self.containers, self.out_path, self.interval = containers, out_path, interval
        self._stopev = threading.Event()

    def _restart_counts(self) -> dict:
        counts = {}
        for c in self.containers:
            try:
                r = subprocess.run(["docker", "inspect", "-f", "{{.RestartCount}}", c],
                                   capture_output=True, text=True, timeout=5)
                counts[c] = int((r.stdout or "0").strip() or 0)
            except Exception:
                counts[c] = -1
        return counts

    def run(self):
        self.start_restarts = self._restart_counts()
        with open(self.out_path, "w") as f:
            f.write("ts,name,cpu_pct,mem\n")
            while not self._stopev.is_set():
                try:
                    r = subprocess.run(
                        ["docker", "stats", "--no-stream", "--format",
                         "{{.Name}},{{.CPUPerc}},{{.MemUsage}}"] + self.containers,
                        capture_output=True, text=True, timeout=15)
                    now = datetime.now(timezone.utc).isoformat()
                    for line in (r.stdout or "").strip().splitlines():
                        f.write(f"{now},{line}\n")
                    f.flush()
                except Exception:
                    pass
                self._stopev.wait(self.interval)

    def stop(self) -> dict:
        self._stopev.set()
        self.join(timeout=self.interval + 5)
        return {"start": getattr(self, "start_restarts", {}), "end": self._restart_counts()}


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> int:
    ap = argparse.ArgumentParser(description="Demo/light-load runner for the Behavioural /score endpoint")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--url", default=os.getenv("DEMO_SCORE_URL", DEFAULT_URL))
    ap.add_argument("--key", default=os.getenv("BP_API_KEY", ""), help="X-Adhere-Key (or BP_API_KEY env)")
    ap.add_argument("--limit", type=int, default=1000, help="number of transactions to send")
    ap.add_argument("--sleep", type=float, default=0.0, help="throttle between requests (seconds)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--outdir", default="demo/demo_run")
    ap.add_argument("--monitor", default="adhere-behaviour,adhere-redis,behaviour-profile-db,adhere-behaviour-sync")
    ap.add_argument("--no-monitor", action="store_true",
                    help="skip docker stats/restart sampling (use when running INSIDE a container, "
                         "which has no docker CLI — watch `docker stats` in another terminal instead)")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    tx_path = os.path.join(a.outdir, "demo_transactions.csv")
    res_path = os.path.join(a.outdir, "demo_results.csv")
    stats_path = os.path.join(a.outdir, "docker_stats.csv")

    if not a.key:
        print("ERROR: no API key. Pass --key or set BP_API_KEY.", file=sys.stderr)
        return 2

    mon = None
    if not a.no_monitor:
        mon = DockerMonitor([c for c in a.monitor.split(",") if c], stats_path)
        mon.start()

    m = {"total": 0, "sent": 0, "skipped": 0, "http_2xx": 0, "http_4xx": 0, "http_5xx": 0,
         "conn_fail": 0, "timeout": 0}
    decisions: dict[str, int] = {}
    codes: dict[str, int] = {}
    latencies: list[float] = []
    errors: list[str] = []

    tx_cols = ["transaction_id", "amount", "currency", "transaction_type", "account_type",
               "identifier", "ip_address", "location", "timestamp"]
    res_cols = ["transaction_id", "amount", "currency", "transaction_type", "account_type",
                "ip_address", "location",                       # <-- INPUT context sent
                "http_status", "status", "activity_code", "zone", "risk_score", "confidence_score",
                "description",                                  # <-- model's analyst-readable text
                "inference_ms", "latency_ms", "model_version",
                "triggered_signals", "detection_reason", "error"]

    t_start = time.perf_counter()
    with open(a.csv, newline="") as fcsv, \
         open(tx_path, "w", newline="") as ftx, \
         open(res_path, "w", newline="") as fres:
        reader = csv.DictReader(fcsv)
        txw = csv.DictWriter(ftx, fieldnames=tx_cols); txw.writeheader()
        rw = csv.DictWriter(fres, fieldnames=res_cols); rw.writeheader()

        for row in reader:
            if m["sent"] >= a.limit:
                break
            m["total"] += 1
            payload = build_payload(row)
            if payload is None:
                m["skipped"] += 1
                continue
            txw.writerow({
                "transaction_id": payload["transaction_id"], "amount": payload["amount"],
                "currency": payload["currency"], "transaction_type": payload["transaction_type"],
                "account_type": payload["account_type"],
                "identifier": payload["customer_details"].get("identifier"),
                "ip_address": payload["additional_info"]["ip_address"],
                "location": payload["additional_info"]["location"],
                "timestamp": payload.get("timestamp", "")})

            data = json.dumps(payload).encode()
            req = urllib.request.Request(a.url, data=data, method="POST", headers={
                "Content-Type": "application/json", "X-Adhere-Key": a.key})
            rec = {c: "" for c in res_cols}
            # INPUT context — so demo_results.csv is self-contained (decision alongside what was sent)
            rec.update(transaction_id=payload["transaction_id"], amount=payload["amount"],
                       currency=payload["currency"], transaction_type=payload["transaction_type"],
                       account_type=payload["account_type"],
                       ip_address=payload["additional_info"]["ip_address"],
                       location=payload["additional_info"]["location"])
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=a.timeout) as resp:
                    rec["http_status"] = resp.status
                    body = json.loads(resp.read().decode() or "{}")
                m["http_2xx"] += 1
                m["sent"] += 1
                st = body.get("status", "")
                decisions[st] = decisions.get(st, 0) + 1
                codes[body.get("activity_code", "")] = codes.get(body.get("activity_code", ""), 0) + 1
                rec.update(status=st, activity_code=body.get("activity_code", ""),
                           zone=body.get("zone", ""), risk_score=body.get("risk_score", ""),
                           confidence_score=body.get("confidence_score", ""),
                           description=body.get("description", ""),
                           inference_ms=body.get("inference_ms", ""),
                           model_version=body.get("model_version", ""),
                           triggered_signals="|".join(body.get("triggered_signals", []) or []),
                           detection_reason=" || ".join(body.get("detection_reason", []) or []))
            except urllib.error.HTTPError as e:
                rec["http_status"] = e.code
                m["sent"] += 1
                (m.__setitem__("http_4xx", m["http_4xx"] + 1) if 400 <= e.code < 500
                 else m.__setitem__("http_5xx", m["http_5xx"] + 1))
                try:
                    rec["error"] = (e.read().decode() or "")[:300]
                except Exception:
                    rec["error"] = f"HTTP {e.code}"
                errors.append(f"{payload['transaction_id']}: HTTP {e.code}")
            except (TimeoutError, ) as e:            # urlopen timeout raises socket.timeout/TimeoutError
                m["timeout"] += 1; rec["error"] = f"timeout: {e}"; errors.append(f"timeout: {e}")
            except Exception as e:
                # connection refused / reset / DNS etc.
                if "timed out" in str(e).lower():
                    m["timeout"] += 1
                else:
                    m["conn_fail"] += 1
                rec["error"] = str(e)[:300]; errors.append(str(e)[:120])
            finally:
                lat = (time.perf_counter() - t0) * 1000
                if rec["http_status"] == 200:
                    latencies.append(lat)
                rec["latency_ms"] = round(lat, 2)
                rw.writerow(rec)
            if a.sleep:
                time.sleep(a.sleep)

    elapsed = time.perf_counter() - t_start
    restarts = mon.stop() if mon else {"start": {}, "end": {}}

    # --- summary --------------------------------------------------------------------------------
    print("\n" + "=" * 62)
    print("  BEHAVIOURAL /score DEMO — SUMMARY")
    print("=" * 62)
    print(f"  dataset rows read : {m['total']}   (skipped {m['skipped']} missing required fields)")
    print(f"  requests sent     : {m['sent']}   in {elapsed:.1f}s   → {m['sent']/elapsed:.1f} req/s")
    print(f"  HTTP 2xx / 4xx / 5xx : {m['http_2xx']} / {m['http_4xx']} / {m['http_5xx']}")
    print(f"  connection failures  : {m['conn_fail']}   timeouts: {m['timeout']}")
    print("  --- decisions (observed, model-assigned) ---")
    for k, v in sorted(decisions.items(), key=lambda x: -x[1]):
        print(f"     {k or '(none)':8} {v}")
    print("  --- activity codes ---")
    for k, v in sorted(codes.items(), key=lambda x: -x[1]):
        print(f"     {k or '(none)':8} {v}")
    if latencies:
        print("  --- latency (ms, 200s only) ---")
        print(f"     avg {sum(latencies)/len(latencies):.1f}  p50 {pct(latencies,0.5):.1f}  "
              f"p95 {pct(latencies,0.95):.1f}  max {max(latencies):.1f}")
    if restarts["end"]:
        print("  --- container restart counts (start → end; any increase = a restart!) ---")
        for c in restarts["end"]:
            s, e = restarts["start"].get(c, "?"), restarts["end"].get(c, "?")
            flag = "  ⚠️ RESTARTED" if isinstance(s, int) and isinstance(e, int) and e > s else ""
            print(f"     {c:26} {s} → {e}{flag}")
    else:
        print("  --- container monitoring skipped (--no-monitor); watch `docker stats` separately ---")
    if errors:
        print(f"  --- first errors ({len(errors)} total) ---")
        for e in errors[:8]:
            print(f"     {e}")
    print(f"\n  outputs: {tx_path} | {res_path} | {stats_path}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
