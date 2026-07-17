# Behaviour-Profile Microservice — run, test & integrate

This packages the whole system as a **microservice** the adhere application calls
for every transaction. It scores the transaction against the customer's learned
behaviour profile **and** retrains that customer's profile in place when their own
behaviour warrants it — **event-driven, no cron**.

---

## 1. What it does per transaction

```text
adhere  ──POST /score──▶  behaviour-profile service
                              │
                              ├─ 1. score txn vs the customer's stored profile
                              │     (rules fire if it breaks their pattern;
                              │      live velocity catches bursts; new / Warming-Up
                              │      customers are judged against their peer group)
                              ├─ 2. bump the customer's counters
                              └─ 3. retrain THIS customer if a trigger is met:
                                    ≥100 new txns  OR  ≥30 days  OR  sustained drift
                              │      (recomputed from the LOCAL CACHE — never prod)
                              │
                    ◀──JSON── { decision, fired_rules, retrained }
```

- **Per customer, not per segment.** Each customer is independent.
- **Concurrent-safe.** Many customers retrain in parallel; the *same* customer is
  guarded by a per-customer **Postgres advisory lock**, so it never double-processes.
- **No scheduler.** Retraining rides on the transaction that adhere already sends.
- **Production is never in the request path.** Scoring and retraining read the local
  store/cache only.

---

## 2b. Where the data comes from (ingestion + cache)

```text
 PRODUCTION Postgres (READ-ONLY)          ← the transaction source
            │
            │  sync_manager.py   ← THE ONLY PROCESS THAT READS PRODUCTION
            │  keyset-paged · chunked · row-capped · throttled ·
            │  statement-timeout · resumable watermark · read-only
            ▼
 PostgreSQL profile store (the `db` container)
   ├─ bp_transactions_cache      ← local copy of prod txns (the cache)
   ├─ bp_sync_state              ← ingestion watermark (resume point)
   ├─ bp_user_behaviour_profile  ← the learned profiles
   └─ bp_rule_definition / bp_event_log / bp_peer_baseline / ...
            ▲                          ▲
            │ learn / retrain          │ score
     (reads the CACHE)          POST /score → {decision, fired_rules}
```

**Who does the caching:**

| Component | Role |
| --- | --- |
| `sync_manager.py` | **Writes** the cache. The only production reader. Pulls deltas in bounded chunks (`WHERE id > :last ORDER BY id LIMIT :n`), caps each run, throttles between chunks, resumes from `bp_sync_state`, re-pulls a trailing window so `clean → blocked` flips are corrected, and prunes to the learning window. Logs every chunk. Run it as a **scheduled job** (cron / k8s CronJob); `POST /sync` exists for demos. |
| `bp_transactions_cache` | **Is** the cache — a Postgres table, so it is *shared* by every replica (not a per-replica file). |
| `retrain.fetch_customer()` | **Reads** the cache — one indexed lookup on `(entity_key, date_created)`. This replaced the unbounded per-retrain production query that used to load the live DB. |

Tuning dials (all env-driven): `BP_SYNC_CHUNK_SIZE`, `BP_SYNC_MAX_ROWS`,
`BP_SYNC_SLEEP_SECONDS`, `BP_SYNC_STATEMENT_TIMEOUT_MS`, `BP_SYNC_REFRESH_DAYS`,
`BP_SYNC_PRUNE`, and the master switch `BP_ALLOW_PROD_PULL` (`0` = stop all live
reads; the service keeps serving from the cache). Design rationale:
[`ingestionstratimprove.md`](ingestionstratimprove.md).

---

## 2. Run it

### Locally (venv)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn service:app --host 0.0.0.0 --port 8080   # needs a reachable STORE_PG_*
```

### Docker (recommended — brings up the store too)
```bash
cp .env.example .env          # STORE_PG_PASSWORD is required
docker compose up -d --build  # starts PostgreSQL 17 (the store) + the service
```
All hosts/passwords/thresholds are environment variables (see `config.py`) — nothing
is hard-coded for production.

**The store is PostgreSQL.** The transaction source is already Postgres, so the
profile store is now Postgres too — one engine, one driver, one dialect end-to-end.
(This replaced the previous MySQL store; see `ingestionstratimprove.md` §7.)

**One-time load:** copy the already-learned profiles across with
`python migrate_mysql_to_pg.py` (store-to-store, ~99k profiles, production untouched).
Only rebuild from scratch if you have no prior store.

Full step-by-step: **[`RUNBOOK.md`](RUNBOOK.md)**.

---

## 3. Test it (what Anita will do)

```bash
# health
curl localhost:8080/health

# look at a real trusted customer
curl localhost:8080/profile/231:1100716290

