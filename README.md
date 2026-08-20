# AI Service — Behavioural Anti-Fraud (Behaviour-Profile Service + ML subsystem)

A FastAPI + PostgreSQL system that learns each customer's **normal** transaction behaviour and
scores every new transaction against it in real time. Scoring is done by an **unsupervised
behavioural anti-fraud MODEL** (GNN + Isolation Forest + Autoencoder ensemble). **Behavioural
detection only — the AML rule engine is deprecated** (a separate service owns AML, per `1.md`).

- **Learns** a per-customer behaviour baseline from transaction history (offline, GPU training).
- **Scores** each incoming transaction with the model → `safe` / `review` / `unsafe` + a behavioural
  activity code (`BF-xxx`), the signals that fired, and which detectors flagged it.
- **Returns** the decision, **sends it to your system by webhook**, and **stores it for audit**
  (`bp_decision` + an immutable per-inference compliance log).

---

````markdown
# Behavioural Anti-Fraud Model Loading and Scoring Flow

## Model Loading

The behavioural service does not load the ML model directly inside `service.py`. Instead, `service.py` delegates model loading and scoring to `ml/serve.py`.

### `ml/serve.py` — Model Library

`ml/serve.py` is responsible for loading the active behavioural ML model and running inference.

### 1. Active Model Loading

**File:** `ml/serve.py`  
**Line:** `56`

The `_active()` function uses `@lru_cache` to load and cache the active model:

```python
v = registry.active()
return _Model(v)
````

This means the service:

1. Gets the active model version from the model registry.
2. Creates the `_Model` instance for that version.
3. Caches the loaded model so the model files are not repeatedly loaded from disk for every request.

### 2. Loading the Model Artifacts

**File:** `ml/serve.py`
**Line:** `30`

The `_Model.__init__(version)` method is responsible for loading the actual model artifacts.

It first gets the model directory:

```python
d = registry.model_dir(version)
```

The model components are then loaded from:

```text
artifacts/models/<version>/
```

The following components are loaded:

```text
featurebuilder.joblib
IsoForestDetector
AutoencoderDetector
GNNDetector
tiering.json
ensemble.json
```

The model therefore consists of:

* **Feature Builder** — builds the feature vector required by the model.
* **Isolation Forest** — detects unusual behavioural patterns.
* **Autoencoder** — detects reconstruction anomalies.
* **GNN** — evaluates behavioural relationships in the transaction graph.
* **Tiering configuration** — determines the behavioural risk zones and activity-code tiers.
* **Ensemble configuration** — defines how the individual detector outputs are combined.

### 3. Scoring a Transaction

**File:** `ml/serve.py`
**Line:** `129`

`score_payload()` calls:

```python
m = _active()
```

The cached active model is then used to generate the behavioural risk assessment.

The model does not need to be loaded from disk again if it is already cached.

---

## Complete `/score` Call Chain

The complete request flow is:

```text
POST /score
    ↓
service.py:score()
    ↓
service.py:_score_with_model()
    ↓
ml.serve.score_payload()
    ↓
ml.serve._active()
    ↓
ml.serve._Model.__init__()
    ↓
registry.active()
    ↓
registry.model_dir(<active-version>)
    ↓
artifacts/models/<active-version>/
    ↓
Load feature builder + Isolation Forest + Autoencoder + GNN
    ↓
Run ensemble inference
    ↓
Return behavioural risk result
```

## Responsibility of Each File

| Component                     | Responsibility                                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------- |
| `service.py`                  | Handles the `/score` API request and coordinates the behavioural scoring flow.              |
| `ml/serve.py`                 | Loads the active ML model and performs model inference.                                     |
| `registry.py`                 | Determines which model version is currently active and where its artifacts are stored.      |
| `artifacts/models/<version>/` | Contains the actual trained model artifacts and configuration for a specific model version. |

## Important Architecture Note

`service.py` **does not open or load the model files itself**.

It delegates model scoring to `ml/serve.py`.

`ml/serve.py` is therefore the **model-serving layer** responsible for:

1. Finding the active model version.
2. Loading the model artifacts.
3. Caching the loaded model.
4. Running inference.
5. Returning the ensemble result to the behavioural service.

This separation keeps the API/service layer independent from the details of how the ML model is stored and loaded.

```




## Repository layout — what lives where (read this first)

There are **two cooperating parts** in one repo. Files named the same in the root and in `ml/`
(`config.py`, `db.py`) are **not accidental duplicates — they belong to different packages** and
are namespaced (`config` vs `ml.config`). This keeps `ml/` a **self-contained, portable subsystem**
that the service merely imports; it is standard monorepo structure, not a mistake.

```
AI-service/
├── service.py            ← THE PRODUCTION API (Behaviour-Profile Service). POST /score runs the
│                            model in-process, persists bp_decision, sends the webhook.
├── eligibility.py        ← §1/§2/§10 profile-trust gate (NOT AML)
├── config.py  db.py      ← SERVICE config/db (STORE_PG_*/PROD_PG_*, pooled read-write, ensure_schema)
├── audit.py              ← SERVICE audit trail (bp_event_log) + file log (./logs/behaviour.log)
├── retrain.py            ← per-customer statistical PROFILE build/refresh (bp_user_behaviour_profile)
├── sync_manager.py       ← scheduled, read-only ingestion  → bp_transactions_cache
├── webhooks.py webhook_relay.py  ← guaranteed webhook delivery (outbox on bp_decision)
├── deploy.sh watchdog.sh ← operator deploy (promote profiles+model) / crash+failure Slack alerts
│
├── ml/                   ← THE MODEL SUBSYSTEM (training + inference LIBRARY; self-contained)
│   ├── train.py          ← end-to-end training pipeline (ingest→…→ensemble→promote)
│   ├── serve.py          ← score_payload(): the inference the service calls in-process
│   ├── pipeline/ models/ eval/   ← features, GNN/IsoForest/Autoencoder, metrics & plots
│   ├── registry.py       ← model versioning + promote/rollback
│   ├── retrain_trigger.py monitor.py   ← MLOps: §4 retrain triggers, health/drift, Slack alerts
│   ├── codes.py          ← behavioural activity codes + analyst-readable explanations
│   ├── config.py db.py   ← MODEL config/db (BF_*/artifacts; read-only store connection)
│   ├── inference_log.py  ← MODEL compliance log (per-inference JSONL) — NOT the service's audit.py
│   ├── api.py            ← OPTIONAL standalone model API (dev/testing only; port 8085)
│   └── README.md         ← details of the ML subsystem (train it, MLOps, artifacts, env vars)
│
├── Dockerfile            ← builds the PRODUCTION service (service.py + model, CPU)
├── Dockerfile.ml         ← builds the OFFLINE TRAINER (GPU)
├── Dockerfile.api        ← builds the optional standalone model API (dev)
└── docker-compose.yaml   ← wires db + behaviour-profile + sync + trainer (no host-port clashes)
```

**Rule of thumb:** end-to-end **training + MLOps** lives in `ml/`. The **serving** — `/score`,
webhook, writing the behavioural analysis, audit logs — lives in the **Behaviour-Profile Service**
(`service.py`), which loads the model `ml/` produced and runs it **in-process** (CPU).

---

## POST /score — the endpoint your platform calls

```
transaction JSON ──▶ POST /score  (service.py, port 8080)
                        │
                        ├─ 1. build the model payload from the transaction
                        ├─ 2. the MODEL reads the customer's baseline + recent history and scores
                        ├─ 3. DECIDE: safe | review | unsafe  (+ activity code, signals, detectors)
                        ├─ 4. RETURN the decision  (fast — model is warmed at startup)
                        └─ 5. AFTER responding (async): deliver the webhook, persist bp_decision,
                              write the per-inference compliance log
```

There is ONE scoring path — the behavioural ML model. **No AML/rule scoring lives here** (AML is a
separate service). Payload + curl examples: see [`ml/README.md`](ml/README.md).

### How `/score` loads the model — and what a git commit actually deploys

**Loading:** on startup each worker calls `ml.serve._active()` → it reads the **ACTIVE** version from
the **registry** (`artifacts/registry/index.json`), loads that version's files from
`artifacts/models/<version>/` (feature builder, Isolation Forest, Autoencoder, GNN scores,
`tiering.json` = the dynamic zones, ensemble weights, graph features) and **caches** it per worker.
`POST /score` scores with the cached model (no per-request load); `POST /reload` drops the cache so
the next `/score` picks up a newly-promoted model — no restart.

**A git commit deploys the CODE, not the model.** These are versioned and shipped separately:

| Thing | In git? | How it reaches production |
|---|---|---|
| **Code** — `service.py`, the `ml/` pipeline + feature engineering + scoring, Dockerfiles, config | **yes** | your commit → Docker image build → deploy |
| **Trained model** — weights, per-customer baselines, tiering, registry (`artifacts/`) | **no** (git-ignored) | produced by the offline **training job**, shipped via the mounted `artifacts/` volume / object store, promoted in the registry (and by `deploy.sh`) |

So: **committing deploys the serving/training logic for production, but the production model is
whatever is ACTIVE in the registry on the mounted `artifacts/`** — it can be retrained and promoted
without a code change, and rolled back independently.

**The rule that ties them together — feature compatibility.** The code's `FEATURE_VERSION` must
match the active model's `feature_version`. If a commit changes features (e.g. the burst windows →
`feat-2026.08-1`), you must **retrain + promote** a model on that feature version, then deploy the
code — otherwise 27-feature serving code would meet a 22-feature model. `deploy.sh` handles both:
build the image **from the commit** *and* promote the matching model artifacts + registry.

---

## Securing `/score` with an API key (`X-Adhere-Key`)

`POST /score` requires the header **`X-Adhere-Key`** (Swagger shows a 🔒 + "Authorize"). It follows
**standard HTTP auth semantics**:

| Case | Response |
|---|---|
| header missing | **401** Missing API key (+ `WWW-Authenticate` challenge) |
| wrong key | **401** Invalid API key (+ `WWW-Authenticate` challenge) |
| valid key | **200** (scores normally) |
| bad request body (e.g. negative amount) | **422** (FastAPI `HTTPValidationError`, naming the field) |

(401 is used for both missing and invalid credentials — the RFC-7235 standard; **403** is reserved
for "authenticated but not authorized", which `/score` does not use yet.)

**A key is REQUIRED server-side.** If neither `BP_API_KEY` nor an active `bp_api_key` row exists
(and `BP_API_KEY_DISABLED` is not set), the service **fails fast at startup** with a clear error —
it does **not** boot and then return 503 per request. A preflight in the container's launch command
runs the same check, so a misconfigured container **exits cleanly** instead of crash-looping. The
Swagger error responses (401 with `{"detail": …}` + `WWW-Authenticate`, and 422) are documented as
real JSON bodies with Example Values on `/docs`.

**Two ways to set the single active key. Pick ONE — copy-paste, they don't fail:**

**(a) Fixed key in `.env`** (simplest for a single consumer). Generate a strong key and write it into
`.env` (works whether or not `BP_API_KEY=` is already there):

```bash
KEY=$(openssl rand -hex 32)
grep -q '^BP_API_KEY=' .env && sed -i "s|^BP_API_KEY=.*|BP_API_KEY=$KEY|" .env || echo "BP_API_KEY=$KEY" >> .env
echo "Your X-Adhere-Key: $KEY"      # store it — send it as  -H \"X-Adhere-Key: $KEY\"
docker compose up -d --force-recreate behaviour-profile     # picks up the new key
```

This value (hashed) is THE active key and **overrides** the DB. `python -c "import secrets;print(secrets.token_hex(32))"` works too if `openssl` isn't installed.

**(b) Rotating key in the DB** (leave `BP_API_KEY` empty). Keys are managed by `manage_api_key.py`;
only the **SHA-256 hash** is stored (`bp_api_key`), the plaintext is shown **once**, and exactly one
key is active — rotating invalidates the former. **Generate it with the store up but BEFORE (or
after) the API is running** — use `docker compose run` so there is no "needs a key to start" deadlock:

