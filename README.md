# AI Service — Customer Behaviour-Profile & AML Rule Engine

A FastAPI + PostgreSQL microservice that learns each customer's **normal** transaction
behaviour and scores every new transaction against it, in real time, for AML monitoring.

- **Learns** a per-customer, **per-currency** behaviour profile from their transaction history.
- **Scores** each incoming transaction against that profile **and** a catalogue of AML rules.
- **Returns** a decision (`allow` / `review`) plus the exact rules that fired, **sends the
  result to your system via a webhook**, and **stores it for audit**.
- **Keeps itself fresh**: ingestion is scheduled, retraining is event-driven per customer,
  and nothing touches production except one bounded, read-only ingestion job.

---

## 1. The endpoint that matters: `POST /score`

This is the hook your platform calls for every transaction.

```
transaction JSON ──▶ POST /score
                        │
                        ├─ 1. GET the customer's profile (this currency) from PostgreSQL
                        ├─ 2. COMPARE the transaction to that profile + AML rules
                        ├─ 3. DECIDE: allow | review  (+ which rules fired)
                        ├─ 4. RETURN the decision  (fast — the caller waits only for this)
                        └─ 5. AFTER responding (async, non-blocking):
                              • deliver the decision to your BP_SCORE_WEBHOOK_URL (the "event")
                              • save the full analysis to PostgreSQL (audit)
                              • retrain THIS customer if they are due (event-driven, no cron)
```

**Request**

```bash
curl -X POST http://localhost:8080/score -H 'Content-Type: application/json' -d '{
  "branch_id": 231, "origin_account_no": "5510027677",
  "amount": 8000, "currency": "NGN",
  "destination_account_no": "0123965972",
  "customer_location": "street, Lagos, State",
  "origin_country": "NG", "destination_country": "NG",
  "transaction_id": "TXN-123", "ts": "2026-07-17T14:00:00"
}'
```

**Response** (this is also what is delivered to the webhook)

```json
{
  "entity_key": "231:5510027677",
  "transaction_id": "TXN-123",
  "decision": "allow",
  "fired_rules": [],
  "judged_against": "own_profile",
  "latency_ms": 8.4
}
```

`judged_against` is `own_profile` when the customer is trusted, or `peer_group` /
`peer_group(new)` when they are not yet trusted (judged against peers — anti-poisoning).

> The webhook (the "event") is delivered **after** the HTTP response, so it never slows the
> caller down. Delivery is **guaranteed** (retried) by the outbox relay — see §6.

---

## 2. Run it in Docker

```bash
cp .env.example .env          # then fill in the secrets (see §4)
docker compose up -d --build  # starts 3 services
curl localhost:8080/health    # -> {"status":"ok",...}
open http://localhost:8080/docs   # interactive API docs
```

Three services start:

| Service             | Container                | Role |
|---------------------|--------------------------|------|
| `behaviour-profile` | `adhere-behaviour`       | The API (`POST /score`, `/demo`, …). Scale with `BP_WORKERS`. |
| `db`                | `behaviour-profile-db`   | The **profile store** (PostgreSQL). Holds all learned behaviour in a named volume. |
| `sync`              | `adhere-behaviour-sync`  | The **only** process that reads production. Scheduled ingestion + webhook relay. |

### ⛔ NEVER run `docker compose down -v`

The `-v` flag **deletes the `behaviour_pgdata17` volume — every learned profile is lost.**
Use `docker compose down` (no `-v`) to stop; the volume (and all learned behaviour)
survives restarts and redeploys. See §7 for how to back it up.

---

## 3. What happens automatically in a deployment

All four are on by default and env-driven:

