# How to test the Behaviour-Profile service

> **This document has moved → [`RUNBOOK.md`](RUNBOOK.md).**

`RUNBOOK.md` is now the single, up-to-date guide to running and testing the service:
prerequisites, Docker/uvicorn start, loading the profiles, pulling fresh live data
safely, the demo, every endpoint with what to expect, troubleshooting, and operations.

This file used to hold a second copy of those instructions. Keeping two run/test
guides meant they drifted apart — the old copy still described the **MySQL** store,
the `ca.pem` TLS certificate, and a retrain that read straight from production. All
three are gone:

- the profile store is now **PostgreSQL** (a container, brought up by `docker compose`),
- **`ca.pem` is no longer needed** (it was the MySQL TLS cert),
- retraining reads a **local cache**, never production — a separate ingestion job
  (`sync_manager.py`) is the only thing that reads the live database.

So there is now **one** guide, and it is `RUNBOOK.md`.

## Where to look

| I want to… | Read |
| --- | --- |
| Run it, demo it, test it end-to-end | **[`RUNBOOK.md`](RUNBOOK.md)** |
| Integrate it into adhere / DevOps + deployment | [`SERVICE.md`](SERVICE.md) (see §6) |
| Understand the ingestion + caching design | [`ingestionstratimprove.md`](ingestionstratimprove.md) |
| Understand what the system does conceptually | [`howitworks.md`](howitworks.md) |
| Understand the build pipeline / design decisions | [`README.md`](README.md) |

## The 60-second version

```bash
cd behaviour_profile_build
cp .env.example .env                  # STORE_PG_PASSWORD is required
docker compose up -d --build          # starts PostgreSQL + the service

# load the already-learned profiles (production untouched, ~1 min)
docker compose run --rm --no-deps behaviour-profile python migrate_mysql_to_pg.py

curl localhost:8080/health            # {"status":"ok",...}
curl localhost:8080/stats             # ~99,254 profiles, 32 rules
curl localhost:8080/demo              # the whole pipeline, stage by stage
```

Then open **<http://localhost:8080/docs>** and click "Try it out" on any endpoint.
Watch it work with `docker compose logs -f`.

Full detail — including the safe live-data pull and what each stage should return —
is in **[`RUNBOOK.md`](RUNBOOK.md)**.