```bash
docker compose up -d db                                                   # the store must be up
docker compose run --rm behaviour-profile python manage_api_key.py rotate --label "adhere $(date +%F)"
docker compose up -d behaviour-profile                                    # now it boots (a key exists)
# already running? rotate live instead, then reload:
#   docker exec adhere-behaviour python manage_api_key.py rotate && curl -X POST localhost:8080/reload
```

Then call it:
```bash
curl -X POST http://localhost:8080/score -H "X-Adhere-Key: <the-key-shown-once>" \
  -H 'Content-Type: application/json' -d @txn.json
```

`docker compose run --rm behaviour-profile python manage_api_key.py show` lists keys (metadata only).
A rotated DB key is picked up automatically within `BP_API_KEY_CACHE_TTL` (~30s) even without `/reload`.
`BP_API_KEY_DISABLED=1` turns auth off (internal/dev only). In **production, `deploy.sh` generates a
key automatically** if none exists and prints it once — so a deploy never blocks on this.

**Input validation** (returns **422** with a clear message): `amount` must be **> 0**; `currency`
must be a **3-letter ISO-4217** code; `transaction_type` ∈ `{transfer, ussd, web, card}`;
`account_type` ∈ `{individual, corporate}`; `customer_email` must be a valid email; required fields
enforced.

### Optional geo-velocity — data requirement (disclaimer)

> **Geo-velocity data requirement:** Geo-velocity is an **optional** behavioural signal and is only
> available when the client provides a valid representation of the customer's **actual** location for
> the transaction. Where supported, provide accurate customer **latitude** and **longitude**
> (`additional_info.latitude` / `additional_info.longitude`, WGS84) in the documented format. If
> location data is missing, malformed, invalid, or represents an **agent/terminal** location rather
> than the customer's actual location, geo-velocity may not be calculated and the system should not be
> expected to produce a geographic anomaly flag. **Missing or invalid location data will not cause the
> transaction to fail and will not affect the existing behavioural fraud decision.**

The client is responsible for supplying accurate customer-location data if they want geo-velocity to be
available; it is **never required and never enforced** — omitting it is a normal, fully-supported case.
Geo is currently an internal **shadow** signal (telemetry only) and does **not** influence the `/score`
decision or the 27-feature model.

**`/score` behaviour for IP and coordinate inputs** (every case returns **200** and the fraud decision
is **unchanged** — geo/IP never fail the request and geo never changes the decision):

| Input | Result | Effect on the decision |
|---|---|---|
| `ip_address` — valid **or** malformed **or** private | **200**, scored normally | none beyond the existing `ip_new` novelty feature (a bad IP makes it neutral). Geo does not use the IP (no local IP→coord resolver). |
| `latitude`/`longitude` — **valid** | **200**, scored normally | none — used only for the **shadow** geo telemetry; the decision is identical with or without them. |
| `latitude`/`longitude` — **missing / malformed / out-of-range** | **200**, scored normally | none — the coordinate is **ignored** (geo simply unavailable). It is **never** rejected with a 4xx and never affects the decision. |

The behavioural fraud decision comes **only** from the 27-feature model (amount, velocity, novelty such
as `new_location`/`ip_new`, timing, graph). A transaction can be `review`/`unsafe` purely from those
signals regardless of any geo/IP input.

**Sending coordinates (client-side):** `latitude`/`longitude` are two **optional** keys inside the
existing `additional_info` object — the rest of the payload is unchanged:
```json
"additional_info": { "ip_address": "...", "location": "Lagos, Nigeria",
                     "latitude": 6.5244, "longitude": 3.3792 }
```
If omitted, geo falls back to the `location` string (via the local registry) or is unavailable; if
present, they take priority. Either way the decision is unchanged.

**Deployment note — the geo location registry ships in the code image (no extra step).** Location
resolution uses a first-party, deterministic dataset, **`ml/data/ng_locations.json`** (36 Nigerian
states + major cities; public geo facts, no PII), loaded once at startup by `ml/geo_registry.py`. It is
a **required runtime file** and is **baked into the serving image** by the Dockerfile (`COPY . .`; it is
tracked in git and not in `.dockerignore`). It is **not** shipped by `deploy.sh` (which only moves the
model + store out-of-band) — like any source file, it travels with the code/image. If the file were
missing the registry would simply be empty (location resolution disabled) — geo would degrade to
unavailable and `/score` would still work; it is never a hard dependency.

---

## Production topology & containers (no clashes)

Use **`docker-compose.yml`** — services reach the database **by service name on the internal
network**, so they never fight over a host port. The `:5433` collision seen in local dev came from
**two unrelated projects both publishing host port 5433**; production avoids it entirely by not
depending on host ports for service-to-service traffic.

| Compose service | From | Role | Runs | GPU |
|---|---|---|---|---|
| **db** (`behaviour-profile-db`) | `postgres:17` | the store | always-on | no |
| **behaviour-profile** (`adhere-behaviour`) | `Dockerfile` | production API, model in-process | always-on (replicas OK) | no |
| **sync** (`adhere-behaviour-sync`) | `Dockerfile` | scheduled read-only ingestion (`sync_manager --loop`) | always-on, **single instance** | no |
| **trainer** (`profile: train`) | `Dockerfile.ml` | offline training job | on schedule / trigger, then exits | **yes** |
| **model-api / loadtest** | `Dockerfile.api` / locust | optional standalone model API / load test | dev only (`profile: dev`/`loadtest`) | no |

- **Replicas:** `behaviour-profile` is stateless (model + config only) → scale it horizontally
  behind a load balancer; all replicas share the read-only store and the `./artifacts` mount.
