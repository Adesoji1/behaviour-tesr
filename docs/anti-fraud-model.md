# Anti-fraud model: contract, evidence, and staged plan

Unsupervised ensemble for behavioural fraud detection, built on the behaviour profiles this
service already learns. This records what Adhere confirmed, **what our own data proves**, two
places where the data forces a design change, and the stage-by-stage plan.

**Hard constraint throughout:** the baseline is learned from **active / trusted / clean
customers only** (governance §1). Warming-up customers are still scored, against their peer
group. Confirmed fraud is never learned from (§7).

---

## 1. Confirmed by Adhere

| Topic | Answer |
|---|---|
| **Population scope** | Baseline from **active / clean only**. Warming-up scored against peers. Already how `/score` works. |
| **Customer identity** | The customer record holds **`identifier`** (the value, e.g. `12345678901`) and **`identifier_type`** (the kind, e.g. `bvn`, `national_id`, `kra_pin`). The customer-branch profile holds **`account_numbers`**: a list of `{account_number, bank_code}`, plus a foreign key to the customer. |
| **PROFILING GRAIN (FINAL, per `1.md`)** | **Per CUSTOMER, keyed on the stable customer `identifier`** (BVN / national ID / `kra_pin`, with its `identifier_type`). **Branch and transaction_type are FEATURES, not identity.** All of a customer's activity (transfer, card, airtime, …) rolls into **one** profile. See §2a. |
| **Card / airtime / other types** | **Do NOT build separate profiling per type.** A card can change and BIN+last-4 is not a stable identity, so **never key on card details.** Whatever the type, the activity updates the **same** customer profile, as long as it links to the customer. |
| **Branch** | Not an identity divider. A customer can transact at multiple branches of the same bank; it is the same customer. Branch is kept only as a **context feature**. |
| **AML rules** | **Removed.** The behavioural service does **behavioural anomaly detection only** — it must not do AML rule validation (a separate rules service owns that). Deprecate `rule_engine.py`, `load_rules.py`, `AML_Rules.xlsx`, `bp_rule_definition`, `bp_blacklist` from the scoring path. |
| **Activity codes** | **Not fixed to 450-457.** Define our **own** codes derived from the actual behavioural signals; each response carries a `status`, an `activity_code`, and a human-readable `description`. Two broad outcomes: **safe** vs **unsafe**. |
| **Graph / network data** | **We only see our own customers' transactions**, both sending and receiving. We do **not** see what a counterparty does next, and **there is no richer source** — Adhere is a security tool for banks/fintechs, not a bank. |
| **Result delivery** | The model **calls a webhook on Adhere**. It must **not** write to Adhere's tables; it has **read-only** DB access. |
| **Output shape** | Match `behavioral_analysis`: `transaction_id`, `result` (JSON), `confidence_score`, `triggered_rules`, `recommended_actions`. Extra fields allowed. |
| **`branch_id` vs `bank_code`** | **Different.** `branch_id` = Adhere's branch the transaction came from. `bank_code` = supplied by the bank. |
| **Account-number uniqueness** | Two customers cannot share an account number. |
| **`is_accurate` feedback loop** | **Deferred**, revisit later. |

---

## 2. What our own data proves (measured on 2,525,260 cached transactions)

These were verified directly, not assumed.

**Good news — the identity fields are already in our cache.** We do *not* need a new data
source to re-key. `bp_transactions_cache` already carries `identifier`, `identifier_type_id`,
`bvn`, `customer_email`, `destination_bank_code`, and `transaction_type_normalized`.

**Card and airtime are already flowing in.** 29+ transaction types are present, including
`airtime` (41,857), `card` (272), `card_request` (14), plus `withdrawal` (922,506),
`transfer` (895,102), `vas` (271,937), `virtualaccount` (209,249), `data`, `betting`,
`electricity`, `cabletv`, `crypto`, and others.

**The identity coverage gap (why we did NOT key on the person).**

| Measure | Value |
|---|---|
| Rows with an `identifier` | **66.3%** (1,675,399 of 2,525,260) |
| Rows with `customer_email` | **100%** |
| Distinct `identifier` values (customers) | **32,895** |
| Distinct accounts | 116,106 |
| **Accounts with NO identifier at all** | **80,995 (70% of accounts)** |

Concentrated by branch, i.e. an integration gap rather than a data-model problem:

| branch_id | rows | % with identifier |
|---|---|---|
| 231 | 2,269,710 | 73.7% |
| **232** | **253,859** | **1.0%** |
| 101 | 400 | 0% |
| 23 | 201 | 0% |
| 252 / 249 / 208 / 152 | small | 89-100% |

**`bvn` does not close the gap — it is the same content as `identifier`.** Measured:

