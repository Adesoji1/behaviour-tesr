# How the Customer Behaviour Profile Works

## The system at a glance

```text
          ┌──────────────────────────────────────────────────────────┐
          │   PRODUCTION DATABASE   (we only READ, never write)        │
          │   real transaction history — last 3 months (quarterly)     │
          └───────────────────────────┬──────────────────────────────┘
                                       │   ① INGEST  (pull fresh transactions SAFELY)
                                       │      sync_manager.py — the ONLY thing that
                                       │      reads production. Small bounded chunks,
                                       │      capped, throttled, read-only, resumable.
                                       │      Lands them in a LOCAL CACHE.
                                       ▼
          ┌──────────────────────────────────────────────────────────┐
          │   ② LEARN THE PROFILE   (reads the LOCAL CACHE, not prod)  │
          │   • usual amount  • how often  • usual cities              │
          │   • who they pay  • busy hours • recency-weighted normal   │
          └───────────────────────────┬──────────────────────────────┘
                                       │   ③ SAVE
                                       ▼
   ┌──────────────── PROFILE STORE (PostgreSQL) ──────────────────────────┐
   │   TRANSACTIONS CACHE — the local copy of production we learn from     │
   │   CURRENT profile   — one row per customer, always the latest (online)│
   │   HISTORY log       — dated snapshots, so we can look back  (offline)  │
   │   RULES + blacklist + an audit log of every rule that fires           │
   └───────────────────────────────┬──────────────────────────────────────┘
                                    │   the rules read the CURRENT profile
     ── then, live ──               ▼
   NEW TRANSACTION  ───────▶  ④ RULE ENGINE:  compare the transaction
                                              to that customer's profile
                                    │
                    ┌───────────────┴────────────────┐
                    ▼                                 ▼
             looks normal                      breaks the pattern
              →  allow it                →  RULE FIRES → alert + logged
```

*In one line: we pull real history (safely, into a local cache), learn each customer's
normal, save it, and the rules flag any new transaction that breaks that customer's
pattern.*

### Where the data comes from — and why we keep a local copy

The live production database is the source of truth, but it is also a **live bank
system**: we must not put load on it. So we do **not** query it every time we want to
learn something.

Instead, one small job (`sync_manager.py`) is the **only** thing that ever reads
production. It copies transactions into a **local cache** (`bp_transactions_cache`)
in our own database, and it does that politely:

- it asks for a **small batch at a time** (a few thousand rows), never "give me
  everything";
- it **caps** how much it will take in one run, and **pauses** between batches;
- it is **read-only** — it can never change anything in production;
- it **remembers where it got to**, so if it fails it carries on rather than starting over;
- it **re-checks the last couple of days**, so if a transaction is later marked
  fraudulent/blocked, our copy learns that too;
- it **forgets** anything older than the learning window.

Everything else — learning a profile, retraining a customer, scoring a transaction —
reads that **local cache**, never production. That matters when the service is scaled
out: no matter how many copies of the service run, **production still has exactly one
reader**.

> Why this exists: a single retrain used to query production directly for one
> customer's whole history — that pulled ~63,000 rows in one go and put the live
> database under load. Now it is one small indexed lookup in our own cache.
> Full design + rationale: `ingestionstratimprove.md`.

Our own database is **PostgreSQL** (it used to be MySQL). Production is Postgres, so
using Postgres on both sides means one engine, one driver, one dialect end-to-end —
fewer moving parts and fewer surprises.

---

## 1. The goal in one sentence

Anita mentioned that we want a system to learn a user's behaviour intelligently so instead of hand-writing "normal behaviour" for each customer, **the system learns
each customer's normal behaviour from their own transaction history, saves it to a
database, and the fraud rules then compare every new transaction against that
learned profile.** If a transaction breaks the customer's usual pattern, a rule fires.

> Example: if a customer's profile shows they normally transact in Lagos and Ibadan,
> and a transaction suddenly comes from Kano, the "unusual city" rule fires.
> If the amount is far bigger than they've ever sent, the "high amount" rule fires.

No machine-learning model is involved at this stage ,only the **profile** (the
learned facts about each customer) and the **rules** that read it.

---

## 2. What is a "behaviour profile"?

For each customer, the system reads their past transactions and works out their
"normal", such as:

| What we learn | Example |
|---|---|
| Their usual spend size | average, typical, and biggest amounts they send |
| How often they transact | counts over the last 24 hours, 7, 30, 60, 90 days |
| Who they usually pay | the set of accounts they've sent money to before |
| Where they transact from | usual cities, countries, and internet locations |
| When they transact | which hours of the day, and which days of the week, are normal for them |
| How varied their behaviour is | steady vs. all-over-the-place |
| A recency-weighted "normal" | recent behaviour counts more than old behaviour |

All of this is calculated automatically and saved as one row per customer.

---

## 3. Where the data comes from, and where the profile is saved

```
   PRODUCTION DATABASE                     TEST DATABASE (where we build)
   (read-only, we never write here)        (safe sandbox)

   Real transaction history      ───────▶  Learned behaviour profiles
   (last 3 months / quarterly)             + the fraud rules
                                                   │
                                        A new transaction comes in
                                                   │
                                        Rules read the profile ─▶ fire if unusual
```

- We **read** the real transaction history from production, but we **never write
  anything to production yet because we are still testing for now not to cause any data integrity issues** ,no risk to live systems.
- We **save** the learned profiles into a separate **test database** for now. Once
  approved, the same thing can be copied or replicated into the production database.

**Which transaction records did we use?** The main transaction table
(`monitoring_transactionmonitoring`)  it's the only place with real, per-transaction
detail. We deliberately did **not** use an older pre-built profile file, because it
was built the wrong way from the customer behaviour profile before (that's exactly what we're replacing).

---

## 4. How a customer is identified  (decided)

To group all of a person's transactions together, the system needs one consistent
label per customer — like a name tag on a folder.

**Decision (confirmed):** the label is the **unique bank account number**. As Anita
put it, two people can share a name but never the same account number, so the number
is the reliable identity. It's also present on almost every transaction.

**One important detail we handle:** an account number on its own is *not* actually
unique across the whole system — about **9% of account numbers appear at more than
one institution** (two different banks happening to reuse the same number). So we
tag each account number with **which institution it belongs to**. That way one bank's
"1234567890" can never get mixed up with another bank's "1234567890". The identity is
still the account number — we just record which bank it's at, exactly to avoid the
"same number, different person" problem.

---

## 4b. Brand-new customers with no history (cold start)

A brand-new account has no past transactions, so there is nothing yet to learn its
personal "normal" from. Rather than leave it unprotected (only the fixed limits like
the hard cap), it **borrows the average behaviour of its peers** other accounts at
the **same institution** of the **same type** (e.g. individual vs business).

- So on day one, a new individual account is judged against "what's normal for
  individual customers at this bank": their usual spend size, usual cities, usual
  active hours. If the very first transaction is ₦900m from an unusual place at 3am,
  it still gets flagged.
- As soon as the account builds up its own history, **its personal profile takes
  over** and the borrowed peer baseline is no longer used.

Importantly, this is **plain arithmetic group averages, not machine learning.** No
clustering, no "embeddings." (Those are a later, separate ML stage; see Section 7.)
Every cold-start flag is clearly labelled as based on the peer baseline, so it's never
confused with a flag from the customer's own history.

---

## 4c. Who earns a trusted profile (the anti-poisoning gate)

We do **not** blindly build and trust a profile for every account. That would be
"porous" — a fraudster could quietly run a little fake activity to make the system
accept their behaviour as "normal." So every profile must earn trust first. This
follows the "Practical rules" Anita provided.

**Only learn from clean transactions.** Anything flagged suspicious, blocked, or tied
to a blacklisted party is left out of learning — so bad behaviour never becomes part
of a customer's "normal." *(In the full build this excluded ~414,000 transactions.)*

**Every customer is marked Active or Warming Up.** An account is **Active** (trusted)
only if it passes all three of the "Practical rules" §1 conditions:

- **at least 90 days** as a customer (measured over their *whole* lifetime; their
  *normal* is still learned from the recent 90 days),
- **at least 100 clean transactions**, and
- **no confirmed fraud cases** — a customer with even one confirmed-fraud transaction
  is never trusted on their own baseline.

Everything else is **Warming Up** and is judged against its peers, not its own thin
history — so fake activity can never quietly become a trusted baseline. All three
numbers are settings the team can tune (`BP_MIN_TENURE_DAYS`, `BP_MIN_TXNS`,
`BP_ELIGIBLE_MAX_FRAUD_TXNS`).

**Every profile has a confidence score (0–100)** based on how much history it has, how
steady the behaviour is, and how complete the data is. Below the trust cutoff, the
system treats the account as still-learning and falls back to the peer comparison.