- **Keep `sync` at ONE instance** so production has exactly one reader (in k8s it's a CronJob).
- **The standalone model API is NOT production** — the model runs inside `behaviour-profile`;
  running both would be a redundant second model server. It's dev-only.
- **One store per environment.** Two Postgres containers on the same host port is a local-dev
  accident; the app reaches the DB as `db:5432` on the internal network, never a shared host port.

```bash
docker compose up -d --build                                # db + redis + behaviour-profile + sync
docker compose --profile train run --rm trainer --promote   # train (GPU), promote, exit
```

### Two operating modes (don't conflate them)

| Mode | Who runs it | Command | GPU |
|---|---|---|---|
| **Serve `/score`** (production) | always-on `behaviour-profile` | `docker compose up -d` → `POST :8080/score` | no |
| **Train / test / MLOps** | offline job on the training host | `docker compose --profile train run --rm trainer --promote`; then `python -m ml.retrain_trigger`, `python -m ml.monitor --live` | yes (train only) |

The **model runs in-process** in the serving service; **training and monitoring never run inside
it**. They are separate ML-side jobs that hand the model over through the **registry**, and the
service picks up a newly-promoted model with `curl -X POST :8080/reload`. The full loop
(monitor → Slack alert → retrain → acceptance gate → promote → reload → rollback) and the
**production runbook** are documented in **[`ml/README.md` → "The MLOps loop"](ml/README.md)**.

### Keeping per-customer behaviour fresh (the pull → retrain loop)

A customer's learned behaviour lives in the **model's baselines**, which are rebuilt on every
training run — so the way the system "updates when a customer's behaviour changes" is the
**batch retrain** (the correct MLOps pattern: batch re-learn, not per-request mutation). The flow:

1. **04:00 daily** — the `sync` service pulls the delta into `bp_transactions_cache`
   (`BP_SYNC_AT_HOUR=4`, the only production reader). **Automatic.**
2. **Right after** — the `sync` service evaluates the §4 triggers (`≥100 new txns OR ≥30 days OR
   amount-drift PSI ≥ 0.25`) and, if any fired, **Slacks "retrain DUE"**. **Automatic (alert only).**
3. **You retrain when ready** (manual, GPU) — retrains + promotes **only if it beats the active
   model**, then `curl -X POST /reload`. See **"Manual model retraining"** just below for the exact
   commands. *(Automating step 3 later = uncomment `deploy/crontab.example`; no k8s required.)*

**Freshness lever:** the retrain **thresholds** (`BF_RETRAIN_MIN_NEW`, `BF_RETRAIN_MAX_DAYS`,
`BF_DRIFT_PSI`) decide when you're alerted. For high-risk banking (PDF §3) retrain **daily**; lower
the thresholds to be alerted sooner. The live monitor (`ml.monitor --live`) is the safety net — it
watches the decision mix and Slacks you if behaviour drifts between retrains.

#### Retraining today: MANUAL, with automatic "when to retrain" alerts

**Decision:** for now, retraining is **manual** — we are **not** on a schedule and there is **no
Kubernetes**. Automatic retraining is deliberately **not wired in yet** (to keep things simple and
because a retrain is a GPU job we want to run deliberately). What *is* automatic is the
**notification**: the always-on `sync` service, after every pull, checks the §4 retrain triggers
**and** the live decision-drift, and **Slacks you when either warrants attention**. You then retrain
by hand whenever you choose.

| Piece | Automatic? | How |
|---|---|---|
| Pull prod → cache | ✅ yes | `sync` service, 04:00 daily (`BP_SYNC_AT_HOUR`) |
| **Detect §4 retrain triggers + Slack "retrain DUE"** | ✅ yes (alert-only) | `sync` calls `retrain_trigger.evaluate()` after each pull |
| **Live drift/health watch + Slack** (§9) | ✅ yes (alert-only) | `sync` calls `monitor.check_live()` after each pull |
| **Retrain the model** (GPU) | ❌ **no — manual** | you run it (see **Manual model retraining** below) |
| Roll the new model into serving | ❌ **no — manual** | `curl -X POST /reload` after a retrain |

**Alerts are sent ONCE, not on every pull.** Both checks are **edge-triggered / de-duplicated**: the
last announced state is stored in `bp_alert_state`, so a *standing* condition is Slacked once and
stays quiet until it changes (and re-fires if it clears then returns). `deploy.sh` also reports
"retrain due" but **only to the deploy console — it does not Slack**, so the `sync` service is the
single source of the Slack message and deploy + sync never double-post.

The **retrain execution** automation is written but **left disabled** — `deploy/crontab.example`
(host cron driving docker compose; **no k8s needed**) and `deploy/retrain-cronjob.yaml` (future k8s),
both **commented out**. The hourly `ml.monitor --live` cron there is now redundant with the in-`sync`
watch above — leave it off unless you want finer-grained (sub-daily) drift checks. Until you automate
retraining, you serve the current model and simply get alerted, once, when a refresh is warranted.

##### Manual model retraining (how, and how the service picks up the new model)

```bash
# 1) Retrain on the GPU host. This is SELF-GATING: it retrains and promotes the new model to
#    "active" ONLY IF it is at least as good as the current one (synthetic-anomaly AUC / precision).
#    If it is worse, it is NOT promoted and the CURRENT model stays — nothing changes.
docker compose --profile train run --rm --entrypoint python trainer -m ml.retrain_trigger --run
#    (force a retrain regardless of triggers with:  … trainer -m ml.train --promote)

# 2) Roll the promoted model into the live service with no downtime (it reloads the ACTIVE model
#    from the registry). If nothing was promoted, this is a harmless no-op.
curl -X POST http://localhost:8080/reload
```

**How the "right" model reaches the behaviour service:** the service loads whatever the **registry**
(`artifacts/registry/index.json`) marks **active** (`ml/serve.py._active()`), from the shared
`artifacts/` volume. A retrain writes a **new** version but only flips "active" to it **through the
acceptance gate** ([`ml/train.py`](ml/train.py) — promote only if `synthetic-AUC ≥ active`, else it
logs *"NOT promoting"*). So the service can never be pushed a worse model. `/reload` (or a restart)
is what makes a newly-promoted model take effect; without a promotion, the service keeps serving the
current one. To revert: `python -c "from ml import registry; registry.rollback()"` then `/reload`.

#### Is drift detected when data is pulled, or only at `/score`?

**Both — and, importantly, drift is detected on the pulled DATA, not just at inference.** After **every**
sync pull, the `sync` service runs two independent checks ([`sync_manager.py`](sync_manager.py), the
`_check_retrain_due` + `_check_live_drift` calls):

1. **Data-side drift → "retrain DUE"** ([`ml/retrain_trigger.py`](ml/retrain_trigger.py), the §4 trigger).
   It measures the freshly-pulled **cache** against the active model's training **watermark** and fires
   if **any** of: new transactions since training ≥ `BF_RETRAIN_MIN_NEW` (100); **or** days elapsed ≥
   `BF_RETRAIN_MAX_DAYS` (30); **or** **amount-distribution drift (PSI)** ≥ `BF_DRIFT_PSI` (0.25) — a
   real distribution-shift signal comparing the new data against the training reference. If it trips,
   the sync Slacks *"retrain DUE — &lt;reasons&gt;"* (de-duplicated via `bp_alert_state`). **So a pull
   can trigger a drift/retrain signal on its own.**
2. **Live-side drift → `monitor.check_live()`** (§9/§11). Reads the `/score` decisions in `bp_decision`
   (the flagged-rate band) **plus** the real precision/recall from the analyst-feedback loop once labels
   exist (see below).

Two clarifications:
- **`/score` itself computes no drift or performance** — it only *writes* each decision to `bp_decision`
  (the old per-request counter was removed). The live check reads those decisions **later**, inside the
  `sync` service — never on the hot request path.
- **The model does not auto-retrain.** These signals **alert humans**; retraining is **manual + gated**
  (`ml.retrain_trigger --run` retrains and promotes *only if it beats the active model*). The automation
  (cron / k8s CronJob) is written but intentionally disabled.

So drift monitoring is **two-sided**: the *pulled data* (PSI / new-data → retrain-due) **and** the
*accumulated `/score` decisions* (flag-rate + precision). The next two sections detail each side.

#### Does `/score` traffic trigger a "model is degrading → retrain" Slack alert?

**Yes — implemented, with one honest caveat.** As `/score` runs, every decision is written to
`bp_decision`. The always-on `sync` service, after each pull, calls `monitor.check_live()` (see
[`sync_manager.py`](sync_manager.py) `_check_live_drift`), which reads those decisions over the last
`BF_LIVE_WINDOW_HOURS` (24h) and computes the **flagged rate** (review+unsafe ÷ total). If the mix
drifts out of its healthy band it **Slacks you — once**, de-duplicated via `bp_alert_state`:

| Live signal (from `/score` decisions) | Meaning | Alert |
|---|---|---|
| flagged rate **> 30%** (`BF_LIVE_FLAG_MAX`) | **over-flagging** — data/behaviour drift or miscalibration | 🚨 *"Behavioural model looks UNHEALTHY in production (flag rate …) — likely drift or a stale model — consider a manual retrain."* |
| flagged rate **< 2%** (`BF_LIVE_FLAG_MIN`) | **under-flagging** — the model may be stale | same alert |
| fewer than `BF_LIVE_MIN_SAMPLE` (200) decisions / 24h | not enough traffic to judge | **silent** (no false alarm) |

Separately, the §4 **retrain-due** check (`retrain_trigger`) also Slacks *"retrain DUE"* on new-data /
age / amount-drift. Both alerts **urge a manual retrain** (retraining stays manual + gated for now).

**The caveat, now closed by the analyst-feedback loop:** the flag-rate signal alone is **drift
detection** — *is the rate of flags outside its normal band?* — a **proxy** for degradation, not true
precision/recall. As of the analyst-feedback loop (below), **real precision/recall is now folded into
the same `check_live` path**: once at least `BF_LIVE_MIN_LABELS` (20) analyst verdicts exist,
`monitor.live_precision()` joins each transaction's latest decision to the analyst's confirmed verdict
and reports **true precision** (flagged decisions confirmed *fraud* vs *genuine*) and recall; precision
below `BF_MIN_PRECISION` then raises the same Slack alert. Until that many verdicts accumulate, the
flag-rate proxy carries the signal. It runs **periodically** (after each pull), over the *accumulated*
`/score` decisions — not on every single request.

#### The analyst-feedback loop — `POST /feedback` (closes Practical Rules §11)

When the fraud-analyst team reviews a decision the model made (a `review`/`unsafe`, or an auto-block)
and confirms the ground truth, they record it:

```bash
curl -X POST http://localhost:8080/feedback -H "X-Adhere-Key: $BP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"transaction_id":"68665786","verdict":"genuine","analyst":"anita","note":"customer confirmed"}'
```

`verdict` is **`genuine`** (a legitimate transaction — a false positive if we flagged it) or **`fraud`**
(confirmed fraud). The verdict is stored in **`bp_decision_feedback`**, keyed by `transaction_id`
(re-submitting updates it; the service links it to the model's own `entity_key`/`decision` from
`bp_decision`). It drives two things:

- **Retraining** — at the next retrain (`ml.pipeline.clean`), a `genuine` verdict **forces** that
  transaction into the CLEAN training set (so the model learns it as normal even if the source status
  looked suspicious); a `fraud` verdict **forces it out** (§7 "never learn confirmed fraud"). The
  unsupervised detectors then re-learn "normal" with the corrected population — this is how a repeated
  false positive stops firing.
- **Live precision** — `ml.monitor --live` reports the **real** precision/recall above.

The model is **unsupervised**, so verdicts don't train a classifier directly; they correct the *clean
vs excluded* population the baselines and detectors are learned from, and they supply the ground truth
for measuring accuracy. That's the mechanism behind the intuition "we take the analyst's answer and
update the model with retraining."

> **Note on the per-request update:** `/score` does **not** touch `bp_user_behaviour_profile`. An
> earlier per-request counter-bump + `retrain.maybe_retrain` was **removed** — it was a no-op
> (`/score` keys on the **identifier**; those profiles are keyed on `branch:account`) *and* a
> per-request profile mutation is what §8 (Behaviour Stability) says not to do. The **authoritative**
> re-learning is the batch retrain above (cache-based, identifier-keyed).

### What `/score` updates when it decides (and what it does NOT)

When `/score` returns a decision it **records** the decision + its audit trail; it **does not
recompute or change the customer's learned behaviour**. There is **no per-request update of any
average / mean / median / max / usual-hours** — the model's per-customer baseline is **read-only at
inference** (`ml/serve.py` has no `INSERT/UPDATE/.fit`).

| Written on every `/score` | What it is |
|---|---|
| `bp_decision` | the decision record (status, activity_code, risk, zone, webhook_status, latency) + the webhook outbox marker |
| `bp_event_log` | an accountability event (`audit.log_event`) |
| `artifacts/inference_log/*.jsonl` | the immutable per-inference compliance record |

**What is NOT touched:** the learned baseline (mean/median/max/usual patterns/known beneficiaries)
the model compares against. This is deliberate — **Practical Rules §8 "Behaviour Stability"**: *do
not shift a profile because of one event; require repeated evidence.* A customer's learned
behaviour changes **only at the offline batch retrain** (`ml.train`), which re-derives every
customer's baseline from fresh data and fires on new-data / age / drift (§4 — the pull → retrain
loop above). (The former per-request `UPDATE bp_user_behaviour_profile` counter-bump has been
**removed** — it changed no decision and conflicted with §8.)

> **In one line:** `/score` **reads** the learned profile to decide and **writes** an audit record;
> it never re-learns the customer. Re-learning is the scheduled batch retrain, not per transaction.

---

## Quick start (local)

```bash
cp .env.example .env    # never commit .env
```
**Set these two before the first start, or the stack will not boot** (both are fail-fast on purpose):
- `STORE_PG_PASSWORD` — the behaviour-store password (compose refuses to start without it).
- An API key for `/score` — the API deliberately refuses to start if none is set. Generate one:
  `KEY=$(openssl rand -hex 32); grep -q '^BP_API_KEY=' .env && sed -i "s|^BP_API_KEY=.*|BP_API_KEY=$KEY|" .env || echo "BP_API_KEY=$KEY" >> .env` —
  or set `BP_API_KEY_DISABLED=1` for local dev only. Full options (incl. the rotating DB key) in
  **"Securing `/score` with an API key"** below. In production `deploy.sh` generates one automatically.

```bash
docker compose up -d --build       # builds + starts ALL FOUR services (see table)
curl -s localhost:8080/health      # -> {"status":"ok", ...}
# score a transaction (Txn payload) — see ml/README.md for the full body / demo/ for a runnable example
```

**The running stack is four services — `docker compose up` starts them all; none is optional except
`sync` (prod-only ingestion). Redis is required — it backs live velocity:**

| service | container | what it is | needed to serve `/score`? |
|---|---|---|---|
| `db` | `behaviour-profile-db` | PostgreSQL behaviour store (learnt profiles + transaction cache) | **yes** |
| `redis` | `adhere-redis` | live-velocity window (real-time burst features); **fail-safe** — if it's down `/score` still works on the batch cache | recommended (velocity degrades without it) |
| `behaviour-profile` | `adhere-behaviour` | the API — `POST /score`, model in-process | **yes** |
| `sync` | `adhere-behaviour-sync` | the scheduled production pull (the only prod reader) + webhook relay | prod only (idles in dev) |

If you prefer to name them explicitly (identical result):
```bash
docker compose up -d --build db redis behaviour-profile sync
```

Train / retrain / MLOps (triggers, drift, health, Slack), artifacts, and env vars are documented
in **[`ml/README.md`](ml/README.md)**. Production bring-up (learnt state + model, out-of-band) is in
**[`DEPLOYMENT.md`](DEPLOYMENT.md)**.

## Start / stop the service (webhook-safe)

**START** (dev, full stack, webhooks firing):
```bash
cd /home/adesoji/AI-service
docker compose up -d db redis behaviour-profile sync
curl -s localhost:8080/health
```

**Before you stop — drain check** (confirm nothing is stuck in the outbox):
```bash
docker exec behaviour-profile-db psql -U behaviour -d behaviour -tAc \
  "SELECT webhook_status, count(*) FROM bp_decision GROUP BY 1 ORDER BY 2 DESC"
# optional: force a final sweep to flush anything due right now
docker compose run --rm -T behaviour-profile python webhook_relay.py --once
```

**STOP** (graceful — keeps data *and* pending webhooks; never loses one):
```bash
docker compose stop        # SIGTERM to all; containers + volumes preserved
```

**RESUME** (the relay auto-redelivers any pending webhooks on restart):
```bash
docker compose start
```

**Why no webhook is lost across a stop:** each decision writes `webhook_status='pending'` in the
**same DB commit** as the decision (Postgres `pgdata` volume, untouched by `stop`). `docker compose
stop` is graceful; anything undelivered stays `pending` and the relay (in the `sync` container)
redelivers it with backoff on `start` — at-least-once delivery. **Never** run `docker compose down
-v` — that deletes the store volume (and the pending outbox with it).

## Demo runner — replay real transactions through `/score`

[`demo/demo_runner.py`](demo/demo_runner.py) streams real transactions from a CSV and sends them
**sequentially to the existing `/score`** (the actual production path — it does **not** duplicate
scoring or fabricate any feature; it sends raw fields, the service computes everything). It records
each response (`status`, `activity_code`, `zone`, `risk_score`, `description`, `triggered_signals`,
`detection_reason`, HTTP status, latency) to `demo/demo_run/demo_results.csv`, and prints a summary
(counts, decisions, latency p50/p95, errors). Because the CSV is customer-grouped, repeats exercise
the **Redis live-velocity** feature. It is an *initial stability indication, not a formal load test*.

> **Webhooks:** the runner hits `/score`, which fires the configured webhook. For a demo, either
> leave `BP_SCORE_WEBHOOK_URL` empty in `.env` (no webhook) or disable it for the run and restore
> after (shown below). The runner uses **only the Python standard library** — no virtualenv / pip.

