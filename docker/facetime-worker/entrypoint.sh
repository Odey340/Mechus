#!/usr/bin/env bash
set -euo pipefail

# ---- Required env ------------------------------------------------------------
# The host camera probe (MECHUS_FT_PROBE=1) does not join a meeting.
if [[ "${MECHUS_FT_PROBE:-0}" != "1" ]]; then
  : "${MECHUS_MEETING_ID:?MECHUS_MEETING_ID is required}"
  : "${MECHUS_MEETING_LINK:?MECHUS_MEETING_LINK is required}"
fi

export HOME="${HOME:-/root}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-runtime}"
mkdir -p "$XDG_RUNTIME_DIR"

export MECHUS_DISPLAY_NAME="${MECHUS_DISPLAY_NAME:-Mechus}"
export MECHUS_NOTES_DIR="${MECHUS_NOTES_DIR:-/notes}"
mkdir -p "$MECHUS_NOTES_DIR"

# A virtual camera is optional. The Python worker owns FFmpeg and degrades to
# audio-only if the host device is missing or unusable.
if [[ -n "${MECHUS_FT_VIDEO_DEVICE:-}" ]]; then
  if [[ ! -c "${MECHUS_FT_VIDEO_DEVICE}" ]]; then
    export MECHUS_FT_VIDEO_DEVICE_ERROR="camera device is not a character device: ${MECHUS_FT_VIDEO_DEVICE}"
    echo "[facetime-worker] WARNING: ${MECHUS_FT_VIDEO_DEVICE_ERROR}; continuing audio-only" >&2
  elif [[ ! -r "${MECHUS_FT_VIDEO_DEVICE}" || ! -w "${MECHUS_FT_VIDEO_DEVICE}" ]]; then
    export MECHUS_FT_VIDEO_DEVICE_ERROR="camera device is not readable/writable: ${MECHUS_FT_VIDEO_DEVICE}"
    echo "[facetime-worker] WARNING: ${MECHUS_FT_VIDEO_DEVICE_ERROR}; continuing audio-only" >&2
  fi
fi

# ---- Start PulseAudio --------------------------------------------------------
# Default Debian default.pa loads udev/hardware modules that fail in Docker (no
# /dev/snd). Start a minimal daemon, then build the FaceTime audio graph via pactl.
_pa_cfg="${XDG_RUNTIME_DIR}/mechus-pulse.pa"
cat > "$_pa_cfg" <<'EOF'
load-module module-native-protocol-unix auth-anonymous=1
EOF
_pa_err="${XDG_RUNTIME_DIR}/mechus-pulse.err"
: > "$_pa_err"
pulseaudio --kill 2>/dev/null || true
sleep 0.3
if ! pulseaudio -D -n -F "$_pa_cfg" --exit-idle-time=-1 --log-level=warn 2>>"$_pa_err"; then
  echo "[facetime-worker] pulseaudio -D failed:" >&2
  cat "$_pa_err" >&2 || true
fi
for _i in 1 2 3 4 5 6 7 8 9 10; do
  if pactl info >/dev/null 2>&1; then break; fi
  sleep 0.4
done
if ! pactl info >/dev/null 2>&1; then
  echo "[facetime-worker] FATAL: PulseAudio did not become ready; cannot load audio graph." >&2
  cat "$_pa_err" >&2 || true
  exit 1
fi

# Create per-meeting virtual audio graph.
# - call_sink:       Chromium outputs incoming FaceTime audio here
# - mechus_sink:       Mechus TTS output; mechus_mic_source is its monitor remap
# - mechus_mic_source: Chrome's microphone source — captures Mechus's TTS
#
# Audio routing:
#   Chrome output  → call_sink         (Mechus hears participants via call_sink.monitor)
#   Chrome input   → mechus_mic_source   (participants hear Mechus's TTS)
#   Mechus TTS       → mechus_sink         (flows into mechus_mic_source via remap)
#   Mechus listen    → call_sink.monitor (MechusAgent sounddevice input)
#
# Chrome inherits:
#   PULSE_SINK   = call_sink            (incoming FaceTime audio)
#   PULSE_SOURCE = mechus_mic_source      (Mechus TTS → FaceTime mic, RUNNING from keepalive)
#   PULSE_SERVER = unix:<socket>
#
# Python MechusAgent (env set AFTER Chrome fork):
#   PULSE_SINK   = mechus_sink            (TTS output)
#   PULSE_SOURCE = call_sink.monitor    (hears incoming FaceTime audio)

