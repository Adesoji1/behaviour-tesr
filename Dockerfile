# Adhere Behaviour-Profile microservice
FROM python:3.11-slim

# CRITICAL for `docker compose logs -f`: when stdout is a pipe (which it always is
# under Docker) Python block-buffers it, so log lines sit in an ~8KB buffer instead of
# streaming out — the logs look empty until the buffer fills. Unbuffered = every line
# appears the instant it is written.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

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

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
