#!/usr/bin/env python3
"""
Step 2 — Build the behaviour profile for every entity and save it to PostgreSQL.

The AI system LEARNS each profile from the raw transaction history itself (no
hand-authored per-customer facts). Entity = (branch_id, origin_account_no).

Implements the "thought flow" requirements WITHOUT any ML model yet:
  * Sliding time windows      : 24h / 7d / 30d / 60d / 90d counts & sums.
  * Velocity metrics          : windowed tx counts + distinct-beneficiary counts.
  * Monetary metrics          : avg / max / std / median / p95, windowed sums.
  * Time-decay (exp smoothing) : decayed mean/count with a half-life (older = lighter).
  * Categorical diversity      : Shannon entropy over tx-type / merchant / location.
  * Geo fingerprint            : usual cities/countries, known IPs & /24 subnets,
                                 last location+ts (feeds impossible-travel).
  * Temporal fingerprint       : 24-bucket peak-hour histogram, night ratio.
  * Incremental state          : EWMA mean/var + decayed count saved separately so
                                 the nightly job can update in place (self-updating).

Sinks (PostgreSQL profile store):
  bp_user_behaviour_profile (upsert) | bp_profile_history (append) |
  bp_incremental_state (upsert)       | bp_build_run (lineage)

Usage:
    python build_profiles.py --in data/transactions_sample.csv
"""
import argparse
import json
import math
import os
import uuid
from datetime import datetime

import polars as pl

import config
import db

HALF_LIFE = config.DECAY_HALF_LIFE_DAYS
TOPN = config.TOP_N_CATEGORICAL


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def city_from_location(expr: pl.Expr) -> pl.Expr:
    """Heuristic city extraction from free-text customer_location.
    'no 9 zafarawa street, Gombi, Adamawa, ...' -> 'Gombi'. Falls back to the
    first token. Consistency per-user matters more than perfect naming for the
    'unusual city' rule."""
    parts = expr.str.split(",")
    return (
        pl.when(parts.list.len() >= 2)
        .then(parts.list.get(1, null_on_oob=True).str.strip_chars())
        .otherwise(parts.list.get(0, null_on_oob=True).str.strip_chars())
    )


def topn_map(df: pl.DataFrame, key: str, col: str, n: int = TOPN, min_count: int = 1) -> dict:
    """Return {entity_key: {value: count}} keeping the top-n values per entity.
    §8 stability: a value must appear >= min_count times to be kept, so a single
    one-off event never becomes part of the customer's "usual" set."""
    g = (
        df.filter(pl.col(col).is_not_null() & (pl.col(col).cast(pl.Utf8) != ""))
        .group_by([key, col])
        .agg(pl.len().alias("c"))
        .filter(pl.col("c") >= min_count)
        .sort([key, "c"], descending=[False, True])
        .group_by(key, maintain_order=True)
        .agg([pl.col(col).head(n).alias("vals"), pl.col("c").head(n).alias("cnts")])
    )
    out = {}
    for row in g.iter_rows(named=True):
        out[row[key]] = {str(v): int(c) for v, c in zip(row["vals"], row["cnts"])}
    return out


def entropy_map(df: pl.DataFrame, key: str, col: str) -> dict:
    """Shannon entropy (nats) of the categorical distribution per entity.https://redcanary.com/blog/threat-detection/threat-hunting-entropy/"""
    counts = (
        df.filter(pl.col(col).is_not_null() & (pl.col(col).cast(pl.Utf8) != ""))
        .group_by([key, col])
        .agg(pl.len().alias("c"))
    )
    ent = counts.group_by(key).agg(
        (
            -(
                (pl.col("c") / pl.col("c").sum())
                * (pl.col("c") / pl.col("c").sum()).log()
            ).sum()
        ).alias("H")
    )
    return {r[key]: (float(r["H"]) if r["H"] is not None else 0.0) for r in ent.iter_rows(named=True)}


def group_topn(df: pl.DataFrame, keys: list[str], col: str, n: int = TOPN) -> dict:
    """Top-n values of `col` per (composite) group -> {(*keys): {value: count}}."""
    g = (
        df.filter(pl.col(col).is_not_null() & (pl.col(col).cast(pl.Utf8) != ""))
        .group_by(keys + [col])
        .agg(pl.len().alias("c"))
        .sort(keys + ["c"], descending=[False] * len(keys) + [True])
        .group_by(keys, maintain_order=True)
        .agg([pl.col(col).head(n).alias("vals"), pl.col("c").head(n).alias("cnts")])
    )
    out = {}
    for row in g.iter_rows(named=True):
        k = tuple(row[kk] for kk in keys)
        out[k] = {str(v): int(c) for v, c in zip(row["vals"], row["cnts"])}
    return out


