#!/usr/bin/env bash
# One-time: add server Zoom OAuth to ~/mechusdemo/.env so Mechus can fetch ZAK without per-user reconnect.
# Usage (on the API VM, or via ./run.sh ssh):
#   ZOOM_ACCOUNT_ID=xxx ZOOM_CLIENT_ID=xxx ZOOM_CLIENT_SECRET=xxx ZOOM_BOT_USER_EMAIL=user@company.com \
#     bash scripts/setup_zoom_server_oauth.sh
set -euo pipefail

ENV_FILE="${1:-$HOME/mechusdemo/.env}"
for key in ZOOM_ACCOUNT_ID ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET ZOOM_BOT_USER_EMAIL; do
  if [[ -z "${!key:-}" ]]; then
    echo "Missing $key. Get values from Zoom Marketplace → Server-to-Server OAuth app." >&2
    exit 1
  fi
done

touch "$ENV_FILE"
for key in ZOOM_ACCOUNT_ID ZOOM_CLIENT_ID ZOOM_CLIENT_SECRET ZOOM_BOT_USER_EMAIL; do
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${!key}|" "$ENV_FILE"
  else
    printf '\n# Server Zoom OAuth (auto ZAK for joins — no dashboard reconnect)\n' >> "$ENV_FILE"
    echo "${key}=${!key}" >> "$ENV_FILE"
  fi
done

echo "Updated $ENV_FILE. Restart Mechus: cd ~/mechusdemo && bash scripts/restart-on-server.sh"