| Behaviour | Runs where | Controlled by | What it does |
|---|---|---|---|
| **Scheduled ingestion** | `sync` service | `BP_SYNC_AT_HOUR` (daily) or `BP_SYNC_INTERVAL_SECONDS` | Pulls fresh prod transactions → local cache (`bp_transactions_cache`). Bounded, throttled, read-only, resumable. The **only** production reader. |
| **Event-driven retraining** | inside `/score` | `BP_RETRAIN_*` thresholds | Rebuilds **that one** customer's profile when they cross a trigger (≥N new txns / ≥D days / sustained drift). **No cron.** |
| **Webhook outbox relay** | `sync` service | `BP_WEBHOOK_*` | Redelivers any decision whose inline webhook was lost (e.g. an API crash), with exponential backoff, until `sent` or `dead`. |
| **Local velocity / burst detection** | inside `/score` | `BP_LOCAL_VELOCITY` | Records each scored transaction locally and computes 1m/10m/1h burst features — real-time, no production read. |

---

## 4. Environment variables

Everything is env-driven. Copy `.env.example` → `.env` and set the values below.
**`.env` is git-ignored and must never be committed** (it holds live credentials).

### 4a. Secrets — required (never commit)

| Variable | What |
|---|---|
| `STORE_PG_PASSWORD` | Password for the profile-store PostgreSQL (compose refuses to start without it). |
| `PROD_PG_HOST` / `PROD_PG_PORT` / `PROD_PG_USER` / `PROD_PG_PASSWORD` / `PROD_PG_DB` | Read-only connection to the **production** transaction DB (for ingestion). |
| `PGSSLMODE` | `require` for the production connection. |
| `BP_SCORE_WEBHOOK_SECRET` | Optional HMAC secret; when set, each webhook carries an `X-Behaviour-Signature` header so your consumer can verify it came from us. |

In Docker, the store host/port are injected by compose (`STORE_PG_HOST=db`, `STORE_PG_PORT=5432`).

### 4b. Turn these ON for **production**

```ini
# --- Production ingestion: once a day, off-peak ---
BP_ALLOW_PROD_PULL=1                # master switch: allow the sync job to read production
BP_SYNC_AT_HOUR=4                   # daily ingestion at 04:00 (comment out = interval mode)
BP_SYNC_AT_MINUTE=0
BP_SYNC_TZ=UTC                      # container TZ is UTC; schedule is unambiguous
BP_SYNC_RUN_ON_START=1             # one catch-up pull on deploy, then follow the schedule
BP_SYNC_MAX_ROWS=0                  # 0 = no per-run cap (a daily off-peak run should not be capped)

# --- Send every decision to your system (the "event") ---
BP_SCORE_WEBHOOK_URL=https://your-consumer.example.com/aml/events   # your real endpoint
BP_SCORE_WEBHOOK_SECRET=<a-strong-shared-secret>

# --- Real-time burst detection (recommended, zero production load) ---
BP_LOCAL_VELOCITY=1
BP_LIVE_VELOCITY=0                  # do NOT use the per-score production velocity query (slow)

# --- Throughput: scale to your peak concurrency ---
BP_WORKERS=4                        # API worker processes
BP_STORE_POOL_MAX=12                # DB connections per worker
```

### 4c. Use these for the **first-time local backfill** (seeding)

Before production’s daily schedule can maintain the data, you must fill the cache once and
build the initial profiles. Run in **interval mode** (fast, repeated pulls) until caught up:

```ini
# BP_SYNC_AT_HOUR=                  # LEAVE UNSET/commented -> interval mode (drives the backfill)
BP_SYNC_INTERVAL_SECONDS=120        # pull every 2 min
BP_SYNC_MAX_ROWS=20000              # cap per run while backfilling
BP_SYNC_CHUNK_SIZE=2000
BP_SYNC_SLEEP_SECONDS=0.25          # throttle between chunks (gentle on prod)
BP_ALLOW_PROD_PULL=1
```

Then seed the profiles **once** (see §5), and switch to the production schedule (§4b).

### 4d. Other useful knobs (sensible defaults; see `config.py` for the full list)