**Option A — run on the host** (simplest; includes live `docker stats` monitoring):
```bash
cd /home/adesoji/AI-service
docker compose up -d db redis behaviour-profile sync
KEY=$(grep '^BP_API_KEY=' .env | cut -d= -f2)

# webhook-safe: disable for the run, restore after (skip if BP_SCORE_WEBHOOK_URL is already empty)
BP_SCORE_WEBHOOK_URL='' docker compose up -d --force-recreate behaviour-profile sync
python3 demo/demo_runner.py --limit 1000 --key "$KEY"
docker compose up -d --force-recreate behaviour-profile sync     # restore webhooks
```

**Option B — run fully inside Docker** (no host Python needed):
```bash
cd /home/adesoji/AI-service
docker compose up -d db redis behaviour-profile sync
# (webhook-safe: BP_SCORE_WEBHOOK_URL='' docker compose up -d --force-recreate behaviour-profile sync)

docker compose run --rm --no-deps \
  -v /home/adesoji/adhere-backend/behaviour_profile_build/data:/data:ro \
  -v "$PWD/demo:/app/demo" \
  behaviour-profile \
  python demo/demo_runner.py \
    --csv /data/transactions.csv \
    --url http://adhere-behaviour:8080/score \
    --limit 1000 --no-monitor
# watch stability in another terminal:  docker stats adhere-behaviour adhere-redis behaviour-profile-db
```
Notes: mounts the CSV (read-only) + `demo/` (so results land back on the host and it uses your latest
runner — **no rebuild**); targets the **API container** `adhere-behaviour` (`localhost` won't resolve
inside a container); `--no-monitor` because a container has no `docker` CLI. Each run **overwrites**
`demo/demo_run/`; pass `--outdir demo/runs/$(date +%Y%m%d_%H%M%S)` to keep runs separate. Outputs are
git-ignored (`*.csv`, contain PII). Swagger stays available at `http://localhost:8080/docs`.

## Reviewing one decision end-to-end — the `demo/` kit + Sanity Audit

To prove a decision is **grounded in learned history vs the live payload** (not guessing), use the
review kit in **[`demo/`](demo/) — full instructions in [`demo/README.md`](demo/README.md)**:

| File | Purpose |
|---|---|
| `payload.json` | the transaction you edit (amount, beneficiary, location, type, …) — **git-ignored** (real id = PII) |
| `run_audit.sh` | one command: clears the customer's velocity window, prints the **Sanity Audit**, then calls the live `/score` — the two decisions must **match** |
| `decision_audit.py` | the learned-vs-real-time audit logic |
| `bf_test.py` / `bf_category_tests.txt` | broader BF-category probes + observed results |

```bash
nano demo/payload.json          # edit the transaction
./demo/run_audit.sh             # audit + live /score (key auto-read from .env)
```

**Model Decision Sanity Audit** (`demo/decision_audit.py`) reproduces the exact `/score` path
(features → detectors → escalate blend → tiering) and prints, side by side: the **learned baseline**
(known beneficiaries/locations/IPs/types + amount stats) → the **real-time payload** → a
**per-dimension comparison** (each signal) → detectors/blend/thresholds → decision → why. Run it on
any payload — nothing hardcoded, piped in:
```bash
docker exec -i adhere-behaviour python demo/decision_audit.py < payload.json
# or:  docker exec adhere-behaviour python demo/decision_audit.py --payload /app/demo/payload.json
```
It makes the "learned vs live" reasoning explicit — e.g. amount `0.05×` the median (in-range) but the
beneficiary **not** in the 19 learned ones → NEW → so the decision is grounded, not a guess. The audit
decision and the live `/score` decision **match** — that's the proof point.

> **Testing tip:** `run_audit.sh` clears the velocity window automatically; if you hit `/score`
> directly (Postman), run `docker exec adhere-redis redis-cli DEL vel:<identifier>` yourself between
> probes so accumulated velocity doesn't skew the result. To test *amount* behaviour cleanly, edit
> `payload.json` to the customer's **real** attributes (a real beneficiary, real IP subnet, real type)
> so novelty flags don't fire.

### Recent-activity explainability (no rule — the model still decides)
When the velocity features are non-zero but **below** the `velocity_burst` threshold they still move
the detectors. To keep that visible, the description adds *"recent transaction activity that raised the
risk (below the burst threshold)"*, and a **`recent_activity`** signal appears in `triggered_signals` /
`detection_reason` on flagged transactions. This is **explainability only** — there is **no** hard rule
forcing review/unsafe from raw `vel_1m`/`vel_24h` (that would reintroduce a rule engine and break the
single behavioural-model path, §8). The risk already absorbs velocity via the detectors, and **BF-305**
remains the code when velocity is the *dominant* unsafe signal.

## How the three detectors decide together — the escalate blend ("consensus of two")

`/score` runs three unsupervised detectors that each vote in `[0,1]` on their own specialty —
**Isolation Forest** (density/amount outlierness), **Autoencoder** (whole-vector reconstruction error),
**GNN** (counterparty graph structure, *blind to amount*) — then combines them:

```
risk = max( weighted_mean(gnn, iso, ae) , 2nd-highest detector score )     # BF_BLEND_MODE=escalate
```

The **2nd-highest** score is the key: it is high **only when at least two detectors agree**. So a
strongly-agreeing **majority (≥2 of 3) escalates**, while a lone detector can neither **veto** the
majority (the old weighted-mean bug, where a quiet GNN diluted IsoForest+Autoencoder) nor **force** a
flag by itself:

| Detectors screaming | example scores (gnn/iso/ae) | mean → **escalate** | decision |
|---|---|---|---|
| all quiet | 0.20 / 0.30 / 0.20 | 0.24 → 0.24 | safe |
| one only (AE) | 0.20 / 0.30 / 0.999 | 0.52 → 0.52 | safe (a lone detector can't flag) |
| **two agree (IF+AE), big amount** | 0.03 / 0.99 / 0.999 | 0.71 → **0.99** | **unsafe** ✅ (no dilution) |
| all three | 0.90 / 0.95 / 0.99 | 0.95 → 0.95 | unsafe |
| GNN alone | 0.99 / 0.30 / 0.20 | 0.47 → 0.47 | safe |

The weighted mean is still the **floor** (weighting matters for borderline cases); the 2nd-highest only
*lifts* the risk when a majority actually agrees. `confidence` is computed from the detector **spread**,
so it honestly reports low (~0.25–0.6) when they disagree — the *decision* follows the majority while
`confidence` shows it wasn't unanimous. Modes are `BF_BLEND_MODE` (`escalate` default · `mean` legacy ·
`max` · `noisy_or`); **changing it requires a retrain** (the p95/p99 tiering cuts recalibrate to the new
distribution). *Caveat:* a pure single-detector signal (e.g. a graph ring only the GNN sees, normal
amount) still won't escalate alone — same as the old mean blend; raising `BF_W_GNN` or adding a
graph-specific escalation is a separate, deliberate tuning.

## Is the model working? — reading the demo results (cold-start vs coverage)

**The model IS working correctly — here's the proof.** Scoring an **established** customer
(`21200336604`, median ₦3M, 205 txns, in the model → `cold_start=False`):

- **₦3,000,000** (their normal high amount) → **`safe` / BF-100** — *"generally consistent with the
  customer's behavioural profile."* ✅ A high-value transaction marked safe because the model knows
  their personal norm.
- **₦5,000** (tiny vs their ₦3M median) → **`safe`** — correct: spending *less* than usual isn't
  fraud-suspicious.

**Why the demo showed lots of `unsafe`: it's cold-start, definitively.** The demo breakdown is stark:

| Group | Decisions | Outcome |
|---|---|---|
| **Established** (model knows them) | 635 | **100% `safe`** — 0 review, 0 unsafe |
| **Cold-start** (not in the model) | 358 | 206 review + **152 unsafe** — 0 safe |

**Every single `unsafe` was a cold-start customer. Every single established customer was `safe`.** So
the unsafe rate is entirely a cold-start effect, not a model flaw.

**Why so many cold-start? Coverage:**
- The active model has personal baselines for **5,734 customers**.
- The cache has **32,895 distinct customers**.
- → only ~**17%** have a learned profile; ~**83%** are cold-start.

A cold-start customer is judged against the **population baseline** (median ~thousands), so someone
whose *personal* norm is millions looks wildly anomalous — e.g. two high-value customers who happen to
be cold-start (`22146110541` ₦5M, `22174414458` ₦20M) both came back `unsafe` ("90×" / "3,xxx×" the
population). That's the model behaving **correctly** given it has no personal history for them — but it
is a false alarm for a legitimate customer.

**So: is the model "excellent"?** For customers it knows — **yes, flawless** in this sample (635/635
safe on normal behaviour, including large amounts). The behaviour ("₦1M → safe") *does* happen — just
only for the ~17% of customers who have a personal profile.

**The real limitation is coverage, not logic.** ~83% of customers are cold-start, and cold-start uses
the whole-population baseline, which over-flags legitimate high-value customers. Two levers:

1. **Retrain with broader eligibility / more history** — as more customers cross the training
   threshold (currently `BF_MIN_TXNS` / `BF_MIN_DAYS_ACTIVE`, clean-only), they graduate out of
   cold-start and their high amounts become correctly safe. This is the main fix and exactly what the
   retrain pipeline is for.
2. **Peer-based cold-start** (future enhancement): judge a new customer against **similar** customers
   (same branch/segment) instead of the entire population, so a high-value new customer isn't compared
   to the population median. The statistical layer has a peer-baseline concept, but the **model's**
   cold-start currently uses the global population baseline — a worthwhile improvement to reduce
   cold-start false positives. 
   
   To expantiate further on this :  The Proposed Fix: "Peer-Based Cold-Start"Instead of comparing a new user to everyone on the platform, Adesoji wants the model to compare them to their immediate peers (people in the same branch, tier, or business segment) right from day one but  i need Anita's call on this to approve,if this is logical?
   
   How it will work: When that premium customer joins, the system checks their account metadata (e.g., "Corporate Segment" or "Premium Branch").It then evaluates their ₦500,000 transaction  for example against the average behavior of other Corporate/Premium customers (where a ₦500,000 transfer is completely normal), rather than a student or retail user baseline.
   
   Why This Is Important For US
   
   It Reduces False Positives: This change will directly lower that high 36% flag rate our demo showed. High-value customers won't get locked out or delayed on their very first transaction.
   
   Better Customer Onboarding: First impressions matter. Stopping a legitimate user's first transaction causes immediate churn. This fix creates a smoother onboarding experience for high-value users.
   
   The Foundation Exists: Adesoji noted that the database/statistical layer already understands the concept of peers. The machine learning model just hasn't been plugged into it yet.

**Bottom line:** the model is sound — it correctly marks high *normal* amounts safe for customers it
has learned, and small amounts safe for everyone. The high unsafe count in the demo is the **cold-start
/ coverage gap** (~17% coverage), which retraining directly addresses. Nothing here indicates a broken
model; it indicates the model needs **more customers trained into it**.

## Start the service for a demo (no fresh data pull — just show /score)

The store already holds the learnt profiles + the promoted model, so bring up **only** the store
and the API (skip the ingestion `sync` service):

```bash
# .env must have STORE_PG_PASSWORD; set the webhook so deliveries are visible
export BP_SCORE_WEBHOOK_URL='https://webhook.site/<your-id>'
docker compose up -d --build db behaviour-profile
curl -s localhost:8080/health

# score a transaction — the model decides; the webhook fires; everything is logged
curl -s -X POST localhost:8080/score -H 'Content-Type: application/json' -d '{
  "branch_id":231,"origin_account_no":"5510027882","identifier":"22598330040",
  "amount":7500000,"currency":"NGN","transaction_type":"transfer",
  "destination_account_no":"NEW-MULE-99","origin_country":"Nigeria","destination_country":"KP",
  "customer_location":"Kano","transaction_id":"TXN-DEMO-1"}'
```

