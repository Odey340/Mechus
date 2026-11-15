#!/bin/bash
# Run Mechus + RTMS in Docker (API on 5001, RTMS webhook on 8080).
# Usage: ./docker-compose-rtms.sh up -d --build
# Requires: ZM_RTMS_CLIENT, ZM_RTMS_SECRET (Zoom App). Optional: ZOOM_APP_CLIENT_ID/SECRET (Meeting SDK for TTS).
# For webhooks to work locally: ngrok http 8080  ->  set Zoom App webhook to https://YOUR_NGROK_URL/webhook

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin"
if [[ -d "$DOCKER_BIN" ]]; then
  export PATH="$DOCKER_BIN:$PATH"
fi

exec docker compose -f docker-compose.yml -f docker-compose.zoom.yml -f docker-compose.rtms.yml "$@"
