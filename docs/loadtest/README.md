# Load test — `POST /score`

Locust against the scoring hook. **SLA target: 500-600 ms** for the round-trip
(transaction received → decision returned). Raw artifacts in this folder are **aggregate
stats only — no customer data**.

## Environment (important caveat)

These numbers are from a **single-host dev setup** — the API, the PostgreSQL store, and the
ingestion service all run on one machine, and the dev Postgres container is CPU-contended.
**Production** (a dedicated/managed PostgreSQL + multiple API replicas) will reach a much
higher ceiling.

The webhook ("send the event") is delivered **after** the HTTP response as a background
task, so it does not add to the measured latency. Measured = receive → decision.

## Results (0 HTTP failures across all runs)

| Concurrency | RPS | p50 | p95 | p99 | SLA breaches | Verdict |
|---|---|---|---|---|---|---|
| 5 users  | 55 | 56 ms  | 140 ms | 200 ms | 0%    | ✅ well within SLA |
| 10 users | 67 | 120 ms | 180 ms | 240 ms | 0%    | ✅ within SLA |
| 20 users | 60 | 280 ms | 630 ms | 770 ms | 7%    | borderline at p95 |
| 50 users | 72 | 610 ms | 1000 ms | 1400 ms | ~52% | ❌ saturated |

`summary.json`, `score.html`, and the `score_stats*.csv` in this folder are the raw output
of the final run.

## Interpretation

- **The SLA is comfortably met up to ~15-20 concurrent requests** (p95 well under 600 ms).
- **Throughput plateaus at ~65-72 RPS** on this dev box, and adding API workers (2 → 4)
  barely changed it — so the ceiling here is the **single dev PostgreSQL**, not the app.
- To raise the ceiling for production: a **dedicated/managed PostgreSQL**, **multiple API
  replicas** (`BP_WORKERS` / horizontal scaling), and — the highest-value app change —
  collapsing the ~7-8 DB round-trips `/score` currently makes per call (it reads the profile
  twice, for example).

## Two concurrency bugs found and fixed during this load test

1. **Retrain connection-storm** — every `/score` spawned a background `maybe_retrain` that
   opened a *fresh* DB connection (~30-70 ms each). Fixed: the per-score check uses the pool.
2. **Pooled `row_factory` leak** — `db.pooled(dict_rows=True)` left `dict_row` on a recycled
   connection, so a later borrower got dicts and `json.loads()` failed → cascading 500s under
   load. Fixed: `pooled()` always resets the row factory on borrow.

## Reproduce it

```bash
# from the repo root, with the stack up (docker compose up -d)
LOCUST_USERS=20 LOCUST_RUN_TIME=60s docker compose --profile loadtest run --rm loadtest
# results land in ./logs/loadtest/  (summary.json, score.html, CSVs, locust.log)
```

Env knobs: `LOCUST_USERS`, `LOCUST_SPAWN_RATE`, `LOCUST_RUN_TIME`, `SCORE_SLA_MS`,
`SCORE_LOAD_ENTITY_KEYS`, `SCORE_ABNORMAL_PCT`. See the top-level README §11.

> **Why the pg_dump and demo logs are NOT in this repo:** they contain real customer data
> (profiles, names, account numbers) and must never be committed. The DB engineer tests the
> dump by generating their own with `./pg_migrate_store.sh dump` (README §7), and the demo
> logs are produced locally by running `GET /demo` in their environment (logged to
> `logs/demo/`).