What to show: the JSON decision (`status`/`activity_code`/`risk_score`/`triggered_signals`); the
webhook arriving at webhook.site; `bp_decision` (`webhook_status=sent`); the ML compliance log
(`artifacts/inference_log/*.jsonl`); and the **plain-text audit log** the container writes to
`./logs/behaviour.log` (`docker compose logs -f behaviour-profile` shows the same live).

## Deploy to production + operate

**Step-by-step runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md)** (what the operator does, in order).

- **`./deploy.sh`** — operator-run. Confirms the server IP (prompts on a TTY, else
  `PROD_IP_ALLOWLISTED=yes`), **verifies the production Postgres connection directly**, checks the
  daily-pull schedule is set, promotes the **learnt state** into the production behaviour store —
  the learnt profiles + peer baselines **and** the transaction cache + sync watermark, so **prod
  continues from the current behaviour and the 4am pull only adds the delta** — **unpacks the
  out-of-band model bundle** if present (step 3b), promotes the validated **model artifacts +
  registry**, then checks `/health`. Logs to `./logs/deploy_*.log`; reports to Slack. All data moves
  **out-of-band — nothing customer-derived is ever committed to git.**
- **Nothing to forget before a deploy** — `./prepare_release.sh` builds **both** out-of-band bundles in
  one step: `model-bundle.tar.gz` (active + previous model) and `store-bundle.tar.gz` (learnt state).
  Neither can live in git (customer data; the store dump also exceeds GitHub's 100 MB limit).
- **Three ways they reach production** (pick what your network allows; deploy.sh auto-detects):
  (1) **deploy from a host that reaches the store** → nothing pre-built (DB→DB + local `artifacts/`);
  (2) **private bucket** → set `MODEL_BUNDLE_URL`/`STORE_BUNDLE_URL`; `prepare_release.sh` uploads,
  `deploy.sh` fetches them if missing — zero manual copying;
  (3) **manual (no bucket)** → `scp` the two bundles next to `deploy.sh`. **You can't forget how:** if a
  bundle is missing and no `*_BUNDLE_URL` is set, deploy.sh **prints the exact commands** — `./prepare_release.sh`
  then a ready-to-paste `scp [-i <key>] model-bundle.tar.gz store-bundle.tar.gz <user>@<server-ip>:<path>/`
  (fill `DEPLOY_SERVER_IP` / `DEPLOY_SSH_USER` / `DEPLOY_SSH_KEY` in `.env` for an exact line — it
  auto-detects and notes the `-i <key>` option otherwise, so it covers key **and** password auth).
  If no transport is available and the store is empty, deploy.sh **stops** (or needs `ALLOW_COLD_START=yes`)
  so prod never starts cold. See **[`DEPLOYMENT.md`](DEPLOYMENT.md) §2/§3/§3a**.
- **PostgreSQL 17 client tools are auto-resolved** (`pgtools.sh`): the deploy/dump/backup scripts use the
  host `pg_dump`/`pg_restore` if it's ≥ 17, else they borrow the tools from the running `postgres:17`
  container (`docker exec`) — so a **PG15 laptop still works** while the `db` container is up. If neither
  exists, **`./install_pg_client.sh`** installs the client for your OS (`--dry-run` to preview). Full-store
  backups: **`./backup_store.sh`** → `./backups/*.dump` (git-ignored). The store container also carries
  `pg_basebackup`/`pg_waldump`, but the scripts deliberately use **logical** dumps (right granularity;
  never `--clean`, so a restore never drops data).
- **The model is shipped OUT-OF-BAND, never in git** — the model files contain customer-derived
  identifiers, beneficiary account numbers and IP subnets, so the repo carries **code +
  `schema_pg.sql`** only. On a host that has the trained `artifacts/` (build/operator host), `deploy.sh`
  rsyncs them across; on a host that only has a git clone, transfer a **model bundle** and `deploy.sh`
  unpacks it into `./artifacts` (`MODEL_BUNDLE`, default `./model-bundle.tar.gz`). Bundle **both** the
  active and previous model so `registry.rollback()` works. Full steps: **[`DEPLOYMENT.md`](DEPLOYMENT.md) §2**.
- **`./watchdog.sh`** — always-on. Slacks (with the real cause + last logs) when a container dies /
  goes unhealthy, or when an ERROR / **profile-retrain failure** appears in the audit log.
- Slack for both is `BP_SLACK_WEBHOOK_URL` (no-op until you set it; alerts are logged meanwhile).

### Production checklist (set these in `.env`, then everything runs without hand-holding)
- `STORE_PG_PASSWORD` (required) · `PROD_PG_*` (the read-only source) · `BP_USE_MODEL=true`.
- `BP_SYNC_AT_HOUR=4` — the daily 04:00 production pull (the `sync` service is the only reader).
- `BP_SCORE_WEBHOOK_URL` — where decisions are delivered · `BP_SLACK_WEBHOOK_URL` — alerts.
- Containers use **`restart: unless-stopped`** + healthchecks (auto-recover on crash); run
  **`./watchdog.sh`** alongside them so a crash / unhealthy / retrain-failure is reported (Slack +
  `./logs/watchdog.log`). Logs persist to **`./logs/`** (service `behaviour.log`, ML `ml.log`).
- **Velocity/bursts** are MODEL features (`vel_1m…vel_24h`, `amt_1h_ratio`, `recency`) computed from
  each customer's recent history at score time — there is no separate velocity engine to run.

### Production data pull — daily 04:00, and the IP allowlist (dev vs prod)

The `sync` service is the **only** process that reads production, and **how often it pulls is one
switch**, `BP_SYNC_AT_HOUR`:

| Environment | `.env` setting | Behaviour |
|---|---|---|
| **Production** | `BP_SYNC_AT_HOUR=4` (**set / uncommented**) | Pulls **once a day at 04:00** (`BP_SYNC_AT_MINUTE`, `BP_SYNC_TZ`). This is the production schedule. |
| **Development** | `#BP_SYNC_AT_HOUR=4` (**commented**) + `BP_ALLOW_PROD_PULL=0` | **No production pulls** — the master switch stops every live read; `/score` serves from the local cache. (Leaving it only commented falls back to *interval* backfill mode — `BP_SYNC_INTERVAL_SECONDS` — which still pulls; set `BP_ALLOW_PROD_PULL=0` to fully stop.) |

When `BP_SYNC_AT_HOUR` is **unset**, the sync runs in **interval mode** (every
`BP_SYNC_INTERVAL_SECONDS`) — that is the initial-**backfill** mode, **not** the production daily
pull. So for production, keep `BP_SYNC_AT_HOUR=4` **active — do not leave it commented/greyed out.**

**Prerequisite for ANY pull (dev or prod): this host's IP must be allowlisted on the production
Postgres.** Without it every pull fails to connect. This is not automatic — the DB admin adds the
server's egress IP to the production allowlist first.

**`deploy.sh` enforces both** so a production release can't silently misconfigure the pull:
1. It **asks** "Has THIS server's IP been allowlisted on the production Postgres?" and then **verifies
   the connection for real** (`select 1`) — answer no / a failed connect **stops** the deploy.
2. It **verifies `BP_SYNC_AT_HOUR` is set** (`verify_sync_schedule`): if it's unset (interval mode) the
   deploy **warns and stops** on a TTY (or requires `ALLOW_INTERVAL_SYNC=yes` unattended), telling the
   operator to set `BP_SYNC_AT_HOUR=4` — so production never accidentally runs backfill-interval pulls.

### Real-time velocity — the live-velocity feed (Redis)

Live velocity does **not** fetch from the cache. It has its **own store (Redis)** that `/score` writes
to and reads from in real time. At score time, the two sources are **merged**, then the velocity
**features are computed** from the combined rows.

**The two sources of "recent history":**

| Source | Filled by | Contains | Freshness |
|---|---|---|---|
| `bp_transactions_cache` (Postgres) | the daily 4am `sync` (from production DB) | historical transactions | up to ~1 day old |
| **Redis live window** (`vel:<customer>`) | **`/score` itself, on every call** | transactions scored since | **real-time** |

Neither *is* the features. Both are just **rows** (transaction_id, amount, timestamp) fed to the
feature builder, which then **computes** `vel_1m…vel_24h`, `amt_1h_ratio`, `recency`.

**What happens on one `/score` call** (`ml/serve.py:score_payload`):

```text
POST /score  (one transaction)
   │
   1. hist  = _recent_history(customer)      → reads bp_transactions_cache (batch, daily)
   2. live  = live_velocity.recent(customer) → reads Redis (real-time, fed by /score)
   3. merge hist + live  (dedup by transaction_id)      ← a synced txn may be in both
   4. append THIS incoming transaction
   5. feature builder computes vel_*, amt_1h_ratio, recency  from all those rows
   6. model scores → decision returned
   7. live_velocity.record(THIS transaction) → writes it to Redis
        └─ so the NEXT /score for this customer sees it immediately
```

So: steps **1–2** *fetch* (from cache **and** Redis); step **5** *computes* the velocity features
from the merged rows; step **7** *feeds back* — the current transaction is written to Redis **after**
scoring, so it is not counted in its own velocity but is there for the next one. That is why firing
the same customer repeatedly makes `velocity_burst` appear — each call adds a row to Redis that the
next call reads.

**Why both, not just one?**
- **Cache alone** (the old behaviour): a burst of 5 calls in one minute wouldn't be seen — `/score`
  doesn't write the cache and the sync runs only daily, so calls #2–5 can't see call #1; velocity
  stayed near zero mid-day.
- **Redis adds the real-time layer:** each `/score` records the transaction instantly, so successive
  calls within the 48h TTL window see each other → true real-time burst detection, **independent of
  the sync cadence**.
- **Cache still provides historical depth** for the longer windows and the general pattern.

It is a **state store, not a second engine** — it only supplies rows to the existing feature builder,
makes no decisions, adds no rules. It is **TTL-bounded** (`BP_VELOCITY_RETAIN_HOURS`, default 48h),
**concurrency-safe** (atomic Redis sorted-set ops, shared across all API workers/containers), and
**fail-safe**: if Redis is disabled (`BP_REDIS_URL` empty) or unreachable, step 2 returns empty and
step 7 is a no-op — `/score` silently falls back to cache-only (the old behaviour), **no errors**.
Runs as the `redis` compose service (durable via the `redis-data` volume).

**Scope:** Redis feeds **only** the velocity/recency features. The amount / new-beneficiary /
new-location / cross-border / time-of-day signals come from the **learned model baseline** (the
trained artifacts), not from either recent-history source — so those are unaffected by Redis entirely.


## Behavioural Feature Vector

The following feature vector is used by the Behavioural Anti-Fraud Model to describe a customer's transaction behaviour. These features are generated during feature engineering and are used as inputs to the ensemble model (Graph Neural Network embeddings, Isolation Forest, and Autoencoder).

| Feature | Full Name | Description |
|---------|-----------|-------------|
| `amt_log` | Logarithm of Transaction Amount | The transaction amount after applying a logarithmic transformation to reduce the effect of very large amounts. |
| `amt_z` | Z-Score of Transaction Amount | Measures how far the transaction amount is from the customer's normal average amount, expressed in standard deviations. |
| `amt_over_median` | Amount Over Customer Median | Compares the current transaction amount against the customer's historical median transaction amount. |
| `amt_over_p95` | Amount Over 95th Percentile | Indicates whether the transaction amount exceeds what the customer normally spends 95% of the time. |
| `amt_over_max` | Amount Relative to Historical Maximum | Compares the current transaction amount with the customer's highest historical transaction amount. |
| `above_max` | Above Historical Maximum | A binary feature indicating whether the current transaction exceeds the customer's previous maximum transaction amount. |
| `hour_rarity` | Hour-of-Day Rarity | Measures how unusual it is for the customer to transact at the current hour of the day. |
| `dow_rarity` | Day-of-Week Rarity | Measures how unusual it is for the customer to transact on the current day of the week. |
| `is_night` | Night-Time Transaction Indicator | Indicates whether the transaction occurred during predefined night hours. |
| `location_new` | New Transaction Location | Indicates whether the transaction originated from a location the customer has never used before. |
| `country_new` | New Country Indicator | Indicates whether the transaction originated from a country the customer has never transacted from before. |
| `cross_border` | Cross-Border Transaction Indicator | Indicates whether the transaction crosses country boundaries. |
| `beneficiary_new` | New Beneficiary Indicator | Indicates whether the recipient has never previously received a payment from this customer. |
| `type_rare` | Rare Transaction Type | Indicates whether the transaction type is unusual for this customer. |
| `ip_new` | New IP Address Indicator | Indicates whether the customer is using a previously unseen IP address. |
| `vel_1h` | Transaction Velocity (Last 1 Hour) | The number of transactions made by the customer during the previous one hour. |
| `vel_24h` | Transaction Velocity (Last 24 Hours) | The number of transactions made by the customer during the previous twenty-four hours. |
| `amt_1h_ratio` | Amount-to-One-Hour Average Ratio | Compares the current transaction amount with the customer's average transaction amount over the last hour. |
| `recency_hours` | Hours Since Previous Transaction | The number of hours since the customer's last transaction. |
| `g_fanout` | Graph Fan-Out | The number of unique recipients or connected nodes the customer has interacted with in the transaction graph. A higher value may indicate broader money movement. |
| `g_distinct_benef` | Graph Distinct Beneficiaries | The number of distinct beneficiaries the customer has transacted with in the transaction graph. |
| `g_shared_cp` | Graph Shared Counterparties | Measures whether the customer shares beneficiaries or counterparties with other customers, which can help detect fraud rings, mule accounts, or coordinated fraud networks. |

### Analyst glossary — what actually appears in a `/score` response

The table above is the **model's internal feature vector**. An analyst never sees those raw names.
Instead, `/score` returns a **curated vocabulary**: `triggered_signals` (machine codes) and
`detection_reason` / `description` (plain English). The mapping lives in
[`ml/codes.py`](ml/codes.py) (`_FEATURE_SIGNALS`) and is generated **dynamically** — a signal only
appears when its condition is met on the live feature vector, and the numbers (e.g. "3.7x") are
computed per transaction. Only the *wording* is fixed (so reasons stay consistent and auditable);
nothing is hard-coded.

| `triggered_signals` code | Analyst-facing reason (in `detection_reason`) | Driven by |
|---|---|---|
| `amount_above_historical_max` | amount exceeds the customer's historical maximum (population baseline maximum for cold-start) | `above_max`, `amt_over_max` |
| `amount_far_above_usual` | amount far above the customer's usual (far above the population baseline for cold-start) | `amt_z`, `amt_over_median` |
| `new_beneficiary` | first-time beneficiary | `beneficiary_new` |
| `new_location` / `new_country` | a location / country the customer has not used before | `location_new`, `country_new` |
| `cross_border` | cross-border transfer | `cross_border` |
| `unusual_hour` / `unusual_day` / `night_time` | uncommon hour / day of week / night-time for this customer | `hour_rarity`, `dow_rarity`, `is_night` |
| `velocity_burst` | burst of transactions in a short window (1–3 min) | `vel_1m`, `vel_2m`, `vel_3m` |
| `hourly_amount_spike` | amount far above the customer's recent hourly average | `amt_1h_ratio` |
| `rare_transaction_type` | an unusual transaction type for this customer | `type_rare` |
| `new_ip` | a device / IP the customer has not used before | `ip_new` |
| `high_fanout` | funds sent to an unusually large number of distinct recipients (fan-out) | `g_fanout` + `g_distinct_benef` |
| `shared_counterparty` | beneficiary is shared by many customers (possible collector) | `g_shared_cp` |
| `cold_start` | no learned profile yet — judged against the population baseline | `is_cold_start` |
| `detector:<name>` | the named ensemble detector produced a very high anomaly score | Isolation Forest / Autoencoder / Graph model |

**Deliberately NOT exposed to analysts** — these are preprocessing/transforms or redundant, so they
never appear in a response: `amt_log` (log transform), the raw `amt_z` value, `amt_over_p95`
(redundant with median/max), `recency_hours` (context, not an anomaly on its own), and
`g_distinct_benef` on its own (only used to gate `high_fanout`). Exposing `amt_log` as
*"logarithm of amount was unusual"* would be meaningless operationally, so we don't.


## §1–16 Practical Rules: Honest Coverage

| § | Rule | Status | Where / Gap |
|---|---|---|---|
| 1 | Clean baseline | ✅ | `ml/pipeline/clean.py` uses a clean-only baseline with minimum transaction and day requirements. **Note:** We currently use 50 transactions / 30 days rather than 100 transactions / 90 days. The cache is 91 days and this is documented. |
| 2 | Minimum data (days / transactions / logins / devices / locations) | ⚠️ | Days and transactions are enforced. Login and device data are not available. Location data is mostly a placeholder. |
| 3 | Retrain frequency (daily) | ⚠️ | Daily **pull** (`BP_SYNC_AT_HOUR`) + daily **retrain-due check** + the **§4 gate** are all automatic; only the retrain *step* is a **human-gated** trigger (`ml.retrain_trigger --run`). The automated daily cron is written but kept disabled by choice (`deploy/crontab.example`) — a deliberate safety gate, **one config flag from fully-daily**. **To reach ✅ (suggested, not yet done):** enable the gated auto-retrain cron; it stays §4-gated so it never trains on tiny changes — an ops flag, not new code. |
| 4 | Retrain only on enough data (≥100 transactions OR 30 days OR drift) | ✅ | Implemented in `ml/retrain_trigger.py` using the exact **OR** condition. |
| 5 | Sliding window (90–180 days) | ✅ | Controlled by `LOOKBACK_MONTHS`. |
| 6 | Time decay | ✅ | **Time decay is now applied INSIDE the ML model.** `ml/pipeline/features.py` (`FeatureBuilder.fit`) weights every clean transaction by an exponential half-life (`BF_DECAY_HALF_LIFE_DAYS`, default 90) when learning each customer's amount mean/std/median/p95 and hour/day histograms — recent behaviour counts more, older behaviour fades — so the baselines the `/score` detectors compare against lean toward recent behaviour, not just the statistical profile layer (`retrain.py`). The historical **max** is intentionally not decayed (it is a hard "above anything ever seen" ceiling). Set the half-life to 0 to disable. |
| 7 | Never learn confirmed fraud | ✅ | Blocked and blacklisted transactions are excluded, **and the analyst-feedback loop now overrides the weak proxy label with confirmed ground truth**: a `fraud` verdict (`bp_decision_feedback`, `POST /feedback`) forces that transaction OUT of the clean training set; a `genuine` verdict forces it IN. See §11. |
| 8 | Stability: do not shift the profile based on one event | ✅ | `/score` never mutates a learned profile — the model uses aggregates and full offline retraining, never a single-event update (the old per-request counter-bump was removed for this reason). |
| 9 | Drift detection (gradual / sudden) | ⚠️ | Drift **is** detected (PSI amount-distribution drift + live flag-rate + per-transaction novelty). The PDF's two responses are already implemented via other rules: *gradual → adapt slowly* (sliding window §5 + time-decay §6); *sudden → flag first, learn only after validation* (stability §8 + analyst verification §11). What's missing is an **explicit gradual-vs-sudden classifier**. **To reach ✅ (suggested, not yet done):** add one — PSI trend over rolling windows = gradual; a large single-window jump / change-point = sudden — with distinct alerts. |
| 10 | Confidence threshold | ✅ | The statistical profile produces a `confidence_score`, and the ML model also produces its own `confidence_score`. |
| 11 | Retrain only after verification using analyst labels | ✅ | **The analyst-feedback loop is now wired.** `POST /feedback` records the fraud team's confirmed `genuine`/`fraud` verdict for a scored transaction into `bp_decision_feedback`; at the next retrain those verdicts override the training clean/fraud split (§7), and `ml.monitor --live` (`live_precision()`) reports **real** precision/recall from them. The remaining dependency is operational — the analyst team supplying verdicts. |
| 12 | Model versioning and rollback | ✅ | Implemented through `ml/registry.py` and `profile_version`. |
| 13 | — | — | No separate rule provided in the current §1–16 list. |
| 14 | Retraining triggers (8 kinds) | ⚠️ | New transactions, behavioural drift, scheduled retraining, performance degradation, **and analyst feedback** (§11 loop) are implemented. New customer segments, new fraud patterns, and feature changes are not yet implemented. |
| 15 | Prevent model/profile poisoning | ✅ | Clean-only data, eligibility checks, and the acceptance gate are implemented, **and analyst verdicts now correct the training population** (§7/§11). The promote step stays gated on the synthetic-anomaly AUC. |
| 16 | Profile components (18 listed) | ⚠️ | Approximately 8 components are implemented, including amount, transaction times, day patterns, velocity, beneficiary behaviour, channel, and partial location/IP information. Device, network, browser, login frequency, failed logins, session behaviour, balance, salary cycle, and merchant/category data are not implemented because the required data is unavailable. |

### Bottom Line

| Area | Coverage |
|---|---|
| **Overall implementation** | Everything that the currently available data supports has been implemented. |
| **Data-limited gaps** | Device, login, session, balance, merchant/category, and other features cannot currently be implemented because the required data is unavailable. |
| **Analyst-loop** | The analyst feedback loop is now **implemented** (`POST /feedback` → `bp_decision_feedback` → retrain override + real live precision). The remaining dependency is operational: the analyst team supplying verdicts. |
| **Geovelocity** | IP-to-latitude/longitude geovelocity detection is not yet implemented and remains future work. |
| **Multi-currency (per-currency profiles)** | **Not yet implemented.** The model is currency-blind (amounts are aggregated across currencies) and no non-NGN customer is eligible, so USD/GBP/EUR transactions fall through to the cold-start population path. See "Not yet implemented — per-currency behavioural profiles" below. |
| **Conclusion** | The remaining gaps are primarily caused by unavailable data (device/login/session/etc.), rather than missing implementation of capabilities that the current data supports. |

### Not yet implemented — per-currency behavioural profiles (USD / GBP / EUR)

**Today the model works well for the NGN population it is trained on, but it does NOT yet model
non-NGN behaviour per currency.** Two reasons, both verified against the live model:

1. **No non-NGN customer is eligible.** The cache is ~99.99% NGN; only ~13 customers have any USD/USDT
   rows (and they look like test data), and **none** meet the §1 eligibility gate (≥50 clean txns /
   ≥30 days). So every USD/GBP/EUR transaction is **cold-start** — judged against the *population*
   baseline, not a personal one.
2. **The model is currency-blind.** `FeatureBuilder` aggregates each customer's `amount` across *all*
   currencies into one set of stats, and scoring compares the **raw amount number** (no FX / no
   per-currency baseline). Because the population baseline is NGN-derived (median ≈ 5,900, p95 ≈
   100,500), a USD amount is judged as if it were that raw number: e.g. cold-start USD `$5,000` →
   *safe*, `$250,000` → *unsafe* — the model is reacting to the **number**, not understanding
   "dollars". A customer who genuinely transacts in USD gets **no learned USD "normal."**

**What it needs (planned, not built):**
- **Per-currency grain** — profile per `(entity_key, currency)` so a customer's USD normal is separate
  from their NGN normal (schema, build, and scoring changes; a design plan already exists).
- **Currency normalization** applied identically on the build and scoring sides (`POUND→GBP`,
  `EURO→EUR`, `USDT→USD`, trim/upper).
- **Real non-NGN volume** so those `(customer, currency)` profiles clear the eligibility gate.

Until then, treat any non-NGN `/score` result as a **cold-start, population-relative** decision, not a
per-customer per-currency one. (The absolute protections — hard caps, cross-border, blacklist,
velocity — still apply regardless of currency.)


## Demo

````markdown
# Behavioural Anti-Fraud Service

## 1. Run the App

The demo uses the existing behavioural profiles and trained model already stored in the local store. No fresh production data pull is required.

```bash
cd /home/adesoji/AI-service

export BP_SCORE_WEBHOOK_URL='https://webhook.site/6af856dd-6288-49ec-b47e-4f1732741c3c'

docker compose up -d --build db behaviour-profile
````

The command above starts:

* `db` for the behavioural profile store
* `behaviour-profile` for the Behavioural Anti-Fraud API

The `sync` service is intentionally not started because the demo does not need to pull fresh production data.

### Check the API health

```bash
curl -s localhost:8080/health
```

### Open Swagger

Open:

```text
http://localhost:8080/docs
```

Then:

1. Select `POST /score`.
2. Click **Try it out**.
3. The request form is pre-filled.
4. Submit the transaction.

---

## 2. Score a Transaction

You can also score a transaction directly from the terminal:

```bash
curl -s -X POST localhost:8080/score \
  -H 'Content-Type: application/json' \
  -d '{
    "transaction_id":"12345678",
    "amount":14000000,
    "currency":"NGN",
    "transaction_type":"transfer",
    "account_type":"individual",
    "origin_account":{
      "account_number":"9876543219",
      "bank_code":"001"
    },
    "destination_account":{
      "account_number":"123456789",
      "bank_code":"002"
    },
    "customer_details":{
      "customer_name":"Muhammad Ibrahim Isah",
      "customer_email":"user@example.com",
      "identifier":"22598330040",
      "identifier_type":"bvn"
    },
    "additional_info":{
      "ip_address":"192.168.1.1",
      "location":"Lagos, Nigeria"
    }
  }'
