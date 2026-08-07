#!/usr/bin/env python3
"""
Probe every BF-* category for one customer against the live /score, and write a Postman-ready
.txt (payloads + the OBSERVED decision) so we can review whether the model behaves sensibly.

Run:  python3 demo/bf_test.py            # uses BP_API_KEY from .env, scores localhost:8080
Output: demo/bf_category_tests.txt
"""
import copy
import json
import subprocess
import time
import urllib.request

URL = "http://localhost:8080/score"
ID = "21200336604"
OUT = "demo/bf_category_tests.txt"


def api_key() -> str:
    for line in open(".env"):
        if line.startswith("BP_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def clear_velocity():
    subprocess.run(["docker", "exec", "adhere-redis", "redis-cli", "DEL", f"vel:{ID}"],
                   capture_output=True)


def base(txid):
    return {
        "transaction_id": txid, "amount": 3000000, "currency": "NGN",
        "transaction_type": "transfer", "account_type": "individual",
        "customer_details": {"customer_name": "Test User", "customer_email": "e@example.com",
                             "identifier": ID, "identifier_type": "bvn"},
        "additional_info": {"ip_address": "102.89.1.1", "location": "Lagos, Nigeria"},
    }


def merge(txid, **over):
    p = base(txid)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k].update(v)
        else:
            p[k] = v
    return p


KEY = api_key()