def build_peer_baselines(df: pl.DataFrame, months: float, run_id: str) -> list[tuple]:
    """Non-ML cold-start baseline: average behaviour per peer group
    (branch_id + account_type). A brand-new account with no history of its own
    inherits its peer group's baseline until it accumulates its own. Plain group
    arithmetic — no clustering, no embeddings, no ML."""
    d = df.with_columns(pl.col("account_type").fill_null("unknown").alias("acct_type"))
    grp = d.group_by(["branch_id", "acct_type"]).agg([
        pl.col("entity_key").n_unique().alias("peer_entities"),
        pl.len().alias("peer_tx_count"),
        pl.mean("amount").alias("avg_amount"),
        pl.median("amount").alias("median_amount"),
        pl.col("amount").quantile(0.95).alias("p95_amount"),
        pl.max("amount").alias("max_amount"),
        pl.std("amount").alias("std_amount"),
    ]).with_columns(
        avg_monthly_tx_count=(pl.col("peer_tx_count") / months / pl.col("peer_entities")),
    )
    cities = group_topn(d, ["branch_id", "acct_type"], "city")
    countries = group_topn(d, ["branch_id", "acct_type"], "origin_country")
    # peak-hour histogram per peer group
    hours = d.group_by(["branch_id", "acct_type", "hour"]).agg(pl.len().alias("c"))
    hour_map: dict[tuple, dict] = {}
    for r in hours.iter_rows(named=True):
        hour_map.setdefault((r["branch_id"], r["acct_type"]), {})[str(int(r["hour"]))] = int(r["c"])

    rows = []
    for r in grp.iter_rows(named=True):
        key = (r["branch_id"], r["acct_type"])

        def jnum(x):
            if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
                return None
            return x

        rows.append((
            r["branch_id"], r["acct_type"], int(r["peer_entities"]), int(r["peer_tx_count"]),
            jnum(r["avg_amount"]), jnum(r["median_amount"]), jnum(r["p95_amount"]),
            jnum(r["max_amount"]), jnum(r["std_amount"]), jnum(r["avg_monthly_tx_count"]),
            json.dumps(cities.get(key, {})), json.dumps(countries.get(key, {})),
            json.dumps(hour_map.get(key, {})), run_id,
        ))
    return rows


def write_peer_baselines(rows: list[tuple]) -> None:
    if not rows:
        return
    conn = db.connect()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO bp_peer_baseline (branch_id, account_type, peer_entities, peer_tx_count, "
        "avg_amount, median_amount, p95_amount, max_amount, std_amount, avg_monthly_tx_count, "
        "usual_cities, usual_countries, peak_transaction_hours, build_run_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (branch_id, account_type) DO UPDATE SET "
        "peer_entities=EXCLUDED.peer_entities, peer_tx_count=EXCLUDED.peer_tx_count, "
        "avg_amount=EXCLUDED.avg_amount, median_amount=EXCLUDED.median_amount, "
        "p95_amount=EXCLUDED.p95_amount, max_amount=EXCLUDED.max_amount, std_amount=EXCLUDED.std_amount, "
        "avg_monthly_tx_count=EXCLUDED.avg_monthly_tx_count, usual_cities=EXCLUDED.usual_cities, "
        "usual_countries=EXCLUDED.usual_countries, peak_transaction_hours=EXCLUDED.peak_transaction_hours, "
        "build_run_id=EXCLUDED.build_run_id",
        rows,
    )
    conn.commit()
    conn.close()
    print(f"[build] wrote {len(rows)} peer baselines (branch x account_type) to PostgreSQL")


def load_tenure() -> dict:
    """Load lifetime stats from data/tenure.csv (produced by extract_tenure.py).
    Returns {entity_key: {tenure_days, lifetime_txns, lifetime_clean_txns}}.
    Empty dict if the file is missing (gate then falls back to window stats)."""
    path = os.path.join(config.DATA_DIR, "tenure.csv")
    if not os.path.exists(path):
        print("[build] WARNING: data/tenure.csv not found — run extract_tenure.py; "
              "eligibility will fall back to window history only")
        return {}
    t = pl.read_csv(path, infer_schema_length=5000, ignore_errors=True).with_columns(
        pl.col("first_seen_ever")
        .str.to_datetime(format="%Y-%m-%d %H:%M:%S%.f%#z", strict=False, time_unit="us")
        .dt.convert_time_zone("UTC").dt.replace_time_zone(None).alias("fs"),
        (pl.col("branch_id").cast(pl.Utf8) + ":" + pl.col("origin_account_no").cast(pl.Utf8)).alias("ek"),
    )
    now = datetime.utcnow()
    t = t.with_columns(((pl.lit(now) - pl.col("fs")).dt.total_seconds() / 86400.0).alias("tenure_days"))
    out = {}
    for r in t.iter_rows(named=True):
        out[r["ek"]] = {
            "tenure_days": int(r["tenure_days"]) if r["tenure_days"] is not None else 0,
            "lifetime_txns": int(r["lifetime_txns"] or 0),
            "lifetime_clean_txns": int(r["lifetime_clean_txns"] or 0),
        }
    print(f"[build] loaded lifetime tenure for {len(out):,} accounts")
    return out