**The gate is checked every single time we score — not just when the profile was
built.** A profile's Active/Warming-Up label is decided when it is *built*. If we later
tighten the policy, or a customer's transaction is later confirmed fraudulent, a profile
built under the old rules would otherwise keep its stale "trusted" label until it
happened to be rebuilt. So we re-check the full gate on **every transaction**: any
change takes effect immediately, and the safe direction (fall back to peers) wins.
Every `/score` and `/customer` response says exactly why, e.g.
`"trust_reason": "peer_baseline (§1 clean txns 61 < 100)"`.

*(We gate on account age, transaction count and confirmed fraud. Login-events and device
checks from the rules are noted for later — that data isn't in our transaction table
yet.)*

---

## 5. How it stays up to date, and stays fast

Three things make this practical to run for real. All three are built and tested.

**a) Two copies of each profile  so we're always current but never lose history.**

- A **"current" copy**: one row per customer holding their latest learned normal.
  This is what the rules read, and it is **overwritten on each refresh**, so it is
  always up to date. *(the "online" store)*
- A **"history" copy**: every refresh is also saved as a dated snapshot, so we can
  always look back at how a customer's behaviour changed over time. *(the "offline"
  history log)*

**b) Per-customer, event-driven refresh — so the profile follows changing habits.**

- The profile is **not** set once and forgotten, and there is **no nightly sweep**.
  Instead, each customer's profile is refreshed **only when their own behaviour
  warrants it** — the moment a transaction of theirs meets a trigger: **≥100 new
  transactions, or 30 days passed, or sustained drift** (repeated changes vs their
  own pattern). This is the "streaming feature store" model.
- It rides on the transaction the app already sends (via the microservice, Section 8b)
  — **no cron/scheduler at all**, and different customers refresh independently and
  in parallel. Because recent behaviour is weighted more heavily, the profile adapts
  smoothly when someone genuinely changes (moves city, starts spending more).

**c) Fast look-ups ,so it stays quick even as data grows.**

- The "current" profile copy is indexed by the customer key, so fetching one
  customer's profile is effectively instant.
- The raw transaction history is already indexed by **account + date**, so pulling
  "this customer's recent activity" stays fast even at millions of rows — it never
  has to scan the whole table.

---

## 6. How the rules use the profile

I loaded the full list of **32 AML rules**. When a transaction arrives, the system
looks up that customer's profile and checks the transaction against it. Examples of
rules that fire off the profile:

- **Unusual city / country** — location not in their usual places.
- **Unusually high amount** — far above their normal or biggest-ever amount.
- **First-time beneficiary** — paying an account they've never paid before.
- **Unusual time of day** — active at an hour they're normally not.
- **Dormant account waking up** — no activity for a long time, then suddenly active.
- **Blacklisted party** — sender or receiver is on the blacklist.
- **Hard limits** — e.g. very large single transfers.

Every time a rule fires, we log it, so there's a clear audit trail.

**Each client sets their own limits.** The numbers behind these rules (what counts
as "too high", how many days is "dormant", the hard cap) are **not fixed** — every
institution can set their own, because a tier-1, tier-2 and tier-3 bank need
different limits. The system ships with sensible defaults, and any client can
override any limit (or switch a rule off entirely) without touching code. A client
can even run the same rule at a different threshold from everyone else.

---

## 6b. Catching bursts as they happen (live velocity)

The nightly profile knows a customer's *long-term* normal, but it's recalculated
only once a day — so on its own it can't see something unfolding **right now**. For
that we also do a **live look-up** of the last few minutes/hours at the moment a
transaction arrives:

- **1 minute** — *card-testing / bots*: 5 transactions in 40 seconds is a machine,
  not a person.
- **15 minutes** — *rapid draining*: a stolen account being emptied fast.
- **1 hour** — *money-mule fan-out*: paying many brand-new accounts in an hour, or
  transacting from several countries in an hour (physically impossible travel).

This is the fast half of the design working alongside the daily profile: the profile
gives the long-term "normal", the live look-up catches the sudden burst. Both were
tested (a simulated 6-transactions-in-a-minute burst correctly fires the rule).

---

## 7. What is built and tested vs. what comes next

