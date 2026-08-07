# Behavioural Anti-Fraud ML subsystem (`ml/`)

> **Scope:** this README is the **model subsystem** — training, MLOps, and the inference library.
> For the **production API** that calls this model (`POST /score`, webhook, `bp_decision`, service
> topology, repo layout), see the [**root README**](../README.md). The model runs **in-process**
> inside that service; `ml/api.py` here is only an optional standalone API for dev/testing.

Unsupervised ensemble that scores each transaction against **that customer's** learned
behaviour. **Behavioural detection only — no AML rules** (a separate service owns AML, per
`1.md`). Full design + decisions: [`../docs/anti-fraud-model.md`](../docs/anti-fraud-model.md).

```
cache (read-only) -> ingest -> validate -> clean(§1: active/clean only, AND gate)
   -> features (per-customer deviation + graph) -> [GNN | Isolation Forest | Autoencoder]
   -> ensemble -> tiered behavioural codes -> /score (FastAPI) -> webhook + compliance log
                                                         (+ plots, metrics, model registry)
```

- **Keyed on the customer** (stable `identifier`); branch and transaction type are *features*.
- Trains **only on active/trusted/clean** customers — PDF §1 is an **AND**: `≥ BF_MIN_TXNS clean
  txns` **and** `≥ BF_MIN_DAYS_ACTIVE days of history` **and** no confirmed fraud.
- **GPU-aware**: the Autoencoder and GNN use the GPU automatically for **training**; inference is
  CPU-cheap (GNN embeddings are precomputed and looked up; the AE forward pass is sub-ms).
- Every run is **versioned** (`ml.registry`) with rollback; every figure overwrites the last in
  `artifacts/plots/`; every inference is written to an immutable **compliance log**.

---

## Production topology — ONE serving service, the model runs inside it

In production there is **one API Adhere calls**: the **Behaviour-Profile Service** (`service.py`,
port **8080**). Its `POST /score` uses the **behavioural anti-fraud MODEL in-process** to decide —
the incoming transaction is compared against the customer's learned baseline + recent history and
the ensemble returns the verdict (`config.USE_MODEL`, default **true**; set `BP_USE_MODEL=false`
to fall back to the legacy rule engine). **You are NOT running two scoring services.**

**The GPU is only for training.** Inference is CPU-only — the GNN score is a lookup and the
autoencoder is a tiny forward pass — so the serving image ships CPU `torch` and never needs a GPU.

| # | Dockerfile | What it is | When it runs | GPU? |
|---|---|---|---|---|
| 1 | **`Dockerfile`** (root) | **Behaviour-Profile Service** — the production API; `/score` runs the model in-process | always-on | no |
| 2 | **`Dockerfile.ml`** | **Offline trainer** (`python -m ml.train`) — builds/promotes the model | scheduled / on trigger, then exits | **yes** |
| 3 | `Dockerfile.api` | Optional standalone model API (`ml/api.py`, port 8085) — for dev/testing the model alone | optional | no |

So: **#1 serves, #2 trains (GPU), #3 is optional.** #1 and #2 share the model **artifacts**
(a mounted volume / object store) and the read-only store.

```
                 ┌── offline trainer (Dockerfile.ml, GPU) ── writes ──▶ artifacts/models/<version>
                 │        (scheduled or via ml.retrain_trigger)                    │  (shared volume)
 sync_manager ──▶ bp_transactions_cache (store, read-only) ◀── reads ──────────────┤
                 │                                                                 ▼
 Adhere txn ──HTTP──▶ Behaviour-Profile Service  POST /score  (Dockerfile, CPU, loads active model)
                          │  compares txn ↔ customer profile/history → ensemble decision
                          ├─▶ HTTP response (behavioural contract)
                          ├─▶ bp_decision (audit) + webhook ▶ Adhere ▶ writes behavioral_analysis
                          └─▶ artifacts/inference_log/*.jsonl (compliance history)
```

### How data → profile → decision is wired

1. **`sync_manager`** (scheduled, the only production reader) pulls transactions into
   `bp_transactions_cache` (read-only for the model).
2. **Training** (`Dockerfile.ml`, GPU, on a schedule or when `ml.retrain_trigger` fires) learns
   each eligible customer's baseline + trains the ensemble, and **promotes** the model.
3. On **`POST /score`**, the service builds the model payload from the transaction, and the model
   reads that customer's **recent history** from the store to compute velocity/deviation, compares
   it to their learned baseline, and returns `safe / review / unsafe` — persisted to `bp_decision`
   and delivered by webhook (which writes `behavioral_analysis`).

---

## Version pinning (no skew)

`ml/requirements-ml.txt` is the **single source of truth** and is **fully pinned**. **Both**
`Dockerfile.ml` (training) and `Dockerfile.api` (serving) run `pip install -r
ml/requirements-ml.txt`, so the model is trained and served with the **exact same** library
versions — the `IsolationForest`/`StandardScaler` pickles load identically, no
`InconsistentVersionWarning`. If you bump a version, **rebuild both images and retrain**.

---

## Run it — training + MLOps (offline, GPU)

