# Deployment runbook — Behavioural Anti-Fraud Service

Step-by-step to take this from a tested build to **production**. The automation is **[`deploy.sh`](deploy.sh)**
(operator-run); this document is what a human follows. Everything is logged to `./logs/deploy_*.log`.

> There is **one** scoring service (the model runs in-process) + a scheduled ingestion job + an
> offline trainer. AML is a separate service. See [`README.md`](README.md) for the topology.

## Why `deploy.sh` and not just `docker compose up`?

`docker compose up` only **starts containers**. `deploy.sh` is the **production release** step — it
does the things a safe release needs *around* starting the containers, which compose does not:

1. **Safety gates** — confirms this host's IP is allowlisted on the production source DB, and
   **verifies the production Postgres connection directly** (`select 1`) before changing anything.
2. **Carries state forward (no cold start)** — `pg_dump | psql` promotes the **learnt profiles**
   into the production behaviour store, and promotes the **validated model artifacts + registry** —
   so production starts from the trained state, not from zero.
3. **Required-config preflight** — ensures an API key is configured (else the API would refuse to
   boot), in the correct order (store healthy → key → API + sync).
4. **Verification + operator handoff** — health-checks, prints the current risk thresholds, reports a
   retrain-due advisory, sends Slack, and reminds you to start `watchdog.sh`.

In short: **compose = start containers; `deploy.sh` = release to production** (promote data + model,
gate, order, verify). For a plain local dev run, `docker compose up -d` is fine; for production, use
`deploy.sh`.

---

## 0. Prerequisites (once per environment)

- Docker + Docker Compose on the host; for the **trainer** only: an NVIDIA GPU + Container Toolkit.
- A **production behaviour store** (PostgreSQL) reachable from the host — this is where the learnt
  profiles are promoted to. (Separate from the read-only production *source* DB, `PROD_PG_*`.)