```

The `/score` endpoint will load the customer's existing behavioural profile, generate the required behavioural features, run the trained behavioural model, produce the risk decision, and record the inference result.

---

## 3. View Live Logs

### Docker container logs

Use the following command to watch the Behavioural Anti-Fraud service in real time:

```bash
docker compose logs -f behaviour-profile
```

### Plain-text audit log

The service also writes an audit log to the host:

```bash
tail -f logs/behaviour.log
```

---

## 4. Audit and Inference Records

### Per-inference model log

Each model inference is recorded in a JSONL audit file:

```bash
tail -f artifacts/inference_log/inference-$(date +%F).jsonl
```

### Check recent decisions in the database

The following query shows recent behavioural decisions, including whether the webhook was successfully sent:

```bash
docker exec behaviour-profile-db psql -U behaviour -d behaviour -c \
"SELECT transaction_id,decision,judged_against,webhook_status FROM bp_decision ORDER BY id DESC LIMIT 5;"
```

This allows us to verify:

* Which transaction was scored
* What decision was made
* What the transaction was judged against
* Whether the webhook was successfully delivered

---

## 5. Stop and Restart the Service

### Stop the containers

This stops the containers while keeping them available to start again:

```bash
docker compose stop
```

### Start the containers again

```bash
docker compose up -d
```

### Remove the containers but keep the data

```bash
docker compose down
```

`docker compose down` removes the containers but **keeps the persistent profile-store data volume**.

> **Important:** Never use `docker compose down -v`.
>
> `docker compose down -v` removes the persistent volumes and can delete the behavioural profile store.

---

## 6. Deploy to Production

The production deployment script is operator-run.

Run:

```bash
./deploy.sh
```

The deployment script will:

1. Confirm the production IP configuration.
2. Verify the production PostgreSQL connection.
3. Promote the existing behavioural profiles to production.
4. Promote the validated model and registry information.
5. Start or update the production service.
6. Check the production health endpoint.
7. Report the deployment result.

The existing customer behavioural profiles are carried forward during deployment so that production does not start with empty profiles for customers that have already been learned.

---

## 7. Start the Production Watchdog

The watchdog continuously monitors the running deployment for container failures and retraining failures.

Start it with:

```bash
nohup ./watchdog.sh >/dev/null 2>&1 &
```

When Slack is configured, the watchdog can send alerts when:

* A container crashes.
* A container becomes unhealthy.
* A retraining job fails.
* Other configured operational failures occur.

Docker's `restart: unless-stopped` policy remains responsible for automatically restarting failed containers. The watchdog is responsible for **detecting the failure and notifying the operator**.

---

## 8. Current Live Demo Instance

A live instance is already available locally at:

```text
http://localhost:8086/docs
```

This instance is connected to the real behavioural profile store and can be used to click through the API immediately.

---

## 9. Important Operational Note

Keep an eye on the behavioural profile database container:

```bash
docker ps
```

The `behaviour-profile-db` container has been observed stopping intermittently in the current sandbox environment. This appears to be an environment issue rather than an application-code issue.

For production, the deployment uses:

* Docker `restart: unless-stopped`
* `watchdog.sh`
* Container health checks
* Operational alerts

to detect and recover from this type of failure.

---

## 10. Repository Status

The current implementation has **not yet been committed** to the repository.

The intended branch for the Behavioural ML integration is:

```text
feat/behavioural-ml
```

Once the implementation has been reviewed and verified, the ML subsystem, behavioural profile integration, deployment scripts, monitoring, and relevant documentation can be committed to that branch.

> Do not commit secrets, PII, generated artifacts that are excluded from version control, or other sensitive deployment data.

```


