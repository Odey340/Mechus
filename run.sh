#!/usr/bin/env bash
# One script to run Koov: local dev, Docker (Zoom), or deploy to VM.
# Usage:
#   ./run.sh              → show help
#   ./run.sh local        → sart API server locally (dev)
#   ./run.sh docker       → run Koov in Docker with Zoom SDK (Mac/Linux)
#   ./run.sh deploy       → sync code to VM and restart (set DEPLOY_HOST first)
#   ./run.sh vultr        → create a Vultr server (scripts/vultr_deploy.py) then deploy to it
#   ./run.sh ssh          → SSH into the VM (uses .deploy.env)
#   ./run.sh ssh-logs     → Follow Koov container logs on the VM (docker-compose)
#   ./run.sh ft-logs [meeting_id]  → Follow FaceTime worker logs on the VM
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

usage() {
  echo "Koov – run everything from one place"
  echo ""
  echo "  ./run.sh local              Start API server locally (development)"
  echo "  ./run.sh docker             Run Koov in Docker with Zoom SDK"
  echo "  ./run.sh deploy             Sync code to VM and restart container"
  echo "  ./run.sh vultr [args]       Create a Vultr server, wait for Docker, then deploy (docs/VULTR.md)"
  echo "  ./run.sh ssh                SSH into the VM (uses .deploy.env)"
  echo "  ./run.sh ssh-logs           Follow Koov logs on the VM (Ctrl+C to stop)"
  echo "  ./run.sh ft-logs [id]       Follow FaceTime worker container logs on the VM"
  echo "                              id = full meeting_id, or partial prefix, or omit for latest"
  echo ""
  echo "Deploy/SSH: set DEPLOY_HOST and optionally DEPLOY_SSH_KEY in .deploy.env"
  echo "  Copy .deploy.env.example to .deploy.env and edit."
}

cmd_local() {
  if [ -d .venv ]; then
    source .venv/bin/activate
  fi
  export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$REPO_ROOT"
  # start_api_server.py loads .env with the Python parser.  Shell-sourcing it
  # breaks valid values containing spaces or shell metacharacters.
  exec python3 scripts/start_api_server.py
}

