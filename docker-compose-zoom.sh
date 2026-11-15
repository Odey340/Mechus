#!/bin/bash
# Run Mechus in Docker with Zoom SDK (Mechus joins the meeting and gets audio in the container).
# Usage: ./docker-compose-zoom.sh up -d --build   or   ./docker-compose-zoom.sh up --build
# Requires ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET in .env.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Use same PATH fix as docker-compose-up.sh (Mac)
DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin"
if [[ -d "$DOCKER_BIN" ]]; then
  export PATH="$DOCKER_BIN:$PATH"
fi

exec docker compose -f docker-compose.yml -f docker-compose.zoom.yml "$@"