CALL_SINK="${MECHUS_FT_CALL_SINK:-call_sink}"
MECHUS_SINK="${MECHUS_FT_MECHUS_SINK:-mechus_sink}"
MECHUS_MIC_SOURCE="${MECHUS_FT_MECHUS_MIC_SOURCE:-mechus_mic_source}"

pactl load-module module-null-sink "sink_name=${CALL_SINK}" "sink_properties=device.description=${CALL_SINK}" >/dev/null
pactl load-module module-null-sink "sink_name=${MECHUS_SINK}" "sink_properties=device.description=${MECHUS_SINK}" >/dev/null

# Remap mechus_sink.monitor -> a source Chromium can select as microphone
pactl load-module module-remap-source "source_name=${MECHUS_MIC_SOURCE}" "master=${MECHUS_SINK}.monitor" "source_properties=device.description=${MECHUS_MIC_SOURCE}" >/dev/null

# Remap call_sink.monitor -> a named source for MechusAgent (PortAudio "pulse" binds to default source).
LISTEN_SOURCE="${MECHUS_FT_LISTEN_SOURCE:-ft_meeting_listen}"
pactl load-module module-remap-source "source_name=${LISTEN_SOURCE}" "master=${CALL_SINK}.monitor" "source_properties=device.description=FaceTimeMeetingListen" >/dev/null

pactl set-default-sink "${MECHUS_SINK}" || true

# Keep mechus_mic_source RUNNING before Chrome starts.
# mechus_mic_source is a remap of mechus_sink.monitor. With nothing playing into
# mechus_sink, the monitor is SUSPENDED. Suspended sources are often omitted from
# Chrome's getUserMedia() device enumeration, causing FaceTime to show
# "No Device Selected". Feeding a continuous silent stream into mechus_sink keeps
# the monitor — and therefore mechus_mic_source — in RUNNING state permanently.
if command -v pacat >/dev/null 2>&1; then
  ( cat /dev/zero | pacat --raw --format=s16le --channels=2 --rate=48000 \
      --stream-name="mechus-keepalive" \
      -d "${MECHUS_SINK}" >/dev/null 2>&1 ) &
  disown || true
fi

# Friendly description helps Chromium's device picker / WebRTC probe.
pactl set-source-property "${MECHUS_MIC_SOURCE}" device.description "Mechus virtual microphone" >/dev/null 2>&1 || true

# Set mechus_mic_source as PA default BEFORE launching Chrome. Chrome inherits
# PULSE_SOURCE=mechus_mic_source, calls getUserMedia(), and immediately captures
# from Mechus's TTS output — no post-join source swap required.
pactl set-default-source "${MECHUS_MIC_SOURCE}" || true

export MECHUS_FT_DEFAULT_SINK="${MECHUS_SINK}"
export MECHUS_FT_DEFAULT_SOURCE="${LISTEN_SOURCE}"
export MECHUS_FT_LISTEN_SOURCE="${LISTEN_SOURCE}"
export MECHUS_FT_PULSE_SINK="${CALL_SINK}"
export MECHUS_FT_PULSE_SOURCE="${MECHUS_MIC_SOURCE}"

# ---- Xvfb -------------------------------------------------------------------
Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 0.5

# ---- Mechus audio device defaults ---------------------------------------------
# sounddevice on Linux typically exposes a single "pulse" device; by setting Pulse defaults above,
# Mechus can use "pulse" for both input and output without manual wiring.
export MECHUS_INPUT_DEVICE="${MECHUS_INPUT_DEVICE:-pulse}"
export MECHUS_OUTPUT_DEVICE="${MECHUS_OUTPUT_DEVICE:-pulse}"

# ---- Ensure Chromium uses correct Pulse routing ------------------------------
# PULSE_SERVER tells Chrome (and all PulseAudio clients in this container)
# exactly where the PA socket is, regardless of XDG_RUNTIME_DIR inheritance.
export PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"
export PULSE_SINK="${MECHUS_FT_PULSE_SINK}"
export PULSE_SOURCE="${MECHUS_FT_PULSE_SOURCE}"

# ---- Host camera probe (scripts/facetime_camera_spike.sh) --------------------
# Same Pulse/Xvfb setup as a real worker, but runs the Phase 0 camera checks
# instead of joining a call.
if [[ "${MECHUS_FT_PROBE:-0}" == "1" ]]; then
  exec python3 -m mechus.facetime_camera_probe
fi

# ---- Start worker ------------------------------------------------------------
exec python3 -m mechus.facetime_worker