** Done and tested — now running on the FULL quarter: 2.67 million transactions → 99,254 customer profiles (after learning from clean transactions only):**
- Reads real history (read-only), learns profiles, saves them to the test database.
- All the "normal behaviour" measures above.
- **Anti-poisoning gate** (Section 4c): learn from clean only; Active vs Warming-Up;
  confidence score; untrusted accounts judged by peers. ~6,700 Active / ~92,600 Warming Up.
- **Event-driven per-customer refresh** (no cron) — a customer is retrained when they
  hit ≥100 new txns / 30 days / sustained drift, plus a history log. Shipped as a
  **microservice** (`service.py`) the app calls per transaction; two customers can
  retrain at once (verified).
- Per-client thresholds (each institution can set its own limits).
- **Cold-start peer baseline** for brand-new accounts with no history (Section 4b).
- **Live velocity** rules for real-time bursts (Section 6b).
- The 32 rules loaded, plus the rule engine — verified: a normal transaction passes,
  an abnormal one fires the matching rules, all logged for audit.
- A one-command **end-to-end demo** (`demo_end_to_end.sh`) that shows every stage.
- A one-command **end-to-end demo** (`demo_end_to_end.sh`) that narrates every stage.

**Not done yet (a later, separate stage):**
- Any machine-learning model — including behavioural "embeddings" and peer-group
  clustering. These consume the profiles we're already producing; nothing is wasted.

---

## 8. Decisions (confirmed) and how each is now handled

