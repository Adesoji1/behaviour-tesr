# AI Service — Customer Behaviour-Profile & AML Rule Engine

A microservice that **learns each customer's normal behaviour** from their transaction
history, stores it in PostgreSQL, and scores every new transaction against it — firing
AML rules when a transaction breaks that customer's pattern. The governance rules come
from **`Practical rules we can use.pdf`** (in this repo), the constraints this system
was built to.

**No ML model yet** — this stage is the behaviour profile (a feature store) plus the
rule engine.

## Quick start (Docker)

```bash
cp .env.example .env          # fill in the DB values; STORE_PG_PASSWORD is required
docker compose up -d --build  # starts PostgreSQL 17 (the store) + the service
curl localhost:8080/health    # {"status":"ok",...}
```

Then open the interactive docs at **<http://localhost:8080/docs>** and try `GET /demo`.
**Full run/demo/test guide: [`RUNBOOK.md`](RUNBOOK.md).** Integration + DevOps:
[`SERVICE.md`](SERVICE.md). Plain-language explanation: [`howitworks.md`](howitworks.md).

> The store starts empty. See [`RUNBOOK.md` §4](RUNBOOK.md) for loading the learned
> profiles.

## ⚠️ Status — what is done vs. what is not yet perfected

**Done and working:** the behaviour profiles, the AML rule engine, the trust/eligibility
gate (per "Practical rules" §1), the safe chunked ingestion design, and a fully
composable demo.

**Not yet perfected (planned for later stages):**

1. **Live data pulling is not production-ready.** It is currently **switched off**
   (`BP_ALLOW_PROD_PULL=0`) and the service runs from a local cache. Two things must be
   resolved with the DB engineer before enabling it in production:
   - production sits behind a **connection pool (PgBouncer)**, so our session-level
     `SET default_transaction_read_only` / `SET statement_timeout` must be replaced with
     a **read-only role** or transaction-scoped `BEGIN READ ONLY` / `SET LOCAL`;
   - a **read replica** is the preferred long-term source, and the entity key must be
     extended to cover **card transactions** (BIN + last-4), not only `origin_account_no`.
   Design + open questions: [`ingestionstratimprove.md`](ingestionstratimprove.md).

2. **Geo-velocity / "impossible travel" is not implemented.** We currently detect
   *unusual country* and *multiple countries in a short window*, but **not** true
   distance-over-time (e.g. Lagos → London in 15 minutes). The IP data exists for ~99% of
   transactions; what is missing is **geocoding those IPs to coordinates** (a GeoIP
   database such as MaxMind GeoLite2) plus a haversine speed check, and confirmation that
   the stored IP is the customer's, not a proxy. Planned for a later stage.

---

## How it works (architecture)

```text
PROD Postgres (READ ONLY)              PostgreSQL profile store (read/write)
monitoring_transactionmonitoring       bp_transactions_cache       <- local copy of prod txns (the CACHE)
       │                               bp_sync_state               <- ingestion watermark (resume point)
       │  sync_manager.py              bp_user_behaviour_profile   <- ONLINE store (upsert, O(1) lookup)
       │  THE ONLY PROD READER:        bp_profile_history          <- OFFLINE store (append, timeline)
       │  keyset-paged · chunked ·     bp_incremental_state        <- EWMA / time-decay accumulators
       │  capped · throttled ·         bp_peer_baseline            <- non-ML cold-start baseline (new accounts)
       ▼  resumable · read-only        bp_rule_definition / bp_rule_settings (per-client) / bp_rule_event / bp_blacklist
  bp_transactions_cache ──build/retrain──▶ profiles
                                           │   (learning reads the CACHE, never production)
                                   incoming txn ─▶ rule engine reads profile ─▶ rule fires
```

**One engine end-to-end.** The source is Postgres, so the profile store is Postgres
too (it was MySQL until the alignment described in `ingestionstratimprove.md` §7).
**Production has exactly one reader** — the sync job — no matter how many service
replicas run.

## Which CSV / which data?
- **Do NOT use** `monitoring_customerbranchprofile_export.csv` — that is a
 pre-computed profile built the wrong way (what we are replacing).
- **Do NOT reuse** `monitoring_transactionmonitoring_202606240748.csv` — it is a
 stale, partial 600k-row export.
- **Source of truth** = the `monitoring_transactionmonitoring` table. We pull the
 last **3 months (quarterly, per CTO)** fresh, straight from production, read-only.

## Entity resolution (the key design decision)
`entity_key = "{branch_id}:{origin_account_no}"`.
- `origin_account_no` is ~100% populated and **99.4% stable to one customer
 name**; `identifier` is only ~45% populated and full of `N/A` / placeholder
 BVNs, so it is unusable as the key.