# score a transaction  (this is what adhere sends per transaction)
curl -X POST localhost:8080/score -H 'Content-Type: application/json' -d '{
  "branch_id":231, "origin_account_no":"1100716290", "amount":5600,
  "currency":"NGN", "destination_account_no":"ACC999",
  "customer_location":"street, Sabon Gari, State",
  "origin_country":"NG", "destination_country":"NG", "transaction_id":"T1"
}'
```

**What to expect:**
- A **normal** transaction for that customer → `"decision":"allow"`, `fired_rules: []`.
- An **abnormal** one (huge amount, unusual city/country, 3 AM, new account) →
  `"decision":"review"` with the specific rules listed.
- A **new / Warming-Up** account → judged against its peer group (rules tagged
  `peer_baseline`), never trusted on thin history.
- After a customer crosses a **retrain trigger**, the response includes
  `"retrained": { "version": N, "trigger": "…", ... }` and their profile is refreshed
  in place. (To see it during a short demo, start with a low threshold:
  `BP_RETRAIN_MIN_NEW_TXNS=3`.)

**See two customers retrain at once** (Anita's question):
```bash
curl -X POST localhost:8080/retrain/231:1100716290 &
curl -X POST localhost:8080/retrain/232:ACCOUNTNO &
wait   # both return independently
```

Interactive API docs are at **`/docs`** (FastAPI/Swagger) once the service is running.

---

## 4. The retrain triggers (editable, no code change)

A customer is retrained when **ANY** is true (env-tunable in `config.py`):

| Trigger | Counter (`bp_user_behaviour_profile`) | Env var | Default |
| --- | --- | --- | --- |
| scored transactions since last build | `txns_since_build` | `BP_RETRAIN_MIN_NEW_TXNS` | 100 |
| days since last build | age of `last_retrained_at` | `BP_RETRAIN_MAX_AGE_DAYS` | 30 |
| sustained drift (consecutive anomalies vs own profile) | `drift_signal_count` | `BP_DRIFT_SIGNAL_THRESHOLD` | 5 |

> **On the transaction-count trigger:** `txns_since_build` is incremented **once per
> `/score` call** for that customer (`service.py`) — it counts every transaction the
> service *scored* since the last build, **not** a separately filtered "clean" count
> re-computed at scoring time. (The clean-baseline filter is applied inside
> `build_profiles.py` when the profile is actually recomputed, not to this counter.)
> All three counters reset to 0 after a retrain.

These are the only knobs to "tune when retraining happens." In production they become
editable in **Django Admin** (see §6).

---

## 5. Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | service overview + where to start |
| GET | `/health` | liveness (for k8s/load-balancer) |
| GET | `/demo` | **run the whole pipeline end-to-end through the service** and return every stage with its real result (the API version of `demo_end_to_end.sh`) |
| GET | `/stats` | system totals: profiles, Active/Warming-Up split, drift, rules |
| POST | `/score` | **the live decision hook** — fast `allow`/`review` + fired rules; the decision is returned, delivered by webhook (`BP_SCORE_WEBHOOK_URL`), and saved to `bp_decision` for audit. Retrain runs in the background so it never slows the response. |
| GET | `/customer/{entity_key}` | **full status of one customer**: eligibility (met/not), what was learned, retrain state (why it will/won't retrain next), and their recent event trail |
| GET | `/profile/{entity_key}` | raw stored profile row |
| POST | `/retrain/{entity_key}` | force a retrain now |
| GET | `/docs` | interactive API docs (try every endpoint in the browser) |

`entity_key` = `"{branch_id}:{origin_account_no}"`.

### Accountability — every step is logged
Nothing the system does is invisible. Each transaction leaves a trail in **stdout**
(so Docker/k8s capture it) **and** the `bp_event_log` table:
- `score` — the decision, what it was judged against (own profile vs peer group), rules fired.
- `retrain` — a customer was retrained (trigger + new version).
- `retrain_skip` — **why not** (e.g. "not due — needs 99 more txns, or 30 more days, or 4 more drift signals").
- `retrain_fail` — the error, if a retrain ever fails.

`GET /customer/{entity_key}` surfaces the last events for that customer, so Anita can
see exactly what happened to anyone, when, and why.

---

## 6. How it plugs into adhere (integration + DevOps)

**Integration point (one line in adhere):** wherever a transaction is saved/screened
in the monitoring pipeline, call `POST /score` and act on `decision` / `fired_rules`.
Because it's HTTP, adhere (Django) stays decoupled — no shared code, deploy independently.

**The service never reads production.** A separate ingestion job (`sync_manager.py`)
is the *only* process that touches the adhere Postgres, and it reads READ-ONLY
(`SET default_transaction_read_only=on`) in bounded, keyset-paged, throttled chunks
into a local cache (`bp_transactions_cache`). Every score and every retrain reads
that cache, so **production sees exactly one reader no matter how many replicas run**.
Profiles are written to the PostgreSQL profile store. See §2b and
`ingestionstratimprove.md`.

**Deployment (DevOps):**
- **Stateless** service → run **N replicas** behind a load balancer; scale horizontally.
- Concurrency is safe across replicas because the per-customer lock is in the DB
  (`GET_LOCK`), not in-process.
- Add **connection pooling** (PgBouncer) — the code
  opens a connection per request today; swap in a pool for high throughput.
- Health/readiness: `/health`; container `HEALTHCHECK` already defined in the Dockerfile.
- Config/secrets via **env vars / k8s secrets** (never in the image).
- Observability: put it behind the API gateway; log `fired_rules` + `retrained` per call.

**Making the thresholds editable in Django Admin (production):**
- Mirror `bp_rule_definition`, `bp_rule_settings`, and the retrain thresholds as
  **Django models** registered in Admin, so compliance/ops edit them with no deploy.
- The retrain trigger values (`RETRAIN_MIN_NEW_TXNS`, `MAX_AGE_DAYS`,
  `DRIFT_SIGNAL_THRESHOLD`) become a small `RetrainPolicy` model the service reads.

---

## 7. Not in this service (by design)
- The **machine-learning score** (anomaly model) — the profiles are the feature store
  it will consume; unblocked once the analyst-confirmed-fraud labels are available.
- Login/device signals — not in the transaction data yet (pending Eric).
