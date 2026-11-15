#!/usr/bin/env bash
# Smoke-test Koov API (e.g. after starting the Koov container).
#
# Prereqs: Docker running. Start Koov with:
#   ./docker-compose-up.sh build --no-cache && ./docker-compose-up.sh up -d
#
# Usage: ./scripts/test-docker-api.sh   or   KOOV_API_URL=http://localhost:5001 ./scripts/test-docker-api.sh
set -e
BASE_URL="${KOOV_API_URL:-http://localhost:5001}"

echo "Testing Koov API at $BASE_URL"

# 1. Health
echo -n "GET /health ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health" || true)
if [[ "$code" != "200" ]]; then
  echo "FAIL (HTTP $code). Is the Koov container running? Try: ./docker-compose-up.sh up -d"
  exit 1
fi
echo "OK ($code)"

# 2. Join-meeting: must not return 503 "Wake word model not found"
echo -n "POST /api/join-meeting (smoke) ... "
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/join-meeting" \
  -H "Content-Type: application/json" \
  -d '{"meeting_link":"https://zoom.us/j/999","join_backend":"external","meeting_id":"test-smoke-id"}')
code=$(echo "$resp" | tail -n 1)
body=$(echo "$resp" | sed '$d')
if [[ "$code" == "503" ]] && echo "$body" | grep -q "Wake word model not found"; then
  echo "FAIL (503 Wake word model not found). Check KOOV_VOSK_MODEL_PATH in docker-compose.yml and rebuild."
  exit 1
fi
# 200 = started, 409 = meeting already active, 400 = bad request are acceptable
echo "OK (HTTP $code)"

echo "All checks passed."