def load_prev_profiles() -> dict:
    """Load the previous build's profile per entity, for drift (§9) and
    incremental-retrain (§4) decisions. Returns {entity_key: {...}}."""
    conn = db.connect()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT entity_key, avg_amount, decayed_avg_amount, usual_cities, "
                "usual_countries, last_seen, updated_at, drift_status, profile_version "
                "FROM bp_user_behaviour_profile")
    prev = {r["entity_key"]: r for r in cur.fetchall()}
    conn.close()
    print(f"[build] loaded {len(prev):,} previous profiles (for drift + incremental retrain)")
    return prev


def detect_drift(prev: dict, new_decayed_avg, new_cities: dict, new_countries: dict):
    """§9 basic drift: compare the new profile to the previous one. Returns
    (drift_status, reason). 'sudden' = a big amount jump or a changed dominant
    city / a new country; 'gradual'/'none' otherwise."""
    if not prev:
        return "none", None
    reasons = []
    # amount jump (recency-weighted average)
    pa = prev.get("decayed_avg_amount")
    try:
        pa = float(pa) if pa is not None else None
    except (TypeError, ValueError):
        pa = None
    if pa and new_decayed_avg and pa > 0:
        change = abs(new_decayed_avg - pa) / pa
        if change >= config.DRIFT_AMOUNT_PCT:
            reasons.append(f"amount {('up' if new_decayed_avg > pa else 'down')} {change*100:.0f}%")
    # dominant city changed
    def top(js):
        try:
            d = json.loads(js) if isinstance(js, str) else (js or {})
            return max(d, key=d.get) if d else None
        except (ValueError, TypeError):
            return None
    prev_top_city, new_top_city = top(prev.get("usual_cities")), (max(new_cities, key=new_cities.get) if new_cities else None)
    if prev_top_city and new_top_city and prev_top_city != new_top_city:
        reasons.append(f"main city {prev_top_city}->{new_top_city}")
    # a new country appeared
    try:
        prev_ctries = set(json.loads(prev.get("usual_countries") or "{}"))
    except (ValueError, TypeError):
        prev_ctries = set()
    new_ctry = set(new_countries) - prev_ctries
    if prev_ctries and new_ctry:
        reasons.append(f"new country {','.join(list(new_ctry)[:2])}")
    if not reasons:
        return "none", None
    return "sudden", "; ".join(reasons)


