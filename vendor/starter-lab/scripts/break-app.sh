#!/bin/bash
# =============================================================================
# Break App Script — Simulates memory leak on Grubify (Scenario 1)
#
# This script:
#   1. Checks app health
#   2. Floods the cart API with rapid POST requests to cause memory leak
#   3. Azure Monitor detects memory pressure / OOM / HTTP errors
#   4. The SRE Agent picks up the alert and begins investigation
#
# NOTE: This is the reference shell version, vendored from the upstream lab
# for parity/documentation purposes. The Python app implements this same
# fault-injection logic, so end users never need to run this script directly.
# =============================================================================
set -e

REQUEST_COUNT=${2:-200}
SLEEP_INTERVAL=${3:-0.5}

APP_URL="${1:-}"
if [ -z "$APP_URL" ]; then
  APP_URL=$(azd env get-values 2>/dev/null | grep "^CONTAINER_APP_URL=" | cut -d'=' -f2 | tr -d '"')
fi

if [ -z "$APP_URL" ]; then
  echo "Error: Could not determine Grubify URL."
  echo "Usage: ./scripts/break-app.sh [https://your-app-url] [request-count] [sleep-seconds]"
  exit 1
fi

echo "🔥 Breaking the Grubify App (Memory Leak) — target: ${APP_URL}"

ERROR_COUNT=0
SUCCESS_COUNT=0
for i in $(seq 1 $REQUEST_COUNT); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${APP_URL}/api/cart/demo-user/items" \
    -H "Content-Type: application/json" \
    -d '{"foodItemId":1,"quantity":1}' 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ] || [ "$STATUS" = "201" ]; then
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
  else
    ERROR_COUNT=$((ERROR_COUNT + 1))
  fi
  sleep $SLEEP_INTERVAL
done

echo "Results: ${SUCCESS_COUNT} successes, ${ERROR_COUNT} errors out of ${REQUEST_COUNT} requests"
echo "Wait 5-8 minutes, then check the SRE Agent portal at https://sre.azure.com"
