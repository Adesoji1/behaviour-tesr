# Data Ingestion Strategy — Behaviour-Profile Microservice

**Status:** ✅ **IMPLEMENTED** (phases 2 + 3) — for review by the DB engineer + Anita.
**Purpose:** stop the behaviour-profile service loading the production database, and
align the profile store onto PostgreSQL — without changing what the service detects.

**What is built and verified:**

- `sync_manager.py` — the Data Synchronization Layer, the **only** process that reads
  production: keyset-paged, chunked, row-capped, throttled, statement-timed-out,
  read-only, resumable (§5).
- `bp_transactions_cache` — the **shared** cache in the profile store. Retrain reads it,
  never production (§5). The unbounded 63k-row query in §1 is gone.
- **PostgreSQL profile store** — migrated off MySQL; all 99,254 profiles copied
  store-to-store with row counts verified, production untouched (§7).
- Governance defects found and fixed — **read §13b, it needs your attention.**

**Still open:** the read replica (§4 — *the biggest win, needs the DB engineer*), the
lifetime-tenure refresh (§13c), and a full profile rebuild (§13b).

---

## 1. Why this document exists (the incident)

While running the end-to-end demo, a single `/demo` call pulled **63,862 rows for one
customer** in one unbounded query and put the live database under load. That is a design
problem, not a one-off.

**As an immediate stop-gap, live production reads are now switched OFF** via a new safety
switch `BP_ALLOW_PROD_PULL=0` (see `config.py`). With it off, retrains skip with a logged
reason, the batch extracts refuse to run, and the service never touches production. Every
pull — or blocked attempt — is logged to stdout. This is a tourniquet, not a fix. This
document is the fix.

## 2. What the service does today (the honest picture)

| Path | Query shape | Load profile |
| --- | --- | --- |
| Per-customer retrain (`fetch_customer` in `retrain.py`) | **2 unbounded `SELECT`s** — the customer's full 3-month clean history + a lifetime tenure aggregate | No `LIMIT`, no pagination, no server-side cursor. One heavy query per retrain. This is what hit us. |
| Live velocity (`ProdVelocitySource` in `live_velocity.py`) | **1 aggregate query per scored transaction** | Small each, but fires on every `/score`. |
| Batch extracts (`extract_transactions.py`, `extract_tenure.py`) | Single `\copy (SELECT …)` of the whole window | One large pass; run manually. |

What limits load today (none of it is batching):
- The **3-month lookback window** caps how far back each query scans.
- A **per-customer lock** (`GET_LOCK`) stops the *same* customer retraining twice at once —
  but **different customers retrain concurrently**, each firing its own full query.
- Reads are forced **read-only** (`SET default_transaction_read_only = on`).
- **No connection pooling** — a fresh connection per pull.

**Summary:** one heavy unbounded query per retrain + one small query per scored
transaction, with concurrency bounded only per-customer. There is no chunking, no
batching, no pooling, no cache.

## 3. Decisions (the bottom line)

1. **Adopt the intent** — cache + incremental sync + separation of concerns. It is the
   correct fix and directly removes the load.
2. **Do not embed a per-replica Parquet cache.** Put the sync in a **separate job**
   writing to **shared** storage. (Reason in §5.)
3. **Seriously consider a read replica first** — simpler and more correct than app-level
   CDC. (§4.)
4. **Do the MySQL → PostgreSQL migration** for alignment, but plan it as a *real*
   migration: locks, upserts, drivers, schema. Prefer **managed / co-located** over a
   self-hosted container for the production store. (§7.)
5. **Drop pandas** (we already use **polars**) and **drop the CPU/RAM-in-app-logs idea**
   (use proper container metrics). **Keep** the durable `bp_event_log` audit trail and add
   a **log shipper** for "no log loss." (§8, §9.)
6. **Skip Celery / RabbitMQ / asyncio for now.** (§10.)

---

## 4. Option A (preferred): read from a replica — *ask for the DB engineer*