def compute_confidence(tenure_days, clean_txns, avg_amount, std_amount, present_fields) -> int:
    """0-100 confidence ("Practical rules" §10) = history + consistency + completeness.
      history      : how much clean history exists (tenure & clean-txn count vs the gate)
      consistency  : how steady the amounts are (low variation => more predictable)
      completeness : how many profile dimensions we actually have data for
    """
    h_days = min(1.0, (tenure_days or 0) / max(config.ELIGIBLE_MIN_TENURE_DAYS, 1))
    h_txn = min(1.0, (clean_txns or 0) / max(config.ELIGIBLE_MIN_TXNS, 1))
    history = 0.5 * (h_days + h_txn)
    cv = (std_amount / (avg_amount + 1.0)) if (avg_amount and avg_amount > 0) else 5.0
    consistency = 1.0 / (1.0 + cv)                 # 1 = perfectly steady
    completeness = present_fields / 5.0            # of 5 key dimensions
    score = 100.0 * (0.5 * history + 0.3 * consistency + 0.2 * completeness)
    return int(max(0, min(100, round(score))))


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------
def build(in_path: str, prune: bool = True) -> None:
    run_id = f"bp_{datetime.utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    print(f"[build] run_id={run_id}  reading {in_path}")

    df = pl.read_csv(in_path, infer_schema_length=5000, ignore_errors=True)
    # types
    df = df.with_columns(
        pl.col("amount").cast(pl.Float64, strict=False),
        # timestamps look like '2026-05-07 02:07:24.420115+00'; parse tz-aware,
        # normalise to UTC, then drop tz so values map cleanly to Postgres TIMESTAMP.
        pl.col("date_created")
        .str.to_datetime(format="%Y-%m-%d %H:%M:%S%.f%#z", strict=False, time_unit="us")
        .dt.convert_time_zone("UTC")
        .dt.replace_time_zone(None)
        .alias("ts"),
        (pl.col("branch_id").cast(pl.Utf8) + ":" + pl.col("origin_account_no").cast(pl.Utf8)).alias("entity_key"),
    ).filter(pl.col("ts").is_not_null() & pl.col("amount").is_not_null())

    # data-quality guard: drop impossible amounts (bad/test data) so they can't
    # poison a customer's max/avg/p95 or the peer baseline
    before = df.height
    df = df.filter((pl.col("amount") > 0) & (pl.col("amount") <= config.MAX_SANE_AMOUNT))
    dropped = before - df.height
    if dropped:
        print(f"[build] dropped {dropped:,} rows with impossible amount (> {config.MAX_SANE_AMOUNT:,.0f} NGN or <= 0)")

    # §1 remove duplicated transactions (same transaction_id within a branch)
    before = df.height
    df = df.unique(subset=["branch_id", "transaction_id"], keep="first")
    if before - df.height:
        print(f"[build] dropped {before - df.height:,} duplicate transactions")

    # §1/§7 CLEAN BASELINE: learn ONLY from clean transactions — exclude anything
    # flagged suspicious, blocked, or tied to a blacklisted party, so fraudulent
    # behaviour never becomes part of a customer's "normal".
    def _boolish(col):
        return pl.col(col).cast(pl.Utf8).str.to_lowercase().is_in(["t", "true", "1"])

    # §1 "No confirmed fraud cases": count each customer's confirmed-fraud txns
    # BEFORE the clean filter removes them — afterwards the evidence is gone.
    # (This is why suspicious_tx_count used to always be 0: it was measured after
    # the filter, on rows that were clean by construction.)
    _fraud = (
        df.filter((pl.col("status") != "clean")
                  | _boolish("is_blocked")
                  | _boolish("sender_blacklisted"))
          .group_by("entity_key").agg(pl.len().alias("n"))
    )
    FRAUD_TXNS: dict[str, int] = {r["entity_key"]: int(r["n"])
                                  for r in _fraud.iter_rows(named=True)}
    print(f"[build] §1 fraud gate: {len(FRAUD_TXNS):,} customers have >=1 confirmed-fraud "
          f"txn in the window and cannot be trusted (max allowed = {config.ELIGIBLE_MAX_FRAUD_TXNS})")

    if config.LEARN_FROM_CLEAN_ONLY:
        before = df.height
        df = df.filter(
            (pl.col("status") == "clean")
            & (~_boolish("is_blocked"))
            & (~_boolish("sender_blacklisted"))
        )
        print(f"[build] clean-baseline filter kept {df.height:,} of {before:,} rows "
              f"(dropped {before - df.height:,} suspicious/blocked/blacklisted)")

    window_end = df["ts"].max()
    window_start = df["ts"].min()
    print(f"[build] window {window_start} -> {window_end}, {df.height:,} rows")

    ln2 = math.log(2.0)
    df = df.with_columns(
        age_days=((pl.lit(window_end) - pl.col("ts")).dt.total_seconds() / 86400.0),
        hour=pl.col("ts").dt.hour(),
        dow=pl.col("ts").dt.weekday(),          # ISO weekday 1=Mon .. 7=Sun (day-of-week pattern)
        is_suspicious=(pl.col("status") == "suspicious"),
        city=city_from_location(pl.col("customer_location")),
        ip_subnet=pl.col("customer_ip_address").cast(pl.Utf8).str.replace(r"\.\d+$", ".0/24"),
    )
    df = df.with_columns(
        w_decay=(pl.lit(0.5) ** (pl.col("age_days") / HALF_LIFE)),
        in_24h=(pl.col("age_days") <= 1),
        in_7d=(pl.col("age_days") <= 7),
        in_30d=(pl.col("age_days") <= 30),
        in_60d=(pl.col("age_days") <= 60),
        in_90d=(pl.col("age_days") <= 90),
        is_night=(pl.col("hour") < 6),
    )
    df = df.with_columns(
        wa=(pl.col("w_decay") * pl.col("amount")),
        wa2=(pl.col("w_decay") * pl.col("amount") ** 2),
    )

    # §4 join previous build's last_seen so we can count NEW transactions per account
    prev_profiles = load_prev_profiles()
    prev_ls = pl.DataFrame(
        {"entity_key": list(prev_profiles.keys()),
         "prev_last_seen": [p["last_seen"] for p in prev_profiles.values()]},
    ) if prev_profiles else pl.DataFrame({"entity_key": [], "prev_last_seen": []})
    if prev_ls.height and prev_ls["prev_last_seen"].dtype != pl.Datetime:
        prev_ls = prev_ls.with_columns(pl.col("prev_last_seen").cast(pl.Datetime("us"), strict=False))
    df = df.join(prev_ls, on="entity_key", how="left")

    span_days = max((window_end - window_start).days, 1)
    months = span_days / 30.0

    # ---- numeric aggregation: one row per entity ----
    agg = df.group_by("entity_key").agg([
        pl.first("branch_id").alias("branch_id"),
        pl.first("origin_account_no").alias("origin_account_no"),
        pl.col("customer_name").drop_nulls().first().alias("customer_name"),
        pl.col("identifier").drop_nulls().first().alias("identifier"),
        pl.col("bvn").drop_nulls().first().alias("bvn"),
        pl.col("account_type").drop_nulls().first().alias("account_type"),
        pl.min("ts").alias("first_seen"),
        pl.max("ts").alias("last_seen"),
        pl.len().alias("total_tx_count"),
        pl.sum("amount").alias("total_tx_amount"),
        # §4 count of transactions NEWER than the previous build's last_seen
        (pl.col("ts") > pl.col("prev_last_seen")).fill_null(False).sum().alias("new_txns"),
        # windowed counts
        pl.col("in_24h").sum().alias("tx_count_24h"),
        pl.col("in_7d").sum().alias("tx_count_7d"),
        pl.col("in_30d").sum().alias("tx_count_30d"),
        pl.col("in_60d").sum().alias("tx_count_60d"),
        pl.col("in_90d").sum().alias("tx_count_90d"),
        # windowed sums
        pl.col("amount").filter(pl.col("in_30d")).sum().alias("amt_sum_30d"),
        pl.col("amount").filter(pl.col("in_90d")).sum().alias("amt_sum_90d"),
        # monetary baseline
        pl.mean("amount").alias("avg_amount"),
        pl.max("amount").alias("max_amount"),
        pl.min("amount").alias("min_amount"),
        pl.std("amount").alias("std_amount"),
        pl.median("amount").alias("median_amount"),
        pl.col("amount").quantile(0.95).alias("p95_amount"),
        pl.col("amount").filter(pl.col("in_30d")).mean().alias("avg_amount_30d"),
        pl.col("amount").filter(pl.col("in_30d")).max().alias("max_amount_30d"),
        pl.col("amount").filter(pl.col("in_30d")).std().alias("std_amount_30d"),
        # time-decay accumulators
        pl.col("w_decay").sum().alias("sum_w"),
        pl.col("wa").sum().alias("sum_wa"),
        pl.col("wa2").sum().alias("sum_wa2"),
        # beneficiaries / velocity
        pl.col("destination_account_no").n_unique().alias("distinct_beneficiaries"),
        pl.col("destination_account_no").filter(pl.col("in_30d")).n_unique().alias("distinct_beneficiaries_30d"),
        pl.col("destination_account_no").filter(pl.col("in_24h")).n_unique().alias("distinct_beneficiaries_24h"),
        # temporal
        pl.col("is_night").mean().alias("night_activity_ratio"),
        # risk
        pl.col("is_suspicious").sum().alias("suspicious_tx_count"),
        pl.col("is_suspicious").mean().alias("suspicious_ratio"),
        (pl.col("sender_blacklisted").cast(pl.Utf8).str.to_lowercase() == "true").any().alias("is_blacklisted"),
        # last-event fingerprint (row with max ts)
        pl.col("customer_location").sort_by("ts").last().alias("last_location"),
        pl.col("city").sort_by("ts").last().alias("last_city"),
        pl.col("origin_country").sort_by("ts").last().alias("last_country"),
        pl.col("customer_ip_address").sort_by("ts").last().alias("last_ip"),
    ])

    # decayed mean/var, cadence, dormancy, top hour
    agg = agg.with_columns(
        decayed_avg_amount=(pl.col("sum_wa") / pl.col("sum_w")),
        decayed_tx_count=pl.col("sum_w"),
        age_days=((pl.lit(window_end) - pl.col("first_seen")).dt.total_seconds() / 86400.0),
        dormant_days=((pl.lit(window_end) - pl.col("last_seen")).dt.total_seconds() / 86400.0),
        avg_monthly_tx_count=(pl.col("total_tx_count") / months),
        avg_monthly_amount=(pl.col("total_tx_amount") / months),
    ).with_columns(
        ewma_var_amount=(pl.col("sum_wa2") / pl.col("sum_w") - (pl.col("sum_wa") / pl.col("sum_w")) ** 2),
    )

    # ---- categorical maps + entropy (separate passes) ----
    print("[build] computing categorical maps + entropy ...")
    types_map = topn_map(df, "entity_key", "transaction_type")
    types_ent = entropy_map(df, "entity_key", "transaction_type")
    # §8 stability: a place/merchant must be seen >= MIN_PATTERN_OBS times to be "usual"
    OBS = config.MIN_PATTERN_OBS
    merch_map = topn_map(df, "entity_key", "merchant_name", min_count=OBS)
    merch_ent = entropy_map(df, "entity_key", "merchant_name")
    loc_map = topn_map(df, "entity_key", "customer_location", min_count=OBS)
    city_map = topn_map(df, "entity_key", "city", min_count=OBS)
    ctry_map = topn_map(df, "entity_key", "origin_country", min_count=OBS)
    loc_ent = entropy_map(df, "entity_key", "city")
    ip_map = topn_map(df, "entity_key", "customer_ip_address")
    subnet_map = topn_map(df, "entity_key", "ip_subnet")
    benef_map = topn_map(df, "entity_key", "destination_account_no")
    # peak hour histogram (all 24 buckets)
    hours = (
        df.group_by(["entity_key", "hour"]).agg(pl.len().alias("c"))
        .sort(["entity_key", "hour"])
    )
    hour_map: dict[str, dict] = {}
    top_hour: dict[str, int] = {}
    for r in hours.iter_rows(named=True):
        d = hour_map.setdefault(r["entity_key"], {})
        d[str(int(r["hour"]))] = int(r["c"])
    for k, d in hour_map.items():
        top_hour[k] = int(max(d, key=d.get))

    # peak day-of-week histogram (§16 component) — {Mon..Sun: count}
    DOW = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
    days = df.group_by(["entity_key", "dow"]).agg(pl.len().alias("c"))
    day_map: dict[str, dict] = {}
    top_day: dict[str, str] = {}
    for r in days.iter_rows(named=True):
        d = day_map.setdefault(r["entity_key"], {})
        d[DOW.get(int(r["dow"]), str(r["dow"]))] = int(r["c"])
    for k, d in day_map.items():
        top_day[k] = max(d, key=d.get)

    # ---- lifetime tenure (for the eligibility gate) ----
    tenure = load_tenure()  # {entity_key: {tenure_days, lifetime_txns, lifetime_clean_txns}}

    # ---- assemble rows for the profile store ----
    print(f"[build] assembling {agg.height:,} profiles ...")
    active = 0
    build_now = datetime.utcnow()
    profile_rows, hist_rows, state_rows = [], [], []
    touch_keys = []          # §4: accounts seen this run but not materially changed
    drift_count = 0
    for r in agg.iter_rows(named=True):
        ek = r["entity_key"]

        def jnum(x):
            if x is None:
                return None
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
                return None
            return x

        # --- GOVERNANCE gate: eligibility (§1/§2) + confidence (§10) ---
        tv = tenure.get(ek, {})
        tenure_days = tv.get("tenure_days")
        lifetime_txns = tv.get("lifetime_txns")
        lifetime_clean = tv.get("lifetime_clean_txns")
        # if tenure data is missing, fall back to the window's own span/count
        gate_days = tenure_days if tenure_days is not None else (int(r["age_days"]) if r["age_days"] is not None else 0)
        gate_txns = lifetime_clean if lifetime_clean is not None else int(r["total_tx_count"])
        # §1: ">= 90 days history AND >= 100 transactions AND No confirmed fraud cases"
        fraud_txns = FRAUD_TXNS.get(ek, 0)
        is_active = (gate_days >= config.ELIGIBLE_MIN_TENURE_DAYS
                     and gate_txns >= config.ELIGIBLE_MIN_TXNS
                     and fraud_txns <= config.ELIGIBLE_MAX_FRAUD_TXNS)
        status = "active" if is_active else "warming_up"
        if is_active:
            active += 1
        present = sum(1 for m in (city_map.get(ek), ctry_map.get(ek), subnet_map.get(ek),
                                  benef_map.get(ek), hour_map.get(ek)) if m)
        confidence = compute_confidence(gate_days, gate_txns,
                                        jnum(r["avg_amount"]) or 0, jnum(r["std_amount"]) or 0, present)

        # --- §9 drift + §4 incremental-retrain decision (vs the previous build) ---
        prev = prev_profiles.get(ek)
        # drift can only be measured once a baselined previous profile exists
        if prev is None or prev.get("drift_status") is None:
            drift_status, drift_reason = "none", None
        else:
            drift_status, drift_reason = detect_drift(
                prev, jnum(r["decayed_avg_amount"]), city_map.get(ek, {}), ctry_map.get(ek, {}))
        new_txns = int(r["new_txns"] or 0)
        if not config.ENABLE_INCREMENTAL_RETRAIN:
            retrain, retrain_reason = True, "full_build"
        elif prev is None:
            retrain, retrain_reason = True, "new_profile"
        elif prev.get("drift_status") is None:      # columns just added -> populate once
            retrain, retrain_reason = True, "init"
        elif drift_status == "sudden":
            retrain, retrain_reason = True, "drift"
        elif new_txns >= config.RETRAIN_MIN_NEW_TXNS:
            retrain, retrain_reason = True, f"{new_txns}_new_txns"
        elif prev.get("updated_at") and (build_now - prev["updated_at"]).days >= config.RETRAIN_MAX_AGE_DAYS:
            retrain, retrain_reason = True, "periodic"
        else:
            retrain, retrain_reason = False, "unchanged"

        prof = {
            "entity_key": ek,
            "branch_id": r["branch_id"],
            "origin_account_no": r["origin_account_no"],
            "customer_id": None,
            "customer_name": r["customer_name"],
            "identifier": r["identifier"],
            "bvn": r["bvn"],
            "account_type": r["account_type"],
            "profile_status": status,
            "confidence_score": confidence,
            "drift_status": drift_status,
            "drift_reason": drift_reason,
            "retrain_reason": retrain_reason,
            "last_retrained_at": build_now,
            "tenure_days": gate_days,
            "lifetime_txns": lifetime_txns,
            "lifetime_clean_txns": lifetime_clean,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "age_days": int(r["age_days"]) if r["age_days"] is not None else None,
            "dormant_days": int(r["dormant_days"]) if r["dormant_days"] is not None else None,
            "total_tx_count": int(r["total_tx_count"]),
            "total_tx_amount": jnum(r["total_tx_amount"]) or 0,
            "tx_count_24h": int(r["tx_count_24h"]),
            "tx_count_7d": int(r["tx_count_7d"]),
            "tx_count_30d": int(r["tx_count_30d"]),
            "tx_count_60d": int(r["tx_count_60d"]),
            "tx_count_90d": int(r["tx_count_90d"]),
            "amt_sum_30d": jnum(r["amt_sum_30d"]) or 0,
            "amt_sum_90d": jnum(r["amt_sum_90d"]) or 0,
            "avg_amount": jnum(r["avg_amount"]),
            "max_amount": jnum(r["max_amount"]),
            "min_amount": jnum(r["min_amount"]),
            "std_amount": jnum(r["std_amount"]),
            "median_amount": jnum(r["median_amount"]),
            "p95_amount": jnum(r["p95_amount"]),
            "avg_amount_30d": jnum(r["avg_amount_30d"]),
            "max_amount_30d": jnum(r["max_amount_30d"]),
            "std_amount_30d": jnum(r["std_amount_30d"]),
            "decayed_avg_amount": jnum(r["decayed_avg_amount"]),
            "decayed_tx_count": jnum(r["decayed_tx_count"]),
            "decay_half_life_days": HALF_LIFE,
            "avg_monthly_tx_count": jnum(r["avg_monthly_tx_count"]),
            "avg_monthly_amount": jnum(r["avg_monthly_amount"]),
            "distinct_beneficiaries": int(r["distinct_beneficiaries"]),
            "distinct_beneficiaries_30d": int(r["distinct_beneficiaries_30d"]),
            "distinct_beneficiaries_24h": int(r["distinct_beneficiaries_24h"]),
            "beneficiaries": json.dumps(benef_map.get(ek, {})),
            "usual_transaction_types": json.dumps(types_map.get(ek, {})),
            "transaction_type_entropy": types_ent.get(ek, 0.0),
            "usual_merchants": json.dumps(merch_map.get(ek, {})),
            "merchant_entropy": merch_ent.get(ek, 0.0),
            "usual_locations": json.dumps(loc_map.get(ek, {})),
            "usual_cities": json.dumps(city_map.get(ek, {})),
            "usual_countries": json.dumps(ctry_map.get(ek, {})),
            "location_entropy": loc_ent.get(ek, 0.0),
            "known_ip_addresses": json.dumps(ip_map.get(ek, {})),
            "known_ip_subnets": json.dumps(subnet_map.get(ek, {})),
            "last_location": r["last_location"],
            "last_city": r["last_city"],
            "last_country": r["last_country"],
            "last_ip": r["last_ip"],
            "last_event_ts": r["last_seen"],
            "peak_transaction_hours": json.dumps(hour_map.get(ek, {})),
            "top_hour": top_hour.get(ek),
            "peak_transaction_days": json.dumps(day_map.get(ek, {})),
            "top_day_of_week": top_day.get(ek),
            "night_activity_ratio": jnum(r["night_activity_ratio"]),
            "is_blacklisted": 1 if r["is_blacklisted"] else 0,
            "is_pep": 0,
            "is_sanction": 0,
            "risk_level": None,
            "risk_score": None,
            # counted BEFORE the clean filter (the agg's value is 0 by construction)
            "suspicious_tx_count": fraud_txns,
            "suspicious_ratio": (fraud_txns / (fraud_txns + int(r["total_tx_count"])))
                                if (fraud_txns + int(r["total_tx_count"])) else 0.0,
            "profile_version": 1,
            "build_run_id": run_id,
            "window_start": window_start,
            "window_end": window_end,
        }
        if drift_status == "sudden":
            drift_count += 1
        # §4: only (re)write profiles that materially changed; others are just
        # "touched" so they survive the prune without a needless version bump.
        if retrain:
            prof["profile_version"] = (prev.get("profile_version", 0) + 1) if prev else 1
            profile_rows.append(prof)
            hist_rows.append((
                ek, run_id, prof["profile_version"], prof["total_tx_count"], prof["total_tx_amount"],
                prof["avg_amount"], prof["decayed_avg_amount"],
                json.dumps(prof, default=str) if config.STORE_HISTORY_JSON else None,
            ))
            state_rows.append((
                ek, jnum(r["decayed_avg_amount"]), jnum(r["ewma_var_amount"]),
                jnum(r["decayed_tx_count"]), jnum(r["max_amount"]),
                r["last_seen"], window_end, HALF_LIFE,
            ))
        else:
            touch_keys.append(ek)

    total = agg.height
    warming = total - active
    print(f"[build] eligibility: {active:,} ACTIVE (trusted) / {warming:,} WARMING UP "
          f"(gate: tenure>={config.ELIGIBLE_MIN_TENURE_DAYS}d AND clean_txns>={config.ELIGIBLE_MIN_TXNS})")
    print(f"[build] §4 incremental: {len(profile_rows):,} retrained / {len(touch_keys):,} unchanged (skipped)"
          f"  |  §9 drift flagged: {drift_count:,}")
    write_store(run_id, window_start, window_end, df.height, profile_rows, hist_rows, state_rows, prune, touch_keys, window_end)

    # cold-start peer baselines (non-ML) for brand-new accounts with no history
    print("[build] computing peer baselines (branch x account_type) ...")
    write_peer_baselines(build_peer_baselines(df, months, run_id))