| Check | Result |
|---|---|
| Rows where `bvn` present but `identifier` missing (would add coverage) | **0** |
| Rows where both present and the values **differ** | **0** (always identical) |
| Rows with neither | **849,861 (33.7%)** |
| Coverage using **either** field | **66.3%** — identical to `identifier` alone |

BVN is optional, and the clients who don't send it send no identifier at all.

**`customer_email` is unusable as a fallback**, despite 100% fill. On branch 232 one address
covers thousands of unrelated accounts: `Xixapay@gmail.com` **21,566**,
`info@securewaveng.com` 7,478, `admin@ercas.ng` 2,386 — the top three cover ~97% of the
branch. These are platform/aggregator addresses, so keying on email would merge tens of
thousands of unrelated accounts into a single profile.

---

## 2a. DECISION: profile per bank relationship (closes R1)

`1.md` is explicit: **the customer is the entity we profile; transaction type and branch are
attributes of the customer's activity, not separate identities.** A card can change and
BIN+last-4 is not a stable identity, so we never key on card details. A customer can transact
at several branches of the same bank and is still the same customer. Therefore:

**FINAL KEY: the stable customer `identifier` (+ `identifier_type`).** Branch, bank and
transaction_type are **features**, not identity. All of a customer's activity — transfer,
card, airtime, VAS — updates **one** profile.

### The coverage reality we must handle honestly

Keying on the customer identifier is the correct design, but **34% of our historical
transactions carry no identifier** (branch 232 sends it on 1%). So:

| | Effect |
|---|---|
| **Live scoring** | The payload **always** carries `identifier`/`bvn`, so every live transaction resolves to a customer. No gap at inference. |
| **Training population** | We can only build baselines for the **32,895 customers we can identify** (1.67M transactions, ~51 each) — a solid training set. |
| **The unidentifiable 34%** | Cannot be attributed to a customer, so they are **excluded from per-customer training** (never keyed by account as a stand-in, per `1.md`). |
| **Fix at source** | Populating `identifier` in ingestion for branches 232/101/23 would grow the training population. Raised as an item, not a blocker. |

`bvn` adds nothing (0 rows where it is present but `identifier` is absent; the two are always
the same value). `customer_email` is unusable (one platform email on branch 232 covers 21,566
accounts). So the customer identifier is the only sound key.

**"Within the bank" (minor open item):** `1.md` says one profile per customer *within the
bank*, and only ~0.07% of customers (22 of 32,895) appear at more than one branch. For v1 we
key on the `identifier` alone; if a customer genuinely uses two banks we can extend the key to
`(identifier, bank_code)` later. Not blocking.

---

## 3. Design notes the evidence and `1.md` set

### 3a. Layer 1 GNN — kept, scoped to the graph we actually have

`1.md` keeps **GNN embeddings** in the ensemble. We build them on the graph we *can* see: a
bipartite **customer → counterparty** graph from `origin` → `destination` edges, plus the
**internal customer-to-customer** edges (when both sides are our customers). The GNN learns
node embeddings capturing structural patterns — fan-out, shared-counterparty communities,
mule-collector accounts (many of our customers paying one destination), counterparty
novelty/concentration.

**Honest limitation:** we never see what a counterparty does *next* (no richer source), so the
GNN cannot trace **multi-hop laundering chains** (A→B→C). It captures one-hop structure and
shared-counterparty rings, which is real and useful. Embeddings are **computed in batch during
training/retrain** and looked up at inference, so they add **no serving latency** and keep
`/score` within the 500-600 ms SLA. True multi-hop tracing stays a phase-2 item if a richer
network source ever appears.

### 3b. Behavioural signals become FEATURES; AML rules are removed

`1.md`: the behavioural service does **behavioural anomaly detection only** — a separate rules
service owns AML. So the deviation signals we compute (amount vs the customer's own
median/p95/max, unusual hour/day, unusual city/country, new beneficiary, dormancy, velocity)
become **features feeding the ensemble**, not rules that "fire". The AML/policy rules
(blacklist, hard cap, cross-border escalation) are **removed** from the behavioural scoring
path — deprecate `rule_engine.py`, `load_rules.py`, `AML_Rules.xlsx`, `bp_rule_definition`,
`bp_blacklist`. The webhook/outbox, pooling, and ingestion we built all stay.

---

## 4. Input contract (must accept BOTH payload shapes)

The adapter must handle the richer and the simpler payload, and must not require fields the
second one omits.

