#!/usr/bin/env bash
# Ship the current commit to the server in .deploy.env and (re)start the API container.
# Works from Git Bash on Windows: needs only git, ssh and scp (no rsync).
#
#   1. python scripts/vultr_deploy.py create --ssh-pubkey ~/.ssh/id_ed25519.pub   (writes .deploy.env)
#   2. bash scripts/deploy-vultr.sh
#
# Sends: the files of the current git commit, plus your local .env (and .env.local if present).
# Uncommitted changes are NOT sent. Data on the server (mechus_notes/) is kept.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f .deploy.env ] || { echo "No .deploy.env. Create the server first (see docs/VULTR.md)."; exit 1; }
set -a; source .deploy.env; set +a
: "${DEPLOY_HOST:?DEPLOY_HOST is not set in .deploy.env}"
[ -f .env ] || { echo "No .env here: the server needs your keys."; exit 1; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
if [ -n "${DEPLOY_SSH_KEY:-}" ]; then SSH_OPTS+=(-i "${DEPLOY_SSH_KEY/#\~/$HOME}"); fi
REMOTE_DIR="mechusdemo"

echo "==> Waiting for the server to finish installing Docker..."
for _ in $(seq 1 60); do
  if ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" 'test -f /root/.mechus-cloud-init-done || command -v docker >/dev/null' 2>/dev/null; then break; fi
  sleep 10
done

echo "==> Sending code ($(git rev-parse --short HEAD)) to $DEPLOY_HOST:~/$REMOTE_DIR"
git archive --format=tar HEAD | ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "mkdir -p ~/$REMOTE_DIR && tar -x -C ~/$REMOTE_DIR"

echo "==> Sending .env"
scp "${SSH_OPTS[@]}" -q .env "$DEPLOY_HOST:~/$REMOTE_DIR/.env"
if [ -f .env.local ]; then scp "${SSH_OPTS[@]}" -q .env.local "$DEPLOY_HOST:~/$REMOTE_DIR/.env.local"; fi

echo "==> Building and starting the container (first build takes several minutes)"
ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" "cd ~/$REMOTE_DIR && chmod 600 .env && mkdir -p mechus_notes && chown -R 1000:1000 mechus_notes \
  && docker compose -f docker-compose.yml -f docker-compose.vultr.yml up -d --build 2>&1 | tail -n 15"

HOST_IP="${DEPLOY_HOST#*@}"
echo "==> Checking http://$HOST_IP:5001/health"
for _ in $(seq 1 30); do
  if curl -fsS -m 5 "http://$HOST_IP:5001/health" >/dev/null 2>&1; then
    echo "Server is up: http://$HOST_IP:5001/dashboard"
    echo "For Vercel, set MECHUS_API_ORIGIN=http://$HOST_IP:5001 and redeploy."
    exit 0
  fi
  sleep 5
done
echo "The server did not answer on port 5001 yet. Logs: ssh $DEPLOY_HOST 'cd ~/$REMOTE_DIR && docker compose logs --tail 80'"
exit 1