def write_store(run_id, window_start, window_end, source_rows, profile_rows, hist_rows, state_rows,
                prune=True, touch_keys=None, touch_window_end=None):
    conn = db.connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bp_build_run (run_id, window_start, window_end, source_rows, status) "
        "VALUES (%s,%s,%s,%s,'running')",
        (run_id, window_start, window_end, source_rows),
    )

    BATCH = 1000
    if profile_rows:
        cols = list(profile_rows[0].keys())
        # profile_version is set explicitly in the row
        sql = db.upsert_sql("bp_user_behaviour_profile", cols, ["entity_key"])
        data = [tuple(p[c] for c in cols) for p in profile_rows]
        # commit periodically so a mid-run interruption persists progress instead of
        # rolling back a huge transaction (which bloats the InnoDB tablespace)
        for i in range(0, len(data), BATCH):
            cur.executemany(sql, data[i:i + BATCH])
            if i % (BATCH * 20) == 0:
                conn.commit()
        conn.commit()

    # §4: "touch" unchanged accounts so the prune keeps them (no feature rewrite,
    # no version bump) — just mark them present in this run. Batched with an
    # IN-list so it's a handful of statements, not one round-trip per account.
    if touch_keys:
        CH = 5000
        for i in range(0, len(touch_keys), CH):
            chunk = touch_keys[i:i + CH]
            ph = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"UPDATE bp_user_behaviour_profile SET build_run_id=%s, window_end=%s "
                f"WHERE entity_key IN ({ph})",
                [run_id, touch_window_end, *chunk],
            )
            conn.commit()

    for i in range(0, len(hist_rows), BATCH):
        cur.executemany(
            "INSERT INTO bp_profile_history (entity_key, build_run_id, profile_version, "
            "total_tx_count, total_tx_amount, avg_amount, decayed_avg_amount, profile_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            hist_rows[i:i + BATCH],
        )
        if i % (BATCH * 20) == 0:
            conn.commit()
    conn.commit()

    if state_rows:
        cur.executemany(
            "INSERT INTO bp_incremental_state (entity_key, ewma_mean_amount, ewma_var_amount, "
            "decayed_count, last_amount, last_event_ts, last_decay_ts, half_life_days) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (entity_key) DO UPDATE SET "
            "ewma_mean_amount=EXCLUDED.ewma_mean_amount, ewma_var_amount=EXCLUDED.ewma_var_amount, "
            "decayed_count=EXCLUDED.decayed_count, last_amount=EXCLUDED.last_amount, "
            "last_event_ts=EXCLUDED.last_event_ts, last_decay_ts=EXCLUDED.last_decay_ts",
            state_rows,
        )

    # Remove profiles NOT refreshed by this run (accounts no longer in the current
    # window, or that dropped out of the clean baseline) so the table always
    # reflects the current population and stale rows never accumulate.
    if prune:
        cur.execute("DELETE FROM bp_user_behaviour_profile WHERE build_run_id <> %s", (run_id,))
        removed = cur.rowcount
        if removed:
            print(f"[build] removed {removed:,} stale profiles from previous runs")
    else:
        print("[build] --no-prune: kept profiles from other runs (partial/test build)")

    entities = len(profile_rows) + len(touch_keys or [])
    cur.execute(
        "UPDATE bp_build_run SET finished_at=NOW(), entities=%s, status='done' WHERE run_id=%s",
        (entities, run_id),
    )
    conn.commit()
    conn.close()
    print(f"[build] wrote {len(profile_rows):,} profiles + touched {len(touch_keys or []):,} "
          f"(total {entities:,}) to PostgreSQL (run {run_id})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default=os.path.join(config.DATA_DIR, "transactions.csv"))
    ap.add_argument("--no-prune", action="store_true",
                    help="do NOT delete profiles from other runs (use for partial/sample builds)")
    args = ap.parse_args()
    build(args.in_path, prune=not args.no_prune)