# Behavioural Anti-Fraud — Customer Identity & Contamination Validation

## 1. What Identifies a Customer — The “Constant” Anita Meant

The behavioural model identifies a customer using a **stable customer identifier** — the government-issued identity value that remains constant for a person.

Examples include:

| Country       | Customer Identifier                     |
| ------------- | --------------------------------------- |
| Nigeria       | BVN                                     |
| Kenya         | National ID / KRA PIN                   |
| Ghana         | Ghana Card                              |
| Other markets | Equivalent government-issued identifier |

In the transaction payload, this is represented as:

```json
customer_details.identifier
customer_details.identifier_type
```

For example:

```json
{
  "customer_details": {
    "identifier": "22430372151",
    "identifier_type": "bvn"
  }
}
```

### Configuration

The identity column is declared in:

```text
ml/config.py:79
```

```python
IDENTITY_COLS = ("identifier",)
```

`identifier_type` is carried alongside the identifier to indicate what type of identity value it represents.

### How the Model Uses the Identifier

The model uses the identifier as the **customer key** throughout the scoring process.

In:

```text
ml/serve.py:68
```

the identifier is read as the `customer_key`.

The recent-history lookup also uses this identifier:

```text
ml/serve.py:72
```

```sql
WHERE identifier = %s
```

The trained per-customer behavioural baselines are also keyed using this identifier.

Therefore:

> **One customer identifier = one customer = one behavioural profile.**

### Why the Identifier Is Used

The identifier is preferred because it represents the stable identity of the customer.

Other transaction attributes can change over time:

* Account numbers can change.
* Cards can change.
* Card BIN / last-4 values can change.
* Branches can change.
* Devices can change.
* Transaction IDs are unique to individual transactions.
* Transaction types describe behaviour rather than identity.

Using these values as the customer key would either:

1. Fragment one customer into multiple behavioural profiles, or
2. Potentially merge different customers incorrectly.

Branch and transaction type are therefore treated as **behavioural features**, not as the customer's identity.

---

## 2. Customer Identity in the Example Payload

For the payload being evaluated:

```text
customer_details.identifier = 22430372151
customer_details.identifier_type = bvn
```

Therefore:

```text
Customer key = 22430372151
```

The BVN is the value the model uses to recognise the customer.

Because the identifier is present, the model can:

1. Look up the customer's learned behavioural baseline.
2. Retrieve the customer's recent transaction history.
3. Compare the current transaction against the customer's established behaviour.
4. Produce a customer-specific behavioural risk assessment.

---

# 3. There Are Two Different Keys

There are two related but importantly different concepts in the implementation:

1. **The model's customer key** — used to recognise the customer during behavioural scoring.
2. **The service's entity key** — used primarily for audit records and maintaining the statistical profile.

These should not be treated as the same thing.

---

## 3.1 Model Customer Key — Used for Behavioural Scoring

The model determines the customer key in:

```text
ml/serve.py:111-114
```

```python
identifier = cust.get("identifier") or cust.get("bvn")

"customer_key": str(identifier) if identifier else "unknown"
```

The scoring logic therefore follows:

```text
customer_details.identifier
        ↓
customer_details.bvn
        ↓
unknown
```

If the identifier is available, it becomes the customer's model key.

For the example payload:

```text
identifier = 22430372151
```

so:

```text
customer_key = "22430372151"
```

### What Happens When the Identifier Is Missing?

If both `identifier` and `bvn` are absent:

```text
customer_key = "unknown"
```

The model then treats the transaction as a **cold-start** case.

It does **not** use the account number as a substitute customer key for the model's learned behavioural baseline.

Instead, because the model cannot associate the transaction with a known customer profile, it evaluates the transaction using the available population-level/cold-start logic rather than the customer's learned behavioural baseline.

This is why tests with an unknown identifier return:

```text
is_cold_start: true
```

---

# 4. Service `entity_key` — Used for Audit and Statistical Profile Management

The service maintains a separate `entity_key` in:

```text
service.py:161-164
```

The fallback chain is:

```python
ident = (
    customer_details.identifier
    or origin_account.account_number
    or transaction_id
)
```

This means the service attempts to obtain an entity key in this order:

```text
identifier
    ↓
origin account number
    ↓
transaction ID
```

The purpose of this fallback is to ensure that the service **always has an entity key available** for:

* Writing the decision/audit record.
* Maintaining the statistical profile.
* Passing the entity into the statistical-profile retraining process.

### Example

For the current payload:

```text
identifier       = 22430372151
origin account   = 9876543219
transaction ID   = 12345678
```

The service resolves:

```text
entity_key = 22430372151
```

because the identifier is available.

If the identifier were missing:

```text
entity_key = 9876543219
```

If both the identifier and account number were missing:

```text
entity_key = 12345678
```

---

# 5. Important Distinction Between the Two Keys

The fallback mechanism in the service **does not mean that account number or transaction ID becomes an alternative customer identity for the behavioural model**.

The distinction is:

| Key                    | Purpose                                                                | Fallback                                                          |
| ---------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------- |
| **Model customer key** | Recognise the customer and retrieve their learned behavioural baseline | `identifier` → `bvn` → `unknown`                                  |
| **Service entity key** | Audit records and statistical-profile management                       | `identifier` → `origin_account.account_number` → `transaction_id` |

Therefore:

> **The identifier is what makes the model recognise a customer.**

The account-number and transaction-ID fallbacks are a **service-level safety mechanism**, not replacements for customer identity during behavioural scoring.

---

# 6. Cold-Start Behaviour

When a transaction does not contain a usable customer identifier, the model cannot reliably associate the transaction with a previously learned customer profile.

The transaction is therefore treated as:

```text
is_cold_start = true
```

The system can still:

* Score the transaction.
* Produce a risk decision.
* Record the decision.
* Maintain an audit trail.
* Use the service-level `entity_key`.

However, it cannot perform the same customer-specific comparison that it would perform when a valid identifier is available.

This is why the production payload should provide:

```json
{
  "customer_details": {
    "identifier": "...",
    "identifier_type": "..."
  }
}
```

whenever the customer's stable identity is known.

---

# 7. Contamination Plot — Where It Is Generated

The contamination plot is generated in:

```text
ml/eval/plots.py:113
```

through:

```python
def contamination(risk_normal, risk_synth, ...):
```

The resulting plot is written to:

```text
artifacts/plots/validation_contamination.png
```