| Question | Decision | How it's handled |
|---|---|---|
| **Who is "one customer"?** | The **unique bank account number** (a name can repeat, a number can't). | Profiles are keyed on the account number, tagged with its institution so no two banks collide (see Section 4). |
| **Whose thresholds do we use?** | **Each client sets their own** — tier-1/2/3 banks differ. | Every rule limit is client-configurable, with sensible defaults; a client can override any limit or switch a rule off (see Section 6). |
| **How far back do we learn?** | **Quarterly (3 months)**, not 6. | The learning window is now 3 months across the whole pipeline. |


## 9. What to do for a brand new user

Why embeddings aren't needed at this (profile-building) stage
The doc's "Vector Embeddings" means: use a neural network (autoencoder / RNN) to squeeze a customer's history into a dense list of numbers — a "signature." "Peer-group clustering" means K-Means/DBSCAN to group similar users. Both are machine learning / deep learning. This stage is explicitly "profile + rules, no ML yet," so they're out of scope by definition.

But there's a deeper, practical reason, not just scope:

The rules can't use an embedding. Our rules fire on explicit, readable facts — "city not in usual cities", "amount above their biggest ever", "first-time beneficiary." An embedding is an opaque list of ~128 numbers; you can't write an explainable, audit-friendly AML rule against it. For rules and compliance, the plain fields we're building are actually better than an embedding.

An embedding has nothing to feed yet. Embeddings are only useful as input to a model (the anomaly-detector / classifier in the architecture diagram). We have no model at this stage, so an embedding would sit there consumed by nothing.

Building one now = training a neural net now — which is exactly the ML work the scope says to defer.

The one part of "Baseline Initialization" that is useful now (and needs no ML)
The doc bundles two ideas under that heading. One of them we can do now, cheaply:



**Note**

Data-quality finding I noticed for Eric: the production monitoring_transactionmonitoring table contains 10 transactions with impossible amounts in the last quarter ,the largest is ₦300 quadrillion (3×10¹⁷), which is ~1,500× Nigeria's entire GDP. These are clearly test/garbage rows. I've added a sanity guard (default: ignore anything above ₦10 trillion) so they can't poison a customer's "biggest ever" or the peer baselines. Worth someone on the data side checking why those rows exist in production.


Build running. Here's a quick summary of the two real-world issues the full run surfaced and how I fixed each — worth knowing since they'd have hit production too:

Impossible amounts in production data — 23 rows in the quarter with amounts up to ₦300 quadrillion (bad/test data). Added a sanity guard (ignore > ₦10 trillion) so they can't poison any profile, and widened the money columns.
Test-DB storage cap — the offline history log was writing a full ~2KB JSON snapshot per account per run; two failed runs bloated the tablespace and briefly flipped the Aiven server to read-only. Fixed by making the history log lightweight (scalar timeline only; full JSON is now an opt-in production flag), reclaiming the bloat (345MB → 0.6MB), and committing in chunks so no single huge transaction can bloat on rollback.

---


## What happens next

So far, everything has been proven on a **small test slice**: 150,000 real
transactions, which the system turned into 9,692 customer profiles, and the rules
fired correctly against them. That was to show the machinery works end to end.

**Now that the three decisions are in**, the next step is to run the exact same
process over the **full quarter (last 3 months) of history — about 2.7 million
transactions, producing profiles for every one of the ~125,000 accounts.** That's
what "at real scale" means: the complete, live-sized dataset instead of a sample.

*Run `python prove_it_works.py` for a ready-to-run proof — it shows, for a real
customer, what the system learned and which rules fired, as evidence this already
works.*

---

## Appendix — "Practical rules" traceability (nothing forgotten)

Each rule Anita provided, mapped to exactly where it lives in the code, with an
honest status. **Legend:** ✅ done · ◑ partial · ⏳ later · ⛔ no data yet.

| Rule (PDF) | Status | Where in the code |
|---|---|---|
| §1 Clean baseline (legit only, exclude fraud/corrupt/dupes, min history) | ✅ | `build_profiles.py` → impossible-amount guard, "remove duplicated transactions", "CLEAN BASELINE" filter; min-history = the eligibility gate |
| §2 Minimum data before building → Warming Up | ◑ | `build_profiles.py` `is_active` gate + `config.ELIGIBLE_MIN_TENURE_DAYS/TXNS`. Days + transactions enforced; **login/devices/locations not** (no data) |
| §3 Retraining frequency | ✅ (reworked) | Segments dropped per CTO; retraining is **per-customer, event-driven** (`retrain.py` + `service.py`), triggered by the customer's own activity — no cron |
| §4 Retrain only when enough new data | ✅ | Event-driven `maybe_retrain()` (`retrain.py`) — retrain iff ≥100 new txns **or** 30 days **or** sustained drift; called per transaction by the service |
| §5 Sliding window (90/180/365d) | ✅ | `config.LOOKBACK_MONTHS`; `in_30d/in_90d` flags in `build_profiles.py` |
| §6 Forgetting / time decay (1.0/0.8/0.5/0.2) | ✅ | `config.DECAY_HALF_LIFE_DAYS=90`; `w_decay=0.5**(age_days/HALF_LIFE)` in `build_profiles.py` |
| §7 Never learn confirmed fraud | ◑ | Clean filter excludes suspicious/blocked/blacklisted; "rebuild if polluted" = idempotent nightly rebuild + stale cleanup. Analyst-confirmed source not wired |
| §8 Behaviour stability (repeated evidence) | ✅ | `config.MIN_PATTERN_OBS` — a city/country/merchant must be seen ≥N times before it enters the "usual" set (`topn_map` in `build_profiles.py`); plus nightly-batch-only updates + scoring `min_history` |
| §9 Drift detection (basic) | ✅ | `detect_drift()` in `build_profiles.py` — flags "sudden" drift (recency-weighted amount jump > `DRIFT_AMOUNT_PCT`, changed main city, or new country) into `drift_status`/`drift_reason`. *Statistical/ML drift = later.* |
| §10 Confidence threshold | ✅ | `compute_confidence()` in `build_profiles.py`; enforced by `rule_engine.py` "TRUST GATE" + `config.CONFIDENCE_TRUST_THRESHOLD` |
| §11 Retrain only after analyst verification | ⏳ | Conservative clean proxy; no analyst-label feed |
| §12 Version profiles + rollback | ✅ | `profile_version`, `bp_profile_history`, `bp_build_run`; **`rollback.py`** reverts a profile or a whole run to an earlier version |
| §14 Retraining triggers | ◑ | Now: schedule + enough-new-data + drift triggers (`build_profiles.py`). Still later: analyst-feedback & model-performance triggers |
| §15 Prevent model poisoning | ✅ | Trust gate + learn-clean + eligibility + nightly-only updates (`rule_engine.py` / `build_profiles.py`).In House Analyst-approval step not built |
| §16 Profile components | ◑ | Have amount/type/merchant/hour/**day-of-week**/location/IP/velocity/beneficiary/channel (everything our data supports). Missing only where we have **no data**: device/network/browser/login/session/balance/salary |

**Demo correctness:** every capability the demo shows is fully implemented; the
remaining gaps are all either "no data yet" or "later/ML stage" — every profile
component our data *can* support (including day-of-week) is now built. None of the
gaps affect what the demo demonstrates.



recency-weighted avg = sum(weight × amount) / sum(weight)

where  weight = 0.5 ^ (age_in_days / 90)
