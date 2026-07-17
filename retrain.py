#!/usr/bin/env python3
"""
Event-driven, PER-CUSTOMER retraining (no cron).

A customer's profile is recomputed only when their own behaviour warrants it —
the "streaming feature store" model. Triggers (OR), per Anita:
    * >= RETRAIN_MIN_NEW_TXNS transactions scored since last build, OR
      (this is `txns_since_build`, incremented once per /score in service.py —
       a count of transactions processed, not a re-filtered "clean" count here;
       the clean-baseline filter is applied inside build_profiles.py on recompute)
    * >= RETRAIN_MAX_AGE_DAYS days since last build, OR
    * significant drift (repeated anomalies vs their own profile).

Retraining one customer is cheap AND it NEVER touches production: it reads that
customer's recent clean transactions from the local `bp_transactions_cache`
(filled by sync_manager.py, the only process that reads prod), recomputes one
profile row, and upserts it. Different customers are independent, so many can
retrain concurrently; the same customer is guarded by a per-customer advisory lock.

    production ──(sync_manager, chunked)──▶ bp_transactions_cache ──▶ retrain

This is what removed the unbounded per-retrain query that loaded the live DB
(see ingestionstratimprove.md §5).

Public API used by the microservice (service.py):
    maybe_retrain(entity_key)  -> dict | None   # retrains iff a trigger is met
    retrain_customer(entity_key) -> dict         # force a recompute now
"""
import json
import math
from datetime import datetime

import polars as pl

import audit
import config
import db
from build_profiles import (topn_map, entropy_map, compute_confidence,
                            detect_drift, city_from_location)

HALF = config.DECAY_HALF_LIFE_DAYS
OBS = config.MIN_PATTERN_OBS
DOW = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}

# Columns read from the LOCAL cache for one customer. Same shape the builder
# expects — the cache mirrors production, so the learning code is unchanged.
_SEL = """
  amount, currency, transaction_type, status, branch_id, origin_account_no,
  origin_account_type, destination_account_no, customer_name, identifier, bvn,
  account_type, customer_ip_address,
  customer_location, merchant_name, origin_country, destination_country, date_created,
  sender_blacklisted, is_blocked
"""


