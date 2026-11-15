#!/bin/bash
# Start PulseAudio with a virtual null sink so the Zoom SDK gets a valid ALSA
# device for raw audio callbacks — no real sound hardware or /dev/snd required.
# User-mode PulseAudio requires a D-Bus session; we use dbus-run-session when available.
set -e

export HOME="${HOME:-/home/appuser}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-runtime}"
mkdir -p "$XDG_RUNTIME_DIR"

_pa_running() {
    pactl info >/dev/null 2>&1
}

_start_pulse() {
    pulseaudio --kill 2>/dev/null || true
    sleep 0.3

    # Stock default.pa loads udev/hardware modules that fail in Docker (no /dev/snd, no udev).
    # Use a minimal script: protocol socket + null sink/source only.
    _pa_cfg="${XDG_RUNTIME_DIR}/mechus-pulse.pa"
    cat > "$_pa_cfg" <<'EOF'
load-module module-native-protocol-unix auth-anonymous=1
load-module module-null-sink sink_name=mechus_null sink_properties=device.description=Mechus-Virtual-Out
load-module module-null-source source_name=mechus_null_source source_properties=device.description=Mechus-Virtual-In
set-default-sink mechus_null
set-default-source mechus_null_source
EOF

    _pa_err="${XDG_RUNTIME_DIR}/mechus-pulse.err"
    : > "$_pa_err"
    if ! pulseaudio -D -n -F "$_pa_cfg" --exit-idle-time=-1 --log-level=warn 2>>"$_pa_err"; then
        echo "[Mechus] pulseaudio -D failed; last messages:"
        tail -n 20 "$_pa_err" 2>/dev/null || true
    fi

    for _i in 1 2 3 4 5 6; do
        if _pa_running; then break; fi
        sleep 0.5
    done

    if _pa_running; then
        pactl set-default-sink   mechus_null        2>/dev/null || true
        pactl set-default-source mechus_null_source 2>/dev/null || true
        echo "[Mechus] PulseAudio running — virtual null sink ready (no sound hardware needed)"
    else
        echo "[Mechus] WARNING: PulseAudio did not start; Zoom raw-audio callbacks may not fire"
        echo "[Mechus] PulseAudio stderr (if any):"
        cat "$_pa_err" 2>/dev/null || true
    fi
}

_run_with_pulse() {
    _start_pulse
    exec "$@"
}

if command -v pulseaudio >/dev/null 2>&1; then
    if command -v dbus-run-session >/dev/null 2>&1; then
        # User-mode PulseAudio needs D-Bus; run PA + gunicorn inside a session
        export -f _start_pulse _pa_running _run_with_pulse
        exec dbus-run-session -- bash -c '_run_with_pulse "$@"' _ "$@"
    else
        _start_pulse
        exec "$@"
    fi
else
    echo "[Mechus] WARNING: pulseaudio not found; ALSA will use dummy PCM fallback"
    exec "$@"
fi