The plot contains:

1. The risk-score distribution for held-out normal transactions.
2. The risk-score distribution for synthetic fraud-like/anomalous transactions.
3. A dashed **p99 normal alert threshold**.

Conceptually:

```text
Risk Score
    │
    │       Normal
    │       ███████
    │      █████████
    │     ███████████
    │
    │                       Synthetic anomalies
    │                       █████████
    │                      ███████████
    │                     █████████████
    │
    └──────────────────────────────────────
                     ↑
                  p99 normal
                  alert cut
```

---

# 8. Where the Contamination Data Comes From

The underlying data is generated by:

```text
ml/eval/unsupervised.py
```

inside:

```python
evaluate()
```

The evaluation process scores two groups:

### Normal Transactions

Held-out normal transactions are scored to produce:

```text
_risk_normal
```

### Synthetic Anomalies

Synthetic fraud-like/anomalous transactions are generated from the normal transactions using:

```text
make_synthetic()
```

These are then scored to produce:

```text
_risk_synth
```

The evaluation calculates:

```text
contamination_gap = median(synth) - p99(normal)
```

Specifically:

```text
unsupervised.py:93-94
```

---

# 9. Where the Plot Is Called

The plot is invoked from:

```text
ml/eval/metrics.py:64-68
```

The two risk arrays are retrieved:

```python
rn = risk_normal
rs = risk_synth
```

and passed to:

```python
plots.contamination(rn, rs)
```

The resulting figure is stored as:

```text
figs["validation_contamination"]
```

---

# 10. What Orchestrates the Evaluation

The complete evaluation flow is orchestrated by:

```text
ml/train.py
```

During training:

```text
ml/train.py:145
```

runs:

```python
unsupervised.evaluate(...)
```

on the **20% holdout dataset**.

The resulting evaluation output is then passed into:

```text
ml/train.py:149
```

which invokes:

```python
evalm.evaluate(...)
```

Therefore, the contamination plot is produced **once for each training run**.

---

# 11. Why the Contamination Plot Exists

At this stage, there are no confirmed fraud labels available for a reliable real-world fraud ROC analysis.

Because of that, using a traditional supervised fraud ROC as the primary validation mechanism would be misleading.

The contamination plot provides a **label-free validation check**.

It answers:

> **Does the model assign substantially higher risk scores to synthetic anomaly-like transactions than it assigns to genuinely normal transactions?**

The plot allows us to inspect:

* The distribution of normal risk scores.
* The distribution of synthetic anomaly risk scores.
* The separation between the two distributions.
* The location of the p99 normal alert threshold.
* The degree of overlap between normal and anomalous behaviour.

---

# 12. Interpreting `contamination_gap`

The metric is:

```text
contamination_gap = median(synthetic risk) - p99(normal risk)
```

A **positive contamination gap** means the typical synthetic anomaly receives a risk score above the extreme upper tail of the normal distribution.

This indicates useful separation.

A larger positive gap generally suggests that the model is better at distinguishing the synthetic anomaly distribution from normal behaviour.

If the distributions overlap heavily, it suggests that the model's representation of normal behaviour may be too loose or that the synthetic anomalies are not sufficiently distinguishable from normal transactions.

---

# 13. Relationship to the Synthetic ROC

The contamination plot is one component of the unsupervised validation process.

It works alongside:

```text
validation_synthetic_roc.png
```

The synthetic ROC evaluates the model's ability to distinguish:

```text
Normal transactions
        vs.
Synthetic anomalies
```

The current target for the synthetic-anomaly AUC is approximately:

```text
0.75 – 0.85
```

The two validation views therefore provide complementary information:

| Validation              | What It Shows                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Contamination plot**  | How the risk-score distributions of normal and synthetic anomalies are separated, including the p99 normal alert threshold |
| **Synthetic ROC / AUC** | How well the model ranks synthetic anomalies above normal transactions across thresholds                                   |

Neither should be interpreted as proof of real-world fraud performance because the anomaly examples are synthetic rather than confirmed fraud cases.

---

# 14. End-to-End Summary

The behavioural system can therefore be understood as follows:

```text
                     TRANSACTION
                          │
                          ▼
              customer_details.identifier
                          │
                          │
              ┌───────────┴───────────┐
              │                       │
              ▼                       ▼
       MODEL CUSTOMER KEY       SERVICE ENTITY KEY
              │                       │
              │                       ├── identifier
              │                       ├── account number
              │                       └── transaction ID
              │
              ▼
      Customer behavioural
           baseline
              │
              ▼
      Recent customer history
              │
              ▼
       Behavioural scoring
              │
              ▼
        Risk / decision
```

The key principle is:

> **The stable government-issued identifier is the actual customer identity used by the behavioural model.**

For the example payload:

```text
BVN = 22430372151
```

therefore:

```text
Model customer key = 22430372151
Service entity key = 22430372151
```

If the BVN/identifier is unavailable:

```text
Model customer key = unknown
→ cold-start scoring
```

while the service can still maintain an audit/entity key through:

```text
origin account number
        ↓
transaction ID
```

Those fallbacks ensure the service can continue recording and managing the transaction, but **they do not replace the customer's stable identity for learned behavioural scoring**.

---

## 15. Key Takeaway

There are two separate concerns:

1. **Identity for the ML model:**
   The customer's stable government-issued identifier (BVN, National ID, KRA PIN, Ghana Card, etc.) is what links transactions to the customer's learned behavioural profile.

2. **Operational entity tracking:**
   The service has a fallback chain of identifier → account number → transaction ID so that every transaction can still have an entity key for audit and statistical-profile operations.

Separately, because confirmed fraud labels are not yet available, the **contamination plot** provides a label-free validation mechanism by comparing normal transaction risk scores against synthetic anomaly risk scores and measuring their separation around the p99 normal threshold.



## Notes
- **AML rules are fully removed** (`1.md`): there is one scoring path (the ML model). AML/rules
  are owned by a separate service. The `profile_is_trusted` eligibility gate lives in
  `eligibility.py`.
- **No confirmed-fraud labels yet** → the model's quality signal is the unsupervised
  synthetic-anomaly AUC; nothing claims analyst-verified precision.
- **Secrets & PII** (`.env`, `1.md`, `behavioral_analysis_data/`, `artifacts/`) are git-ignored —
  keep them out of the repo.



# 📢 Operational Handover: Calibrated Behavioral Risk Thresholds

**Model:** `bf-ensemble-2026.08.03-094608`  
**Status:** Successfully calibrated  
**Calibration approach:** Dynamic, quantile-based alert zones based on observed behavioral density

Our updated **GNN + Autoencoder + Isolation Forest ensemble model** has been successfully calibrated. We have moved from static score interpretation to **dynamic, quantile-based risk zones**, allowing review decisions to be aligned with real behavioral patterns and risk density.

## Operational Risk Tiers

Please apply the following three operational tiers to review queues immediately:

| Zone | Score Range | Risk Level | Operational Action |
|---|---:|---|---|
| 🟥 **Priority-1 Unsafe Zone** | **Score ≥ 0.819** | High | Route directly to immediate auto-block or top-priority analyst verification |
| 🟨 **Review / Grey Zone** | **0.761 ≤ Score < 0.819** | Medium / Ambiguous | Route to secondary manual review |
| 🟩 **Clear Normal Zone** | **Score < 0.761** | Low | Bypass manual review pipelines |

### 🟥 Priority-1 Unsafe Zone — Score ≥ 0.819

These are **high-confidence behavioral anomalies**, representing approximately the **top 1% of highest-risk structural deviations**.

**Action:**
- Route directly to immediate auto-block, where applicable; or
- Escalate to top-priority analyst verification.

### 🟨 Review / Grey Zone — 0.761 ≤ Score < 0.819

This is the **legitimate overlap area**, containing approximately the **top 5% of complex transaction patterns** where intense normal activity can overlap with near-normal anomalies.

**Action:**
- Route to secondary manual review.
- Do not treat the score alone as sufficient for an automatic block.

### 🟩 Clear Normal Zone — Score < 0.761

These transactions fall within the **trusted behavioral baseline**, where legitimate customer behavior dominates.

**Action:**
- Bypass manual review pipelines.
- Allow normal processing to reduce unnecessary friction for legitimate users.

## Key Operational Principle

> **Higher scores indicate greater deviation from the learned behavioral baseline.**

The thresholds are **calibrated quantile boundaries**, rather than arbitrary static scores. They should therefore be treated as operational decision boundaries derived from the current behavioral distribution.

### Live burst test


KEY=xxxxxxxxxxx
ID=70000000001
# optional: start with a clean live window
docker exec adhere-redis redis-cli DEL "vel:$ID"

for n in 1 2 3 4 5 6; do
  curl -s -X POST http://localhost:8080/score \
    -H "X-Adhere-Key: $KEY" -H 'Content-Type: application/json' \
    -d "{\"transaction_id\":\"BURST-$n\",\"amount\":5000,\"currency\":\"NGN\",\"transaction_type\":\"transfer\",\"account_type\":\"individual\",\"customer_details\":{\"customer_name\":\"Test User\",\"customer_email\":\"t@example.com\",\"identifier\":\"$ID\",\"identifier_type\":\"bvn\"},\"additional_info\":{\"ip_address\":\"10.0.0.9\",\"location\":\"Lagos\"}}" \
    | python3 -c 'import sys,json;r=json.load(sys.stdin);print("call:",r["status"],r["activity_code"],r["triggered_signals"])'
done



## Immediate Handover

All review queues should use the following decision logic:

```text
Score ≥ 0.819
    → 🟥 Priority-1 Unsafe
    → Auto-block or priority analyst verification

0.761 ≤ Score < 0.819
    → 🟨 Review / Grey Zone
    → Secondary manual review

Score < 0.761
    → 🟩 Clear Normal
    → Bypass manual review



```markdown
## Behavioural Anti-Fraud Activity Codes

| Code | Status / Zone | Triggered When |
|---|---|---|
| **BF-100** | Safe · 🟩 `clear_normal` | Risk is below the **p95 review cut**, indicating behaviour is consistent with the customer's normal pattern. |
| **BF-110** | Safe · 🟩 `clear_normal` | Risk is well below the normal range (**< 0.5**), indicating a strongly recurring or well-known behavioural pattern. |
| **BF-200** | Review · 🟨 `review_grey` | Risk falls within the **p95–p99 grey zone**, indicating a mild or borderline behavioural anomaly. |
| **BF-301** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99** and the dominant behavioural signal is **AMOUNT**, such as `amt_z`, `amt_over_max`, `above_max`, or `amt_over_p95`. |
| **BF-302** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99** and the dominant behavioural signal is **TIME**, such as `hour_rarity`, `dow_rarity`, or `is_night`. |
| **BF-303** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99** and the dominant behavioural signal is **LOCATION**, such as `location_new`, `country_new`, or `cross_border`. |
| **BF-304** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99** and the dominant behavioural signal is **BENEFICIARY / COUNTERPARTY**, such as `beneficiary_new`, `g_shared_cp`, or `g_fanout`. |
| **BF-305** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99** and the dominant behavioural signal is **VELOCITY / BURST**, such as `vel_1m` through `vel_24h` or `amt_1h_ratio`. |
| **BF-400** | Unsafe · 🟥 `priority_1` | Risk is **≥ p99.9**, indicating the strongest multi-signal behavioural anomaly. |

### Threshold Logic

The **p95, p99, and p99.9** boundaries are dynamic quantile thresholds provided by `/thresholds`.

Within the unsafe zone, the **BF-30x** activity code identifies the primary behavioural factor responsible for the anomaly:

- **BF-301** → Amount
- **BF-302** → Time
- **BF-303** → Location
- **BF-304** → Beneficiary / Counterparty
- **BF-305** → Velocity / Burst

**BF-400** is reserved for the strongest multi-signal anomalies where the risk score reaches or exceeds the **p99.9** threshold.
```
