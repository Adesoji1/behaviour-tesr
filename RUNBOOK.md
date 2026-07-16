# Behaviour-Profile Microservice — End-to-End Runbook

Everything you need to **run, demo and test** the service. Follow it top to bottom.

> **Two documents to share:** this one (run + test) and [`SERVICE.md`](SERVICE.md)
> (integration + DevOps, see its §6). Background: [`README.md`](README.md),
> [`howitworks.md`](howitworks.md), and the ingestion design in
> [`ingestionstratimprove.md`](ingestionstratimprove.md).

---

## TL;DR — the whole demo in six commands

Already set up? This is the entire thing. Details follow.

```bash
cd behaviour_profile_build
docker compose up -d --build                            # 1. start (Postgres + service)
curl localhost:8080/health                              # 2. ready?

docker compose logs -f                                  # 3. IN A SECOND TERMINAL: the story

curl 'localhost:8080/customers?trusted=true&limit=3'    # 4. pick a customer
curl 'localhost:8080/demo?entity_key=231:7064038214'    # 5. run all 10 stages for THEM
curl 'localhost:8080/demo?entity_key=232:9077799070'    #    ...now an UNTRUSTED one

docker compose down                                     # 6. stop (data survives)
```

**First run only:** load the profiles — `docker compose run --rm --no-deps
behaviour-profile python migrate_mysql_to_pg.py` ([§4](#4-load-the-profiles-one-time)).

⚠️ **Never `docker compose down -v`.** `-v` deletes the volume and all 99,254 profiles.

Prefer clicking? <http://localhost:8080/docs> — "Try it out" on any endpoint.

---

## 1. What you are running (60-second model)

The service learns each customer's normal behaviour, stores it, and scores every
new transaction against it — firing AML rules when a transaction breaks their pattern.

```text
 PRODUCTION Postgres (READ-ONLY)
            │
            │  sync_manager.py  ← THE ONLY THING THAT READS PRODUCTION
            │  keyset-paged · chunked · capped · throttled · resumable
            ▼
 ┌──────────────────────────────────────────────┐
 │  PROFILE STORE — PostgreSQL 17 (container)   │
 │   bp_transactions_cache   ← local copy of prod txns
 │   bp_user_behaviour_profile ← the learned profiles (99,254)
 │   bp_rule_definition / bp_event_log / ...    │
 └──────────────────────────────────────────────┘
            ▲                      ▲
            │ learn/retrain        │ score
     (reads the CACHE,        POST /score → {decision, fired_rules}
      never production)
```

**The key property:** only the sync job reads production. Every retrain and every
score reads the local store. So production sees **one** reader no matter how many
service replicas run.

**Two databases, don't mix them up:**

- **Production Postgres** — the transaction *source*. READ-ONLY. Remote (DigitalOcean).
- **Profile store** — PostgreSQL 17 running as the `db` container. Everything we write.
  (This replaced the old MySQL store; the source was already Postgres, so we now run
  one engine end-to-end.)

## 2. Prerequisites

- **Docker** + the Compose plugin. That's it for the Docker path.
- A **`.env`** file: `cp .env.example .env`, then fill it in. It must contain:
  - `PROD_PG_*` — production (read-only source). Point at a **read replica** if one exists.
  - `STORE_PG_*` — the profile store. **`STORE_PG_PASSWORD` is required** (compose
    refuses to start without it).
  - `BP_ALLOW_PROD_PULL` — the production safety switch (see §5).
  - `BP_SYNC_*` — the safe-ingestion dials (see §5).
- **Production access is IP-allowlisted.** Until the DB engineer adds your public IP to
  the DigitalOcean **Trusted Sources**, any live pull times out (see [§9.1](#91-production-postgres-times-out)).
  **Everything except the live sync works without it.**

> `ca.pem` is **no longer needed** — it was the MySQL TLS cert and the MySQL store is gone.

## 3. Start it

```bash
cd behaviour_profile_build
cp .env.example .env          # then edit .env (STORE_PG_PASSWORD is required)

docker compose up -d --build  # starts BOTH the Postgres store and the service
docker compose logs -f        # watch it (Ctrl+C stops following, not the service)
```

Compose starts the store first and waits for it to be **healthy** before the service
starts. The schema (`schema_pg.sql`) is applied automatically on first init.

Ready when:

```bash
curl localhost:8080/health    # {"status":"ok",...}
```

**Stop:** `docker compose down` — the data survives (named volume `behaviour_pgdata17`).
⚠️ **Never `docker compose down -v`** — `-v` deletes the volume and your profiles.

## 4. Load the profiles (one-time)

The store starts empty. Two ways to fill it — **you almost certainly want (a)**.

**(a) Migrate the already-learned profiles from the old MySQL store** — *no production
access needed, takes ~1 minute:*

```bash
docker compose run --rm --no-deps behaviour-profile python migrate_mysql_to_pg.py
```

Copies the ~99k profiles + rules + peer baselines store-to-store and verifies row
counts. **Production is never touched.** Safe to re-run (everything upserts).

**(b) Rebuild from scratch** — only if you have no MySQL and full prod access:

```bash
docker compose exec behaviour-profile python sync_manager.py --max-rows 0   # fill the cache
docker compose exec behaviour-profile python build_profiles.py --in data/transactions.csv
docker compose exec behaviour-profile python load_rules.py
```

Confirm either way:

```bash
curl localhost:8080/stats     # profiles: 99254, active/warming_up split, rules: 32
```

## 5. Pulling fresh live data — safely

This is the part that must never hammer the live DB. The sync job is the only
process that reads production, and every read is bounded:

| Protection | Env var | Default | What it does |
| --- | --- | --- | --- |
| Keyset pagination | — | always | `WHERE id > :last ORDER BY id LIMIT :n` — bounded, index-friendly. Never `OFFSET`. |
| Chunk size | `BP_SYNC_CHUNK_SIZE` | 5000 | rows per bounded query |
| Row cap per run | `BP_SYNC_MAX_ROWS` | 50000 | a run can't drag the whole table down (0 = uncapped) |
| Throttle | `BP_SYNC_SLEEP_SECONDS` | 0.2 | sleep between chunks — **the dial to be gentler** |
| Statement timeout | `BP_SYNC_STATEMENT_TIMEOUT_MS` | 30000 | the server kills a runaway query |
| Refresh window | `BP_SYNC_REFRESH_DAYS` | 2 | re-pulls recent days so `clean → blocked` flips are corrected |
| Prune | `BP_SYNC_PRUNE` | 1 | drops cached rows older than the learning window |
| Read-only | — | always | `SET default_transaction_read_only = on` |
| Resume | — | always | watermark advances only **after** a chunk commits |
| **Master switch** | `BP_ALLOW_PROD_PULL` | 1 | **`0` = STOP every live read**, service serves from cache |

Run it:

```bash
# from the CLI (shows a progress bar + per-chunk logs)
docker compose exec behaviour-profile python sync_manager.py
docker compose exec behaviour-profile python sync_manager.py --max-rows 2000   # small demo pull
docker compose exec behaviour-profile python sync_manager.py --status          # where the watermark sits

# or over HTTP
curl -X POST 'localhost:8080/sync?max_rows=2000'
curl localhost:8080/sync/status
```

Every chunk is logged, so you can *show* that the pull is light:

```text
sync START from id>0 window=3months chunk=2000 cap=20000 throttle=0.25s refresh=2d
sync[new] chunk rows=2000 fetch=412ms last_id=8451233 total=2000
sync[new] chunk rows=2000 fetch=388ms last_id=8453301 total=4000
```

> **In production** the sync runs as a **separate scheduled job** (cron / k8s CronJob),
> not inside the request path. `POST /sync` exists so the demo can show it live.

## 6. The demo — a script you can follow live

> Everything below runs **without production access**. Only stage 1 (and, until the
> cache has been filled once, stage 10) needs it — and they fail *cleanly and
> explain themselves*, which is itself worth showing.

### Step 0 — open two terminals

**Terminal A** — the story, narrated live. Leave this running and talk over it:

```bash
cd behaviour_profile_build
docker compose logs -f
```

**Terminal B** — where you drive it.

### Step 1 — pick a customer

Every row comes with a ready-made demo URL, so nobody has to know an account number:

```bash
curl 'localhost:8080/customers?trusted=true&limit=3'    # ones the engine TRUSTS
curl 'localhost:8080/customers?trusted=false&limit=3'   # ones judged against PEERS
curl 'localhost:8080/customers?q=OLABUNMI'              # search by name / account no
```

```text
231:7064038214   NWANCHOR CHUKWUDI   trusted=True    /demo?entity_key=231:7064038214
232:9077799070   capable             trusted=False   /demo?entity_key=232:9077799070
                 └─ why: peer_baseline (warming_up)
```

### Step 2 — run the full story for that one person

```bash
curl 'localhost:8080/demo?entity_key=231:7064038214'   # or omit entity_key to auto-pick
```

### Compose the transaction yourself — and prove nothing is hard-coded

Open <http://localhost:8080/docs> → `/demo` → **Try it out**. Every field is optional
and documented there with an example. **Anything you leave blank is filled from THAT
CUSTOMER'S OWN profile**, read from Postgres — never from a fixed script.

| Field | Means | Example | Left blank → |
| --- | --- | --- | --- |
| `entity_key` | WHO | `231:7064038214` | auto-picks a trusted customer |
| `amount` | HOW MUCH (NGN) | `5000` | their own median (stage 6) / 10× biggest-ever (stage 7) |
| `city` | WHERE | `Pyongyang` | a city they use (stage 6) / one they never use (stage 7) |
| `destination_country` | TO WHERE | `KP` | `NG` (stage 6) / `KP` (stage 7) |
| `hour` | WHAT TIME (0–23) | `3` | their busiest hour (stage 6) / an hour they never use (stage 7) |
| `destination_account_no` | TO WHOM | `NEW-ACCT-9999` | a beneficiary they pay (stage 6) / a new account (stage 7) |

**There is no blocklist of "bad cities".** A city is unusual only because it is absent
from **that customer's** learned `usual_cities`. Kano is perfectly normal for a Kano
customer and unusual for a Lagos one. Prove it — send a **normal amount** with a
**foreign city**:

```bash
curl 'localhost:8080/demo?entity_key=231:7064038214&amount=5000&city=Pyongyang'
```

Stage 6 — *an ordinary day*, their own median amount — now flags, and **only** on the city:

```text
outcome: review — 1 rule(s) fired: detect_unusual_city
transaction_sent.city = {
  "value": "Pyongyang",
  "source": "you supplied",
  "their_usual_cities": ["Itire St"],          <- read from Postgres, not a constant
  "is_one_of_their_usual_cities": false        <- THIS is why the rule fired
}
```

The amount fired nothing (it is their median). Swap `city=Itire St` and the rule goes
silent. **The engine reacts to your input, judged against their learned history.**

Every stage returns **`transaction_sent`** — each field, where the value came from
(`you supplied` / `from their own profile` / `chosen to break their pattern`), and how
it compares to that customer's real data. You see the comparison, not just the verdict.

### Send your own amount — one field, like a real customer

A customer never says *"this one is abnormal"*. **They just pay an amount, and the
system decides.** So there is **one** `amount` field. Use it when someone asks
*"what if he sends 2 million?"* — type it in:

```bash
curl 'localhost:8080/demo?entity_key=231:7064038214&amount=250000'
```

That **same** amount is then put through **two contexts**, and the rules judge each:

| Stage | Context | Question it answers |
| --- | --- | --- |
| 6 | Their **usual** context — a city they use, their busiest hour, a beneficiary they already pay | Is the **AMOUNT** wrong for this person? |
| 7 | A **suspicious** context — a city they've never used, cross-border, an hour they never transact in, a brand-new account | Is the **CONTEXT** wrong? |

**This is the lesson of the pair** — send their own median, `amount=5000`:

```text
stage 6 (usual context)      -> allow — no rules fired
stage 7 (suspicious context) -> review — 4 rule(s) fired
why: the amount is IDENTICAL to stage 6 — only the context changed. Flagged on the
     CONTEXT, NOT the amount. 5,000 NGN is only 0.01x their biggest-ever, so no amount
     rule fired — that same figure passed in stage 6. What changed is where and how it
     was sent: Pyongyang, cross-border NG->KP, brand-new beneficiary, 3am.
```

**Same money. Same person. Different verdict.** An ordinary amount is still flagged when
the surroundings break their pattern.

Now send `amount=2000000000` and stage 6 flips too — the amount alone is enough:

```text
stage 6 (usual context) -> review — 4 rule(s) fired
why: the surroundings are entirely normal for them, so this fired on the AMOUNT:
     2,000,000,000 NGN is 400000.0x their median (5,000) and ABOVE their biggest-ever
     (814,000). The customer just paid an amount — the engine decided it was wrong for
     THIS person. Lower it towards their median to see it pass.
```

Omit `amount` and both stages derive from the customer's own profile (their median for
stage 6; 10× their biggest-ever and above the AML hard cap for stage 7).

Each stage reports `amount_used_ngn`, `amount_source`, `their_median_ngn`,
`their_biggest_ever_ngn`, `times_their_median` and `changed_from_stage_6` — so the
number is always judged against what that person actually does, and the demo says
plainly whether the **amount** or the **context** did the flagging.

Watch Terminal A narrate all 10 stages as it happens. Or use
<http://localhost:8080/docs> → `/demo` → "Try it out" (`entity_key` is a field).

**All 10 stages are about the ONE customer you chose** — nothing invented, no other
account dragged in.

### Step 3 — the money shot: run it again for an UNTRUSTED customer

```bash
curl 'localhost:8080/demo?entity_key=232:9077799070'
```

Stage 8 flips. This is the anti-poisoning gate deciding, per person, on real data:

```text
# trusted customer
result: NOT a cold start — NWANCHOR CHUKWUDI is judged on their OWN learned profile
why   : passes every §1 condition (tenure 118d >= 90, 1930 clean txns >= 100,
        0 confirmed fraud <= 0, confidence 73 >= 60)

# untrusted customer
result: COLD START — capable is judged against their PEER GROUP, not their own history
why   : peer_baseline (warming_up). Failing conditions: ['tenure_days']
        (76 days < 90 — even though they have 4,770 clean txns and confidence 77)
```

That second one is the point: **one failing condition is enough to deny trust.**

### Does the demo exercise the real `/score` path? (honest answer)

The production flow is:

```text
Transaction Engine -> POST /score -> get profile from Postgres -> compare behaviour
                                  -> decide risk -> retrain if required -> return decision
```

| Step | In `POST /score` | In `/demo` |
| --- | --- | --- |
| Get profile from Postgres | ✅ indexed read on `entity_key` | ✅ same read |
| Apply the §1 trust gate | ✅ `profile_is_trusted()` | ✅ **the same helper** |
| Compare behaviour / fire rules | ✅ `RuleEngine.evaluate()` | ✅ **the same engine** |
| Decide risk (`allow` / `review`) | ✅ | ✅ |
| Bump counters (`txns_since_build`, drift) | ✅ | ❌ **skipped on purpose** |
| Write `bp_event_log` row | ✅ | ❌ **skipped on purpose** |
| Retrain if required | ✅ `maybe_retrain()` | ⚠️ **stage 10 does it explicitly** |
| Return decision | ✅ | ✅ |

**Why the three gaps are deliberate:** the demo is meant to be run repeatedly. If each
stage bumped `txns_since_build`, clicking the demo 100 times would fake a retrain
trigger and pollute that customer's real state. So the demo shares the *decision* path
exactly — same profile read, same gate, same rules — and leaves the *side effects* to
the real hook. Stage 10 still performs a real retrain, visibly and once.

**To exercise the complete path including side effects, call the real hook** —
`POST /score` ([§7](#7-test-it-endpoint-by-endpoint)). It returns the decision *and*
the `retrain` verdict, and writes the audit row.

### What each stage shows

| # | Stage | Shows |
| --- | --- | --- |
| 1 | **Pull FRESH data from production — safely** | chunked / capped / throttled ingestion. Skips cleanly when the safety switch is off |
| 2 | Ingestion state | the watermark + what the local cache holds |
| 3 | Configuration | every env-driven knob (nothing hard-coded) |
| 4 | What the system has learned | ~99,254 profiles, Active vs Warming-Up |
| 5 | The customer under test | who the rest of the demo is about, and whether they're trusted |
| 6 | **The customer pays — in their USUAL context** | Is the AMOUNT wrong for this person? Their own city/hour/known beneficiary, so only the amount is in question |
| 7 | **The SAME amount — in a SUSPICIOUS context** | Is the CONTEXT wrong? Identical amount, but a city they've never used, cross-border, an hour they never use, a new account |
| 8 | **COLD START — verdict for THIS customer** | own history or peer group? Every §1 condition with its value, and exactly which fail |
| 9 | LIVE velocity | a burst caught by the velocity rules |
| 10 | Event-driven retrain | recomputed **from the cache** (production untouched), version bumps |

### Every stage answers three questions

In the JSON **and** in the logs:

| Field | Means |
| --- | --- |
| `description` | what this stage demonstrates |
| `outcome` | what actually happened (`review — 8 rule(s) fired: ...`) |
| `why` | **why** it happened — the reason, not just the value |

So you can demo straight from `docker compose logs -f`:

```text
demo 7/10 | Score an ABNORMAL transaction for this SAME customer
demo 7/10 |   what : NWANCHOR CHUKWUDI: 2,000,000,000 NGN — 10x their biggest-ever ...
demo 7/10 |   result: review — 8 rule(s) fired: block_above_hard_cap, flag_cross_border_transfer, ...
demo 7/10 |   why  : same customer as stage 6, but this breaks their pattern on several axes
                     at once: 10x their biggest-ever and over the AML hard cap, Pyongyang is
                     a city they have never used, cross-border NG->KP, new beneficiary, and
                     3am is an hour they have never transacted in
```

### Seeing the writes

Every save to Postgres names its destination, so "it learnt and saved it" is never
just a claim:

```text
DB WRITE ok | entity=231:7064038214 -> postgres://db:5432/behaviour | tables:
             bp_user_behaviour_profile (upsert, version 7), bp_profile_history (append)
sync[new] chunk rows=2000 ... | DB WRITE ok -> postgres://db:5432/behaviour
             table bp_transactions_cache (upsert on production id)
```

### What is honest about this demo

Say these out loud rather than hiding them — they are the design working:

- **Stage 1 skipping is a feature.** `BP_ALLOW_PROD_PULL=0` means the service *refuses*
  to touch production. That switch exists because an earlier unbounded query loaded the
  live DB (see [`ingestionstratimprove.md`](ingestionstratimprove.md) §1).
- **Stage 10 says `cache_not_populated`** until a sync has run. That is a statement about
  the **cache**, not the customer — see [§7](#why-a-retrain-was-skipped--read-the-reason-carefully).
- **Some customers' location data is a placeholder** (`{"-", "N/A"}`) in production, so
  the unusual-city rule carries no signal for them. The demo **says so** instead of
  pretending. A real data-quality issue in the source, not the model.

## 7. Test it endpoint by endpoint

```bash
curl localhost:8080/health
curl localhost:8080/stats
curl 'localhost:8080/customers?trusted=true'    # real customers you can copy-paste
```

`POST /score` is the hook adhere calls per transaction. The examples below use
**231:7064038214** (a customer who passes the trust gate) and are built from **their
own profile**: their median amount (5,000), a city they use (`Itire St`), a beneficiary
they already pay, at their busiest hour (19:00).

```bash
# NORMAL -> allow, judged on their OWN profile, no rules fired
curl -X POST localhost:8080/score -H 'Content-Type: application/json' -d "{
  \"branch_id\":231, \"origin_account_no\":\"7064038214\", \"amount\":5000, \"currency\":\"NGN\",
  \"destination_account_no\":\"0018334290\", \"customer_location\":\"street, Itire St, Lagos\",
  \"origin_country\":\"NG\", \"destination_country\":\"NG\", \"transaction_id\":\"TX-NORMAL\",
  \"ts\":\"$(date -u +%Y-%m-%d)T19:30:00\"}"

# ABNORMAL -> review (hard cap, cross-border, unusual city, exceeds historical max, 3am, ...)
curl -X POST localhost:8080/score -H 'Content-Type: application/json' -d "{
  \"branch_id\":231, \"origin_account_no\":\"7064038214\", \"amount\":2000000000, \"currency\":\"NGN\",
  \"destination_account_no\":\"NEW-ACCT-9999\", \"customer_location\":\"road, Pyongyang, DPRK\",
  \"origin_country\":\"NG\", \"destination_country\":\"KP\", \"transaction_id\":\"TX-ABNORMAL\",
  \"ts\":\"$(date -u +%Y-%m-%d)T03:00:00\"}"

curl localhost:8080/customer/231:7064038214     # eligibility, learned, retrain state, event trail
curl -X POST localhost:8080/retrain/231:7064038214
```

> ⚠️ **A "normal" transaction only returns `allow` if it really is normal for THAT
> customer.** Send 5,000 NGN at 3am, or to a city they never use, and rules will fire —
> correctly. If you invent your own payload, take the amount/city/hour from
> `GET /customer/{entity_key}` first, or the engine will (rightly) disagree with you.
> And if the customer isn't trusted, they're scored against their **peer group**, so
> your "normal" is judged against the peer baseline, not their own history.

Every `/score` explains itself: `decision`, `fired_rules`, `judged_against`
(`own_profile` vs `peer_group`), `trust_reason` (**why** — e.g.
`peer_baseline (§1 clean txns 61 < 100)`), and `retrain` — whether it retrained or
**why not** (e.g. *"not due — needs 99 more txns, or 25 more days, or 4 more drift signals"*).

**Reading `learned`:** `learned_from_txn_count` is a **count of transactions**
(not money, not days). Fields ending `_ngn` are money in Naira.

### Why a retrain was skipped — read the reason carefully

"Nothing to learn from" has **three different causes**, and only one is about the
customer. Each returns a distinct `reason` plus an `about_this_customer` flag, so a
cache problem is never mistaken for a compliance signal:

| `reason` | Means | About the customer? |
| --- | --- | --- |
| `cache_not_populated` | The local cache is **empty** — no sync has run yet | ❌ No. Nothing has been ingested for *anyone*. Run the sync. |
| `customer_not_in_cache` | The cache has data, but **none for them** in the window | ❌ No. Either no activity in the window, or the capped sync hasn't reached their rows. |
| `all_cached_txns_excluded` | They **have** cached transactions, but **every one** is suspicious / blocked / blacklisted | ✅ **Yes — a real signal.** §1/§7 say learn only from clean data, so their profile is deliberately **not** rebuilt from dirty data. It is left as-is and they stay untrusted, rather than fraud being absorbed into their "normal". |

> The old single code `no_clean_history` was ambiguous — it read as *"this customer's
> every transaction is dirty"* even when the truth was simply "the cache is empty".
> In an AML system that distinction matters, so the three are now reported separately.

## 8. Endpoint reference

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | overview + where to start |
| GET | `/health` | liveness |
| GET | `/demo` | the whole pipeline end-to-end, stage by stage |
| GET | `/stats` | profiles, Active/Warming-Up split, drift, rules |
| GET | `/demo?entity_key=<key>` | the same demo, run for a **specific** customer |
| GET | `/demo?entity_key=<key>&amount=<n>` | send your **own amount** (one field, like a real customer). The same figure is scored in their usual context AND a suspicious one |
| GET | `/customers` | **browse customers + entity keys** (`?trusted=`, `?q=`, `?limit=`), each with a ready-made demo URL |
| POST | `/sync` | **pull fresh production data into the cache, safely** |
| GET | `/sync/status` | watermark + cache contents |
| POST | `/score` | score one transaction (+ maybe retrain) — the adhere hook |
| GET | `/customer/{entity_key}` | full status of one customer |
| GET | `/profile/{entity_key}` | raw stored profile row |
| POST | `/retrain/{entity_key}` | force a retrain now (from the cache) |
| GET | `/examples` | real customers to test with |
| GET | `/docs` | interactive Swagger UI |

`entity_key = "{branch_id}:{origin_account_no}"` (e.g. `231:1100716290`).

## 9. Troubleshooting

| Symptom | Fix |
| --- | --- |
| **`psql`/sync: `Connection timed out`** | Production is IP-allowlisted — see [§9.1](#91-production-postgres-times-out). |
| **`STORE_PG_PASSWORD must be set`** | Compose refuses to start without it. Set it in `.env`. |
| **`bind: address already in use` (5432)** | You have a local Postgres. The store publishes `STORE_PG_HOST_PORT` (default **5433**); change it in `.env`. |
| **`/stats` shows 0 profiles** | The store is empty — run the migration in [§4](#4-load-the-profiles-one-time). |
| **Retrain skipped: `cache_not_populated` / `customer_not_in_cache`** | Expected until a sync has run — it is about the **cache**, not the customer. Run `sync_manager.py` ([§5](#5-pulling-fresh-live-data--safely)). See [§7](#why-a-retrain-was-skipped--read-the-reason-carefully). |
| **Retrain skipped: `all_cached_txns_excluded`** | This one **is** about the customer: every cached txn of theirs is suspicious/blocked/blacklisted, so §1/§7 leave nothing clean to learn from. Not a bug. |
| **Port 8080 in use** | Stop the other process or change the compose port mapping. |
| **Need to see what happened** | `docker compose logs -f`. Every request, every sync chunk, every score/retrain is logged — and also stored in `bp_event_log`. |

### 9.1 Production Postgres times out

```text
psql: error: connection to server at "adhere-db-...ondigitalocean.com"
(165.245.223.131), port 25061 failed: Connection timed out
```

**Meaning:** production is a DigitalOcean managed Postgres behind a **"Trusted Sources"**
allowlist. It *drops* connections from non-allowlisted IPs (a *timeout*, not
"connection refused", is the tell). This is a firewall issue — not the code.

**Find your IP, then have it allowlisted:**

```bash
curl -4 -s https://ifconfig.me ; echo     # give this to the DB engineer
```

DigitalOcean → Databases → the adhere cluster → Settings → **Trusted Sources** → add it.

> ⚠️ **This IP is dynamic and has already changed once** (it was `129.222.206.171`, then
> became `98.97.77.181`), which silently locked us out again. Ask the DB engineer for a
> **stable route** — a static IP, VPN, bastion, or an agreed CIDR — or this recurs every
> time the connection changes. Re-check with the command above before blaming the code.

**Works without production access:** `/health`, `/stats`, `/score`, `/customer`,
`/profile`, `/examples`, and demo stages 2–9.
**Needs production access:** `POST /sync` and demo stage 1 (and stage 10, which reports
`cache_not_populated` until the cache has been filled once).

### 9.2 Restart / update

```bash
cd behaviour_profile_build

docker compose up -d                 # .env-only change
docker compose up -d --build         # code change
docker compose restart               # plain restart
docker rm -f adhere-behaviour && docker compose up -d    # name conflict

docker compose ps && curl -s localhost:8080/health ; echo
```

## 10. Operations

- **The store is a named volume** (`behaviour_pgdata17`) — survives `down`, **not** `down -v`.
- **A volume is not a backup.** Schedule a dump, store it off-host, and **test the restore**:

  ```bash
  docker exec behaviour-profile-db pg_dump -U behaviour behaviour | gzip > backup_$(date +%F).sql.gz
  ```

- **Access:** the store is published on **loopback only**; the password is on the
  database (`STORE_PG_PASSWORD`). Volume contents are protected by host permissions
  and at-rest encryption — not by a password.
- **Logs:** rotated (10 MB × 5). Container stdout is lost on removal, so the durable
  audit trail is the **`bp_event_log`** table; ship stdout to an aggregator in prod.
- **Resource metrics:** use `docker stats` (or cAdvisor + Prometheus + Grafana) —
  not the application log.

Direct SQL access to the store:

```bash
docker exec -it behaviour-profile-db psql -U behaviour -d behaviour
# \dt                                    list tables
# SELECT count(*) FROM bp_user_behaviour_profile;
# SELECT * FROM bp_sync_state;           the ingestion watermark
```
