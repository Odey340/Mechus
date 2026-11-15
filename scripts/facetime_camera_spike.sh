#!/usr/bin/env bash
# Phase 0 feasibility probe: generated frames -> v4l2loopback -> Docker -> Chromium.
# Runs on the Linux deployment VM after scripts/setup_facetime_cameras.sh.
#
#   scripts/facetime_camera_spike.sh            # full probe on the first pool device
#   scripts/facetime_camera_spike.sh --quick    # skip the producer kill/restart check
#   MECHUS_FT_PROBE_DEVICE=/dev/video21 scripts/facetime_camera_spike.sh
#
# Prints a JSON report; exit code 0 only when every check passes.
set -euo pipefail

quick=0
if [[ "${1:-}" == "--quick" ]]; then
  quick=1
fi
image="${MECHUS_FT_WORKER_IMAGE:-mechus-facetime-worker:latest}"
raw_devices="${MECHUS_FT_VIDEO_DEVICES:-/dev/video20,/dev/video21,/dev/video22,/dev/video23}"
device="${MECHUS_FT_PROBE_DEVICE:-${raw_devices%%,*}}"
device="${device//[[:space:]]/}"

if [[ ! "$device" =~ ^/dev/video[0-9]+$ ]]; then
  echo "ERROR: invalid probe device: $device" >&2
  exit 2
fi
if [[ ! -c "$device" ]]; then
  echo "ERROR: $device is missing; run scripts/verify_facetime_camera_host.sh" >&2
  exit 3
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "ERROR: worker image $image not found; build it first:" >&2
  echo "  docker build -t mechus-facetime-worker:latest -f docker/facetime-worker/Dockerfile ." >&2
  exit 4
fi
if fuser "$device" >/dev/null 2>&1; then
  echo "ERROR: $device is in use (an active worker may own it). Pick a free device with MECHUS_FT_PROBE_DEVICE." >&2
  exit 5
fi

exec docker run --rm \
  --name "mechus-camera-probe-$$" \
  --device "${device}:${device}:rwm" \
  -e MECHUS_FT_PROBE=1 \
  -e MECHUS_FT_PROBE_QUICK="$quick" \
  -e MECHUS_FT_VIDEO_DEVICE="$device" \
  -e MECHUS_FT_VIDEO_WIDTH="${MECHUS_FT_VIDEO_WIDTH:-640}" \
  -e MECHUS_FT_VIDEO_HEIGHT="${MECHUS_FT_VIDEO_HEIGHT:-360}" \
  -e MECHUS_FT_VIDEO_FPS="${MECHUS_FT_VIDEO_FPS:-12}" \
  "$image"
