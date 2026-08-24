# Adhere Behaviour-Profile microservice
FROM python:3.11-slim

# CRITICAL for `docker compose logs -f`: when stdout is a pipe (which it always is
# under Docker) Python block-buffers it, so log lines sit in an ~8KB buffer instead of
# streaming out — the logs look empty until the buffer fills. Unbuffered = every line
# appears the instant it is written.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Standard container timezone (per the database engineer): everything runs in UTC, so
# log timestamps and the daily ingestion schedule are unambiguous across environments.
# The scheduler reads BP_SYNC_TZ separately (default UTC); keep them aligned.
ENV TZ=UTC

# psql client is required by the pipeline (reads production READ-ONLY) + TLS certs
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All secrets/hosts are env-overridable (see config.py). Provide these at run time:
#   PROD_PG_HOST/PORT/USER/PROD_PG_PASSWORD/PROD_PG_DB    (production, READ ONLY)
#   STORE_PG_HOST/PORT/USER/STORE_PG_PASSWORD/STORE_PG_DB (the profile store)
#   plus any BP_* tuning knobs (thresholds, windows, sync chunking/throttle).
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Start via render-entrypoint.sh — a single start path that works EVERYWHERE:
#   * Render (model NOT in image): fetches MODEL_BUNDLE_URL into ./artifacts on boot.
#   * Local compose (model volume-mounted): sees ./artifacts/registry/index.json already
#     present and SKIPS the fetch.
# It then runs the SAME API-key preflight (exits cleanly if unconfigured, no crash-loop),
# and binds uvicorn to $PORT (Render injects it; falls back to 8080 locally) with
# BP_WORKERS workers. The `sync` service overrides this command, so it is unaffected.
CMD ["sh", "render-entrypoint.sh"]