| Variable | Default | Meaning |
|---|---|---|
| `BP_LOOKBACK_MONTHS` | 3 | Learning window (quarterly, per CTO). |
| `BP_RETRAIN_MIN_NEW_TXNS` / `BP_RETRAIN_MAX_AGE_DAYS` / `BP_DRIFT_SIGNAL_THRESHOLD` | 100 / 30 / 5 | Event-driven retrain triggers (OR-ed). |
| `BP_ELIGIBLE_MIN_TENURE_DAYS` / `BP_ELIGIBLE_MIN_TXNS` / `BP_ELIGIBLE_MAX_FRAUD_TXNS` | 90 / 100 / 0 | §1 trust gate (who is judged on their own profile vs peers). |
| `BP_WEBHOOK_MAX_ATTEMPTS` / `BP_WEBHOOK_BACKOFF_BASE_SECONDS` / `BP_WEBHOOK_RELAY_INTERVAL_SECONDS` | 8 / 5 / 5 | Webhook outbox retry policy. |
| `BP_VELOCITY_RETAIN_HOURS` | 48 | How long recent transactions are kept for burst detection. |
| `BP_DEFAULT_CURRENCY` | NGN | Currency assumed when a transaction has none. |
| `BP_DEMO_LOG_DIR` | `/app/logs/demo` | Where `GET /demo` responses are logged. |

---

## 5. First-time seeding (backfill → build profiles)

Do this once, on first deploy, while in **interval mode** (§4c):

```bash
# 1. Let the sync service run until the cache stops growing (backfill complete).
docker compose exec behaviour-profile python -c "import sync_manager,json;print(json.dumps(sync_manager.status(),default=str))"

# 2. Build the initial per-currency profiles from the cache (one-time).
docker compose exec behaviour-profile python retrain.py --rebuild-all

# 3. Switch .env to the production daily schedule (§4b) and redeploy the sync service.
```

After this, retraining is **event-driven per customer** — you never run `--rebuild-all` again.

---

## 6. Guaranteed webhook delivery (the outbox)

Every `/score` writes the decision **and** a `webhook_status='pending'` marker in the **same
database transaction**, then delivers the webhook inline. If that inline attempt is lost
(e.g. the API crashes), the **relay** in the `sync` service redelivers it with exponential
backoff until it succeeds (`sent`) or exhausts the retry budget (`dead`). No decision's
delivery is ever lost, and it needs **no extra infrastructure** (no Redis/queue).

- **Monitor** the dead-letter queue: rows in `bp_decision` with `webhook_status='dead'`.
- Every attempt is recorded in `bp_webhook_delivery` for audit.

---

## 7. Carry learned behaviour to production (`pg_dump` sync)

Production starts with an **empty** profile store. Two options:

**A. Rebuild in production** — let it build itself (backfill → `retrain.py --rebuild-all`, §5).

**B. Carry the profiles over** (no re-learning) — the profiles were learned from real
production data, so they are valid in production. Use **`pg_migrate_store.sh`**:

```bash
# on the source (this) environment — writes a data-only dump to ./migrate_dumps/
./pg_migrate_store.sh dump

# into the production store (its schema is created automatically on first API start)
DEST_PG_HOST=<prod-db-host> DEST_PG_PORT=5432 DEST_PG_USER=<user> \
DEST_PG_PASSWORD=<pw> DEST_PG_DB=<db> DEST_PG_SSLMODE=require \
  ./pg_migrate_store.sh restore ./migrate_dumps/store_<timestamp>.dump --yes

# verify
DEST_PG_*=... ./pg_migrate_store.sh verify --dest
```

It carries the learned tables (profiles, peer baselines, rules, watermark). **Dumps contain
customer-derived data and are git-ignored** (`migrate_dumps/`).

> For production durability, run the profile store on a **managed PostgreSQL** (with
> backups / point-in-time recovery) and point `STORE_PG_*` at it — then learned behaviour is
> backed up and inherited by every deploy automatically.

---

## 8. Logs & auditing — where everything is

**PostgreSQL audit tables** (queryable, the system of record):

| Table | What it records |
|---|---|
| `bp_decision` | Every `/score` decision: the transaction, verdict, fired rules, latency, webhook status. |
| `bp_webhook_delivery` | Every webhook delivery **attempt** (append-only) — proves when/how each decision was sent. |
| `bp_event_log` | Per-customer accountability trail (scored, retrained, skipped-with-reason, failures). |
| `bp_profile_history` | A snapshot each time a profile is (re)built. |