- When `origin_account_no` matches `monitoring_customer.account_numbers[].account_number`
 we can fill `customer_id` to later replicate the profile into
 `monitoring_customerbranchprofile` in production. (enrichment step, optional)

## Governance gate (anti-poisoning — "Practical rules")
Profiles must **earn trust** before the engine relies on them (config-tunable):
- **Learn from clean only** — suspicious / blocked / blacklisted txns are excluded from learning.
- **Eligibility (§1)** — a trusted profile needs tenure ≥ `ELIGIBLE_MIN_TENURE_DAYS` (**90**, full
 lifetime) **and** ≥ `ELIGIBLE_MIN_TXNS` (**100**) clean lifetime txns **and**
 ≤ `ELIGIBLE_MAX_FRAUD_TXNS` (**0**) confirmed-fraud txns; otherwise `warming_up`.
 This is §1 verbatim: *"≥90 days history, ≥100 transactions, No confirmed fraud cases"*.
 (§2's table sanctions 50–500 txns; 100 satisfies **both** §1 and §2, so it is the default.)
- **The gate is enforced at DECISION time, not just at build time** (`profile_is_trusted()`
 in `rule_engine.py`). `profile_status` is decided when a profile is *built*, so a profile
 built under an older/looser policy would keep a stale `active` flag until it happened to be
 rebuilt. Re-checking the gate on every score means a policy change — or newly-seen fraud —
 takes effect **immediately** and fails safe. `/score` and `/customer` return a
 `trust_reason` explaining exactly why a profile was or wasn't trusted.
- **Tenure is a LIFETIME property, but a retrain only sees the learning window.** The
 batch build gets true lifetime tenure from `extract_tenure.py`. An event-driven retrain
 **carries it forward** from the stored profile (`tenure_days + days elapsed`), which is
 arithmetically exact and needs no production query. With **no prior profile** it falls
 back to what the cache can prove, which *under-states* tenure — deliberately: an
 unproven account stays `warming_up` and is judged against peers, which fails safe and
 matches the PDF's "Otherwise: Profile Status = Warming Up".
- **Confidence** (`confidence_score` 0–100 = history + consistency + completeness); trusted at ≥ `CONFIDENCE_TRUST_THRESHOLD` (60).
- The rule engine judges an account against its **own** profile only when Active **and** confident; otherwise it uses the **peer baseline**. This is what stops a fraudster establishing a fake "normal".
- Time-decay half-life = 90 days (weights ≈ 1.0 / 0.8 / 0.5 / 0.2 at 0 / 30 / 90 / 180 days).

## What each script does

| File | Purpose |
|------|---------|
| `config.py` | All connection settings + pipeline params (env-overridable). Prod is forced read-only. |
| `schema_pg.sql` | The `bp_` **PostgreSQL** tables (feature store + rules + per-client settings + peer baseline + lineage + transactions cache + sync watermark). Applied automatically by the `db` container. (`schema.sql` is the superseded MySQL version, kept for reference.) |
| `db.py` | Profile-store access layer: connection, dict cursors, `ON CONFLICT` upsert builder, per-customer **advisory locks**. One place owns the SQL dialect. |
| `sync_manager.py` | **The Data Synchronization Layer — the ONLY process that reads production.** Keyset-paged, chunked, row-capped, throttled, statement-timed-out, resumable. Fills `bp_transactions_cache`. |
| `migrate_mysql_to_pg.py` | One-time copy of the already-learned profiles from the old MySQL store into PostgreSQL (store-to-store; production untouched). |
| `extract_tenure.py` | READ-ONLY: each account's **full-lifetime** age + clean-txn counts → `data/tenure.csv` (for the eligibility gate). |
| `extract_transactions.py` | READ-ONLY `psql \copy` of the last 3 months (quarterly) → `data/transactions.csv`. |
| `build_profiles.py` | **polars** builder: clean-baseline filter, sliding windows, time-decay/EWMA, entropy, **eligibility gate (Active/Warming-Up) + confidence**, peer baselines → upserts PostgreSQL. |
| `load_rules.py` | Loads the 32 AML rules + mirrors `users_blacklist` (read-only). |
| `client_thresholds.py` | Set/override rule thresholds **per client** (per institution) or switch a rule off — tier-1/2/3 differ. |
| `rollback.py` | §12 revert a profile (or a whole build run) to an earlier version from `bp_profile_history`. |
| `live_velocity.py` | The "live feature factory": recent-window (1m/10m/15m/1h/24h) look-up for velocity rules. CSV source (demo) + prod-SQL source (real). |
| `rule_engine.py` | Reads a stored profile + client thresholds (+ optional live velocity), fires rules on an incoming txn, logs to `bp_rule_event`. |
| `retrain.py` | **Event-driven per-customer retrain** — recompute one customer **from the local cache** (production is never touched) when a trigger is met; per-customer locked. |
| `service.py` | **FastAPI microservice** the adhere app calls per transaction (`/score`, `/profile`, `/retrain`). See `SERVICE.md`. |
| `Dockerfile` / `requirements.txt` | Package + run the microservice. |
| `demo_end_to_end.sh` + `demo_helpers.py` | Narrated, timestamped end-to-end demo → `logs/demo_*.log`. |

## Run it end-to-end
```bash
source ../.venv/bin/activate

# 1. create schema (idempotent)
# (nothing to do: the db container applies schema_pg.sql automatically on first start)
docker compose up -d db

# 2a. lifetime tenure for the eligibility gate (READ ONLY from prod)
python extract_tenure.py                             # -> data/tenure.csv

# 2b. extract fresh quarterly (3-month) dataset (READ ONLY from prod)
python extract_transactions.py                       # full pull -> data/transactions.csv
# or a quick slice:  python extract_transactions.py --branch 231 --sample-limit 150000 --out data/transactions_sample.csv

# 3. learn + save profiles (clean-baseline filter + Active/Warming-Up gate + confidence)
python build_profiles.py --in data/transactions.csv

# 4. load rules + blacklist
python load_rules.py

# 5. (optional) a client sets its own thresholds — tier-1/2/3 differ
python client_thresholds.py --branch 231 --rule block_above_hard_cap --set hard_cap=250000000
python client_thresholds.py --branch 231 --rule detect_unusual_city --disable

# 6. see rules fire off the learned profiles (uses each client's thresholds)
python rule_engine.py --demo --log
```

## Self-updating: event-driven per-customer retraining (NO cron)
Per CTO, there is **no nightly cron**. The batch build (`build_profiles.py`) seeds all
profiles **once**; after that each customer is refreshed **event-driven** by the
microservice when their own activity meets a trigger — **≥100 new txns OR 30 days OR
sustained drift**. Retraining is per-customer, concurrent-safe (per-customer DB lock),
and rides on the transaction the app already sends. See **`SERVICE.md`**, `retrain.py`,
`service.py`. `bp_incremental_state` holds the EWMA/time-decay state
(half-life = `BP_DECAY_HALF_LIFE`, default 90 days).

## Demo for a CTO / client

```bash
./demo_end_to_end.sh                 # fast: narrates every stage using built profiles
BUILD_SLICE=1 ./demo_end_to_end.sh   # also learns a fresh small slice live
```

Writes a timestamped transcript to `logs/demo_*.log`: config → read-only source →
(build) → what a real customer looks like → load rules → a client setting its own
thresholds → rules firing (normal passes / abnormal flagged) → live velocity burst →
cold-start peer baseline → the nightly scheduler.

## Not built yet (by design, per scope)

- ML inference (XGBoost / IsolationForest) — profiles are the feature vectors it will consume.
- Behavioural embeddings / peer-group clustering (needs the ML stage).

## Exact fetch SQL (if you prefer a manual export over the extractor)
```sql
SELECT id, transaction_id, amount, currency, transaction_type,
      transaction_type_normalized, status, branch_id, origin_account_no,
      origin_account_type, destination_account_no, destination_bank_code,
      customer_name, customer_email, identifier, identifier_type_id, bvn,
      account_type, host(customer_ip_address) AS customer_ip_address,
      customer_location, merchant_name, merchant_location,
      origin_country, destination_country, date_created,
      sender_blacklisted, receiver_blacklisted, is_blocked, indicator
FROM   monitoring_transactionmonitoring
WHERE  date_created >= now() - interval '3 months'   -- quarterly window (per CTO)
 AND  origin_account_no IS NOT NULL
 AND  origin_account_no NOT IN ('N/A','')
ORDER  BY branch_id, origin_account_no, date_created;
```
⚠️ The "blind spot" (replication lag on rapid txns) — acknowledged, and mostly avoided by design. That risk only bites if you read a replica that lags the primary. Our hybrid reads the primary at scoring time for the recent-window velocity, so a burst is visible immediately. The residual risk is only if you later scale reads onto a replica — then you'd add a short read-your-writes window or a Redis counter. I've documented this honestly rather than pretending it's fully solved, because without a streaming platform it's inherent.

MAX_SANE_AMOUNT (default ₦10 trillion)
This is a data-quality guard against garbage amounts. Production contains a handful of transactions with impossible values — the worst we found was ₦300 quadrillion (bigger than Nigeria's entire GDP), clearly test/entry errors. Any transaction above this cap is treated as bad data and excluded from learning, so one garbage row can't wreck a customer's "biggest ever" (max_amount), their averages, or the peer baseline. Without it, a single ₦300-quadrillion row would make the "exceeds historical maximum" rule useless for that account and inflate the peer group. It's in config.py and applied in the builder's impossible-amount guard. ₦10 trillion is far above any real transaction, so nothing legitimate is ever dropped.

STORE_HISTORY_JSON (default OFF)
This controls how much detail the offline history log keeps per nightly run. We always keep a compact timeline (counts, amounts, confidence) per profile per run. When this is ON, we also store a full JSON snapshot of every profile every night — great for point-in-time analysis and future ML retraining, but heavy: ~2 KB × 100k profiles × every night. On this small Aiven test DB that filled the disk and flipped it read-only, so we default it OFF (the current profile always holds full detail anyway). In production, where storage is ample, you'd turn it ON (BP_STORE_HISTORY_JSON=1) to keep full historical snapshots. It's the reason bp_profile_history.profile_json is nullable.

Both are env-overridable, so nothing is hard-coded. Standing by for the build to finish, then I'll run the demo to confirm it's all clean.

## Configuration

Every setting is an environment variable — compliance can retune the system by editing
`.env` and restarting. **No code change, no redeploy.** All 42 variables are documented
in plain language at the top of `config.py`, grouped by what a reader actually wants:
where the data comes from, where we save it, how we learn a customer's normal, who is
trusted, when a customer is re-learned, how we protect the live database, storage, and
legacy.

Each entry explains **what the number means**, not just the name:

```text
BP_DECAY_HALF_LIFE          90 = a transaction from 90 days ago counts HALF as much as
                            one from today. Lower = forget faster.
BP_SYNC_SLEEP_SECONDS       0.2 = wait 0.2s before asking production again. RAISE THIS
                            to be gentler on the live DB.
BP_DRIFT_SIGNAL_THRESHOLD   5 anomalies IN A ROW. One normal transaction resets the
                            count, so a single odd payment never triggers a retrain.
```

## Known finding: `BP_CONFIDENCE_TRUST` is near-inert at 60

Measured against the real 99,254 profiles, the confidence threshold **denies only 1 of
the 6,653 profiles** that pass the other §1 conditions. It is not doing the work one
might assume.

**Why.** `BP_MIN_TENURE_DAYS` (90) and `BP_MIN_TXNS` (100) already force the *history*
component to 1.0, which banks **50 of the 100 points automatically**. To score under 60
a customer would have to be wildly erratic *and* missing most data dimensions —
effectively nobody. Among profiles that pass §1 the minimum confidence is 59 and the
median is 76.

**It is still worth keeping.** It is a cheap backstop: if the tenure/transaction gates
are ever lowered, history stops being pinned at 1.0 and confidence immediately starts
doing real work.

### Raising it to 75 would be a mistake (evidence)

Raising the threshold to 75 would move ~1,030 customers from own-profile to peer
judgement. Who they are matters:

| Group | Customers | Avg clean txns | Variability (cv) |
| --- | --- | --- | --- |
| **Would be demoted** (confidence 60–74) | 1,003 individual · **25 corporate** · 3 agency | 324 — *corporate: 10,385* | 2.73 – 3.40 |
| **Would keep trust** (confidence ≥ 75) | 3,081 individual · 13 corporate · 22 agency | 260 — *corporate: 568* | 1.60 – 1.68 |

Two things stand out:

1. **The demoted group has _more_ history, not less** (324 vs 260 average clean
   transactions). The only real difference between the groups is **variability**.
2. **It is backwards for corporates.** 25 corporates averaging **10,385 transactions**
   would be demoted, while 13 corporates averaging 568 keep their trusted profile —
   discarding the richest profiles in the system.

The cause is that the confidence formula's *consistency* term penalises variable
spending — but **variability is legitimate for a business**. Lumpy invoices are a
corporate's normal. Demoting them means judging a 10,000-transaction business against
the average of a peer group of ~13–38 accounts, which would produce **more** false
positives, not fewer — alert fatigue on exactly the best-understood customers.

**Recommendation:** leave `BP_CONFIDENCE_TRUST` at 60. The issue is not the threshold
but the formula, which conflates *"variable"* with *"unknowable"*. If confidence should
be load-bearing, fix the **formula** (e.g. do not penalise variability for corporates,
or score consistency relative to the customer's own peer group) rather than raising the
dial. That is an analysis to do with compliance, not a config flip.
