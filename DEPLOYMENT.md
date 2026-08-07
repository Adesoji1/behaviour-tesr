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
- **Make the shell scripts executable** (a fresh checkout may not carry the bit):
  ```bash
  chmod +x deploy.sh watchdog.sh pg_migrate_store.sh
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

## 3. Run the deploy

```bash
./deploy.sh
```
What it does, in order (and logs to `./logs/deploy_*.log`):
1. **Confirms the server IP is allowlisted** — prompts `[y/N]` on a terminal; for unattended runs
   set `PROD_IP_ALLOWLISTED=yes`. Answer **no** → it stops, nothing changed.
2. **Verifies the production Postgres connection directly** (`select 1`) — the real gate, not just
   the confirmation. Fails → stops with a Slack alert.
3. **Promotes the learnt profiles** into the production behaviour store (`pg_dump --data-only` of
   `bp_user_behaviour_profile`, `bp_peer_baseline`, `bp_sync_state`) — so **production starts from
   the learnt state, not from zero**.
4. **Promotes the validated model** artifacts + registry (`PROD_ARTIFACTS_DEST`, or a shared volume).
5. **Checks `/health`** — the service is up and answering.
6. Reports success/failure to Slack.

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