**Files** (under `./logs/`, bind-mounted to the host, git-ignored):

| Path | What |
|---|---|
| `logs/demo/demo_responses.jsonl` | The full JSON of every `GET /demo` run (for the DB engineer). |
| `logs/loadtest/` | Load-test results (CSV, HTML, `summary.json`). |
| `docker compose logs -f behaviour-profile` \| `sync` | Live structured stdout (every stage narrated with its reason). |

---

## 9. Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /score` | **The hook.** Score a transaction → decision + fired rules; webhook + audit + maybe retrain. |
| `GET /demo` | End-to-end walkthrough for one customer, every stage narrated. Logged to `logs/demo/`. |
| `GET /customer/{entity_key}` | Everything about a customer: identity, eligibility, what was learned (per currency), retrain state. |
| `GET /profile/{entity_key}` | The raw learned profile row(s) — one per currency. |
| `GET /customers` / `GET /examples` | Browse real customers / copy-paste sample keys. |
| `GET /stats` | Totals: customers, per-currency counts, Active vs Warming-Up, rules, peer baselines. |
| `GET /sync/status` | Ingestion state: schedule, watermark, cache size (read-only). |
| `POST /retrain/{entity_key}` | Force a rebuild of one customer now (from the cache). |
| `POST /reload` | Refresh the in-process rule/blacklist/peer cache. |
| `GET /health` | Liveness. |

> `POST /sync` (a manual, on-demand production pull) is **intentionally disabled** — nothing
> can pull from production on request; ingestion is only the scheduled `sync` service.

---

## 10. How it works (architecture)

Three distinct layers — never confuse them:

```
PRODUCTION (read-only)          PROFILE STORE (PostgreSQL, the named volume)
transaction DB                  ┌─ bp_transactions_cache      ← CACHE: raw prod txns (filled by `sync`)
      │  scheduled              ├─ bp_user_behaviour_profile  ← PROFILE: learned normal, PER CURRENCY
      │  ingestion only ───────▶├─ bp_peer_baseline           ← cold-start baseline (per branch/type/currency)
                                ├─ bp_recent_txn              ← recent window for live burst detection
                                ├─ bp_decision / _webhook_delivery / _event_log  ← audit
                                └─ bp_rule_definition / _settings / _blacklist   ← AML rules
POST /score reads the PROFILE (never production, never the raw cache) → fast decision.
```

- **Multi-currency:** a customer has one profile **per currency**; a transaction is scored
  against the profile and peer baseline **for its currency**, so NGN and USD never blend.
  Adding a new currency (e.g. a `£10,000` escalation rule) is **data only** — no code change.
- **Governance gate (anti-poisoning):** a customer is judged on their **own** profile only
  once they pass the §1 eligibility gate (tenure + clean-txn count + no confirmed fraud +
  confidence). Otherwise they are judged against their **peer group** — so a fraudster can't
  establish a "trusted" baseline from a little fake activity.

---

## 11. Load testing (optional)

Locust against `POST /score`, opt-in via the `loadtest` compose profile:

```bash
LOCUST_USERS=20 LOCUST_RUN_TIME=60s docker compose --profile loadtest run --rm loadtest
# results in ./logs/loadtest/  (CSV, HTML, summary.json)
```

Knobs: `LOCUST_USERS`, `LOCUST_SPAWN_RATE`, `LOCUST_RUN_TIME`, `SCORE_SLA_MS` (default 600ms),
`SCORE_LOAD_ENTITY_KEYS`, `SCORE_ABNORMAL_PCT`.

---

## 12. Not yet perfected (planned)

- **Read replica** as the ingestion source (shields the production primary entirely) and
  **card transactions** in the entity key (BIN + last-4).
- **Geo-velocity / impossible-travel** (GeoIP + haversine speed check).
- **Per-currency thresholds** for the two remaining absolute NGN rules (`block_above_hard_cap`,
  `high_outbound_amount_15m`); the escalation rules are already per-currency.