def fetch_customer(branch_id, account_no, conn=None):
    """Return (txns_df, tenure) for one customer FROM THE LOCAL CACHE.

    No production read happens here — `bp_transactions_cache` is filled by
    sync_manager.py in bounded chunks. This is one small indexed lookup on
    (entity_key, date_created), not the unbounded prod query it replaced.

    txns_df = their clean transactions inside the LOOKBACK window.
    tenure  = {tenure_days, lifetime_clean_txns}. NOTE: tenure is a LIFETIME
    property and the cache only holds the learning window, so it is carried
    forward from the customer's existing profile when we have one (it only ever
    grows). With no prior profile we fall back to what the cache can prove, which
    UNDER-states tenure — deliberately: an unproven account stays `warming_up`
    and is judged against peers, which fails safe.
    """
    entity_key = f"{int(branch_id)}:{account_no}"
    own = conn is None
    c = conn or db.connect()
    try:
        cur = db.dict_cursor(c)
        cur.execute(
            f"SELECT {_SEL} FROM bp_transactions_cache "
            "WHERE entity_key = %s "
            f"  AND date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months') "
            "  AND status = 'clean' AND is_blocked = false AND sender_blacklisted = false "
            "ORDER BY date_created",
            (entity_key,),
        )
        rows = cur.fetchall()
        audit.log.info("cache_read entity=%s rows=%d (no production read)",
                       entity_key, len(rows))
        if not rows:
            return None, {"tenure_days": 0, "lifetime_clean_txns": 0}

        # polars wants plain python types; date_created -> str so the existing
        # datetime parsing in compute_customer_profile still applies.
        recs = []
        for r in rows:
            r = dict(r)
            r["amount"] = float(r["amount"]) if r["amount"] is not None else None
            r["date_created"] = r["date_created"].strftime("%Y-%m-%d %H:%M:%S.%f+00") \
                if r["date_created"] else None
            recs.append(r)
        df = pl.DataFrame(recs, infer_schema_length=None)

        # §1 "No confirmed fraud cases" — count this customer's confirmed-fraud txns
        # in the window. The SELECT above is clean-only, so we must ask separately;
        # still the local cache, still no production read.
        cur.execute(
            "SELECT count(*) AS n FROM bp_transactions_cache WHERE entity_key = %s "
            f"  AND date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months') "
            "  AND (status <> 'clean' OR is_blocked = true OR sender_blacklisted = true)",
            (entity_key,),
        )
        fraud_txns = int((cur.fetchone() or {}).get("n") or 0)

        # Tenure: carry forward from the stored profile; the cache alone cannot
        # see beyond the learning window.
        # Customer-level tenure across ALL of their currency rows: account age is a
        # customer property (take the max), last_retrained_at the latest.
        cur.execute("SELECT MAX(tenure_days) AS tenure_days, "
                    "MAX(lifetime_clean_txns) AS lifetime_clean_txns, "
                    "MAX(last_retrained_at) AS last_retrained_at "
                    "FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
        prev = cur.fetchone()
        cache_span = 0
        if rows and rows[0]["date_created"]:
            cache_span = (datetime.utcnow() - rows[0]["date_created"].replace(tzinfo=None)).days
        if prev and (prev["tenure_days"] or 0) > 0:
            grown = 0
            if prev["last_retrained_at"]:
                grown = max((datetime.utcnow() - prev["last_retrained_at"]).days, 0)
            # Carry the LAST AUTHORITATIVE lifetime figures forward. tenure_days is
            # exact (it only grows with elapsed time). lifetime_clean_txns is carried
            # as-is — we do NOT take max() with the window count: max() could only ever
            # ratchet the number UP, which would let a customer keep a trusted profile
            # on the strength of transactions later confirmed fraudulent (§7). The
            # fraud gate below is what actually protects us; the authoritative refresh
            # of this count is extract_tenure.py.
            tenure = {
                "tenure_days": int((prev["tenure_days"] or 0) + grown),
                "lifetime_clean_txns": int(prev["lifetime_clean_txns"] or len(rows)),
                "fraud_txns": fraud_txns,
            }
        else:
            # No prior profile: use only what the cache can prove. This UNDER-states
            # tenure, so the account stays warming_up and is judged against peers —
            # exactly §2's "Otherwise: Profile Status = Warming Up". Fails safe.
            tenure = {"tenure_days": int(cache_span),
                      "lifetime_clean_txns": int(len(rows)),
                      "fraud_txns": fraud_txns}
        return df, tenure
    finally:
        if own:
            c.close()


def _jnum(x):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return x


def compute_customer_profile(df: pl.DataFrame, entity_key: str, prev: dict | None, tenure: dict,
                             currency: str | None = None) -> dict:
    """Compute one customer's profile row FOR ONE CURRENCY (same fields as the batch
    builder). `df` must already be filtered to `currency`; the amount stats it produces
    (max/p95/avg/…) are therefore per-currency, so a NGN and a USD profile never blend."""
    currency = config.normalize_currency(currency) if currency else config.DEFAULT_CURRENCY
    now = datetime.utcnow()
    df = df.with_columns(
        pl.col("amount").cast(pl.Float64, strict=False),
        pl.col("date_created").str.to_datetime(format="%Y-%m-%d %H:%M:%S%.f%#z", strict=False, time_unit="us")
        .dt.convert_time_zone("UTC").dt.replace_time_zone(None).alias("ts"),
        pl.lit(entity_key).alias("entity_key"),
    ).filter(pl.col("ts").is_not_null() & pl.col("amount").is_not_null()
             & (pl.col("amount") > 0) & (pl.col("amount") <= config.MAX_SANE_AMOUNT))
    df = df.with_columns(
        age_days=((pl.lit(now) - pl.col("ts")).dt.total_seconds() / 86400.0),
        hour=pl.col("ts").dt.hour(), dow=pl.col("ts").dt.weekday(),
        city=city_from_location(pl.col("customer_location")),
        ip_subnet=pl.col("customer_ip_address").cast(pl.Utf8).str.replace(r"\.\d+$", ".0/24"),
    ).with_columns(w=(pl.lit(0.5) ** (pl.col("age_days") / HALF)))

    amt = df["amount"]
    sw = float((df["w"]).sum()) or 1.0
    decayed_avg = float((df["w"] * df["amount"]).sum()) / sw
    branch_id, account_no = entity_key.split(":", 1)

    def win(days):
        return int(df.filter(pl.col("age_days") <= days).height)

    cities = topn_map(df, "entity_key", "city", min_count=OBS).get(entity_key, {})
    countries = topn_map(df, "entity_key", "origin_country", min_count=OBS).get(entity_key, {})
    hours = df.group_by("hour").agg(pl.len().alias("c"))
    hour_map = {str(int(r["hour"])): int(r["c"]) for r in hours.iter_rows(named=True)}
    days = df.group_by("dow").agg(pl.len().alias("c"))
    day_map = {DOW[int(r["dow"])]: int(r["c"]) for r in days.iter_rows(named=True)}

    gate_days = tenure.get("tenure_days") or 0
    gate_txns = tenure.get("lifetime_clean_txns") or df.height
    # §1: ">= 90 days history AND >= 100 transactions AND No confirmed fraud cases"
    fraud_txns = int(tenure.get("fraud_txns") or 0)
    is_active = (gate_days >= config.ELIGIBLE_MIN_TENURE_DAYS
                 and gate_txns >= config.ELIGIBLE_MIN_TXNS
                 and fraud_txns <= config.ELIGIBLE_MAX_FRAUD_TXNS)
    present = sum(1 for m in (cities, countries, hour_map,
                              topn_map(df, "entity_key", "destination_account_no").get(entity_key)) if m)
    confidence = compute_confidence(gate_days, gate_txns, _jnum(amt.mean()) or 0, _jnum(amt.std()) or 0, present)
    drift_status, drift_reason = ("none", None)
    if prev and prev.get("drift_status") is not None:
        drift_status, drift_reason = detect_drift(prev, decayed_avg, cities, countries)

    last_seen = df["ts"].max()
    last_row = df.sort("ts").tail(1)
    last_location = last_row["customer_location"][0] if last_row.height else None
    last_country = last_row["origin_country"][0] if last_row.height else None
    prof = {
        "entity_key": entity_key, "currency": currency,
        "branch_id": int(branch_id), "origin_account_no": account_no,
        "customer_name": df["customer_name"].drop_nulls().first() if df.height else None,
        "identifier": df["identifier"].drop_nulls().first() if df.height else None,
        "account_type": df["account_type"].drop_nulls().first() if df.height else None,
        "profile_status": "active" if is_active else "warming_up",
        "confidence_score": confidence, "drift_status": drift_status, "drift_reason": drift_reason,
        "retrain_reason": "event", "txns_since_build": 0, "drift_signal_count": 0,
        "last_retrained_at": now, "tenure_days": gate_days,
        "lifetime_clean_txns": gate_txns,
        "suspicious_tx_count": fraud_txns,   # §1 gate evidence, auditable
        "first_seen": df["ts"].min(), "last_seen": last_seen,
        "dormant_days": int((now - last_seen).days) if last_seen else None,
        "total_tx_count": df.height, "total_tx_amount": _jnum(amt.sum()) or 0,
        "tx_count_24h": win(1), "tx_count_7d": win(7), "tx_count_30d": win(30),
        "tx_count_60d": win(60), "tx_count_90d": win(90),
        "avg_amount": _jnum(amt.mean()), "max_amount": _jnum(amt.max()), "min_amount": _jnum(amt.min()),
        "std_amount": _jnum(amt.std()), "median_amount": _jnum(amt.median()),
        "p95_amount": _jnum(amt.quantile(0.95)), "decayed_avg_amount": decayed_avg,
        "decay_half_life_days": HALF,
        "distinct_beneficiaries": int(df["destination_account_no"].n_unique()),
        "beneficiaries": json.dumps(topn_map(df, "entity_key", "destination_account_no").get(entity_key, {})),
        "usual_transaction_types": json.dumps(topn_map(df, "entity_key", "transaction_type").get(entity_key, {})),
        "transaction_type_entropy": entropy_map(df, "entity_key", "transaction_type").get(entity_key, 0.0),
        "usual_cities": json.dumps(cities), "usual_countries": json.dumps(countries),
        "usual_merchants": json.dumps(topn_map(df, "entity_key", "merchant_name", min_count=OBS).get(entity_key, {})),
        "known_ip_subnets": json.dumps(topn_map(df, "entity_key", "ip_subnet").get(entity_key, {})),
        "peak_transaction_hours": json.dumps(hour_map),
        "top_hour": int(max(hour_map, key=hour_map.get)) if hour_map else None,
        "peak_transaction_days": json.dumps(day_map),
        "top_day_of_week": max(day_map, key=day_map.get) if day_map else None,
        "last_location": last_location,
        "last_country": last_country,
        "last_event_ts": last_seen,
        "profile_version": (prev.get("profile_version", 0) + 1) if prev else 1,
        "window_end": last_seen,
    }
    return prof


def _lock(cur, key):
    """Per-customer advisory lock (Postgres). Non-blocking: a concurrent retrain
    of the SAME customer is reported 'busy' and skipped. Different customers
    never contend, so they retrain in parallel."""
    return db.try_lock(cur, key)


def _unlock(cur, key):
    db.unlock(cur, key)


def _diagnose_empty(conn, entity_key: str) -> dict:
    """Explain WHY there was nothing to learn from — precisely.

    "no clean history" is dangerously ambiguous on its own: in an AML system it reads
    as "every transaction this customer has is dirty", which is a serious claim. In
    reality there are three quite different causes, and only the last one is about the
    customer at all:

      1. cache_not_populated       - the local cache is empty; no sync has run yet.
                                     Says nothing about the customer.
      2. customer_not_in_cache     - the cache has data, but none for this customer
                                     (no activity in the window, or the capped sync has
                                     not reached their rows yet). Also says nothing bad.
      3. all_cached_txns_excluded  - the customer HAS cached transactions in the window,
                                     but every one is suspicious/blocked/blacklisted, so
                                     §1 ("learn only from clean") leaves nothing to learn.
                                     THIS one is a real signal about the customer.
    """
    cur = db.dict_cursor(conn)
    cur.execute("SELECT count(*) AS n FROM bp_transactions_cache")
    total = int((cur.fetchone() or {}).get("n") or 0)
    cur.execute("SELECT count(*) AS n FROM bp_transactions_cache WHERE entity_key=%s "
                f"AND date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months')",
                (entity_key,))
    theirs = int((cur.fetchone() or {}).get("n") or 0)

    if total == 0:
        return {
            "reason": "cache_not_populated",
            "meaning": "The local transaction cache is EMPTY — no sync has run yet.",
            "about_this_customer": False,
            "note": ("Nothing has been ingested from production yet, so there is nothing "
                     "for ANY customer to learn from. Run the sync (POST /sync, or "
                     "sync_manager.py) once production access is available."),
            "cache_rows_total": total, "cache_rows_for_customer": theirs,
        }
    if theirs == 0:
        return {
            "reason": "customer_not_in_cache",
            "meaning": (f"The cache holds {total:,} transactions, but none for this "
                        f"customer inside the {config.LOOKBACK_MONTHS}-month window."),
            "about_this_customer": False,
            "note": ("Either they had no activity in the learning window, or the sync is "
                     "capped per run and has not reached their rows yet. Not a red flag."),
            "cache_rows_total": total, "cache_rows_for_customer": theirs,
        }
    return {
        "reason": "all_cached_txns_excluded",
        "meaning": (f"This customer HAS {theirs:,} transactions in the window, but every "
                    f"one is suspicious / blocked / blacklisted, so there is nothing "
                    f"CLEAN to learn from."),
        "about_this_customer": True,
        "note": ("This IS a signal about the customer. Under 'Practical rules' §1/§7 we "
                 "learn only from clean transactions, so their profile is deliberately "
                 "NOT rebuilt from dirty data — it is left as-is and they stay untrusted "
                 "rather than having fraud absorbed into their 'normal'."),
        "cache_rows_total": total, "cache_rows_for_customer": theirs,
    }


def retrain_customer(entity_key: str, trigger: str = "manual") -> dict:
    """Force a recompute of one customer's profile now, FROM THE LOCAL CACHE.
    Per-customer locked. Every outcome — success, failure, no-history, busy — is
    logged for audit. This never reads production."""
    from audit import log_event
    branch_id, account_no = entity_key.split(":", 1)
    conn = db.connect()
    cur = db.dict_cursor(conn)
    if not _lock(cur, entity_key):
        conn.close()
        log_event(entity_key, "retrain_skip", "busy", {"reason": "another retrain in progress"})
        return {"entity_key": entity_key, "retrained": False, "reason": "busy"}
    try:
        cur.execute("SELECT * FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
        prev_by_ccy = {r["currency"]: r for r in cur.fetchall()}
        df, tenure = fetch_customer(branch_id, account_no, conn=conn)
        if df is None or df.height == 0:
            # "nothing to learn from" has THREE very different causes and they must not be
            # confused — especially in an AML system, where "no clean history" could be
            # misread as "this customer's every transaction is dirty". Diagnose which.
            diag = _diagnose_empty(conn, entity_key)
            log_event(entity_key, "retrain_skip", diag["reason"], diag)
            return {"entity_key": entity_key, "retrained": False, **diag}

        # ONE profile per (normalized) currency. Amount stats are computed WITHIN each
        # currency, so a customer's NGN and USD baselines never blend. skip_nulls=False so
        # an untagged currency normalizes to the default (NGN) rather than a null group.
        df = df.with_columns(
            pl.col("currency").map_elements(config.normalize_currency,
                                            return_dtype=pl.Utf8, skip_nulls=False).alias("_ccy"))
        tenure_days = int(tenure.get("tenure_days") or 0)   # customer-level (account age)
        fraud_txns = int(tenure.get("fraud_txns") or 0)     # customer-level (fraud is per customer)
        # dominant currency first (most rows) so the summary/return reflects it
        ccy_order = (df.group_by("_ccy").agg(pl.len().alias("n"))
                       .sort("n", descending=True)["_ccy"].to_list())

        built = []
        for ccy in ccy_order:
            gdf = df.filter(pl.col("_ccy") == ccy)
            prev_c = prev_by_ccy.get(ccy)
            # lifetime_clean_txns: carry the per-currency authoritative figure forward when
            # we have a prior row for THIS currency; else fall back to the window count,
            # which under-states and so keeps a thin/rare currency in warming_up (peer-judged).
            life = (int(prev_c["lifetime_clean_txns"])
                    if prev_c and (prev_c.get("lifetime_clean_txns") or 0) > 0 else int(gdf.height))
            tenure_c = {"tenure_days": tenure_days, "lifetime_clean_txns": life,
                        "fraud_txns": fraud_txns}
            prof = compute_customer_profile(gdf, entity_key, prev_c, tenure_c, ccy)
            cols = list(prof.keys())
            cur.execute(db.upsert_sql("bp_user_behaviour_profile", cols, ["entity_key", "currency"]),
                        [prof[c] for c in cols])
            cur.execute("INSERT INTO bp_profile_history (entity_key, currency, build_run_id, "
                        "profile_version, total_tx_count, total_tx_amount, avg_amount, "
                        "decayed_avg_amount, profile_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (entity_key, ccy, "event", prof["profile_version"], prof["total_tx_count"],
                         prof["total_tx_amount"], prof["avg_amount"], prof["decayed_avg_amount"],
                         json.dumps(prof, default=str) if config.STORE_HISTORY_JSON else None))
            built.append(prof)
        conn.commit()                            # <-- profile(s) now SAVED in PostgreSQL

        primary = built[0]                       # dominant currency
        # Explicit, visible proof of the WRITE — host, db, which currency rows + versions.
        audit.log.info(
            "DB WRITE ok | entity=%s -> postgres://%s:%s/%s | bp_user_behaviour_profile "
            "upsert %d currency row(s): %s | learned from %s cached txns",
            entity_key, config.STORE_PG["host"], config.STORE_PG["port"], config.STORE_PG["dbname"],
            len(built), ", ".join(f"{p['currency']} v{p['profile_version']}" for p in built),
            sum(p["total_tx_count"] for p in built))
        learned = {
            # a COUNT of transactions the profile was learned from — not money, not days
            "learned_from_txn_count": primary["total_tx_count"],
            "currency": primary["currency"],
            "usual_avg_amount": primary["avg_amount"],
            "recency_weighted_avg": primary["decayed_avg_amount"],
            "biggest_ever": primary["max_amount"],
            "usual_cities": list(json.loads(primary["usual_cities"] or "{}"))[:5],
            "busiest_day": primary["top_day_of_week"],
        }
        out = {"entity_key": entity_key, "retrained": True, "trigger": trigger,
               "saved_to": "PostgreSQL · bp_user_behaviour_profile",
               "learned_from": "local cache (bp_transactions_cache) — production untouched",
               "currencies": [{"currency": p["currency"], "version": p["profile_version"],
                               "status": p["profile_status"], "confidence": p["confidence_score"],
                               "learned_from_txn_count": p["total_tx_count"]} for p in built],
               "version": primary["profile_version"], "status": primary["profile_status"],
               "confidence": primary["confidence_score"], "drift": primary["drift_status"],
               "learned": learned}
        # explicit, human-readable proof in the logs that fresh behaviour was learnt AND
        # persisted — so Anita can see exactly what the system saved, per currency.
        log_event(entity_key, "retrain", "learned_and_saved", out)
        return out
    except Exception as e:                       # retrain failure is LOGGED, not swallowed silently
        conn.rollback()
        log_event(entity_key, "retrain_fail", "failed", {"error": str(e), "trigger": trigger})
        return {"entity_key": entity_key, "retrained": False, "reason": "error", "error": str(e)}
    finally:
        _unlock(cur, entity_key)
        conn.close()


def retrain_decision(entity_key: str) -> dict:
    """Evaluate the OR-triggers for a customer WITHOUT retraining. Returns the full
    breakdown (each check + whether met + how far off), so 'why / why not' is explicit.
    Reads only the profile store — no production, no cache. Runs after EVERY /score, so it
    uses the POOL (an established connection) — a fresh connect here would dominate latency
    under load."""
    # A customer now has one row per currency; retrain (which rebuilds ALL their currencies)
    # is due if ANY currency crosses a trigger. Aggregate: MAX new-txn / drift counters, and
    # the OLDEST (MIN) last_retrained_at so the most stale currency drives the age check.
    with db.pooled() as conn:
        cur = db.dict_cursor(conn)          # cursor-scoped dict rows (does not touch the conn)
        cur.execute("SELECT MAX(txns_since_build) AS nt, MAX(drift_signal_count) AS dr, "
                    "MIN(last_retrained_at) AS oldest, COUNT(*) AS n "
                    "FROM bp_user_behaviour_profile WHERE entity_key=%s", (entity_key,))
        p = cur.fetchone()
    if p is None or int(p["n"] or 0) == 0:
        return {"has_profile": False,
                "reason": "no profile yet — customer is new/Warming-Up, judged against peers"}
    nt = int(p["nt"] or 0)
    ds = (datetime.utcnow() - p["oldest"]).days if p["oldest"] else None
    dr = int(p["dr"] or 0)
    checks = {
        "new_transactions": {"value": nt, "threshold": config.RETRAIN_MIN_NEW_TXNS, "met": nt >= config.RETRAIN_MIN_NEW_TXNS},
        "days_since_build": {"value": ds, "threshold": config.RETRAIN_MAX_AGE_DAYS,
                             "met": ds is not None and ds >= config.RETRAIN_MAX_AGE_DAYS},
        "drift_signals": {"value": dr, "threshold": config.DRIFT_SIGNAL_THRESHOLD, "met": dr >= config.DRIFT_SIGNAL_THRESHOLD},
    }
    trigger = next((k for k, v in checks.items() if v["met"]), None)
    return {"has_profile": True, "due": trigger is not None, "trigger": trigger, "checks": checks}


def maybe_retrain(entity_key: str) -> dict:
    """Retrain iff an OR-trigger is met. ALWAYS returns a structured decision
    (retrained, or skipped-with-reason), and logs the skip for accountability."""
    from audit import log_event
    d = retrain_decision(entity_key)
    if not d.get("has_profile"):
        return {"retrained": False, "reason": d["reason"], "checks": None}
    if not d["due"]:
        c = d["checks"]
        reason = ("not due — needs "
                  f"{c['new_transactions']['threshold'] - c['new_transactions']['value']} more txns, "
                  f"or {c['days_since_build']['threshold'] - (c['days_since_build']['value'] or 0)} more days, "
                  f"or {c['drift_signals']['threshold'] - c['drift_signals']['value']} more drift signals")
        log_event(entity_key, "retrain_skip", "skipped", {"reason": reason, "checks": c})
        return {"retrained": False, "reason": reason, "checks": c}
    return retrain_customer(entity_key, trigger=d["trigger"])


def rebuild_all_from_cache(min_rows: int | None = None, limit: int | None = None) -> dict:
    """ONE-TIME batch (re)build of profiles from the local cache — the initial SEED,
    the modern equivalent of build_profiles.py (reads the cache instead of a CSV).

    This is NOT a cron and NOT auto-fired. Steady-state refresh stays purely
    event-driven per customer (maybe_retrain). Run this ONCE after the cache has fully
    backfilled to get a clean, current snapshot of every profile:

        python retrain.py --rebuild-all

    It rebuilds each customer via the SAME per-customer path used at runtime
    (retrain_customer) — same clean-only filter, §1 gate, drift, carry-forward — so a
    batch profile is identical to an event-driven one. Per-customer locked and safe to
    re-run or resume (each customer is independent).
    """
    min_rows = config.REBUILD_MIN_ROWS if min_rows is None else min_rows
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute(
        "SELECT entity_key, count(*) AS n FROM bp_transactions_cache "
        f"WHERE date_created >= (now() - interval '{int(config.LOOKBACK_MONTHS)} months') "
        "GROUP BY entity_key HAVING count(*) >= %s ORDER BY count(*) DESC",
        (min_rows,),
    )
    keys = [r["entity_key"] for r in cur.fetchall()]
    conn.close()
    if limit:
        keys = keys[:limit]

    total = len(keys)
    audit.log.info("rebuild_all START — %d customers in cache (min_rows=%d). This is a "
                   "ONE-TIME seed; steady-state refresh remains event-driven.", total, min_rows)
    built = skipped = failed = 0
    for i, ek in enumerate(keys, 1):
        try:
            out = retrain_customer(ek, trigger="batch_rebuild")
            if out.get("retrained"):
                built += 1
            else:
                skipped += 1
        except Exception as e:                       # never let one customer stop the seed
            failed += 1
            audit.log.warning("rebuild_all: %s failed: %s", ek, e)
        if i % 500 == 0 or i == total:
            audit.log.info("rebuild_all progress %d/%d (built=%d skipped=%d failed=%d)",
                           i, total, built, skipped, failed)
    summary = {"customers": total, "built": built, "skipped": skipped, "failed": failed}
    audit.log.info("rebuild_all DONE %s", summary)
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Per-customer retrain, or the one-time "
                                             "batch seed of all profiles from the cache.")
    ap.add_argument("entity_key", nargs="?", help="e.g. 231:1100716290")
    ap.add_argument("--force", action="store_true", help="force this customer's retrain now")
    ap.add_argument("--rebuild-all", action="store_true",
                    help="ONE-TIME: rebuild every cached customer's profile (initial seed)")
    ap.add_argument("--min-rows", type=int, default=None,
                    help="with --rebuild-all: only rebuild customers with >= this many cached rows")
    ap.add_argument("--limit", type=int, default=None,
                    help="with --rebuild-all: cap how many customers (for a quick trial)")
    a = ap.parse_args()

    import json
    if a.rebuild_all:
        print(json.dumps(rebuild_all_from_cache(min_rows=a.min_rows, limit=a.limit), indent=2))
    elif a.entity_key:
        print(json.dumps(retrain_customer(a.entity_key) if a.force
                         else maybe_retrain(a.entity_key), indent=2, default=str))
    else:
        ap.error("give an entity_key, or --rebuild-all")