cmd_docker() {
  DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin"
  if [[ -d "$DOCKER_BIN" ]]; then
    export PATH="$DOCKER_BIN:$PATH"
  fi
  NOTES_SPEC="${KOOV_HOST_NOTES_DIR:-./koov_notes}"
  if [[ "$NOTES_SPEC" == /* ]]; then
    mkdir -p "$NOTES_SPEC"
    export KOOV_NOTES_HOST_PATH="$(cd "$NOTES_SPEC" && pwd)"
  else
    mkdir -p "$REPO_ROOT/$NOTES_SPEC"
    export KOOV_NOTES_HOST_PATH="$(cd "$REPO_ROOT/$NOTES_SPEC" && pwd)"
  fi
  exec docker compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
}

cmd_deploy() {
  if [ -f .deploy.env ]; then
    set -a
    source .deploy.env
    set +a
  fi
  if [ -z "$DEPLOY_HOST" ]; then
    echo "Error: DEPLOY_HOST not set. Example: export DEPLOY_HOST=ubuntu@3.16.207.181"
    echo "Or create .deploy.env with DEPLOY_HOST=ubuntu@YOUR_VM_IP"
    exit 1
  fi
  RSYNC_SSH="ssh"
  SSH_EXTRA=()
  if [ -n "$DEPLOY_SSH_KEY" ]; then
    RSYNC_SSH="ssh -i $DEPLOY_SSH_KEY"
    SSH_EXTRA=(-i "$DEPLOY_SSH_KEY")
  fi
  echo "Syncing to $DEPLOY_HOST..."
  rsync -avz -e "$RSYNC_SSH" \
    --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude '.deploy.env' \
    "$REPO_ROOT/" "$DEPLOY_HOST:~/koovdemo/"
  echo "Running restart on server..."
  ssh "${SSH_EXTRA[@]}" "$DEPLOY_HOST" 'cd ~/koovdemo && bash scripts/restart-on-server.sh'
  echo "Done. API should be at http://<VM_IP>:5001"
}

cmd_vultr() {
  python3 scripts/vultr_deploy.py create "$@"
  set -a
  source .deploy.env
  set +a
  SSH_ARGS=(-o StrictHostKeyChecking=accept-new)
  if [ -n "$DEPLOY_SSH_KEY" ]; then
    SSH_ARGS+=(-i "$DEPLOY_SSH_KEY")
  fi
  echo "Waiting for cloud-init to finish installing Docker on $DEPLOY_HOST..."
  for _ in $(seq 1 60); do
    if ssh "${SSH_ARGS[@]}" -o ConnectTimeout=5 "$DEPLOY_HOST" 'test -f /root/.koov-cloud-init-done' 2>/dev/null; then
      cmd_deploy
      return
    fi
    sleep 10
  done
  echo "Server is not ready yet; run ./run.sh deploy in a minute."
}

cmd_ssh() {
  if [ -f .deploy.env ]; then
    set -a
    source .deploy.env
    set +a
  fi
  if [ -z "$DEPLOY_HOST" ]; then
    echo "Error: DEPLOY_HOST not set. Create .deploy.env (see .deploy.env.example)"
    exit 1
  fi
  SSH_ARGS=()
  if [ -n "$DEPLOY_SSH_KEY" ]; then
    SSH_ARGS=(-i "$DEPLOY_SSH_KEY")
  fi
  exec ssh "${SSH_ARGS[@]}" "$DEPLOY_HOST" "$@"
}

cmd_ssh_logs() {
  if [ -f .deploy.env ]; then
    set -a
    source .deploy.env
    set +a
  fi
  if [ -z "$DEPLOY_HOST" ]; then
    echo "Error: DEPLOY_HOST not set. Create .deploy.env (see .deploy.env.example)"
    exit 1
  fi
  SSH_ARGS=()
  if [ -n "$DEPLOY_SSH_KEY" ]; then
    SSH_ARGS=(-i "$DEPLOY_SSH_KEY")
  fi
  # Use ``docker logs`` (not ``docker-compose logs -f``): compose v1's log follower can
  # crash with KeyError: 'id' in watch_events while the Koov container is still healthy.
  exec ssh "${SSH_ARGS[@]}" "$DEPLOY_HOST" 'cd ~/koovdemo && \
    CID=$(docker-compose -f docker-compose.yml -f docker-compose.zoom.yml ps -q koov 2>/dev/null | head -1) && \
    if [ -z "$CID" ]; then echo "No running koov container (docker-compose ps -q koov is empty)"; exit 1; fi && \
    echo "Following logs for container $(docker inspect -f "{{.Name}}" "$CID" | sed "s#^/##")" && \
    docker logs -f --tail=200 "$CID"'
}

cmd_ft_logs() {
  if [ -f .deploy.env ]; then
    set -a
    source .deploy.env
    set +a
  fi
  if [ -z "$DEPLOY_HOST" ]; then
    echo "Error: DEPLOY_HOST not set. Create .deploy.env (see .deploy.env.example)"
    exit 1
  fi
  SSH_ARGS=()
  if [ -n "$DEPLOY_SSH_KEY" ]; then
    SSH_ARGS=(-i "$DEPLOY_SSH_KEY")
  fi
  local MEETING_PREFIX="${2:-}"
  # Try live container first; fall back to saved log file in koov_notes.
  if [ -n "$MEETING_PREFIX" ]; then
    exec ssh "${SSH_ARGS[@]}" "$DEPLOY_HOST" "bash -s" <<EOF
CNAME=\$(docker ps -a --filter name=koov-facetime-${MEETING_PREFIX} --format '{{.Names}}' | head -1)
if [ -n "\$CNAME" ]; then
  echo "==> live container: \$CNAME"
  docker logs --tail=500 "\$CNAME" 2>&1
else
  LOGF=\$(ls -1t ~/koovdemo/koov_notes/facetime-*${MEETING_PREFIX}*-container.log 2>/dev/null | head -1)
  if [ -n "\$LOGF" ]; then
    echo "==> saved log: \$LOGF"
    cat "\$LOGF"
  else
    echo "No live container or saved log found for prefix: ${MEETING_PREFIX}"
    echo "Saved logs available:"
    ls -1t ~/koovdemo/koov_notes/facetime-*-container.log 2>/dev/null || echo "  (none)"
  fi
fi
EOF
  else
    exec ssh "${SSH_ARGS[@]}" "$DEPLOY_HOST" "bash -s" <<'EOF'
# Live containers first
CNAME=$(docker ps --filter name=koov-facetime- --format '{{.Names}}' | head -1)
if [ -n "$CNAME" ]; then
  echo "==> live container: $CNAME"
  docker logs -f --tail=200 "$CNAME" 2>&1
else
  # Fall back to most recent saved log
  LOGF=$(ls -1t ~/koovdemo/koov_notes/facetime-*-container.log 2>/dev/null | head -1)
  if [ -n "$LOGF" ]; then
    echo "==> saved log (container already exited): $LOGF"
    cat "$LOGF"
  else
    echo "No live facetime container running and no saved logs found."
    echo "Start a FaceTime meeting first, or check koov_notes/ for container logs."
  fi
fi
EOF
  fi
}

case "${1:-help}" in
  local)     cmd_local ;;
  docker)    cmd_docker ;;
  deploy)    cmd_deploy ;;
  vultr)     cmd_vultr "${@:2}" ;;
  ssh)       cmd_ssh "${@:2}" ;;
  ssh-logs)  cmd_ssh_logs ;;
  ft-logs)   cmd_ft_logs "$@" ;;
  help|--help|-h) usage ;;
  *)
    echo "Unknown: $1"
    usage
    exit 1
    ;;
esac
