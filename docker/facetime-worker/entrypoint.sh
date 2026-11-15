#!/usr/bin/env bash
set -euo pipefail

# ---- Required env ------------------------------------------------------------
# The host camera probe (KOOV_FT_PROBE=1) does not join a meeting.
if [[ "${KOOV_FT_PROBE:-0}" != "1" ]]; then
  : "${KOOV_MEETING_ID:?KOOV_MEETING_ID is required}"
  : "${KOOV_MEETING_LINK:?KOOV_MEETING_LINK is required}"
fi

export HOME="${HOME:-/root}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-runtime}"
mkdir -p "$XDG_RUNTIME_DIR"

export KOOV_DISPLAY_NAME="${KOOV_DISPLAY_NAME:-Koov}"
export KOOV_NOTES_DIR="${KOOV_NOTES_DIR:-/notes}"
mkdir -p "$KOOV_NOTES_DIR"

# A virtual camera is optional. The Python worker owns FFmpeg and degrades to
# audio-only if the host device is missing or unusable.
if [[ -n "${KOOV_FT_VIDEO_DEVICE:-}" ]]; then
  if [[ ! -c "${KOOV_FT_VIDEO_DEVICE}" ]]; then
    export KOOV_FT_VIDEO_DEVICE_ERROR="camera device is not a character device: ${KOOV_FT_VIDEO_DEVICE}"
    echo "[facetime-worker] WARNING: ${KOOV_FT_VIDEO_DEVICE_ERROR}; continuing audio-only" >&2
  elif [[ ! -r "${KOOV_FT_VIDEO_DEVICE}" || ! -w "${KOOV_FT_VIDEO_DEVICE}" ]]; then
    export KOOV_FT_VIDEO_DEVICE_ERROR="camera device is not readable/writable: ${KOOV_FT_VIDEO_DEVICE}"
    echo "[facetime-worker] WARNING: ${KOOV_FT_VIDEO_DEVICE_ERROR}; continuing audio-only" >&2
  fi
fi

# ---- Start PulseAudio --------------------------------------------------------
# Default Debian default.pa loads udev/hardware modules that fail in Docker (no
# /dev/snd). Start a minimal daemon, then build the FaceTime audio graph via pactl.
_pa_cfg="${XDG_RUNTIME_DIR}/koov-pulse.pa"
cat > "$_pa_cfg" <<'EOF'
load-module module-native-protocol-unix auth-anonymous=1
EOF
_pa_err="${XDG_RUNTIME_DIR}/koov-pulse.err"
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
# - koov_sink:       Koov TTS output; koov_mic_source is its monitor remap
# - koov_mic_source: Chrome's microphone source — captures Koov's TTS
#
# Audio routing:
#   Chrome output  → call_sink         (Koov hears participants via call_sink.monitor)
#   Chrome input   → koov_mic_source   (participants hear Koov's TTS)
#   Koov TTS       → koov_sink         (flows into koov_mic_source via remap)
#   Koov listen    → call_sink.monitor (KoovAgent sounddevice input)
#
# Chrome inherits:
#   PULSE_SINK   = call_sink            (incoming FaceTime audio)
#   PULSE_SOURCE = koov_mic_source      (Koov TTS → FaceTime mic, RUNNING from keepalive)
#   PULSE_SERVER = unix:<socket>
#
# Python KoovAgent (env set AFTER Chrome fork):
#   PULSE_SINK   = koov_sink            (TTS output)
#   PULSE_SOURCE = call_sink.monitor    (hears incoming FaceTime audio)

CALL_SINK="${KOOV_FT_CALL_SINK:-call_sink}"
KOOV_SINK="${KOOV_FT_KOOV_SINK:-koov_sink}"
KOOV_MIC_SOURCE="${KOOV_FT_KOOV_MIC_SOURCE:-koov_mic_source}"

pactl load-module module-null-sink "sink_name=${CALL_SINK}" "sink_properties=device.description=${CALL_SINK}" >/dev/null
pactl load-module module-null-sink "sink_name=${KOOV_SINK}" "sink_properties=device.description=${KOOV_SINK}" >/dev/null

