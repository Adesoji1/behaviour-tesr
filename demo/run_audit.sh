#!/usr/bin/env bash
# =============================================================================
# Model decision reviewer — inspect ONE /score decision end-to-end (for review).
#
#   ./demo/run_audit.sh [path/to/payload.json]      (default: demo/payload.json)
#
# It:
#   0) CLEARS the customer's live-velocity window, so this probe is NOT skewed by
#      earlier tests (accumulated velocity would otherwise raise the risk).
#   1) prints the MODEL DECISION SANITY AUDIT — what the model LEARNED historically
#      vs the REAL-TIME payload (per behavioural dimension), the detector scores,
#      the blend, the thresholds and the decision.
#   2) calls the LIVE /score API (auth from BP_API_KEY in .env) and prints the
#      actual response. The audit decision and the API decision should MATCH.
#
# Edit demo/payload.json (amount, beneficiary, location, type, …) and re-run to
# test scenarios. Stack must be up: docker compose up -d db redis behaviour-profile sync
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"        # .../demo
ROOT="$(cd "$HERE/.." && pwd)"               # repo root (AI-service)
cd "$ROOT"

# Payload precedence: explicit arg > your local demo/payload.json (git-ignored; may hold a REAL
# customer) > the committed demo/payload.example.json (synthetic — so the demo runs on a fresh clone).
PAYLOAD="${1:-}"
if [ -z "$PAYLOAD" ]; then
  if [ -f "$HERE/payload.json" ]; then
    PAYLOAD="$HERE/payload.json"
  else
    PAYLOAD="$HERE/payload.example.json"
    echo "NOTE: using the committed sample demo/payload.example.json (synthetic values)."
    echo "      To test a real customer's personal profile: cp demo/payload.example.json demo/payload.json  and edit it (it stays git-ignored)."
  fi
fi
[ -f "$PAYLOAD" ] || { echo "ERROR: payload not found: $PAYLOAD"; exit 1; }

KEY="$(grep '^BP_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)"
[ -n "$KEY" ] || { echo "ERROR: BP_API_KEY not found in .env"; exit 1; }
URL="${SCORE_URL:-http://localhost:8080/score}"
ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["customer_details"]["identifier"])' "$PAYLOAD")"

echo "#############################################################################"
echo "#  MODEL DECISION REVIEW  —  customer $ID"
echo "#  payload: $PAYLOAD"
echo "#############################################################################"

echo
echo ">>> STEP 0 — clear the live-velocity window (isolate this probe)"
if docker exec adhere-redis redis-cli DEL "vel:$ID" >/dev/null 2>&1; then
  echo "    cleared vel:$ID"
else
  echo "    (could not reach redis — continuing; velocity may be stale)"
fi

echo
echo ">>> STEP 1 — SANITY AUDIT: what the model LEARNED vs this REAL-TIME payload"
docker exec -i adhere-behaviour python demo/decision_audit.py < "$PAYLOAD" 2>/dev/null \
  || { echo "    ERROR: audit failed (is 'adhere-behaviour' running?)"; exit 1; }

echo
echo ">>> STEP 2 — LIVE /score RESPONSE (the actual API decision; should match the audit)"
curl -s -X POST "$URL" -H "X-Adhere-Key: $KEY" -H 'Content-Type: application/json' \
  --data-binary @"$PAYLOAD" | python3 -m json.tool

echo
echo ">>> done. Edit $PAYLOAD and re-run to try another scenario."
