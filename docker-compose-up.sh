#!/bin/bash
# Run Docker Compose with PATH set so docker-credential-desktop is found (Mac).
# On Mac, "docker compose" alone often fails with: executable file not found in $PATH.
# Usage: ./docker-compose-up.sh up --build   or   ./docker-compose-up.sh build --no-cache

set -e
DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin"
if [[ -d "$DOCKER_BIN" ]]; then
  export PATH="$DOCKER_BIN:$PATH"
fi
exec docker compose "$@"