Production is a **DigitalOcean managed Postgres**. A **read replica is a console click**.

Point every read (retrain, velocity, extracts) at the replica and the primary is shielded
**regardless of query shape**. This is my strongest recommendation because:

- It **removes the load problem immediately**, with near-zero application change (a
  different host in `.env`).
- It **sidesteps the watermark-correctness holes** of hand-rolled CDC entirely (§6) —
  a replica sees `UPDATE`s and `DELETE`s, not just inserts.
- It is **operationally boring**, which is a virtue.

**Trade-off — replication lag:** a replica lags the primary by a short interval. That is
fine for profile *learning* (a 3-month window, 90-day decay). It is **not** fine for the
per-transaction **live velocity** look-up, which exists precisely to catch a burst *right
now* — a lagging replica could miss the burst it is meant to detect. So:

> **Hybrid:** learning / retrain reads → **replica**. Live velocity → **primary**
> (it is a small, indexed, bounded aggregate), or drop live velocity to a
> short-window counter later. This is already the documented intent in `README.md`.

**Question for the DB engineer:** can we have a read replica, and what lag should we
expect? If yes, Option A alone may be enough for the demo and much of production.

## 5. Option B: Data Synchronization Layer + shared cache (Level 1, corrected)

If we still want a cache (worth it regardless — it makes retrains fast and cheap), build
it as originally sketched **with one structural correction**.

### The correction that matters

The original sketch ended at a **Local Parquet Cache**. That breaks the moment we scale:
the service is designed to run as **N replicas behind a load balancer**. With a per-replica
local file cache, either **every replica syncs from production** (N× the load — the exact
opposite of the goal) or the caches **diverge** and different replicas score the same
customer differently. A local file cache only works for a single process.

### Corrected architecture

```
                    ┌───────────────────────────────┐
   PRODUCTION       │  Data Synchronization Layer   │      ONE reader only
   Postgres  ──────▶│  (separate scheduled job)     │
   (replica)        │  • check watermark            │
                    │  • detect delta               │
                    │  • chunked / keyset paging    │
                    │  • resume after failure       │
                    │  • pooled connection          │
                    │  • log every chunk + progress │
                    └───────────────┬───────────────┘
                                    │ writes
                                    ▼
                    ┌───────────────────────────────┐
                    │      SHARED cache             │   (not per-replica!)
                    │  bp_transactions_cache table  │
                    │  in the profile Postgres      │
                    │      — or —                   │
                    │  Parquet in object storage    │
                    │  (Spaces / S3)                │
                    └───────────────┬───────────────┘
                                    │ reads
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
      profile-service        profile-service       profile-service
        replica 1              replica 2             replica 3
```

**Key properties:**
- **Exactly one process reads production.** All replicas read the shared cache.
- The sync job is **independently schedulable and restartable**; a failure never breaks
  scoring (the service keeps serving from the last good cache).
- The profiling logic stays focused on modelling behaviour — clean separation of concerns.

### Storage choice for the cache

| Storage | Verdict | Why |
| --- | --- | --- |
| **Postgres table** (`bp_transactions_cache`) | ✅ **Recommended for us** | We are already moving to Postgres. SQL-queryable, transactional, one system to back up, and the per-customer retrain becomes a simple indexed local `SELECT`. |
| Parquet in **object storage** | ✅ Viable | Fast/compressed, great for future ML training. Needs an object store + a read path. Good as a *second* (offline/analytics) copy. |
| Parquet on a **local volume** | ❌ **Rejected** | Breaks horizontal scaling (see above). |

**Recommendation:** cache into the **profile Postgres** first (simplest, one system);
optionally export Parquet to object storage later for ML/analytics.

### Load discipline the sync job must implement

These are the "batching/chunking/pooling" asks — implemented **once**, in the sync job,
instead of on every request:

- **Keyset pagination** (`WHERE id > :last_id ORDER BY id LIMIT :chunk`), *not* `OFFSET`
  (which degrades linearly).