| Field | Shape A | Shape B | Notes |
|---|---|---|---|
| `transaction_id`, `amount`, `currency` | ✅ | ✅ | already used |
| `transaction_type` | ✅ (`transfer`) | ✖ | payload says `transfer`/`ussd`/`web`, but our data has 29+ raw types → **needs a mapping** (we have `transaction_type_normalized`) |
| `account_type` | ✅ (`individual`/`corporate`) | ✖ | peer grouping |
| `timestamp` | ✅ | ✖ | default to now when absent |
| `origin_account.{account_number, bank_code, account_type}` | ✅ | ✅ (no account_type) | **`bank_code` is new to us on the origin side** |
| `destination_account.{...}` | ✅ | ✅ | we already cache `destination_bank_code` |
| `customer_details.identifier` + `identifier_type` | ✅ (`kra_pin`) | ✖ | **the primary key for the profile** |
| `customer_details.bvn` | ✖ | ✅ | an `identifier` whose type is `bvn` |
| `customer_details.customer_name`, `customer_email` | ✅ | ✅ | email is 100% populated in our data |
| `customer_details.country` | ✅ (`Kenya`) | ✖ | multi-country customers |
| `additional_info.ip_address` | ✅ | ✅ | we learn `known_ip_subnets` |
| `additional_info.location` | ✅ | ✅ | may be a string **or coordinates** (`lat=..,lon=..`) → enables real geo-velocity |
| `additional_info.transaction_description` | ✅ | ✅ | unused today |
| `run_kyc` | ✖ | ✅ | Adhere's concern, not the model's |

**Identity resolution order:** `identifier` (+`identifier_type`) → else `bvn` (as
`identifier_type=bvn`) → else fall back per blocker R1.

---

## 5. Output contract

`1.md`: **do not force the 450-457 codes.** We define our **own** behavioural activity codes
derived from the actual signals the ensemble detects. Every response has a **`status`**
(safe / unsafe), an **`activity_code`**, and a human-readable **`description`**, plus the fields
Adhere needs. Delivered **by webhook** (never a DB write).

### Response fields

```json
{
  "transaction_id": "TXN-12345678",
  "status": "unsafe",                       // safe | unsafe
  "activity_code": "BF-301",                // our behavioural codes (below)
  "description": "Amount ₦250,000 is 41x this customer's usual and to a first-time beneficiary.",
  "risk_score": 0.87,                       // 0..1 ensemble score
  "confidence_score": 0.82,                 // model confidence
  "detection_reason": ["amount_deviation","new_beneficiary"],
  "result": { ...per-detector breakdown, is_cold_start, changes_detected... },
  "triggered_signals": ["amount_deviation","new_beneficiary"],
  "recommended_actions": ["manual_review"],
  "customer_ref": "231:BVN:12345678901",    // profile identity (never raw PII in logs)
  "model_version": "bf-ensemble-2026.07.20-1",
  "timestamp": "2026-07-20T14:00:00Z"
}
```

### Our behavioural activity codes (v1 proposal, derived from the signals)

| Code | Status | Meaning | Driven by |
|---|---|---|---|
| **BF-100** | safe | Consistent with the customer's profile | low ensemble score |
| **BF-110** | safe | Recurring / known pattern for this customer | matches usual amount, time, beneficiary |
| **BF-200** | review | Mild anomaly, monitor | moderate ensemble score, single weak signal |
| **BF-301** | unsafe | Amount anomaly | amount far above the customer's own history (IF/AE) |
| **BF-302** | unsafe | Temporal anomaly | unusual hour / day for this customer |
| **BF-303** | unsafe | Location anomaly | unusual city / country vs the customer's usual |
| **BF-304** | unsafe | New / unusual beneficiary or counterparty structure | first-time beneficiary, graph anomaly |
| **BF-305** | unsafe | Velocity / burst anomaly | recent-window rate spikes |
| **BF-400** | unsafe | Strong multi-signal anomaly, high-risk | top ensemble band, several detectors agree |

Codes are **explainable** (each maps to concrete signals) and **extensible**. Final list to be
confirmed with Adhere; these are behavioural, not AML.

---

## 6. MLOps lifecycle (B7) — endorsed, with three adjustments

The proposed lifecycle is sound and I endorse it:
**Production Model → Monitor → Detect Degradation → Alert → Retrain Candidate → Holdout
Validation → Compare With Production → Deploy If Better → Monitor → Roll Back If Necessary.**
Model versioning, holdout validation before deploy, never overwriting the last known-good
model, rollback, and keeping it in-house on existing infrastructure are all correct and match
governance §3, §12 and workflow steps 10-12.

**Three adjustments, because of where we actually are:**

1. **Lead with drift, not recall.** Precision/recall/F1 need ground-truth labels, and the
   feedback loop is deferred (B6). Until labels exist, "degradation" cannot be measured by
   recall. So v1 monitoring should lead with **operational metrics** (latency, volume, errors,
   model version) plus **score-distribution and feature drift**, and switch on precision/recall
   /F1 only when labels start arriving. Retrain triggers in v1 = **drift + max review
   interval**, not recall.
