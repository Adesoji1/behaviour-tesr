#!/bin/sh
# =============================================================================
# Render start command for the behaviour-profile WEB service.
#
# Why this exists: Render builds the image FROM the git repo, and `artifacts/` is
# .dockerignored, so the trained MODEL is not baked in. We fetch it on start from
# MODEL_BUNDLE_URL (a direct-download link, e.g. a Google Drive
# `uc?export=download&id=...` URL) and unpack it at the repo root so it lands in
# ./artifacts/{models,registry} — exactly where ml.config.ARTIFACTS (= /app/artifacts)
# looks. Then we fail-fast if no API key is set, and start uvicorn on Render's $PORT.
#
# Set this as the Render "Docker Command":   sh render-entrypoint.sh
# POSIX sh only (python:slim has /bin/sh, not necessarily bash).
# =============================================================================
set -eu

ART=/app/artifacts
INDEX="$ART/registry/index.json"

if [ -f "$INDEX" ]; then
  echo "entrypoint: model already present ($INDEX) — skipping fetch"
elif [ -n "${MODEL_BUNDLE_URL:-}" ]; then
  echo "entrypoint: fetching model bundle from MODEL_BUNDLE_URL ..."
  # Use python (always present) so we don't depend on curl/wget being in the slim image.
  python - "$MODEL_BUNDLE_URL" /tmp/model-bundle.tar.gz <<'PY'
import sys, re, urllib.request
url, out = sys.argv[1], sys.argv[2]
op = urllib.request.build_opener()
op.addheaders = [('User-Agent', 'Mozilla/5.0')]
urllib.request.install_opener(op)
urllib.request.urlretrieve(url, out)
# Google Drive files <100MB download directly (our model is ~6MB). If a >100MB file
# returns the virus-scan interstitial, recover the confirm token and retry (best effort).
try:
    with open(out, 'rb') as f:
        head = f.read(4096)
    if b'<html' in head.lower() and 'drive.google' in url:
        with open(out, 'r', errors='ignore') as f:
            html = f.read()
        tok = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
        idm = re.search(r'[?&]id=([^&]+)', url)
        if tok and idm:
            u2 = "https://drive.google.com/uc?export=download&confirm=%s&id=%s" % (tok.group(1), idm.group(1))
            urllib.request.urlretrieve(u2, out)
except Exception as e:
    print("entrypoint: interstitial check skipped:", e)
print("entrypoint: downloaded ->", out)
PY
  echo "entrypoint: unpacking -> /app (populates ./artifacts/models + ./artifacts/registry) ..."
  tar xzf /tmp/model-bundle.tar.gz -C /app
  rm -f /tmp/model-bundle.tar.gz
  if [ -f "$INDEX" ]; then
    echo "entrypoint: model unpacked OK (active: $(grep -o '\"active\"[^,]*' "$INDEX" 2>/dev/null | head -1))"
  else
    echo "entrypoint: WARNING unpacked but $INDEX missing — /score will 500 until a model is present" >&2
  fi
else
  echo "entrypoint: WARNING no model at $INDEX and MODEL_BUNDLE_URL unset — /health works, /score will 500" >&2
fi

# Same fail-fast the app does at startup: exit cleanly (not a crash-loop) if no key is configured.
python -c 'import service; service._require_api_key_configured()'

exec uvicorn service:app --host 0.0.0.0 --port "${PORT:-8080}" --workers "${BP_WORKERS:-1}"