- **Server-side cursor** / streaming so we never materialise a huge result set in memory.
- **Chunk size** and a **max-rows-per-run cap**, both env-driven.
- **Connection pooling** (PgBouncer, or a pool in the job).
- **Resume from the last committed watermark** after any failure.
- **Optional throttle** (a small sleep between chunks) so we can dial the load down to
  whatever the DB engineer is comfortable with.
- **Log every chunk**: rows fetched, watermark advanced, elapsed. A **`tqdm` progress bar**
  is right for the **CLI/batch job**; inside the HTTP service a progress bar is meaningless —
  emit **structured log lines per chunk** there instead.

---

## 6. Correctness requirements (must be resolved before implementation)

These are the parts that make the difference between "works" and "silently wrong."

### 6.1 A `MAX(id)` watermark misses UPDATEs — this is an AML correctness hole

Our learning filter depends on **mutable** columns:

```sql
WHERE status='clean' AND is_blocked=false AND sender_blacklisted=false
```

An append-only, `MAX(id)`-based delta caches a transaction as **clean** and **never sees it
later flipped** to blocked / suspicious / blacklisted. We would keep learning from a
transaction production has since marked dirty — corrupting the customer's baseline.

**Mitigations (pick at least one):**
1. **Use a replica** (§4) — no watermark, no hole. *Preferred.*
2. Watermark on a **reliable `updated_at`** *and* **re-pull a bounded trailing window**
   (e.g. the last N days) every run, so status flips inside that window are caught.
3. Have the source emit **soft-delete / status-change events** we can consume.

> ⚠️ **Blocking question for the DB engineer:** does
> `monitoring_transactionmonitoring` actually have an **`updated_at`** column, and is it
> **reliably maintained on every UPDATE** (trigger / ORM `auto_now`)? Our current extract
> SQL only ever references **`date_created`**. **If there is no trustworthy `updated_at`,
> the entire delta-by-`updated_at` design is not viable** and we should go with the
> replica (Option A).

### 6.2 The cache must be pruned to the lookback window

Profiles are learned over a **rolling 3-month window** (`BP_LOOKBACK_MONTHS`) with a
**90-day decay half-life**. A pure append cache grows forever and drags in out-of-window
rows, skewing averages, `usual_cities`, and "biggest ever." The sync job must **prune /
expire** to the window (tenure is a separate lifetime aggregate and stays a small query).

### 6.3 Watermark snapshot consistency

Reading `MAX(id)` and then `SELECT WHERE id > watermark` can miss rows committed in
between, and id gaps from rolled-back transactions are normal. Advance the watermark
**only after** a chunk is durably written, and prefer a **transactional snapshot** for the
read.

---

## 7. MySQL → PostgreSQL migration (profile store)

**Agreed — do it.** Reasons:

- **One engine end-to-end.** The source is already Postgres; a Postgres profile store means
  one driver, one dialect, one skillset, one set of tooling.
- **Drops the Aiven/MySQL dependency** and the MySQL-specific TLS `ca.pem` handling.
- **Opens co-location:** the profile store *could* live in the same managed Postgres as the
  source (already hinted in `SERVICE.md`), giving read-your-writes and simpler ops.

### This is a real migration, not a config change

Everything that speaks MySQL has to change:

| What | From (MySQL) | To (PostgreSQL) |
| --- | --- | --- |
| Per-customer lock | `GET_LOCK()` / `RELEASE_LOCK()` | **`pg_advisory_xact_lock()`** (auto-released at transaction end — arguably better) |
| Upsert | `INSERT … ON DUPLICATE KEY UPDATE` | **`INSERT … ON CONFLICT (entity_key) DO UPDATE`** |
| Driver | `pymysql` | **`psycopg` (v3)** — already used by `config.prod_connect()` |
| JSON columns | MySQL JSON | **`TEXT`** — deliberately *not* `jsonb`: the app already does `json.dumps()`/`json.loads()` at every call site, and psycopg auto-deserializes `jsonb` to dicts, which would break them. We never query *inside* the JSON. Switch to `jsonb` only if that changes. |
| TLS | `ssl={"ca": ca.pem}` | `sslmode` / `sslrootcert` |
| Schema | `schema.sql` (9 `bp_` tables) | Postgres types, indexes, constraints |