# Remap koov_sink.monitor -> a source Chromium can select as microphone
pactl load-module module-remap-source "source_name=${KOOV_MIC_SOURCE}" "master=${KOOV_SINK}.monitor" "source_properties=device.description=${KOOV_MIC_SOURCE}" >/dev/null

# Remap call_sink.monitor -> a named source for KoovAgent (PortAudio "pulse" binds to default source).
LISTEN_SOURCE="${KOOV_FT_LISTEN_SOURCE:-ft_meeting_listen}"
pactl load-module module-remap-source "source_name=${LISTEN_SOURCE}" "master=${CALL_SINK}.monitor" "source_properties=device.description=FaceTimeMeetingListen" >/dev/null

pactl set-default-sink "${KOOV_SINK}" || true

# Keep koov_mic_source RUNNING before Chrome starts.
# koov_mic_source is a remap of koov_sink.monitor. With nothing playing into
# koov_sink, the monitor is SUSPENDED. Suspended sources are often omitted from
# Chrome's getUserMedia() device enumeration, causing FaceTime to show
# "No Device Selected". Feeding a continuous silent stream into koov_sink keeps
# the monitor — and therefore koov_mic_source — in RUNNING state permanently.
if command -v pacat >/dev/null 2>&1; then
  ( cat /dev/zero | pacat --raw --format=s16le --channels=2 --rate=48000 \
      --stream-name="koov-keepalive" \
      -d "${KOOV_SINK}" >/dev/null 2>&1 ) &
  disown || true
fi

# Friendly description helps Chromium's device picker / WebRTC probe.
pactl set-source-property "${KOOV_MIC_SOURCE}" device.description "Koov virtual microphone" >/dev/null 2>&1 || true

# Set koov_mic_source as PA default BEFORE launching Chrome. Chrome inherits
# PULSE_SOURCE=koov_mic_source, calls getUserMedia(), and immediately captures
# from Koov's TTS output — no post-join source swap required.
pactl set-default-source "${KOOV_MIC_SOURCE}" || true

export KOOV_FT_DEFAULT_SINK="${KOOV_SINK}"
export KOOV_FT_DEFAULT_SOURCE="${LISTEN_SOURCE}"
export KOOV_FT_LISTEN_SOURCE="${LISTEN_SOURCE}"
export KOOV_FT_PULSE_SINK="${CALL_SINK}"
export KOOV_FT_PULSE_SOURCE="${KOOV_MIC_SOURCE}"

# ---- Xvfb -------------------------------------------------------------------
Xvfb "${DISPLAY}" -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 0.5

# ---- Koov audio device defaults ---------------------------------------------
# sounddevice on Linux typically exposes a single "pulse" device; by setting Pulse defaults above,
# Koov can use "pulse" for both input and output without manual wiring.
export KOOV_INPUT_DEVICE="${KOOV_INPUT_DEVICE:-pulse}"
export KOOV_OUTPUT_DEVICE="${KOOV_OUTPUT_DEVICE:-pulse}"

# ---- Ensure Chromium uses correct Pulse routing ------------------------------
# PULSE_SERVER tells Chrome (and all PulseAudio clients in this container)
# exactly where the PA socket is, regardless of XDG_RUNTIME_DIR inheritance.
export PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"
export PULSE_SINK="${KOOV_FT_PULSE_SINK}"
export PULSE_SOURCE="${KOOV_FT_PULSE_SOURCE}"

# ---- Host camera probe (scripts/facetime_camera_spike.sh) --------------------
# Same Pulse/Xvfb setup as a real worker, but runs the Phase 0 camera checks
# instead of joining a call.
if [[ "${KOOV_FT_PROBE:-0}" == "1" ]]; then
  exec python3 -m koov.facetime_camera_probe
fi

# ---- Start worker ------------------------------------------------------------
exec python3 -m koov.facetime_worker

