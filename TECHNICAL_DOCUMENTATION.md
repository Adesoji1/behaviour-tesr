# Behavioural Anti-Fraud Service — Technical Documentation

**Component:** behavioural transaction-scoring microservice
**Model family / active version:** `bf-ensemble` · `bf-ensemble-2026.08.06-234348` (feature set `feat-2026.08-2`)
**Audience:** engineering + fraud/risk stakeholders
**Status:** operational; NGN production traffic. Known limitations are stated in §6.

---

## 1. Executive summary

The service learns each customer's **normal transaction behaviour** from their own clean history and
scores every new transaction for how far it deviates from that norm. It exposes a single decision
endpoint, `POST /score`, which returns a **status** (`safe` / `review` / `unsafe`), an **activity
code**, a **risk score**, and a plain-language **analyst explanation**. Scoring is **unsupervised** —
it does not require labelled fraud; it flags behaviour that is anomalous relative to the customer's (or,
for new customers, the population's) learned baseline. Decisions are persisted for audit and delivered
by webhook. Model training is an offline, GPU-accelerated, versioned job with an acceptance gate and
rollback.

---

## 2. Architecture

Five cooperating components, all containerised (Docker Compose); the model runs **in-process** in the
API (no separate model server in production).

| Component | Container | Responsibility |
|---|---|---|
| **API** | `adhere-behaviour` | `POST /score` (+ `/feedback`, `/health`, `/thresholds`, admin). Loads the active model at startup and scores in-process (CPU). |
| **Behaviour store** | `behaviour-profile-db` (PostgreSQL 17) | Learnt per-customer profiles, the transaction **cache** (mirror of source), decisions, feedback, API keys. |
| **Live-velocity store** | `adhere-redis` | Real-time velocity window per customer (burst features). Fail-safe: if down, `/score` still works on the batch cache. |
| **Ingestion** | `adhere-behaviour-sync` | The **only** production reader. Pulls the daily delta from the source DB into the cache; runs the drift/retrain and live-health checks; relays webhooks. |
| **Trainer** | `adhere-bf-trainer` (opt-in, GPU) | Offline training job. Builds + validates + registers a model, promotes only if it beats the active one, then exits. |

**Data flow (score path):**

```
              (offline, GPU)                         (online, CPU)
 source DB ──►  sync  ──►  bp_transactions_cache ──►  trainer ──►  model artifacts + registry
                 │                    ▲                                    │
                 │ daily delta        │ read-only                         │ loaded at startup
                 ▼                    │                                    ▼
        drift / health checks         └───────────────┐          ┌──►  API  /score
        (retrain-due, live)                           │          │        │
                                                       │          │        ├─► bp_decision (audit + webhook outbox)
 client ──► POST /score ─────────────────────────────►┼──────────┘        └─► webhook delivery
                                     redis (live velocity) ◄── writes each txn, reads recent window
```

**Two databases, never confused:** the **source** production DB is *read-only* (ingestion only); the
**behaviour store** is the model's own database. The service never writes to production tables.

---

## 3. The scoring pipeline

For each transaction, `/score` performs:

1. **Load baseline** — the customer's learned profile (amount statistics, hour/day histograms, known
   beneficiaries / locations / IP subnets / transaction types) from the active model, plus their recent
   history from the cache **and** the Redis live window.