**Files that touch the store and will need updating:** `config.py`, `schema.sql`,
`build_profiles.py`, `retrain.py`, `service.py`, `load_rules.py`, `client_thresholds.py`,
`rollback.py`, `demo_helpers.py`.

**Migration approach:** stand the Postgres schema up, re-run the seed to populate it
(rather than migrating MySQL rows), verify counts/spot-check profiles against MySQL, then
cut over. Keep MySQL read-only as a fallback until we're satisfied.

**Note on `psycopg2-binary` vs `psycopg` (v3):** the original doc suggested
`psycopg2-binary`; the codebase already uses **psycopg v3**. Pick **one** — recommend v3.

### The compose snippet — corrected

The proposed snippet is fine **for local development only**, but has issues:

```yaml
services:
  db:
    image: postgres:17
    container_name: behaviour-profile-db
    # Do NOT publish 5432 to the host in shared/prod environments.
    # For local dev, bind to loopback only:
    ports:
      - "127.0.0.1:5432:5432"
    environment:
      POSTGRES_DB: ${POSTGRES_DB}            # was commented out — the DB must be created
      POSTGRES_USER: ${POSTGRES_USERNAME}    # original had a typo: POSSTGRES_USERNAME
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      PGDATA: /var/lib/postgresql/data/pgdata17
    env_file:
      - .env
    restart: unless-stopped
    shm_size: 256mb                          # Postgres wants more than the 64mb default
    volumes:
      - pgdata17:/var/lib/postgresql/data/pgdata17
      # Idempotent schema bootstrap on first init:
      - ./schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    healthcheck:                             # so dependents wait for readiness
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
    logging:                                 # bounded logs; ship them off-host too
      driver: json-file
      options: { max-size: "10m", max-file: "5" }

volumes:
  pgdata17:                                  # named volume — survives `docker compose down`
```

**Fixes applied:** the `POSSTGRES_USERNAME` typo; `POSTGRES_DB` uncommented (otherwise no
database is created); `env_file` enabled; added healthcheck, `shm_size`, log rotation,
loopback-only port binding, and schema bootstrap.

> ⚠️ **Self-hosting makes us the owner of backups, HA, and upgrades.** For local/dev this
> container is fine. **For production, prefer the managed / co-located Postgres.** A single
> container with one volume is a single point of failure with no backup story.

---

## 8. "No volume wiped / password on the volume / no log loss" — corrected

These are reasonable instincts; two need correcting:

