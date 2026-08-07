#!/usr/bin/env bash
# =============================================================================
# prepare_release.sh — build (and optionally upload) the OUT-OF-BAND deploy
# artifacts in ONE step, so nothing is forgotten before a deployment:
#
#   * model-bundle.tar.gz  — active + previous model dirs + registry/index.json
#   * store-bundle.tar.gz  — learnt profiles + peer baselines + cache + watermark
#
# Neither can live in git (they hold customer data; the store dump also exceeds
# GitHub's 100 MB file limit). This script is the single memorable pre-deploy step.
#
#   ./prepare_release.sh                      # build both bundles into ./ (git-ignored)
#   MODEL_BUNDLE_URL=s3://bucket/model-bundle.tar.gz \
#   STORE_BUNDLE_URL=s3://bucket/store-bundle.tar.gz \
#     ./prepare_release.sh                    # build AND upload to a private object store
#
# When *_BUNDLE_URL are set, deploy.sh will FETCH them automatically if the local files are
# missing — so a disconnected deploy host needs zero manual copying (see DEPLOYMENT.md §3a).
# Supported URL schemes: s3:// (aws cli), gs:// (gsutil), http(s):// (curl, download-only), file://.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
[ -f .env ] && { set -a; . ./.env; set +a; }

MODEL_BUNDLE_OUT="${MODEL_BUNDLE_OUT:-$HERE/model-bundle.tar.gz}"
STORE_BUNDLE_OUT="${STORE_BUNDLE_OUT:-$HERE/store-bundle.tar.gz}"

_put(){ # _put <local-file> <url>
  local f="$1" url="$2"
  case "$url" in
    s3://*)        command -v aws    >/dev/null || { echo "ERROR: aws cli not found for $url"; return 1; }; aws s3 cp "$f" "$url" ;;
    gs://*)        command -v gsutil >/dev/null || { echo "ERROR: gsutil not found for $url"; return 1; }; gsutil cp "$f" "$url" ;;
    file://*)      mkdir -p "$(dirname "${url#file://}")"; cp "$f" "${url#file://}" ;;
    http://*|https://*) echo "NOTE: $url is http(s) — upload manually (curl can only download). Skipping."; return 0 ;;
    *) echo "ERROR: unsupported URL scheme: $url"; return 1 ;;
  esac
}

# --- 1) model bundle: active + previous model (so registry.rollback works) + the registry index ----
IDX="artifacts/registry/index.json"
if [ -f "$IDX" ]; then
  ACTIVE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("active") or "")' "$IDX" 2>/dev/null || true)"
  PREV="$(python3   -c 'import json,sys;print(json.load(open(sys.argv[1])).get("previous_active") or "")' "$IDX" 2>/dev/null || true)"
  if [ -n "$ACTIVE" ] && [ -d "artifacts/models/$ACTIVE" ]; then
    paths="artifacts/models/$ACTIVE $IDX"
    [ -n "$PREV" ] && [ -d "artifacts/models/$PREV" ] && paths="artifacts/models/$PREV $paths"
    echo "[model] packing active=$ACTIVE ${PREV:+(+ previous=$PREV)} -> $MODEL_BUNDLE_OUT"
    # shellcheck disable=SC2086
    tar czf "$MODEL_BUNDLE_OUT" $paths
    echo "        $(du -h "$MODEL_BUNDLE_OUT" | cut -f1)  sha256:$( (sha256sum "$MODEL_BUNDLE_OUT" 2>/dev/null||shasum -a256 "$MODEL_BUNDLE_OUT")|awk '{print $1}')"
    [ -n "${MODEL_BUNDLE_URL:-}" ] && { echo "[model] uploading -> $MODEL_BUNDLE_URL"; _put "$MODEL_BUNDLE_OUT" "$MODEL_BUNDLE_URL"; }
  else
    echo "[model] WARNING: active model dir not found (active='$ACTIVE') — skipping model bundle."
  fi
else
  echo "[model] WARNING: $IDX not found — no trained model here; skipping model bundle."
fi

# --- 2) store bundle: reuse make_store_dump.sh (resolves PG17 tools automatically) -----------------
echo "[store] building store bundle ..."
STORE_BUNDLE_OUT="$STORE_BUNDLE_OUT" ./make_store_dump.sh
[ -n "${STORE_BUNDLE_URL:-}" ] && { echo "[store] uploading -> $STORE_BUNDLE_URL"; _put "$STORE_BUNDLE_OUT" "$STORE_BUNDLE_URL"; }

echo
echo "Release artifacts ready:"
echo "  $MODEL_BUNDLE_OUT"
echo "  $STORE_BUNDLE_OUT"
echo "Both are git-ignored (customer data). To deploy on a disconnected host, either:"
echo "  • scp both next to deploy.sh, or"
echo "  • set MODEL_BUNDLE_URL / STORE_BUNDLE_URL (a private bucket) and deploy.sh will fetch them."
