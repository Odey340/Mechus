#!/usr/bin/env bash
# Run this on the VM after syncing code (e.g. via ./run.sh deploy from Mac).
# Rebuilds and restarts the Mechus Zoom container.
set -e

cd "$(dirname "$0")/.."

# FaceTime worker mode (`join_backend=facetime_web`) needs docker-out-of-docker. The API container user must
# be in the same gid as `/var/run/docker.sock` on the host (see `docker-compose.zoom.yml` `group_add`).
if [[ -z "${DOCKER_GID:-}" ]]; then
  if [[ -S /var/run/docker.sock ]]; then
    if command -v stat &>/dev/null; then
      if stat -c '%g' /var/run/docker.sock >/dev/null 2>&1; then
        DOCKER_GID="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
        export DOCKER_GID
      elif stat -f '%g' /var/run/docker.sock >/dev/null 2>&1; then
        DOCKER_GID="$(stat -f '%g' /var/run/docker.sock 2>/dev/null || true)"
        export DOCKER_GID
      fi
    fi
  fi
fi
if [[ -z "${DOCKER_GID:-}" ]]; then
  echo "ERROR: DOCKER_GID is not set and could not be inferred from /var/run/docker.sock." >&2
  echo "Set it in your server .env (same folder as docker-compose*.yml), e.g.:" >&2
  echo "  DOCKER_GID=\$(stat -c '%g' /var/run/docker.sock)" >&2
  exit 1
fi

REPO_ROOT="$(pwd)"
NOTES_SPEC="${MECHUS_HOST_NOTES_DIR:-./mechus_notes}"
if [[ "$NOTES_SPEC" == /* ]]; then
  mkdir -p "$NOTES_SPEC"
  export MECHUS_NOTES_HOST_PATH="$(cd "$NOTES_SPEC" && pwd)"
else
  mkdir -p "$REPO_ROOT/$NOTES_SPEC"
  export MECHUS_NOTES_HOST_PATH="$(cd "$REPO_ROOT/$NOTES_SPEC" && pwd)"
fi
mkdir -p "$MECHUS_NOTES_HOST_PATH/mechus_meetings"
# Container runs as uid 1000 (appuser); bind-mounted notes must be writable there.
if command -v chown &>/dev/null; then
  chown -R 1000:1000 "$MECHUS_NOTES_HOST_PATH" 2>/dev/null || sudo chown -R 1000:1000 "$MECHUS_NOTES_HOST_PATH" 2>/dev/null || true
fi

# Camera support is opt-in. When enabled, fail before rebuilding if the host
# module/device pool is not ready; an old healthy deployment remains untouched.
case "${MECHUS_FT_CAMERA_FEATURE:-0}" in
  1|true|TRUE|yes|YES|on|ON)
    echo "Checking FaceTime virtual camera host prerequisites..."
    bash scripts/verify_facetime_camera_host.sh
    ;;
esac

echo "Stopping existing container..."
if command -v docker-compose &>/dev/null; then
  docker-compose -f docker-compose.yml -f docker-compose.zoom.yml down || true
else
  docker compose -f docker-compose.yml -f docker-compose.zoom.yml down || true
fi

# Free a bit of space from failed/partial builds (safe: only untagged dangling images).
if command -v docker &>/dev/null; then
  docker image prune -f >/dev/null 2>&1 || true
fi
if command -v df &>/dev/null; then
  avail_kb="$(df -Pk / 2>/dev/null | awk 'NR==2 {print int($4)}')"
  if [[ -n "$avail_kb" && "$avail_kb" -lt 524288 ]]; then
    echo "WARNING: root filesystem has under ~512MB free (${avail_kb}KB). Builds may fail with 'no space left on device'." >&2
    echo "  On the server run: docker system prune -f   (add -a to remove unused images too; review first)" >&2
  fi
fi

echo "Building FaceTime worker image (host Docker)..."
docker build -t mechus-facetime-worker:latest -f docker/facetime-worker/Dockerfile .

# Camera enabled: prove frames reach Chromium inside the freshly built image.
# Warn only — audio-only FaceTime keeps working if this fails.
case "${MECHUS_FT_CAMERA_FEATURE:-0}" in
  1|true|TRUE|yes|YES|on|ON)
    if [[ -z "${MECHUS_API_KEY:-}" ]] && ! grep -Eq '^MECHUS_API_KEY=.+' .env 2>/dev/null; then
      echo "WARNING: MECHUS_API_KEY is empty; camera/vision endpoints would be unauthenticated." >&2
    fi
    echo "Running FaceTime camera probe (quick)..."
    if ! bash scripts/facetime_camera_spike.sh --quick; then
      echo "WARNING: FaceTime camera probe failed; workers will fall back to audio-only." >&2
      echo "  See docs/FACETIME_CAMERA_RUNBOOK.md (Troubleshooting)." >&2
    fi
    ;;
esac

echo "Building and starting..."
if command -v docker-compose &>/dev/null; then
  docker-compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
else
  docker compose -f docker-compose.yml -f docker-compose.zoom.yml up -d --build
fi

echo "Waiting for health..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf http://127.0.0.1:5001/health >/dev/null 2>&1; then
    echo "OK – API is up at http://127.0.0.1:5001"
    exit 0
  fi
  sleep 2
done
echo "Warning: /health did not respond in time; container may still be starting."
exit 0