2. **Feature engineering** — deviation features, e.g. `amt_z` (amount vs the customer's mean/std),
   `amt_over_median`, `amt_over_max` / `above_max`, hour/day rarity, `is_night`, novelty flags
   (new beneficiary / location / country / IP / rare type), velocity (`vel_1m … vel_24h`, `amt_1h_ratio`),
   and graph features (fan-out, shared counterparties).
3. **Three unsupervised detectors** score the feature vector:
   - **Isolation Forest** — density/outlier detector (amount + tabular structure).
   - **Autoencoder** — reconstruction error; a transaction it cannot reconstruct as "normal" scores high.
   - **Graph Neural Network (GNN)** — learns the money-movement graph structure; **amount-blind**.
4. **Ensemble blend — "escalate" (consensus-of-two).** `risk = max(weighted_mean, 2nd-highest detector)`
   with weights `iso 0.35 · ae 0.35 · gnn 0.30`. This prevents a quiet, amount-blind detector from
   **diluting** two detectors that strongly agree (the failure mode of a plain weighted mean), while
   still resisting a single lone spike. Changing the blend requires a retrain (it recalibrates the cuts).
5. **Dynamic tiering** — the risk is bucketed by **percentile cut-offs calibrated on the held-out normal
   distribution at training time** (not hard-coded scores):

   | Zone | Percentile | Active cut | Status |
   |---|---|---|---|
   | Clear | &lt; p95 | risk &lt; 0.8245 | `safe` |
   | Grey / review | p95–p99 | 0.8245 ≤ risk &lt; 0.9207 | `review` |
   | Priority-1 | ≥ p99 | risk ≥ 0.9207 | `unsafe` |
   | Priority-1 (strongest, BF-400) | ≥ p99.9 | risk ≥ 0.9806 | `unsafe` |

6. **Activity code + analyst description** — the primary behavioural finding becomes an activity code
   (e.g. BF-400), and a **dynamically generated** description states the decision, then the evidence
   (amount vs the customer's typical/max, novelty signals, which detectors fired). Wording is
   mathematically consistent with the features — e.g. an amount *equal* to the historical maximum reads
   "**is at** their historical maximum," and only a *strictly greater* amount reads "**exceeds**" it.
7. **Persist + deliver** — the decision is written to `bp_decision` (audit + a transactional webhook
   outbox) and delivered by webhook. `/score` never mutates the learned profile (Practical Rules §8).

**Worked example (verified, customer with historical max ≈ ₦26.26M, a *known* beneficiary — no novelty
flags, so this isolates the amount signal):** ₦1.2M (≈ median) → `safe`; ₦4M–₦10M → `review`; ₦25M+ →
`unsafe` / auto-block, with detector scores Isolation Forest ≈ 0.99 and Autoencoder ≈ 1.00 agreeing and
the escalate blend refusing to let the amount-blind GNN (≈ 0.77) water it down.

---

## 4. Learning & MLOps

**Training population (Practical Rules §1 / §7).** The model learns **only from clean transactions of
eligible customers** — a customer needs ≥ 50 clean transactions and ≥ 30 days of history, and
confirmed-bad transactions (blocked / blacklisted / non-clean) are **never** used for training. The
active model was trained on **5,528 eligible customers**; its unsupervised synthetic-anomaly AUC is
**0.948**.

**Behaviour time-decay (§6) — in the model.** When learning each customer's baseline, every clean
transaction is weighted by an exponential half-life (`BF_DECAY_HALF_LIFE_DAYS = 90`): recent behaviour
counts more, old behaviour fades. This is applied inside the model's feature builder (the `/score`
decision path), not only in a separate statistical layer. The historical *maximum* is intentionally not
decayed (it is a hard "above anything ever seen" ceiling).

**Drift detection & retraining triggers — two-sided, after every pull.** The `sync` service, after each
daily pull, runs two independent checks:

1. **Data-side (retrain-due, §4).** Measures the freshly-pulled cache against the active model's
   training **watermark** and fires if **any**: new transactions ≥ 100, **or** days since training ≥ 30,
   **or** **amount-distribution drift (PSI) ≥ 0.25** (a genuine distribution-shift signal). → Slack
   *"retrain DUE"*, de-duplicated.
2. **Live-side (§9 / §11).** `monitor.check_live()` reads the accumulated `/score` decisions in
   `bp_decision` for the flagged-rate band, **and** — once ≥ 20 analyst verdicts exist — the **real
   precision/recall** from the feedback loop (below).

`/score` itself computes no drift; it only writes decisions. **Retraining is manual and gated** —
`ml.retrain_trigger --run` retrains and promotes **only if the new model beats the active one** on the
synthetic-anomaly AUC; otherwise it stays on the current model. Automation (cron / k8s CronJob) is
written but intentionally disabled.

**Analyst-feedback loop (§11).** `POST /feedback` records the fraud team's confirmed verdict
(`genuine` / `fraud`) for a scored transaction into `bp_decision_feedback`. At the next retrain those
verdicts **override the clean/fraud training split** — a `genuine` verdict forces the transaction into
the clean set (so a repeated false positive stops firing), a `fraud` verdict forces it out (§7). The
same verdicts give `monitor` a **real precision/recall** signal, replacing the flag-rate proxy once
labels accumulate.

**Versioning & rollback (§12).** Every trained model has a unique version and a manifest (data window,
feature version, hardware, validation metrics, watermark). A registry tracks the `active` and
`previous_active` versions; `promote` / `rollback` switch the pointer and the API reloads with no
downtime.

---

## 5. Alignment with the Practical Rules (§1–16)

| § | Rule | Status | Notes |
|---|---|---|---|
| 1 | Clean baseline (min history) | Yes | Clean-only; ≥ 50 txns / ≥ 30 days (target 90 as the window grows). |
| 2 | Minimum data (days/txns/…) | Partial | Days + transactions enforced; login/device/session data unavailable. |
| 3 | Retrain frequency (daily) | Partial | Daily pull (04:00) + daily retrain-due check + the §4 gate are **all automatic**; only the retrain *step* is a **human-gated** trigger (the automated daily cron/CronJob is written but kept disabled by choice, as a safety gate). One config flag from fully-daily. **To reach ✅ (suggested, not yet done):** enable the gated auto-retrain cron — still §4-gated, so it never trains on tiny changes. An ops flag, not new code. |
| 4 | Retrain only on enough data (≥100 txns OR 30 days OR drift) | Yes | Exact OR condition in `retrain_trigger`. |
| 5 | Sliding window (90–180 days) | Yes | `LOOKBACK_MONTHS`. |
| 6 | Time decay | Yes | Applied **in the model** (half-life 90 days). |
| 7 | Never learn confirmed fraud | Yes | Blocked/blacklisted excluded; analyst `fraud` verdicts also excluded. |
| 8 | Stability (no single-event profile shift) | Yes | `/score` never mutates the profile; re-learning is the offline batch retrain. |
| 9 | Drift detection | Partial | Drift **is** detected (PSI amount-distribution drift + live flag-rate + per-transaction novelty flags). The PDF's responses are covered: *gradual → adapt slowly* via the sliding window (§5) + time-decay (§6); *sudden → flag first, learn only after validation* via the stability rule (§8) + analyst verification (§11). **Missing: an explicit gradual-vs-sudden classifier. To reach ✅ (suggested, not yet done):** add one — PSI trend over rolling windows = gradual; a large single-window jump / change-point = sudden — with distinct alerts. |
| 10 | Confidence threshold | Yes | A `confidence_score` is produced. |
| 11 | Retrain after analyst verification | Yes | `POST /feedback` loop wired (training override + real precision); depends on analysts supplying verdicts. |
| 12 | Versioning & rollback | Yes | Registry + promote/rollback. |
| 14 | Retraining triggers | Partial | New-txns / drift / scheduled / performance / **analyst-feedback** done; new-segment / new-pattern / feature-change not yet. |
| 15 | Prevent model/profile poisoning | Yes | Clean-only data, eligibility gate, acceptance gate, analyst-verdict correction. |
| 16 | Profile components (18 listed) | Partial | ~8 implemented (amount, times, day patterns, velocity, beneficiary, channel, partial location/IP); device/network/session/balance/salary/merchant unavailable. |

**On the two "Partial" rows that are choices, not gaps:**

- **§3 (daily retraining)** is Partial *only* because the retrain execution is a human-gated trigger.
  The daily schedule, the retrain-due check and the §4 gate are all built and automatic. **The path to
  ✅ is a single ops flag** — enable the (already-written) gated daily cron; it stays §4-gated so it
  never retrains on tiny changes. We keep it manual for now as a deliberate safety gate.
- **§9 (drift detection)** is Partial *only* because there is no explicit gradual-vs-sudden **classifier**.
  Drift is detected (PSI + novelty), and both PDF responses are already implemented via other rules
  (gradual → §5 window + §6 decay; sudden → §8 stability + §11 verification). **The path to ✅** is to
  add a small classifier (PSI trend = gradual; single-window jump / change-point = sudden) with its own
  alerts. *(Suggested here; not implemented.)*

The remaining Partials (§2, §16) are **data-limited** — device / login / session / balance / merchant
signals are not available in the source feed, so they cannot be implemented until that data exists.

---

## 6. Current limitations (stated plainly)

- **Multi-currency (USD / GBP / EUR) — not modelled per currency.** The model is **currency-blind**
  (amounts are aggregated across currencies), and **no non-NGN customer is eligible** (the data is
  ~99.99% NGN; the few USD rows look like test data). Any non-NGN transaction therefore falls through to
  the **cold-start population path** and is judged on the raw amount *number* against an NGN-derived
  population — it is reacting to the number, not "understanding dollars." Proper support needs a
  per-`(customer, currency)` profile grain, currency normalization on both build and scoring sides, and
  real non-NGN volume so those profiles become eligible. **A design plan exists; it is not implemented.**
- **Geovelocity (impossible-travel) — not implemented.** IP-to-geo distance/time ("logged in from Lagos
  and London 5 minutes apart") is not computed. Location/IP novelty *is* used; geovelocity is future work.
- **Cold-start coverage.** The model knows **5,528 of 32,895 customers (~17%)**; the remaining ~83% are
  **cold-start** and are judged against the **population** baseline rather than a personal one. This is a
  *coverage* limitation (not enough per-customer history yet), not a logic defect — coverage grows as the
  rolling window accumulates history and more customers clear the eligibility gate.
- **Labels / precision maturity.** Detection quality is currently proven by the unsupervised
  synthetic-anomaly AUC and a flag-rate drift proxy. **True precision/recall** becomes available only as
  the **analyst-feedback** loop accumulates verdicts (the mechanism is built and waiting on volume).

---

## 7. Security & data handling

- **Authentication.** `POST /score` (and `/feedback`) require the `X-Adhere-Key` header; standard HTTP
  semantics (401 for missing/invalid + `WWW-Authenticate`). A key is **required** — the API fails fast at
  startup if none is configured. Only the **SHA-256 hash** of a key is stored; the plaintext is shown once.
- **No customer PII in git.** The repository carries **code + schema only**. The trained model
  (which embeds customer identifiers, beneficiary account numbers and IP subnets) and the store dump are
  shipped **out-of-band** (private object store or secure copy), never committed; the store dump also
  exceeds GitHub's 100 MB file limit.
- **Least privilege at the source.** Ingestion is read-only against the production source DB and requires
  the host IP to be allowlisted; the model delivers by webhook and never writes production tables.
- **Auditability.** Every decision is persisted to `bp_decision` with the model version, activity code,
  risk, latency and the analyst explanation; a separate event log records scoring and feedback events.

---

*Generated from the codebase; figures reflect active model `bf-ensemble-2026.08.06-234348`. Operational
runbook (run, deploy, key management, backups) is in `README.md` and `DEPLOYMENT.md`.*