This is a **separate offline job**, not the serving path. Easiest via compose (reaches the store
by service name):

```bash
docker compose --profile train run --rm trainer --promote
```

or standalone:

```bash
docker build -f Dockerfile.ml -t adhere-bf-ml .
docker run --rm --gpus all --network host \
  -e BF_PG_HOST=localhost -e BF_PG_PORT=5433 -e BF_PG_USER=behaviour -e BF_PG_DB=behaviour \
  -e BF_PG_PASSWORD='<STORE_PG_PASSWORD>' -e BF_MIN_TXNS=50 -e BF_MIN_DAYS_ACTIVE=30 \
  -v "$PWD/artifacts:/app/artifacts" adhere-bf-ml --promote
```

`--gpus all` needs the NVIDIA Container Toolkit (falls back to CPU otherwise). Output: a versioned
model under `artifacts/`, all plots, `artifacts/metrics/metrics.json`, and a performance-history
entry. `--promote` publishes it as ACTIVE **only if it passes the acceptance gate** (below).

## Run it — production serving (the Behaviour-Profile Service, CPU)

This is the API Adhere calls. It runs the model in-process; **no GPU**. Mount the `artifacts/`
the trainer produced so the service loads the promoted model.

```bash
docker build -t adhere-behaviour .           # root Dockerfile (service.py + model, CPU)
docker run -d --name behaviour --network host \
  -e STORE_PG_HOST=localhost -e STORE_PG_PORT=5433 -e STORE_PG_USER=behaviour \
  -e STORE_PG_DB=behaviour -e STORE_PG_PASSWORD='<STORE_PG_PASSWORD>' \
  -e BP_USE_MODEL=true \
  -e BP_SCORE_WEBHOOK_URL='https://adhere.example/api/behavioural-webhook' \
  -v "$PWD/artifacts:/app/artifacts" \
  adhere-behaviour
```

- Swagger UI: **http://localhost:8080/docs** · health: `GET /health`.
- `BP_USE_MODEL=true` (default) → `/score` returns the **model** decision; `false` → legacy rules.
- The model reaches the store via the same `STORE_PG_*` the service uses (read-only).

### curl `/score` once the service is up (the production payload)

```bash
# NORMAL transaction (known customer) -> expect "safe"
curl -s -X POST http://localhost:8080/score -H 'Content-Type: application/json' -d '{
  "branch_id":231,"origin_account_no":"5510027882","identifier":"22598330040",
  "amount":10100,"currency":"NGN","transaction_type":"transfer",
  "destination_account_no":"3032682286","origin_country":"Nigeria",
  "customer_location":"-","ts":"2026-07-15T19:00:00Z","transaction_id":"TXN-NORMAL-001"}'

# ANOMALOUS transaction (same customer) -> expect "review"/"unsafe"
curl -s -X POST http://localhost:8080/score -H 'Content-Type: application/json' -d '{
  "branch_id":231,"origin_account_no":"5510027882","identifier":"22598330040",
  "amount":7500000,"currency":"NGN","transaction_type":"transfer",
  "destination_account_no":"NEW-MULE-99","origin_country":"Nigeria","destination_country":"KP",
  "customer_location":"Kano","ts":"2026-07-15T03:12:00Z","transaction_id":"TXN-ANOMALY-001"}'
```

The service returns the behavioural contract (`status`, `activity_code`, `risk_score`,
`triggered_signals`, `recommended_actions`, `model_version`, `inference_ms`, …), persists it to
`bp_decision`, and delivers it by webhook.

## Optional — standalone model API (dev/testing only)

`Dockerfile.api` runs `ml/api.py` on port **8085** with the richer nested payload and Swagger at
`/docs`. Handy for exercising the model alone; **not** the production entry point.

---

## Compliance logging, webhook & behavioral_analysis

Every `/score` is appended to `artifacts/inference_log/inference-YYYY-MM-DD.jsonl` (JSONL, one
line per inference): `transaction_id`, masked `customer_ref`, `status`, `activity_code`,
`risk_score`, `confidence_score`, `model_version`, `timestamp`, and `inference_ms` — an immutable
history for compliance. Each webhook delivery is logged too (`event: webhook_delivery`).

The response the model POSTs to the webhook maps to `behavioral_analysis` as:
`transaction_id → transaction_id`, `status/activity_code/description/risk_score → result`,
`confidence_score → confidence_score`, `triggered_signals → triggered_rules`,
`recommended_actions → recommended_actions`, `model_version → model_id`.

## The MLOps loop — who does what (no complexity in the service)

**The serving service stays thin.** It only: loads the ACTIVE model from the registry, scores
`/score`, records every decision to `bp_decision` + the compliance log, and reloads the model on
request. **All ML/MLOps logic lives here on the ML side** and hands off through the **registry** —
so there is no training/monitoring code in the profile service and no confusion.

```
                        ┌──────────────── ML side (offline / scheduled jobs) ────────────────┐
 SERVICE (thin)         │  ml.monitor --live   →  drift/degradation?  →  Slack alert (steps)  │
 /score → bp_decision ──┼─▶ reads bp_decision      │                                          │
   + compliance log     │  ml.retrain_trigger --run (new data OR 30d OR drift)                │
   loads ACTIVE model ◀─┼─── registry (handoff) ◀── PROMOTE *only if better* (acceptance gate)│
   (POST /reload)       └────────────────────────────────────────────────────────────────────┘
```

