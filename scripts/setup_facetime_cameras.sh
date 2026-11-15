#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "ERROR: run this script on the Linux deployment VM." >&2
  exit 2
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: run with sudo: sudo scripts/setup_facetime_cameras.sh" >&2
  exit 3
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: this setup script currently supports Debian/Ubuntu apt hosts." >&2
  exit 4
fi

kernel="$(uname -r)"
apt-get update
apt-get install -y "linux-headers-${kernel}" v4l2loopback-dkms v4l-utils

raw_devices="${MECHUS_FT_VIDEO_DEVICES:-/dev/video20,/dev/video21,/dev/video22,/dev/video23}"
IFS=',' read -r -a device_paths <<< "$raw_devices"
video_numbers=()
labels=()
for raw in "${device_paths[@]}"; do
  device="${raw//[[:space:]]/}"
  if [[ ! "$device" =~ ^/dev/video([0-9]+)$ ]]; then
    echo "ERROR: invalid video device: $raw" >&2
    exit 5
  fi
  video_numbers+=("${BASH_REMATCH[1]}")
  labels+=("Mechus Virtual Camera ${#video_numbers[@]}")
done
if [[ ${#video_numbers[@]} -eq 0 ]]; then
  echo "ERROR: no devices configured." >&2
  exit 6
fi

numbers_csv="$(IFS=,; echo "${video_numbers[*]}")"
labels_csv="$(IFS=,; echo "${labels[*]}")"
install -d /etc/modprobe.d /etc/modules-load.d
printf '%s\n' "v4l2loopback" > /etc/modules-load.d/mechus-facetime-camera.conf
printf '%s\n' \
  "options v4l2loopback devices=${#video_numbers[@]} video_nr=${numbers_csv} card_label=\"${labels_csv}\" exclusive_caps=1" \
  > /etc/modprobe.d/mechus-facetime-camera.conf

if [[ -d /sys/module/v4l2loopback ]]; then
  echo "v4l2loopback is already loaded. Refusing to unload it automatically because active calls may own devices."
  echo "Reboot during a maintenance window, then run scripts/verify_facetime_camera_host.sh."
else
  if ! modprobe v4l2loopback \
    "devices=${#video_numbers[@]}" \
    "video_nr=${numbers_csv}" \
    "card_label=${labels_csv}" \
    "exclusive_caps=1"; then
    echo "ERROR: modprobe v4l2loopback failed." >&2
    if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
      echo "  Secure Boot is enabled: the DKMS module must be signed and its key enrolled" >&2
      echo "  (sudo mokutil --import /var/lib/dkms/mok.pub, then reboot and confirm in the MOK manager)." >&2
      echo "  Do not disable Secure Boot; see docs/FACETIME_CAMERA_RUNBOOK.md." >&2
    fi
    exit 7
  fi
fi

MECHUS_FT_VIDEO_DEVICES="$raw_devices" "$(dirname "$0")/verify_facetime_camera_host.sh"
