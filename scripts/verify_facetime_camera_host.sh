#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: FaceTime virtual cameras require the Linux deployment host." >&2
  exit 2
fi
if [[ ! -d /sys/module/v4l2loopback ]]; then
  echo "ERROR: v4l2loopback is not loaded. Run scripts/setup_facetime_cameras.sh with sudo." >&2
  exit 3
fi

raw_devices="${MECHUS_FT_VIDEO_DEVICES:-/dev/video20,/dev/video21,/dev/video22,/dev/video23}"
IFS=',' read -r -a devices <<< "$raw_devices"
if [[ ${#devices[@]} -eq 0 ]]; then
  echo "ERROR: MECHUS_FT_VIDEO_DEVICES is empty." >&2
  exit 4
fi
for raw in "${devices[@]}"; do
  device="${raw//[[:space:]]/}"
  if [[ ! "$device" =~ ^/dev/video[0-9]+$ ]]; then
    echo "ERROR: invalid video device in MECHUS_FT_VIDEO_DEVICES: $raw" >&2
    exit 5
  fi
  if [[ ! -c "$device" ]]; then
    echo "ERROR: expected V4L2 character device is missing: $device" >&2
    exit 6
  fi
  if [[ ! -r "$device" || ! -w "$device" ]]; then
    echo "ERROR: device must be readable and writable by Docker workers: $device" >&2
    exit 7
  fi
  if command -v v4l2-ctl >/dev/null 2>&1; then
    v4l2-ctl --device "$device" --all >/dev/null
  fi
done
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to launch FaceTime workers." >&2
  exit 8
fi
echo "OK: v4l2loopback is loaded and ${#devices[@]} Mechus camera device(s) are ready."