**1. Live monitoring (production).** A scheduled job reads the decisions the service wrote:

```bash
python -m ml.monitor --live     # reads bp_decision over the last BF_LIVE_WINDOW_HOURS
```

Signal today (no fraud labels yet): the **flagged (review+unsafe) rate**. The tiering is calibrated
to flag ~12%, so a live rate outside `BF_LIVE_FLAG_MIN..MAX` (2%–30%) means the data/behaviour has
drifted → it **alerts to Slack** (`BF_SLACK_WEBHOOK_URL`) with the exact steps. When analyst
feedback arrives, live **precision** is added here and gated on `BF_MIN_PRECISION`.

**2. Retraining triggers (PDF §4 — an OR).**

```bash
python -m ml.retrain_trigger          # print the decision only
python -m ml.retrain_trigger --run    # retrain + promote IF a trigger fired AND the gate passes
```

Fires when **ANY** holds vs the active model's data watermark: `≥100 new transactions` **OR**
`≥30 days` **OR** `amount-drift PSI ≥ 0.25`.

**3. Acceptance gate (decide whether to keep the new model).** `--promote` publishes the candidate
as ACTIVE **only if it beats the current active** on the **synthetic-anomaly AUC** (or precision,
once labelled); otherwise it keeps the old model and logs/says why. Training also records the run to
`artifacts/monitor/performance_history.jsonl` and writes `artifacts/monitor/health.json`.

**4. Roll it out (registry handoff).** Promotion only changes the registry pointer — the running
service still serves the old model until told. Pick up the new one with **no downtime**:

```bash
curl -X POST http://<service>:8080/reload      # drops the cached model, loads the new ACTIVE
# rollback if needed:
python -c "from ml import registry; registry.rollback()"   # then POST /reload again
```

**Schedule 1 + 2** (cron / k8s CronJob) on the training host; everything else follows from them.
`python -m ml.monitor` (no `--live`) shows the active model's training-time health/floors.

---

## Artifacts (git-ignored, under `artifacts/`)

| Dir / file | Contents |
|---|---|
| `plots/` | confusion matrix, PR, ROC, metric bars, score distribution, **train+val loss curves** (AE & GNN), **validation_synthetic_roc.png**, **validation_contamination.png** — all overwritten each run |
| `metrics/metrics.json` | canonical metrics for the latest run (+ `metrics_<version>.json` per run) |
| `models/<version>/` | feature builder, graph features, Isolation Forest, Autoencoder, GNN, tiering, ensemble |
| `registry/index.json` | versions, status, active/previous pointers (rollback) |
| `inference_log/` | per-inference compliance JSONL (daily) |
| `monitor/` | `performance_history.jsonl`, `health.json` |

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `BF_PG_HOST/PORT/USER/PASSWORD/DB` | localhost:5433 | read-only store connection |
| `BF_WEBHOOK_URL` | — | Adhere webhook for the decision (enables delivery) |
| `BF_DEVICE` | auto | force `cpu`/`cuda`/`mps` |
| `BF_MIN_TXNS` / `BF_MIN_DAYS_ACTIVE` | 50 / 30 | §1 eligibility AND (target 100 / 90 as the window grows) |
| `BF_RETRAIN_MIN_NEW` / `BF_RETRAIN_MAX_DAYS` / `BF_DRIFT_PSI` | 100 / 30 / 0.25 | §4 retraining triggers (OR) |
| `BF_MIN_SYNTH_AUC` / `BF_MIN_PRECISION` | 0.75 / 0.02 | health floors (alert if below) |
| `BF_SLACK_WEBHOOK_URL` | — | health/MLOps alerts |

## Notes on metrics

Supervised plots use **weak proxy labels** (`status='blocked'` / `sender_blacklisted` /
`is_blocked`) because there are no confirmed-fraud labels yet; they are titled as proxy. The
**synthetic-anomaly AUC** (target 0.75–0.85) is the honest, label-free quality signal today. When
confirmed labels arrive, the same evaluation switches to real precision/recall/F1 and the tier
cut-offs are recalibrated.

## Known limitations / future work

- **Velocity / bursts.** Captured as **model features** computed from each customer's recent
  history at score time: **`vel_1m`, `vel_2m`, `vel_3m`, `vel_10m`, `vel_15m`, `vel_1h`, `vel_24h`**
  — all `log1p`-squashed so a bursty customer's heavy tail no longer stretches the normal
  distribution. There is no separate velocity engine (the retired `live_velocity.py`/`bp_recent_txn`
  path is gone). Sub-minute grain would need further features + a retrain.
- **Geo-velocity** (IP → latitude/longitude, impossible-travel) is not implemented — planned.
- **Data-limited profile components** (device fingerprints, login events, session duration, balance,
  merchant category — PDF §16) and the **analyst feedback loop** (§11) await upstream data.