2. **Start on what we already have, add Prometheus later.** Every decision already lands in
   `bp_decision` with latency and outcome. We can compute the v1 metrics with SQL and push
   alerts to your Slack channel, with no new infrastructure. Add Prometheus/Grafana when the
   dashboards justify it, rather than up front.
3. **Record the model version on every prediction** — add `model_version` (and
   `feature_version`) to `bp_decision`, so any score can be traced to the exact model that
   produced it, which is what makes rollback meaningful.

---

## 7. The staged pipeline (your 12 stages, mapped to this system)

| # | Stage | Status |
|---|---|---|
| 1 | **Data ingestion** | ✅ **Done.** Scheduled, bounded, read-only, resumable. 2.53M transactions cached. |
| 2 | **Data validation** | ✅ Mostly done (sane-amount guard, null/`N/A` account filter). Extend for card/airtime shapes. |
| 3 | **Data cleaning** | ✅ Done (clean-only filter, exclude blocked/blacklisted, prune outside the window). |
| 4 | **Feature engineering** | 🔨 **Building now.** Per-transaction deviation features vs the **customer** baseline (§3b) + graph features (§3a). Versioned. |
| 5 | **GNN training** | 🔨 GNN embeddings on the bipartite customer→counterparty graph (§3a), GPU-aware, batch-computed. |
| 6 | **Model training** | 🔨 Isolation Forest + Autoencoder on clean vectors from **active/trusted customers only** (§1). No labels needed. |
| 7 | **Ensemble scoring** | 🔨 Blend GNN + IF + AE → `risk_score` + `confidence_score`. |
| 8 | **Threshold / decision** | 🔨 Percentile tiers → our **behavioural activity codes** (§5). |
| 9 | **Inference API** | ✅ Foundation exists (`POST /score`, pooled, webhook + outbox). **AML rule engine removed**; replaced by the ensemble + new payload adapter + output format. |
| 10 | **Production monitoring** | ⏳ Per §6 (drift + operational metrics first). |
| 11 | **Feedback / labels** | ⛔ Deferred by Adhere (B6). |
| 12 | **Retraining loop** | ⏳ Per §6, drift-triggered with a max review interval. |

---

## 8. Open items (none block starting feature engineering)

### ✅ R1: identity / profiling grain — **CLOSED** (`1.md`)
**Key on the stable customer `identifier`.** Branch and transaction type are features. One
profile per customer; card/airtime/VAS all roll into it. Training population = the 32,895
identifiable customers (§2a). Branch 232 (aggregator question) is now moot for identity — it is
just context.

### R2 (non-blocking): card identity in the LIVE payload
Cards carry no account number and BIN+last-4 is not stable, so cards are attributed to the
**customer identifier** like everything else (`1.md`). Confirm the live payload always carries
the customer identifier for card transactions.

### R3 (before go-live, not before training): threshold calibration
The ensemble is unsupervised, so the tiers that map a `risk_score` to our behavioural codes
(§5) need calibration and we have no fraud labels. **Proposal:** start with **percentile
tiers** on the score distribution, review with analysts, recalibrate once feedback (B6) is on.
Needs sign-off. *Not blocking:* training produces the score; tiers only label it.

### R4: smaller confirmations
- `transaction_type` mapping: payload `transfer`/`ussd`/`web` vs our 29+ raw types — we use
  `transaction_type_normalized` and keep raw type as a feature.
- Whether improving `identifier` coverage in ingestion (branches 232/101/23) is planned, which
  would grow the training population.

### B6: feedback loop — deferred by Adhere
Revisit when analyst outcomes become available; needed for precision/recall and R3 recalibration.
Until then, evaluation uses the **weak proxy labels** already in our data (`status='blocked'`,
`sender_blacklisted`, `is_blocked`) to produce the confusion-matrix / precision / recall / F1
plots, clearly marked as proxy until real labels arrive.

---

## 9. Build order (in progress)

1. **ML scaffold** — `ml/` package, hardware detection (GPU/CPU), config, artifact dirs,
   requirements, GPU-aware Docker.
2. **Stages 1-3** — load (read-only, customer-keyed) → validate → clean (clean-only, exclude
   fraud, §1/§7).
3. **Stage 4** — customer-baseline deviation features + graph features, versioned.
4. **Stages 5-7** — GNN embeddings + Isolation Forest + Autoencoder (hardware-aware) → ensemble.
5. **Stage 8 + eval** — percentile tiers → behavioural codes; plots (confusion matrix, PR/ROC,
   F1/precision/recall/accuracy, training loss) into `artifacts/plots/`.
6. **Stages 9-12** — hardware-aware inference, model registry/versioning, drift monitoring +
   Slack alerts, holdout validation, rollback.
7. **Remove AML** from the scoring path; `/score` returns the behavioural output via webhook.