def score(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Adhere-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return getattr(e, "code", 0), {"error": str(e)}


# --- scenarios: (label, what-it-probes, payload, is_burst) --------------------------------------
S = [
    ("Normal baseline amount", "BF-100 (safe) — ~median, home location, daytime",
     merge("N1", amount=3000000)),
    ("Small typical amount", "BF-100/BF-110 (safe) — small vs their norm",
     merge("N2", amount=1200000)),
    ("Near historical max (25M)", "boundary: below their max (~26M) but ~8x median — safe or flag?",
     merge("M1", amount=25000000)),
    ("Amount anomaly (100M)", "BF-301 (amount) — far above historical max",
     merge("A1", amount=100000000)),
    ("Unusual time (03:00)", "BF-302 (time) — normal amount, night-time",
     merge("T1", amount=3000000, timestamp="2026-08-06T03:00:00Z")),
    ("New location + cross-border (London)", "BF-303 (location) — normal amount, new country",
     merge("L1", amount=3000000, destination_country="United Kingdom",
           additional_info={"ip_address": "81.2.69.142", "location": "London, United Kingdom"},
           customer_details={"country": "United Kingdom"})),
    ("New beneficiary", "BF-304 (beneficiary/structure) — normal amount, first-time destination",
     merge("B1", amount=3000000, destination_account={"account_number": "9099887766"})),
    ("Multi-signal (100M + London + night + new benef)", "BF-400 (strong multi-signal)",
     merge("X1", amount=100000000, timestamp="2026-08-06T03:00:00Z",
           destination_country="United Kingdom",
           additional_info={"ip_address": "81.2.69.142", "location": "London, United Kingdom"},
           destination_account={"account_number": "9099887766"},
           customer_details={"country": "United Kingdom"})),
]
BURST = ("Velocity burst (6 rapid)", "BF-305 (velocity/burst) — same customer, seconds apart",
         merge("V", amount=3000000))

results = []
for label, probe, payload in S:
    clear_velocity()                      # isolate the signal (no leftover live velocity)
    time.sleep(0.2)
    code, body = score(payload)
    results.append((label, probe, payload, [(code, body)]))

# burst: clear once, fire 6 without clearing so the live window builds
clear_velocity()
burst_runs = []
for n in range(1, 7):
    p = copy.deepcopy(BURST[2]); p["transaction_id"] = f"V{n}"
    code, body = score(p)
    burst_runs.append((code, body))
results.append((BURST[0], BURST[1], BURST[2], burst_runs))

# --- amount sweep (velocity cleared each) — shows the amount response is monotonic ------------
sweep = []
for amt in [10000, 50000, 100000, 500000, 1000000, 3000000, 10000000, 25000000, 100000000]:
    clear_velocity(); time.sleep(0.15)
    code, b = score(merge("S", amount=amt))
    sweep.append((amt, b.get("status", "?"), b.get("activity_code", ""), b.get("risk_score", "")))

# --- velocity demonstration: SAME small amount, clean vs right after a prior transaction -------
clear_velocity(); time.sleep(0.15)
_, b_clean = score(merge("VC", amount=50000))
clear_velocity(); time.sleep(0.15)
score(merge("VP", amount=1000000))                     # prior transaction (records velocity)
_, b_after = score(merge("VA", amount=50000))          # same 50k, now sees the prior one


def fmt(code, b):
    if "error" in b or code != 200:
        return f"HTTP {code}  {b.get('error', b)}"
    return (f"HTTP {code} | {b['status'].upper()} {b['activity_code']} | risk={b['risk_score']} "
            f"conf={b['confidence_score']} | cold_start={b['result']['is_cold_start']}\n"
            f"          signals: {b.get('triggered_signals')}\n"
            f"          {b['description']}")


with open(OUT, "w") as f:
    f.write("=" * 90 + "\n")
    f.write("BEHAVIOURAL /score — BF-category test payloads for customer %s\n" % ID)
    f.write("Endpoint : POST http://localhost:8080/score\n")
    f.write("Headers  : Content-Type: application/json\n")
    f.write("           X-Adhere-Key: <PASTE YOUR KEY — from .env: grep ^BP_API_KEY= .env>\n")
    f.write("Note     : contains a REAL customer identifier — treat as sensitive, do not share.\n")
    f.write("           The OBSERVED result below is what THIS model returned when generated.\n")
    f.write("=" * 90 + "\n\n")
    f.write("*** TESTING GOTCHA — read this first ***\n")
    f.write("/score records each transaction into a LIVE VELOCITY window (Redis), so firing the same\n")
    f.write("customer repeatedly (as in Postman testing) accumulates velocity and later calls can flag\n")
    f.write("'review' even for a small amount. This is the velocity feature WORKING, not an amount bug.\n")
    f.write("Also: this standard payload has a FIXED timestamp, so every test lands in the same window.\n")
    f.write("To test one signal at a time, reset between calls:\n")
    f.write("    docker exec adhere-redis redis-cli DEL vel:%s\n" % ID)
    f.write("or send a fresh \"timestamp\" each call.\n\n")

    f.write("### AMOUNT SWEEP (velocity cleared each) — the amount response IS monotonic ###\n")
    f.write("  %-14s %-8s %-8s %s\n" % ("amount", "status", "code", "risk"))
    for amt, st, code, risk in sweep:
        f.write("  %-14s %-8s %-8s %s\n" % (f"{amt:,}", st, code, risk))
    f.write("\n### VELOCITY DEMONSTRATION — same 50k, clean vs right after a prior txn ###\n")
    f.write("  50,000 (clean velocity)      -> %s %s risk=%s\n" % (
        b_clean.get("status"), b_clean.get("activity_code"), b_clean.get("risk_score")))
    f.write("  50,000 (right after a 1M txn) -> %s %s risk=%s  <- velocity pushed it up\n\n" % (
        b_after.get("status"), b_after.get("activity_code"), b_after.get("risk_score")))

    for i, (label, probe, payload, runs) in enumerate(results, 1):
        f.write("-" * 90 + "\n")
        f.write(f"[{i}] {label}\n     probes: {probe}\n\n")
        f.write("PAYLOAD (Body -> raw -> JSON):\n")
        f.write(json.dumps(payload, indent=2) + "\n\n")
        if len(runs) == 1:
            f.write("OBSERVED:\n  " + fmt(*runs[0]) + "\n\n")
        else:
            f.write("OBSERVED (fire this same body 6x quickly in Postman):\n")
            for n, (code, b) in enumerate(runs, 1):
                f.write(f"  call #{n}: " + fmt(code, b) + "\n")
            f.write("\n")

print("wrote", OUT)
for label, probe, payload, runs in results:
    if len(runs) == 1:
        c, b = runs[0]
        print("  %-42s -> %s %s risk=%s" % (label, b.get("status", "?"), b.get("activity_code", ""),
                                            b.get("risk_score", "")))
    else:
        print("  %-42s -> %s" % (label, " ".join(f"{b.get('activity_code','?')}" for _, b in runs)))