- **"Password for accessing data stored in the docker volume"** — this is a
  **misconception**. You don't password-protect a volume. You protect:
  - the **database** with authentication (`POSTGRES_PASSWORD`, roles, least privilege),
  - the **data at rest** with disk/volume **encryption** and host filesystem permissions,
  - **network access** (don't publish 5432; bind to loopback or a private network).
  Anyone with host root can read a volume regardless of the DB password — at-rest
  encryption and host access control are the real controls.

- **"No volume should be wiped"** — correct instinct. Use a **named volume** (survives
  `docker compose down`), **never run `docker compose down -v`** (that is what deletes it),
  and — most importantly — **volumes are not backups**. Add scheduled **`pg_dump`** to
  off-host storage with a tested **restore** procedure. A volume that is never wiped but
  never backed up still loses everything when the disk dies.

- **"Ensure no logging can be lost"** — container stdout **is** lost on rotation or
  container removal. Two layers:
  1. **Already in place:** the durable **`bp_event_log`** table — every score / retrain /
     skip / failure is written to the DB, queryable per customer. **Keep this.**
  2. **Add a log shipper:** ship stdout to an aggregator (Loki / ELK / CloudWatch / Datadog)
     and set `max-size`/`max-file` rotation. Then logs survive the container.

## 9. Metrics: CPU / RAM / memory — do **not** put these in app logs

> Original ask: *"the docker logs should show memory usage, cpu usage, ram…"*

This is an **anti-pattern**. Application logs are for events, not resource telemetry;
hand-rolled CPU/RAM log lines are noisy, sampled badly, and duplicate what the runtime
already exposes. Use the proper tools:

- **Quick / local:** `docker stats` (live CPU, memory, I/O per container).
- **Proper:** **cAdvisor → Prometheus → Grafana** (container CPU/RAM/IO, dashboards,
  alerting), or the platform's built-in metrics (k8s metrics-server, DO monitoring).
- **App-level:** expose a `/metrics` endpoint (`prometheus-fastapi-instrumentator`) for
  request rate, latency, error rate — plus our own counters (rows synced, watermark lag,
  retrains, rules fired). **That** is the useful application telemetry.

Keep resource metrics in the metrics system; keep the audit trail in logs + `bp_event_log`.

## 10. Tech stack: asyncio / background tasks / RabbitMQ / Celery — not yet

| Tech | Needed now? | Reason |
| --- | --- | --- |
| **Celery + RabbitMQ** | ❌ Not yet | They buy distributed task queues, retries, scheduled fan-out across workers. We have **one** sync job and per-customer retrains. A **scheduled job** (k8s CronJob / systemd timer / APScheduler) covers it. Revisit when we genuinely need distributed retries/fan-out. |
| **asyncio** | ❌ Not yet | Helps when drivers are async and work is I/O-bound. Our drivers are **sync** (psycopg/pymysql) and the heavy work is **CPU-bound polars**. Async adds complexity for little gain. Don't async-ify prematurely. |
| **Background tasks** | ✅ **Yes — worth doing** | Retrain currently runs **inside the `/score` request**, so a heavy recompute blocks the caller. Moving it to a FastAPI background task / small internal queue is a genuine scaling win and needs no new infrastructure. |
| **pandas** | ❌ **Drop** | We already use **polars**, which reads/writes Parquet natively via pyarrow. Adding pandas is redundant weight. |
| **tqdm** | ✅ CLI only | Progress bar for the batch sync job; use structured per-chunk log lines inside the service. |

### Dependencies (corrected)

```text
# already in requirements.txt — keep
polars, pyarrow, fastapi, uvicorn, python-dotenv

# add for the Postgres migration + sync layer
psycopg[binary]        # v3 — NOT psycopg2-binary; v3 is already used for prod reads
tqdm                   # progress bar for the batch/CLI sync job only

# do NOT add
pandas                 # polars already covers this
sqlalchemy             # we use plain SQL; an ORM buys nothing here
diskcache / joblib     # the shared cache is Postgres/object storage, not a local cache
```

Remove `pymysql` once the migration is complete. The **Dockerfile already installs the
`psql` client** (needed by the extract path) — keep that, and drop it later if we move
fully to `psycopg`.

---

## 11. Does this change how we detect **drift**? (short answer: no)

Good question — it was worth checking the code rather than assuming. There are **two
separate drift mechanisms**, and they are affected differently:

### 11.1 Live drift signal (the retrain trigger) — **completely unaffected**

`drift_signal_count` is incremented in `service.py` on **every `/score`**, when a rule fires
against the customer's **own** profile:

```sql
drift_signal_count = CASE WHEN <anomaly> THEN drift_signal_count + 1 ELSE 0 END
```

This is computed from **the incoming transaction + the stored profile**. It **never touches
production**. Ingestion changes cannot affect it. The same is true of **all three retrain
triggers** — `txns_since_build`, `days_since_build`, `drift_signal_count` all live in the
profile store. So the entire **event-driven trigger system is unaffected**.

### 11.2 Rebuild-time drift status — **mechanism unchanged; accuracy depends on cache quality**

`drift_status` / `drift_reason` come from `detect_drift(prev, decayed_avg, cities,
countries)`, run when a profile is **recomputed**. It compares the **newly computed**
recency-weighted average / cities / countries against the **previous stored profile**
(threshold `BP_DRIFT_AMOUNT_PCT`, default 50%).

The **logic does not change at all** — it still compares new-vs-previous profile. What
changes is *where the input transactions come from*. So drift stays correct **if and only
if** the cache is:

1. **Fresh within a bounded lag.** Drift here is a **slow-moving** signal (a 3-month window,
   90-day half-life, comparing profile versions). A sync lag of minutes or a few hours is
   **negligible** — it shifts *when* drift is noticed by at most the sync interval, not
   whether it is noticed. The *live* burst detection is the lag-sensitive one, which is why
   velocity should keep reading the primary (§4).
2. **Correct about mutable status fields.** ⚠️ **This is the one real risk.** If the cache
   is append-only and never sees a transaction flipped clean → blocked/suspicious (§6.1),
   we keep learning from dirty data. That corrupts `decayed_avg` and the city/country maps
   — which are **exactly** `detect_drift`'s inputs. Drift would degrade *silently*. This is
   the strongest argument for the **replica** (Option A) or a trailing re-pull window.
3. **Pruned to the lookback window.** Out-of-window rows skew `decayed_avg` and the "usual"
   maps, and therefore the drift comparison (§6.2).

**Conclusion:** get §6.1 and §6.2 right and drift detection behaves **identically** to
today, just fed from a cache instead of a live query. Get §6.1 wrong and both the profile
*and* drift degrade quietly. This is why the correctness section is not optional.

---

## 12. Recommended phasing

| Phase | What | Why first |
| --- | --- | --- |
| **0 — done** | `BP_ALLOW_PROD_PULL=0` safety switch; every pull/blocked attempt logged | Production is protected **now** |
| **1** | **Read replica** for learning/retrain reads; keep velocity on the primary | Biggest win, smallest change. May be sufficient on its own |
| **2** | **Postgres profile store** (schema, advisory locks, `ON CONFLICT`, psycopg v3), reseed + verify, cut over | Alignment; unblocks the shared cache living in the same DB |
| **3** | **Sync layer** as a separate scheduled job → shared `bp_transactions_cache`, with keyset paging, chunking, pooling, resume, per-chunk logging | Makes retrains fast and cheap; one reader of prod |
| **4** | Retrain as a **background task**; `/metrics` + cAdvisor/Prometheus/Grafana; log shipper; `pg_dump` backups | Scaling + operability |
| **Later / if ever** | Debezium / Kafka / logical replication; feature store (Feast); Celery | Only with a real driver — not for this demo |

## 13. What we are explicitly **not** building (and why)

- **Level 2/3 CDC (Debezium + Kafka)** — infrastructure-heavy; a replica or a small sync
  job solves our problem. Revisit only if the company invests in that platform.
- **A feature store (Feast / Tecton)** — valuable when multiple models share features, or
  you need online/offline consistency across teams. We have **one** profiling service.
  Our `bp_user_behaviour_profile` table already *is* the feature store for the future ML
  stage. Extra complexity, little benefit today.
- **Celery / RabbitMQ / asyncio** — see §10.

## 13b. Governance findings found while doing this work (ACTION REQUIRED)

Reviewing the code against **"Practical rules we can use"** while moving to the cache
turned up two genuine defects. Both are now fixed in code, but **the existing data is
still stale** — read this before the demo.

### Finding 1 — §1's "No confirmed fraud cases" was never enforced (or even measured)

§1 says: *"Only build a behavior profile if the customer has: ≥90 days history,
**≥100 transactions**, **No confirmed fraud cases**."*

- The fraud condition **did not exist** in the eligibility gate at all.
- Worse, `suspicious_tx_count` was computed **after** the clean-only filter had already
  removed every non-clean row — so it was **always 0**, for all 99,254 profiles. The
  signal was never measured, so nobody could have noticed.

**Fixed:** fraud is now counted **before** the clean filter (batch) and from the cache
(retrain), and it is a hard eligibility condition (`BP_ELIGIBLE_MAX_FRAUD_TXNS=0`).

### Finding 2 — the transaction minimum was 50, not §1's 100

`BP_MIN_TXNS` was **50**. §1's example says **≥100**; §2's table sanctions **50–500**.
**100 satisfies both**, 50 satisfies only §2. **Fixed:** the default is now **100**.

### The consequence you must know about

The 99,254 stored profiles were built under the **old** rules. Measured now:

| Check | Count |
| --- | --- |
| Profiles marked `active` | 6,653 |
| …of those, failing §1's ≥100 clean txns | **2,505 (38%)** |
| Profiles with any fraud recorded | **0** — because it was never measured |

A `profile_status` is decided when a profile is **built**, so those 2,505 would have
kept a stale `active` flag until rebuilt — and a rebuild needs production data.

**So the gate is now also enforced at DECISION time** (`profile_is_trusted()` in
`rule_engine.py`), re-checked on every score. A profile that no longer meets §1 is
**not trusted**, falls back to the peer baseline, and says why:

```text
"trust_reason": "peer_baseline (§1 clean txns 61 < 100)"
```

This is self-healing: the policy takes effect **immediately**, fails safe, and needs
no rebuild. It is also strictly more conservative — 2,505 accounts move from
"judged on their own profile" to "judged against peers".

**Still required:** a **full rebuild** once production access is restored, so
`profile_status` and `suspicious_tx_count` are recomputed under the correct rules.
Until then the stored flags are stale — the live gate is what protects us.

**For Anita — one decision:** §1 says ≥100 transactions, §2's table allows 50–500. We
have set **100** (satisfies both). If compliance prefers 50, it is a one-line env
change (`BP_MIN_TXNS=50`) — but it would then meet §2 only, not §1.

---

## 13c. The lifetime-tenure refresh (`bp_account_tenure`) — NOT built, needs sign-off

### What it is for

The §1 gate needs **two lifetime facts** about each customer:

| Fact | Meaning | Why "lifetime" matters |
| --- | --- | --- |
| `tenure_days` | days since their **very first** transaction | §1: "≥90 days history". A customer's first txn may be *years* old. |
| `lifetime_clean_txns` | clean transactions they have **ever** made | §1: "≥100 transactions" — ever, not in the last quarter. |

**The problem:** our cache only holds the **3-month learning window**. Lifetime facts
cannot be computed from a 3-month cache — by definition.

**How we handle it today (and why it is safe):**

- The **batch build** gets the true figures from `extract_tenure.py` (a full-history
  aggregate against production). The 99,254 stored profiles carry real values.
- An **event-driven retrain** *carries them forward* from the stored profile:
  `tenure_days + days elapsed`. This is **arithmetically exact** — tenure is
  `now − first_txn`, and the stored value was `last_retrained_at − first_txn`, so adding
  the elapsed days reconstructs it precisely. No production query needed.
- With **no prior profile**, we fall back to what the cache can prove. That *under-states*
  tenure, so the account stays `warming_up` and is judged against peers — §2's
  "Otherwise: Profile Status = Warming Up". **Fails safe.**

**The residual gap:** `lifetime_clean_txns` cannot *decrease*. If fraud is confirmed on a
transaction older than the cache window, the lifetime clean count should drop; carry-forward
cannot see that. (In-window fraud **is** caught — the §1 fraud gate reads the cache.)

### The proper fix

A small table, one row per account:

```sql
bp_account_tenure(entity_key, tenure_days, lifetime_clean_txns, refreshed_at)
```

refreshed by a **periodic job**, which retrain then reads instead of carrying forward.

### Why it needs the DB engineer's sign-off

The refresh query is the **heaviest in the whole system** — a full-history aggregate over
every account, not a 3-month slice:

```sql
SELECT branch_id, origin_account_no,
       min(date_created) AS first_seen,
       count(*) FILTER (WHERE status='clean' AND is_blocked=false
                          AND sender_blacklisted=false) AS lifetime_clean_txns
  FROM monitoring_transactionmonitoring
 GROUP BY branch_id, origin_account_no;
```

That is a **full-table scan + GROUP BY over all history** — exactly the query shape we
have spent this whole document removing. It must not be run casually, and never
per-retrain. It is rare (weekly is plenty — tenure moves by 1 day per day), but it is big.

**What we need agreed before building it:**

1. **May we run it at all**, and **when**? (Weekly, off-hours, in a maintenance window?)
2. **On a read replica?** If a replica exists (§4), this becomes a non-issue — run it there
   and the primary never notices. **This is the preferred answer.**
3. **Chunked how?** We can range-partition by `branch_id` (or an `id` range) and run it in
   slices with a throttle, exactly like `sync_manager.py` does — instead of one giant query.
4. **Index support?** An index on `(branch_id, origin_account_no)` would make the grouping
   far cheaper.
5. **How stale may these figures be?** If weekly is acceptable, the job is trivial. If it
   must be daily, chunking + a replica become mandatory.

**Until this is agreed, carry-forward stands.** It is exact for tenure and fails safe for
everything else, and the §1 fraud gate — re-checked on every score — is what actually
protects us in the meantime.

---

## 14. Open questions for the DB engineer

0. **Trusted Sources — our IP keeps changing.** An IP was allowlisted earlier
   (`129.222.206.171`) but our egress has already moved to `98.97.77.181`, so we are
   locked out again. Please re-add it — and, more importantly, can we get a **stable**
   route: a static IP, a VPN, a bastion host, or an agreed CIDR? Otherwise this recurs
   every time the connection changes. (Get the current value with `curl -4 -s ifconfig.me`.)
1. **Can we have a read replica** of the adhere Postgres, and what **replication lag**
   should we expect? (This is the single highest-value answer — it may make §5 optional,
   and it also solves §13c.)
1b. **Are these ingestion settings gentle enough?** The sync now runs keyset-paged in
   **2,000-row chunks**, capped at **20,000 rows per run**, with a **0.25s throttle**
   between chunks and a **30s server-side statement timeout**, read-only. Every chunk is
   logged. Tell us what numbers you want and we will set them — they are all env vars.
1c. **The lifetime-tenure aggregate (§13c)** — may we run it, when, how often, and
   chunked how? This is the one genuinely heavy query left.
2. Does `monitoring_transactionmonitoring` have a **reliable `updated_at`** maintained on
   every UPDATE? **If not, the watermark/delta design is not viable** and we go replica-only.
3. Is there an **index** on `(branch_id, origin_account_no, date_created)`? The per-customer
   retrain query filters on exactly that; without it, every retrain is a large scan.
4. What **query rate / chunk size / time window** are you comfortable with for the sync job?
   We will make chunk size, row caps, and throttle **env-tunable** to whatever you specify.
5. For the profile store: **managed / co-located Postgres** (preferred) or self-hosted
   container? If self-hosted, who owns **backups, HA, and upgrades**?
6. Any **maintenance window** the sync job should avoid?
7. Can the DB emit **status-change events** (or should we rely on a trailing re-pull window)
   to catch clean → blocked/blacklisted flips?

---

### Appendix — original Level 1 sketch (kept for reference)

The original watermark metadata idea, unchanged in spirit:

```json
{
  "last_max_id": 8456381,
  "last_updated_at": "2026-07-14T09:30:20",
  "last_sync": "2026-07-14T10:00:00"
}
```

On each run: read the watermark → `SELECT MAX(id), MAX(updated_at)` → if unchanged, do
nothing; if changed, pull **only the delta in chunks**, append to the **shared** cache,
advance the watermark **after** a durable write, log every chunk.

**Still true:** ~150–250 lines of Python, no special infrastructure.
**Corrected:** it runs as a **separate job** writing to **shared** storage (not a local
per-replica Parquet file), it must handle **UPDATEs** (§6.1), and it must **prune to the
lookback window** (§6.2).