- This host's **public IP allowlisted** on the production source Postgres (the sync job reads it).
- **PostgreSQL 17 client tools** for the store promotion/backup (`pg_dump`/`pg_restore` must be ≥ the
  store's major). You usually don't need to do anything — the scripts borrow the tools from the running
  `postgres:17` container automatically. **If** the store is a *managed* Postgres (no local container)
  and this host's client is old/absent, install them **first**:
  ```bash
  ./install_pg_client.sh --dry-run     # preview what it will do for your OS
  ./install_pg_client.sh               # detects OS -> installs the client-only PostgreSQL 17 package
  ```
- **The model + store are shipped OUT-OF-BAND, never in git** (they hold customer data; the store dump
  also exceeds GitHub's 100 MB file limit). Build them with `./prepare_release.sh` (see §2) — the repo
  carries only code + `schema_pg.sql`.
- **Make the shell scripts executable** (a fresh checkout may not carry the bit):
  ```bash
  chmod +x deploy.sh watchdog.sh make_store_dump.sh backup_store.sh install_pg_client.sh prepare_release.sh
  ```
  `deploy.sh` also re-applies `+x` to `watchdog.sh` at the end, but set it here so `./deploy.sh`
  itself runs.

## 1. Configure `.env`

```bash
cp .env.example .env
```
Set at least:
- `STORE_PG_PASSWORD` — the behaviour store password (compose refuses to start without it).
- `PROD_PG_HOST/PORT/USER/PROD_PG_PASSWORD/PROD_PG_DB` — the **read-only** production source.
- `BP_USE_MODEL=true`, `BP_SYNC_AT_HOUR=4` (daily 04:00 pull).
- `BP_SCORE_WEBHOOK_URL` — where decisions are delivered.
- `BP_SLACK_WEBHOOK_URL` — alerts (optional now; empty = alerts are logged, not posted).
- For `deploy.sh`: `SRC_STORE_DSN` (store holding the learnt profiles) and `PROD_STORE_DSN`
  (the production behaviour store); optionally `PROD_ARTIFACTS_DEST` (rsync target for the model
  if `artifacts/` is not a shared volume). `.env` is git-ignored — never commit it.

## 2. Have a validated, promoted model

Training is **offline** (GPU) and produces the model the service serves:
```bash
docker compose --profile train run --rm trainer --promote
```
It promotes the new model **only if it beats the current active** on the synthetic-anomaly AUC.
Confirm: `python -m ml.monitor` (or check `artifacts/registry/index.json` has an `active` version).

### The model + store are shipped OUT-OF-BAND — never in git

The trained model files (`artifacts/models/<version>/featurebuilder.joblib`, …) and the store dump
contain **customer-derived data** — identifiers, beneficiary account numbers, IPs, full transaction
history — so they are **git-ignored and must never be committed** (zipping does not redact them; and
the store dump exceeds GitHub's 100 MB file limit, so a push would be rejected anyway). The git repo
carries **code + `schema_pg.sql`** only.

**One command builds everything you need for a deploy** (so nothing is forgotten):
```bash
./prepare_release.sh          # -> model-bundle.tar.gz  +  store-bundle.tar.gz   (both git-ignored)
```
`model-bundle.tar.gz` = the **active + previous** model dirs + `registry/index.json` (so
`registry.rollback()` works immediately). `store-bundle.tar.gz` = the learnt state (see §3).

**Getting them to production — three ways, no manual step forgotten:**
1. **Deploy from a host that can reach the store** (e.g. your PC): you need **nothing pre-built** —
   `deploy.sh` promotes the store DB→DB and rsyncs the local `artifacts/`. This is the simplest path.
2. **Private object store (recommended for a disconnected deploy host):** set `MODEL_BUNDLE_URL` /
   `STORE_BUNDLE_URL` (an `s3://`/`gs://` bucket) in `.env`. `prepare_release.sh` **uploads** the
   bundles; `deploy.sh` **fetches** them automatically if they aren't local. Nothing to scp, nothing to
   remember. (Use a **private** bucket — this is customer data.)
3. **Manual copy (no bucket):** `scp model-bundle.tar.gz store-bundle.tar.gz` next to `deploy.sh` (or
   point `MODEL_BUNDLE=` / `STORE_BUNDLE=` at them). `deploy.sh` unpacks the model (step 3b) and
   restores the store (step 3). **You can't forget this:** if a bundle is missing and no
   `*_BUNDLE_URL` is set, `deploy.sh` **prints the exact commands** to run — `./prepare_release.sh` on
   your laptop, then a ready-to-paste `scp … <user>@<server-ip>:<path>/` line. Set `DEPLOY_SERVER_IP`,
   `DEPLOY_SSH_USER`, and (for key auth) `DEPLOY_SSH_KEY` in `.env` so that `scp` line is filled in
   exactly (they auto-detect otherwise, and a note reminds you to add `-i <key>` for key auth).

## 3. Run the deploy

```bash
./deploy.sh
```
What it does, in order (and logs to `./logs/deploy_*.log`):
1. **Confirms the server IP is allowlisted** — prompts `[y/N]` on a terminal; for unattended runs
   set `PROD_IP_ALLOWLISTED=yes`. Answer **no** → it stops, nothing changed.
2. **Verifies the production Postgres connection directly** (`select 1`) — the real gate, not just
   the confirmation. Fails → stops with a Slack alert.
3. **Promotes the learnt state** into the production behaviour store, so **production continues from
   the current learnt behaviour and the daily pull only adds the delta**. Two parts either way:
   - **(a)** the learnt `bp_user_behaviour_profile` + `bp_peer_baseline` — idempotent
     (`INSERT … ON CONFLICT DO NOTHING`, so a re-run never clobbers profiles prod learned itself);
   - **(b)** the raw `bp_transactions_cache` (the ~1.3 GB history the model uses for velocity +
     retraining) **plus** `bp_sync_state` (the watermark) — seeded **only when the prod cache is
     empty**, so cache and watermark stay consistent and a re-run never duplicates it.

   **Two transports — deploy.sh auto-detects (see §3a); provide ONE:**
   - **Direct DB→DB pipe** — set `SRC_STORE_DSN` (reachable from the deploy host) + `PROD_STORE_DSN`;
     deploy.sh pipes it live (`pg_dump | psql`).
   - **Store bundle file** — when the two DBs **can't talk directly** (e.g. you build it on your PC):
     run `./make_store_dump.sh` to produce `store-bundle.tar.gz`, `scp` it next to `deploy.sh` (or set
     `STORE_BUNDLE=/path`), and deploy.sh restores from the file. **The bundle wins if both are set.**

   (Everything is out-of-band — **no customer data is ever committed to git**; the bundle is
   git-ignored.) If neither transport is provided **and** the prod store is empty, deploy.sh stops
   (or, unattended, needs `ALLOW_COLD_START=yes`) so production never silently starts cold.
3b. **Unpacks the model bundle** if one is present (`MODEL_BUNDLE`, default `./model-bundle.tar.gz`)
   into `./artifacts` — the out-of-band way to get the model onto a host that only has a git clone
   (see §2). Skipped when `artifacts/models` is already populated.
4. **Promotes the validated model** artifacts + registry (`PROD_ARTIFACTS_DEST`, or a shared volume).
5. **Checks `/health`** — the service is up and answering.
6. Reports success/failure to Slack.

### 3a. If the two databases can't talk — build a store bundle on your PC

Use this when the **deploy host can't reach your behaviour store** (so the direct DB→DB pipe isn't
possible) — for example you're on your own PC and production is elsewhere. It's a **dump once → scp →
restore** flow, and `deploy.sh` handles the restore automatically.

```bash
# 1) On a machine that CAN reach your store (reads SRC_STORE_DSN from .env, or pass a DSN):
./make_store_dump.sh                      # -> ./store-bundle.tar.gz  (learnt profiles + cache + watermark)

# 2) Copy it to the deploy host, next to deploy.sh (it is git-ignored — it holds customer data):
scp store-bundle.tar.gz  operator@deploy-host:/opt/adhere/AI-service/

# 3) On the deploy host, just run the normal deploy — step 3 detects the bundle and restores it:
./deploy.sh
```

The bundle carries `store-learnt.sql` (profiles + peer baselines, idempotent) and `store-cache.dump`
(cache + watermark, compressed). `deploy.sh` restores the profiles idempotently and the cache **only
when the prod cache is empty** (re-run safe). Ship the bundle securely; **never commit it** (it is
git-ignored alongside `model-bundle.tar.gz`).

### PostgreSQL 17 client tools — auto-resolved (host **or** the container)

`pg_dump`/`pg_restore` must be **≥ the store's major version** (the store is **PostgreSQL 17**; a v15
`pg_dump` against a v17 server aborts). You do **not** have to hand-manage this — `make_store_dump.sh`,
`deploy.sh` and `backup_store.sh` all resolve the tools automatically (via `pgtools.sh`), in order:

1. **Host client** if `pg_dump` on `PATH` is ≥ 17.
2. **The `postgres:17` container as the toolbox** — if the host client is older/absent but a store
   container is running (`docker compose up -d db`), the scripts run the tools inside it
   (`docker exec`). This is why a PG15 laptop can still build/restore against the v17 store.
3. Otherwise they point you at **`./install_pg_client.sh`** — it detects your OS (Debian/Ubuntu → PGDG
   `apt`, RHEL/Fedora/Alma → `dnf`, macOS → Homebrew) and installs the **client-only** package. Preview
   it with `./install_pg_client.sh --dry-run`; run `--yes` for non-interactive.

The store container (official `postgres:17`) already ships the full client set —
`psql`, `pg_dump`, `pg_restore`, `pg_basebackup`, `pg_waldump` (the last two live under
`/usr/lib/postgresql/17/bin/`). Our promotion + `backup_store.sh` use **logical** dumps
(`pg_dump`/`pg_restore`) because they are per-table, selective, and cross-host — the right granularity
for shipping the learnt state. **`pg_basebackup`** (physical, whole-cluster, needs replication
privileges) and **`pg_waldump`** (WAL forensics) are **available but intentionally not wired into the
scripts** — they are DBA/PITR tools, not what this per-store promotion needs. Nothing here uses
`pg_restore --clean` (which drops objects); restores are `--data-only`, so a promotion never drops
anything.

**Full-store backup** (disaster recovery), written via the same resolved tools:
```bash
./backup_store.sh            # -> ./backups/store_<ts>.dump  (compressed; git-ignored)
# restore:  pg_restore --no-owner --data-only -d "$TARGET_DSN"  backups/store_<ts>.dump
```

## 4. Start (or confirm) the running stack

```bash
docker compose up -d --build db redis behaviour-profile sync
curl -s http://localhost:8080/health
```
- `db` — the store · `redis` — the live-velocity window (real-time burst features; fail-safe) ·
  `behaviour-profile` — the API (`/score`, model in-process) · `sync` — the scheduled production
  pull (the **only** production reader; keep it a **single** instance).
- All use `restart: unless-stopped` + healthchecks, so they auto-recover after a crash. `redis` is
  enrichment-only: if it is down, `/score` keeps working on the batch cache.

## 5. After a successful deploy — START THE WATCHDOG (do not forget)

`deploy.sh` does **not** start the watchdog (it is a long-running tail) — it prints this reminder at
the end. Start it once, after every successful deploy:
```bash
nohup ./watchdog.sh >> logs/watchdog.log 2>&1 &      # or install a systemd unit
```
Alerts (Slack + `./logs/watchdog.log`) when a container dies / goes unhealthy or a profile-retrain
fails — with the actual cause + last log lines. It only **notifies**; `restart: unless-stopped`
does the healing. Confirm it's running: `pgrep -af watchdog.sh`.

## 6. Verify

```bash
curl -s -X POST http://localhost:8080/score -H 'Content-Type: application/json' -d @sample_txn.json
docker exec <db> psql -U behaviour -d behaviour -c \
  "SELECT transaction_id,decision,webhook_status FROM bp_decision ORDER BY id DESC LIMIT 5;"
tail -f logs/behaviour.log                 # plain-text audit trail
```
Green when: `/score` returns a decision, `bp_decision.webhook_status='sent'`, and
`logs/behaviour.log` shows `webhook sent -> HTTP 200`.

---

## Ongoing operation
- **Retraining** (offline, GPU): `python -m ml.retrain_trigger --run` fires when `≥100 new txns OR
  ≥30 days OR drift`; it retrains + promotes only if better. Roll it into serving with no downtime:
  `curl -X POST http://localhost:8080/reload`. Rollback:
  `python -c "from ml import registry; registry.rollback()"` then reload. (Full loop:
  [`ml/README.md` → "The MLOps loop"](ml/README.md).)
- **Never** run `docker compose down -v` — that deletes the profile store volume.

## Rollback a bad deploy
- Model: `registry.rollback()` + `POST /reload` (§ above).
- Service: `docker compose up -d --build behaviour-profile` from the previous image tag.
- Profiles: restore the production behaviour store from your `pg_dump` backup (take one before §3).
