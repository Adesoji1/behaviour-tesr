#!/usr/bin/env python3
"""
Step 4 — The rule engine: reads the LEARNED behaviour profile and fires a rule
when an incoming transaction violates the stored baseline or a hard condition.

This is the "quick DB lookup" leg of the hybrid architecture: one indexed read
of bp_user_behaviour_profile by entity_key, then rules are evaluated in memory.
Velocity rules (1m/15m/1h) are the "live feature factory" leg — they need a
short recent-window query; that hook is stubbed and documented below.

    profile (DB)  +  incoming txn  ->  evaluate rules  ->  fire + log

Usage:
    python rule_engine.py --demo            # score real sample txns, show firings
    python rule_engine.py --demo --log      # also persist firings to bp_rule_event
"""
import argparse
import json
import time

import config
import db


def _city(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = [p.strip() for p in loc.split(",")]
    return parts[1] if len(parts) >= 2 else parts[0]


def profile_is_trusted(p: dict | None) -> tuple[bool, str]:
    """THE trust gate — "Practical rules" §1/§2/§10, re-checked at DECISION time.

    Returns (trusted, reason). The engine judges a transaction against the account's
    OWN profile only when this says yes; otherwise it falls back to the peer baseline.

    Why re-check here instead of trusting `profile_status` alone: `profile_status` is
    decided when a profile is BUILT. A profile built under an older/looser policy would
    keep its stale `active` flag until it happens to be rebuilt. Re-evaluating the gate
    on every decision means a policy change (or newly-seen fraud) takes effect
    IMMEDIATELY and fails safe — no rebuild required.

    §1: ">= 90 days history AND >= 100 transactions AND No confirmed fraud cases"
    §2: "Otherwise: Profile Status = Warming Up"  (-> judged against peers)
    §10: confidence must clear the trust threshold
    """
    if p is None:
        return False, "peer_baseline (new account, no own history)"
    if p.get("profile_status") != "active":
        return False, f"peer_baseline ({p.get('profile_status')})"
    if (p.get("confidence_score") or 0) < config.CONFIDENCE_TRUST_THRESHOLD:
        return False, f"peer_baseline (low confidence {p.get('confidence_score')})"
    # §1 "No confirmed fraud cases" — a customer with confirmed fraud is never
    # trusted on their own baseline, whatever their stored status says.
    if (p.get("suspicious_tx_count") or 0) > config.ELIGIBLE_MAX_FRAUD_TXNS:
        return False, (f"peer_baseline (§1 confirmed fraud: "
                       f"{p.get('suspicious_tx_count')} txn(s))")
    # §1/§2 minimum-data gate, re-checked live against current policy
    if (p.get("tenure_days") or 0) < config.ELIGIBLE_MIN_TENURE_DAYS:
        return False, (f"peer_baseline (§1 tenure {p.get('tenure_days')}d "
                       f"< {config.ELIGIBLE_MIN_TENURE_DAYS}d)")
    if (p.get("lifetime_clean_txns") or 0) < config.ELIGIBLE_MIN_TXNS:
        return False, (f"peer_baseline (§1 clean txns {p.get('lifetime_clean_txns')} "
                       f"< {config.ELIGIBLE_MIN_TXNS})")
    return True, "own_profile"


# ---------------------------------------------------------------------------
# Reference-data cache (rules / blacklist / per-client overrides / peer baselines)
# ---------------------------------------------------------------------------
# These four tables are small (tens of rows) and change RARELY — only when an admin
# loads rules (load_rules.py) or edits thresholds (client_thresholds.py). Re-reading
# them from the DB on every /score was the bulk of the latency. We cache them IN
# PROCESS (no Redis: the data is tiny, identical per replica, and an extra network hop
# to Redis would defeat the point) behind a short TTL, refreshed lazily.
#
# IMPORTANT — what is NOT cached: the per-customer PROFILE. That is read fresh on every
# score (see load_profile / the /score handler), because it is the behaviour data that
# must always be current. Only the rarely-changing rule CATALOGUE is cached, so
# correctness of a decision is never based on stale behaviour.
#
# Staleness bound: a rule/threshold/blacklist change takes effect within TTL seconds
# (default 30). POST /reload forces an immediate refresh after such a change.
import threading


class _RuleCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._loaded_at = 0.0

    def _load(self, conn) -> dict:
        cur = conn.cursor()
        cur.execute("SELECT rule_code, params FROM bp_rule_definition WHERE enabled=1")
        params = {code: json.loads(p or "{}") for code, p in cur.fetchall()}
        cur.execute("SELECT identifier FROM bp_blacklist WHERE identifier IS NOT NULL")
        blacklist = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT branch_id, rule_code, params, enabled FROM bp_rule_settings")
        overrides: dict[tuple, dict] = {}
        override_enabled: dict[tuple, int] = {}
        for branch_id, rule_code, prm, enabled in cur.fetchall():
            key = (int(branch_id), rule_code)
            overrides[key] = json.loads(prm or "{}")
            if enabled is not None:
                override_enabled[key] = int(enabled)
        pcur = db.dict_cursor(conn)
        pcur.execute("SELECT * FROM bp_peer_baseline")
        # keyed per (branch, account_type, currency) — peers are per-currency too
        peer = {(int(r["branch_id"]), r["account_type"], r["currency"]): r
                for r in pcur.fetchall()}
        return {"params": params, "blacklist": blacklist, "overrides": overrides,
                "override_enabled": override_enabled, "peer": peer,
                "rules": len(params), "blacklist_n": len(blacklist), "peer_n": len(peer)}

    def get(self, conn) -> dict:
        """Return the cached reference data, refreshing from the DB if the TTL expired."""
        now = time.monotonic()
        with self._lock:
            if self._data is None or (now - self._loaded_at) >= config.RULES_CACHE_TTL:
                self._data = self._load(conn)
                self._loaded_at = now
            return self._data

    def refresh(self, conn) -> dict:
        """Force an immediate reload (POST /reload). Returns a small summary."""
        with self._lock:
            self._data = self._load(conn)
            self._loaded_at = time.monotonic()
            d = self._data
        return {"rules": d["rules"], "blacklist": d["blacklist_n"], "peer_baselines": d["peer_n"]}


_RULE_CACHE = _RuleCache()


def refresh_rule_cache(conn) -> dict:
    """Public hook for POST /reload — refresh the in-process reference-data cache."""
    return _RULE_CACHE.refresh(conn)


class RuleEngine:
    def __init__(self, conn, velocity=None):
        # `velocity` is an optional live-velocity source (see live_velocity.py):
        # any object with .features(branch_id, account_no, as_of, amount) -> dict.
        # When present, the fast recent-window rules (1m/10m/15m/1h/24h) evaluate.
        self.conn = conn
        self.velocity = velocity
        # Reference data comes from the in-process cache (rarely changes). The
        # per-customer profile is still read live in evaluate()/load_profile().
        ref = _RULE_CACHE.get(conn)
        self.params = ref["params"]
        self.blacklist = ref["blacklist"]
        self.overrides = ref["overrides"]
        self.override_enabled = ref["override_enabled"]
        self.peer = ref["peer"]

    def _peer_baseline(self, branch_id, account_type, currency) -> dict | None:
        """Find the peer baseline for a brand-new account, IN THE TRANSACTION'S CURRENCY:
        exact (branch, type, ccy), then the branch's 'individual'/'unknown' group in that
        ccy, then any group in the branch IN THAT CURRENCY. No cross-currency fallback —
        mixing currencies is exactly the blend we are removing; if there is no peer for
        this currency the relative peer rules simply don't fire (absolute rules still do)."""
        try:
            b = int(branch_id)
        except (TypeError, ValueError):
            return None
        at = (account_type or "unknown")
        ccy = config.normalize_currency(currency)
        for k in [(b, at, ccy), (b, "individual", ccy), (b, "unknown", ccy)]:
            if k in self.peer:
                return self.peer[k]
        for (pb, _, pc), v in self.peer.items():
            if pb == b and pc == ccy:
                return v
        return None

    def _params_for(self, branch_id) -> dict:
        """Merge this client's overrides over the global defaults, per rule."""
        try:
            b = int(branch_id)
        except (TypeError, ValueError):
            b = None
        merged = {code: dict(v) for code, v in self.params.items()}
        if b is not None:
            for (ob, code), ov in self.overrides.items():
                if ob == b:
                    merged[code] = {**merged.get(code, {}), **ov}
        return merged

    def _disabled_for(self, branch_id) -> set:
        """Rules a client has explicitly switched off."""
        try:
            b = int(branch_id)
        except (TypeError, ValueError):
            return set()
        return {code for (ob, code), en in self.override_enabled.items() if ob == b and en == 0}

    def load_profile(self, entity_key: str, currency=None) -> dict | None:
        """The profile for THIS customer IN THIS CURRENCY. No row for the currency ->
        None -> the cold-start / peer path runs (per-currency), exactly as for a new
        account. `currency` is normalized so it matches how the profile was stored."""
        ccy = config.normalize_currency(currency)
        cur = db.dict_cursor(self.conn)
        cur.execute("SELECT * FROM bp_user_behaviour_profile WHERE entity_key=%s AND currency=%s",
                    (entity_key, ccy))
        return cur.fetchone()

    def _eval_cold_start(self, txn, amt, oc, params, fire, tag, currency=None):
        """Judge a not-yet-trusted account (new / Warming Up / low confidence)
        against its PEER baseline IN THIS CURRENCY instead of its own thin history. Non-ML."""
        base = self._peer_baseline(txn.get("branch_id"),
                                   txn.get("account_type") or txn.get("origin_account_type"),
                                   currency)
        if base is None:
            return
        pr = params.get("block_significantly_high_amount", {})
        if base["p95_amount"] and amt > pr.get("factor", 3.0) * float(base["p95_amount"]):
            fire("block_significantly_high_amount", "high",
                 amount=amt, peer_p95=float(base["p95_amount"]), basis=tag)
        if base["max_amount"] and amt > float(base["max_amount"]):
            fire("outbound_exceeds_historical_max", "medium",
                 amount=amt, peer_max=float(base["max_amount"]), basis=tag)
        usual_city = json.loads(base["usual_cities"] or "{}")
        city = _city(txn.get("customer_location"))
        if city and usual_city and city not in usual_city:
            fire("detect_unusual_city", "low", city=city, peer_usual=list(usual_city)[:8], basis=tag)
        usual_ctry = json.loads(base["usual_countries"] or "{}")
        if oc and usual_ctry and oc not in usual_ctry:
            fire("detect_unusual_country", "medium", country=oc, peer_usual=list(usual_ctry), basis=tag)
        pr = params.get("unusual_time_for_user", {})
        hours = json.loads(base["peak_transaction_hours"] or "{}")
        tot_h = sum(hours.values())
        ts = txn.get("ts")
        if ts is not None and tot_h > 0:
            hr = str(ts.hour if hasattr(ts, "hour") else int(str(ts)[11:13]))
            if hours.get(hr, 0) / tot_h < pr.get("min_share", 0.02):
                fire("unusual_time_for_user", "low", hour=hr, basis=tag)

    def _eval_velocity(self, txn, amt, params, fire):
        """Fire the recent-window velocity rules using the recent-window look-up.
        The current transaction is counted in (the +1 / +amt). Passes the engine's own
        connection so the LOCAL source reuses it (no second pooled connection per score)."""
        v = self.velocity.features(txn.get("branch_id"), txn.get("origin_account_no"),
                                   txn.get("ts"), amt, conn=self.conn)

        pr = params.get("transaction_velocity_1m", {})
        if v["n_1m"] + 1 > pr.get("max_count", 3):
            fire("transaction_velocity_1m", "high", count_1m=v["n_1m"] + 1)

        pr = params.get("flag_high_frequency_10m", {})
        if v["n_10m"] + 1 > pr.get("max_count", 10):
            fire("flag_high_frequency_10m", "medium", count_10m=v["n_10m"] + 1)

        pr = params.get("high_outbound_amount_15m", {})
        if v["amt_15m"] + amt > pr.get("max_amount", 5_000_000):
            fire("high_outbound_amount_15m", "high", amount_15m=v["amt_15m"] + amt)

        pr = params.get("high_recipient_count_1h", {})
        if v["recip_1h"] + 1 > pr.get("max_distinct_recipients", 10):
            fire("high_recipient_count_1h", "high", recipients_1h=v["recip_1h"] + 1)

        pr = params.get("excessive_beneficiary_count_24h", {})
        if v["benef_24h"] + 1 > pr.get("max_distinct_beneficiaries", 20):
            fire("excessive_beneficiary_count_24h", "medium", beneficiaries_24h=v["benef_24h"] + 1)

        pr = params.get("flag_high_frequency_1d", {})
        if v["n_24h"] + 1 > pr.get("max_count", 50):
            fire("flag_high_frequency_1d", "medium", count_24h=v["n_24h"] + 1)

        pr = params.get("multiple_countries_short_timeframe", {})
        if v["countries_1h"] > pr.get("max_countries", 2):
            fire("multiple_countries_short_timeframe", "high", countries_1h=v["countries_1h"])

    def evaluate(self, txn: dict) -> list[dict]:
        """Return a list of fired rules for one incoming transaction."""
        ek = f"{txn['branch_id']}:{txn['origin_account_no']}"
        ccy = config.normalize_currency(txn.get("currency"))
        p = self.load_profile(ek, ccy)      # THIS customer's profile IN THIS currency
        fired: list[dict] = []

        # thresholds resolved for THIS client (institution), with global fallback
        params = self._params_for(txn.get("branch_id"))
        disabled = self._disabled_for(txn.get("branch_id"))

        def fire(code, severity, **details):
            if code in disabled:            # client switched this rule off
                return
            fired.append({"rule_code": code, "severity": severity, "details": details})

        amt = float(txn.get("amount") or 0)

        # ---- hard-condition rules (no profile needed) ----
        # PER-CURRENCY single-transfer escalation — multi-currency by design. Any rule
        # whose params carry a `currency` + `max_amount` fires when the transaction is in
        # that currency and exceeds that threshold. So NGN (10m) and USD (10k) both work
        # today, and EUR/GBP/... are pure DATA: add a bp_rule_definition row, no code
        # change. This replaces the old hard-coded `currency == "NGN"` check so the system
        # is not restricted to Nigeria (per the DB engineer).
        for _code, _prm in params.items():
            if (isinstance(_prm, dict) and "currency" in _prm and "max_amount" in _prm
                    and ccy == config.normalize_currency(_prm["currency"])
                    and amt > _prm["max_amount"]):
                fire(_code, "high", amount=amt, currency=ccy, threshold=_prm["max_amount"])

        pr = params.get("block_above_hard_cap", {})
        if amt > pr.get("hard_cap", 1e18):
            fire("block_above_hard_cap", "critical", amount=amt)

        oc, dc = txn.get("origin_country"), txn.get("destination_country")
        if oc and dc and oc != dc:
            fire("flag_cross_border_transfer", "medium", origin=oc, destination=dc)

        # blacklist (works even with no profile)
        if txn.get("identifier") and txn.get("identifier") in self.blacklist:
            fire("flag_blacklisted_users_transactions", "critical", who=txn.get("identifier"))
        if txn.get("destination_account_no") and txn.get("destination_account_no") in self.blacklist:
            fire("blacklisted_beneficiary_detection", "critical", beneficiary=txn.get("destination_account_no"))

        # ---- LIVE VELOCITY rules (recent-window; needs the live look-up) ----
        # These fire regardless of whether a long-term profile exists, because a
        # burst is about what is happening right now.
        if self.velocity is not None:
            self._eval_velocity(txn, amt, params, fire)

        # ---- TRUST GATE (governance §1/§2/§10/§15) ----
        # Judge the transaction against the account's OWN profile only when the full
        # eligibility gate passes RIGHT NOW (see profile_is_trusted). A brand-new,
        # "Warming Up", low-confidence, under-evidenced or fraud-touched account is
        # judged against its PEERS instead — so a fraudster cannot establish a
        # trusted baseline from a little fake activity.
        trusted, tag = profile_is_trusted(p)
        if not trusted:
            self._eval_cold_start(txn, amt, oc, params, fire, tag, ccy)
            return fired

        if p["is_blacklisted"]:
            fire("flag_blacklisted_users_transactions", "critical", reason="profile flag")

        # ---- profile-comparison rules (the heart: rule reads the profile) ----
        # unusual country
        pr = params.get("detect_unusual_country", {})
        usual_ctry = json.loads(p["usual_countries"] or "{}")
        if oc and sum(usual_ctry.values()) >= pr.get("min_history", 5) and oc not in usual_ctry:
            fire("detect_unusual_country", "high", country=oc, usual=list(usual_ctry))

        # unusual city
        pr = params.get("detect_unusual_city", {})
        usual_city = json.loads(p["usual_cities"] or "{}")
        city = _city(txn.get("customer_location"))
        if city and sum(usual_city.values()) >= pr.get("min_history", 5) and city not in usual_city:
            fire("detect_unusual_city", "medium", city=city, usual=list(usual_city)[:8])

        # unusual time-of-day
        pr = params.get("unusual_time_for_user", {})
        hours = json.loads(p["peak_transaction_hours"] or "{}")
        tot_h = sum(hours.values())
        ts = txn.get("ts")
        if ts is not None and tot_h >= pr.get("min_history", 10):
            hr = str(ts.hour if hasattr(ts, "hour") else int(str(ts)[11:13]))
            share = hours.get(hr, 0) / tot_h
            if share < pr.get("min_share", 0.02):
                fire("unusual_time_for_user", "low", hour=hr, share=round(share, 4))

        # amount exceeds historical maximum
        if p["max_amount"] is not None and amt > float(p["max_amount"]):
            fire("outbound_exceeds_historical_max", "high", amount=amt, hist_max=float(p["max_amount"]))

        # significantly high vs p95
        pr = params.get("block_significantly_high_amount", {})
        if p["p95_amount"] and amt > pr.get("factor", 3.0) * float(p["p95_amount"]):
            fire("block_significantly_high_amount", "high", amount=amt, p95=float(p["p95_amount"]))

        # first-time beneficiary
        benef = json.loads(p["beneficiaries"] or "{}")
        dest = txn.get("destination_account_no")
        if dest and dest not in benef:
            fire("first_time_beneficiary", "low", beneficiary=dest)

        # dormancy reactivation
        pr = params.get("dormant_account_reactivation", {})
        if p["dormant_days"] is not None and p["dormant_days"] >= pr.get("dormancy_days", 90):
            fire("dormant_account_reactivation", "medium", dormant_days=p["dormant_days"])
            if p["avg_amount"] and amt > 3.0 * float(p["avg_amount"]):
                fire("high_value_after_dormancy", "high", amount=amt, avg=float(p["avg_amount"]))

        return fired

    def log(self, entity_key, txn, fired):
        cur = self.conn.cursor()
        cur.executemany(
            "INSERT INTO bp_rule_event (entity_key, transaction_id, rule_code, severity, details) "
            "VALUES (%s,%s,%s,%s,%s)",
            [(entity_key, txn.get("transaction_id"), f["rule_code"], f["severity"],
              json.dumps(f["details"], default=str)) for f in fired],
        )
        self.conn.commit()


def demo(do_log: bool):
    import polars as pl
    conn = db.connect()
    eng = RuleEngine(conn)
    # pull some real sample transactions and craft a couple of synthetic anomalies
    df = pl.read_csv("data/transactions_sample.csv", infer_schema_length=5000, ignore_errors=True)
    df = df.with_columns(
        pl.col("date_created").str.to_datetime(format="%Y-%m-%d %H:%M:%S%.f%#z", strict=False)
        .dt.convert_time_zone("UTC").dt.replace_time_zone(None).alias("ts")
    )
    sample = df.sample(6, seed=7).to_dicts()
    # inject an obvious anomaly on the first record (huge amount + odd city/time)
    sample[0] = {**sample[0], "amount": 5_000_000_000, "customer_location": "Somewhere, Pyongyang, DPRK",
                 "destination_account_no": "9999999999"}

    fired_total = 0
    for t in sample:
        ek = f"{t['branch_id']}:{t['origin_account_no']}"
        fired = eng.evaluate(t)
        fired_total += len(fired)
        print(f"\nTXN {t.get('transaction_id')}  entity={ek}  amount={t.get('amount')}")
        if not fired:
            print("   (no rules fired — matches learned behaviour)")
        for f in fired:
            print(f"   FIRED [{f['severity']:>8}] {f['rule_code']}  {f['details']}")
        if do_log and fired:
            eng.log(ek, t, fired)
    print(f"\n[demo] total rule firings: {fired_total}  (logged={do_log})")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--log", action="store_true")
    args = ap.parse_args()
    if args.demo:
        demo(args.log)
