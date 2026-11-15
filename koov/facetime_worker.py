"""Koov FaceTime-web worker — runs one FaceTime link session in isolation.

This worker is intended to run inside a dedicated Docker container per meeting_id.

High-level responsibilities:
- Start KoovAgent using PulseAudio's default input/output devices (sounddevice "pulse").
- Join the FaceTime link in Chromium via Selenium (headed inside Xvfb).
- Write status to ``<KOOV_NOTES_DIR>/worker-status-<meeting_id>.json`` so api_server can monitor.

Limitations:
- FaceTime on the web requires the host to admit participants; we can only wait for admission.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse, urlunparse


def _normalize_facetime_web_url(link: str) -> str:
    """Linux Chromium often will not load the web join UI from ``facetime://``; use https."""
    s = (link or "").strip()
    if not s:
        return s
    low = s.lower()
    if low.startswith("facetime://"):
        rest = s[len("facetime://") :].lstrip("/")
        if rest.lower().startswith(("http://", "https://")):
            return rest
        return "https://" + rest
    return s


def _chromedriver_path() -> str | None:
    for p in ("/usr/bin/chromedriver", "/usr/local/bin/chromedriver"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("chromedriver")


def _rewire_chrome_source_outputs(meeting_id: str, target_source: str = "koov_mic_source") -> int:
    """Ensure Chrome/Chromium PulseAudio capture streams end up on target_source.

    In the current setup, Chromium should ideally start on ``koov_mic_source`` from the
    moment it launches. This function therefore treats both cases as success:

    1. a Chromium source-output needed moving to ``target_source``
    2. a Chromium source-output was already on ``target_source``

    We still skip Python/PortAudio capture streams so KoovAgent keeps listening to
    ``call_sink.monitor`` rather than being rewired onto its own virtual microphone.
    """
    import subprocess as _sp

    def _resolve_source_index(name: str) -> str | None:
        """Return the PulseAudio numeric index for a source by name."""
        try:
            r2 = _sp.run(
                ["pactl", "list", "sources", "short"],
                stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
            )
            for ln in (r2.stdout or "").splitlines():
                parts = ln.split("\t")
                if len(parts) >= 2 and name in parts[1]:
                    return parts[0].strip()
        except Exception:
            pass
        return None

    def _parse_prop_line(line: str) -> tuple[str, str] | None:
        """Parse ``application.name = "Chromium"``-style lines from pactl long output."""
        m = re.match(r'^\s+([a-zA-Z0-9_.]+)\s*=\s*"(.*)"\s*$', line)
        if m:
            return m.group(1), m.group(2)
        m2 = re.match(r"^\s+([a-zA-Z0-9_.]+)\s*=\s*(\S+)\s*$", line)
        if m2:
            return m2.group(1), m2.group(2)
        return None

    def _looks_like_chrome(props: dict[str, str]) -> bool:
        name = (props.get("application.name") or "").lower()
        binary = (props.get("application.process.binary") or "").lower()
        icon = (props.get("application.icon_name") or "").lower()
        media = (props.get("media.name") or "").lower()
        joined = " ".join((name, binary, icon, media))
        for blob in (name, binary, icon, media):
            if any(
                k in blob
                for k in ("chromium", "chrome", "google-chrome", "audioipc", "libcef")
            ):
                return True
        if "webrtc" in joined or "libpeer" in joined:
            return True
        return False

    def _driver_ok_for_capture(driver: str | None) -> bool:
        """Chromium capture is usually protocol-native; ALSA→Pulse can show other drivers."""
        if not driver:
            return False
        d = driver.lower()
        if "protocol-native" in d:
            return True
        if "alsa" in d:
            return True
        if "pipewire" in d:
            return True
        return False

    def _looks_like_python_capture(props: dict[str, str]) -> bool:
        """KoovAgent records FaceTime playback from call_sink.monitor — do not rewire that."""
        name = (props.get("application.name") or "").lower()
        binary = (props.get("application.process.binary") or "").lower()
        if "python" in binary or binary.endswith("python3") or "python3." in binary:
            return True
        if "portaudio" in name or "python" in name:
            return True
        if "alsa plug-in" in name and "python" in name:
            return True
        return False

    def _move_source_output(so_idx: str, label: str, src_before: str) -> bool:
        result = _sp.run(
            ["pactl", "move-source-output", so_idx, target_source],
            check=False,
            stdout=_sp.DEVNULL,
            stderr=_sp.PIPE,
            text=True,
        )
        if result.returncode == 0:
            _ft_step(
                meeting_id,
                "pulse_rewire",
                "moved source-output #%s → %s (%s src_was=%s)"
                % (so_idx, target_source, label, src_before or "?"),
            )
            return True
        _ft_step(
            meeting_id,
            "pulse_rewire",
            "move source-output #%s failed: %s"
            % (so_idx, (result.stderr or "").strip()),
            level=logging.WARNING,
        )
        return False

    try:
        r = _sp.run(
            ["pactl", "list", "source-outputs"],
            stdout=_sp.PIPE,
            stderr=_sp.STDOUT,
            text=True,
        )
        output = r.stdout or ""
        target_idx = _resolve_source_index(target_source)
        moved = 0
        already_on_target = 0
        non_python_unmatched: list[str] = []

        blocks: list[dict[str, Any]] = []
        current_idx: str | None = None
        current_source: str | None = None
        current_driver: str | None = None
        props: dict[str, str] = {}

        def _flush_block() -> None:
            nonlocal current_idx, current_source, current_driver, props
            if current_idx is not None:
                blocks.append(
                    {
                        "idx": current_idx,
                        "source": (current_source or "").strip(),
                        "driver": current_driver or "",
                        "props": dict(props),
                    }
                )
            current_idx = None
            current_source = None
            current_driver = None
            props = {}

        for line in output.splitlines():
            m = re.match(r"Source Output #(\d+)", line.strip())
            if m:
                _flush_block()
                current_idx = m.group(1)
                continue
            if current_idx is None:
                continue
            sm = re.match(r"^\s*Source:\s*(\S+)", line)
            if sm:
                current_source = sm.group(1)
                continue
            dm = re.match(r"^\s*Driver:\s*(\S+)", line)
            if dm:
                current_driver = dm.group(1)
                continue
            pr = _parse_prop_line(line)
            if pr:
                props[pr[0]] = pr[1]
        _flush_block()

        for blk in blocks:
            so_idx = str(blk.get("idx") or "")
            src = str(blk.get("source") or "")
            driver = str(blk.get("driver") or "")
            bprops = dict(blk.get("props") or {})
            appn = (bprops.get("application.name") or "")
            binary = (bprops.get("application.process.binary") or "")
            media = (bprops.get("media.name") or "")

            _ft_step(
                meeting_id,
                "pulse_rewire",
                "stream #%s driver=%r src=%r app=%r binary=%r media=%r"
                % (so_idx, driver[:30], src, appn[:40], binary[:30], media[:30]),
            )

            if not _driver_ok_for_capture(driver):
                continue

            if src == target_source or (target_idx and src == target_idx):
                if not _looks_like_python_capture(bprops):
                    already_on_target += 1
                    _ft_step(
                        meeting_id,
                        "pulse_rewire",
                        "stream #%s already on target %s (app=%r binary=%r)"
                        % (so_idx, target_source, appn[:40], binary[:20]),
                    )
                continue

            if _looks_like_python_capture(bprops):
                continue

            if not _looks_like_chrome(bprops):
                non_python_unmatched.append(
                    "idx=%s app=%r binary=%r driver=%r src=%r"
                    % (so_idx, appn[:40], binary[:30], driver[:30], src)
                )
                continue

            label = "chrome app=%r binary=%r" % (appn[:40], binary[:20])
            if _move_source_output(so_idx, label, src):
                moved += 1

        total_ok = moved + already_on_target
        if total_ok == 0:
            n = len(blocks)
            if n == 0:
                detail = (
                    "Chromium has no Pulse capture stream yet — FaceTime often shows "
                    "'No Device Selected' until PULSE_SOURCE/default_source point at a RUNNING source."
                )
            else:
                detail = (
                    "no streams on target (target=%s, total_blocks=%d, unmatched_non_python=%d)"
                    % (target_source, n, len(non_python_unmatched))
                )
            _ft_step(meeting_id, "pulse_rewire", detail)
        return total_ok
    except Exception as exc:
        _ft_step(meeting_id, "pulse_rewire", "error: %s" % exc, level=logging.WARNING)
        return 0


def _rewire_chrome_sink_inputs(meeting_id: str, target_sink: str = "call_sink") -> int:
    """Route Chromium **playback** (remote audio) onto ``call_sink`` so KoovAgent hears the call.

    Chrome often ignores inherited ``PULSE_SINK`` and plays FaceTime audio to the wrong sink;
    without this, ``call_sink.monitor`` stays silent and transcript wake never fires.
    """
    import subprocess as _sp

    target_sink = (target_sink or "call_sink").strip()
    moved = 0
    try:
        r = _sp.run(
            ["pactl", "list", "sink-inputs", "short"],
            stdout=_sp.PIPE,
            stderr=_sp.STDOUT,
            text=True,
        )
        for line in (r.stdout or "").splitlines():
            low = line.lower()
            if not any(
                k in low
                for k in (
                    "chromium",
                    "chrome",
                    "google-chrome",
                    "libcef",
                    "webrtc",
                    "audioipc",
                )
            ):
                continue
            parts = line.split("\t")
            if not parts:
                continue
            idx = parts[0].strip()
            if not idx.isdigit():
                continue
            cur_sink = parts[1].strip() if len(parts) > 1 else ""
            if target_sink in cur_sink or cur_sink.endswith(target_sink):
                continue
            res = _sp.run(
                ["pactl", "move-sink-input", idx, target_sink],
                check=False,
                stdout=_sp.DEVNULL,
                stderr=_sp.PIPE,
                text=True,
            )
            if res.returncode == 0:
                moved += 1
                _ft_step(
                    meeting_id,
                    "pulse_playback",
                    "moved sink-input #%s → %s (was %r)"
                    % (idx, target_sink, cur_sink),
                )
            else:
                _ft_step(
                    meeting_id,
                    "pulse_playback",
                    "move sink-input #%s failed: %s"
                    % (idx, (res.stderr or "").strip()[:200]),
                    level=logging.WARNING,
                )
        if moved == 0:
            _ft_step(
                meeting_id,
                "pulse_playback",
                "no chromium sink-inputs moved to %r (FaceTime may not be playing audio yet)"
                % target_sink,
            )
        return moved
    except Exception as exc:
        _ft_step(
            meeting_id,
            "pulse_playback",
            "sink-input rewire error: %s" % exc,
            level=logging.WARNING,
        )
        return 0


def _pin_koov_tts_to_sink(meeting_id: str, target_sink: str = "koov_sink") -> int:
    """Force Koov's (python) TTS playback onto ``koov_sink`` so it reaches FaceTime.

    PortAudio's ``pulse`` device binds to whatever the *default sink* was when the
    output stream first opened. If that race lands Koov's TTS on ``call_sink``
    (where Koov *listens*) instead of ``koov_sink`` (what ``koov_mic_source``
    monitors and FaceTime captures), Koov can hear everyone but nobody hears Koov.
    We defensively move any python sink-input onto ``koov_sink`` every maintenance
    pass. Idempotent: a sink-input already on the target is skipped.

    Returns the number of sink-inputs moved this pass.
    """
    import subprocess as _sp

    target_sink = (target_sink or "koov_sink").strip()
    moved = 0
    try:
        # Resolve the target sink's numeric index so we can skip no-op moves.
        target_idx = ""
        rs = _sp.run(
            ["pactl", "list", "sinks", "short"],
            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
        )
        for line in (rs.stdout or "").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[1].strip() == target_sink:
                target_idx = cols[0].strip()
                break

        r = _sp.run(
            ["pactl", "list", "sink-inputs"],
            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
        )
        cur_idx: str | None = None
        cur_sink: str = ""
        is_python = False

        def _maybe_move() -> int:
            nonlocal cur_idx, cur_sink, is_python
            n = 0
            if cur_idx is not None and is_python and cur_sink != target_idx:
                res = _sp.run(
                    ["pactl", "move-sink-input", cur_idx, target_sink],
                    check=False, stdout=_sp.DEVNULL, stderr=_sp.PIPE, text=True,
                )
                if res.returncode == 0:
                    n = 1
                    _ft_step(
                        meeting_id, "pulse_tts",
                        "pinned Koov TTS sink-input #%s → %s (was sink %r)"
                        % (cur_idx, target_sink, cur_sink or "?"),
                    )
                else:
                    _ft_step(
                        meeting_id, "pulse_tts",
                        "move TTS sink-input #%s failed: %s"
                        % (cur_idx, (res.stderr or "").strip()[:160]),
                        level=logging.WARNING,
                    )
            return n

        for line in (r.stdout or "").splitlines():
            s = line.strip()
            if s.startswith("Sink Input #"):
                moved += _maybe_move()
                cur_idx = s.split("#", 1)[1].strip()
                cur_sink = ""
                is_python = False
            elif s.startswith("Sink:"):
                cur_sink = s.split(":", 1)[1].strip()
            elif "application.process.binary" in s or "application.name" in s:
                low = s.lower()
                if "python" in low:
                    is_python = True
        moved += _maybe_move()
    except Exception as exc:  # noqa: BLE001
        _ft_step(
            meeting_id, "pulse_tts",
            "TTS sink-input pin error: %s" % exc,
            level=logging.DEBUG,
        )
    return moved


def _reassert_pulse_for_chromium() -> None:
    """After ``load_env_file``, restore Pulse routing from the FaceTime entrypoint.

    The API often bind-mounts the host ``.env`` into the worker; that file may set
    ``PULSE_SINK`` / ``PULSE_SOURCE`` for Zoom on the host and would overwrite the
    per-container FaceTime graph if applied blindly.

    Also ensures PULSE_SERVER is set so Chrome's out-of-process audio service
    (which may not inherit XDG_RUNTIME_DIR) can always find the PulseAudio socket.
    """
    ps = (os.environ.get("KOOV_FT_PULSE_SINK") or "").strip()
    psrc = (os.environ.get("KOOV_FT_PULSE_SOURCE") or "").strip()
    if ps:
        os.environ["PULSE_SINK"] = ps
    if psrc:
        os.environ["PULSE_SOURCE"] = psrc
    # Ensure PULSE_SERVER is set so Chrome's sandboxed audio subprocess
    # can locate PulseAudio without relying on XDG_RUNTIME_DIR inheritance.
    if not os.environ.get("PULSE_SERVER"):
        xdg = os.environ.get("XDG_RUNTIME_DIR", "/tmp/pulse-runtime")
        os.environ["PULSE_SERVER"] = f"unix:{xdg}/pulse/native"


def _ft_listen_source_name() -> str:
    return (os.environ.get("KOOV_FT_DEFAULT_SOURCE") or "call_sink.monitor").strip()


def _ft_tts_sink_name() -> str:
    return (os.environ.get("KOOV_FT_DEFAULT_SINK") or "koov_sink").strip()


def _reassert_agent_listen_route(meeting_id: str, *, reason: str) -> None:
    """KoovAgent must hear FaceTime playback on call_sink.monitor, not the bot mic source."""
    src = _ft_listen_source_name()
    try:
        import subprocess as _sp

        _sp.run(
            ["pactl", "set-default-source", src],
            check=False,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )
        _ft_step(
            meeting_id,
            "pulse_listen",
            "PA default-source -> %r (%s)" % (src, reason),
        )
    except Exception as exc:
        _ft_step(
            meeting_id,
            "pulse_listen",
            "set-default-source %r failed (non-fatal): %s" % (src, exc),
            level=logging.WARNING,
        )


def _reassert_tts_output_route(meeting_id: str) -> None:
    """TTS must play into koov_sink so it reaches FaceTime via koov_mic_source."""
    sink = _ft_tts_sink_name()
    try:
        import subprocess as _sp

        _sp.run(
            ["pactl", "set-default-sink", sink],
            check=False,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )
    except Exception:
        pass


def _diagnose_ft_audio_routing(meeting_id: str) -> dict[str, Any]:
    """Snapshot the live PulseAudio graph for the Koov-voice-out path and log it.

    Captures the ground truth for "remote can't hear Koov":
      - koov_mic_source state (must be RUNNING; SUSPENDED → Chrome drops it)
      - whether a Chromium capture stream is on koov_mic_source (mic → FaceTime)
      - whether the Python/TTS playback stream is on koov_sink (TTS → koov_mic_source)
    Logged each maintenance cycle so a single user test reveals the exact break
    from worker-status steps, with no need for perfectly-timed live inspection.
    """
    import subprocess as _sp

    sink = _ft_tts_sink_name()
    mic_src = (os.environ.get("KOOV_FT_KOOV_MIC_SOURCE") or "koov_mic_source").strip()
    info: dict[str, Any] = {
        "mic_source_state": "?",
        "chrome_on_mic_source": False,
        "tts_on_koov_sink": False,
        "tts_sink_actual": "",
    }
    try:
        r = _sp.run(
            ["pactl", "list", "short", "sources"],
            stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True,
        )
        for ln in (r.stdout or "").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2 and mic_src in parts[1]:
                info["mic_source_state"] = (parts[-1] or "").strip() or "?"
                break
    except Exception:
        pass
    try:
        r = _sp.run(
            ["pactl", "list", "source-outputs"],
            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
        )
        blob = (r.stdout or "").lower()
        if mic_src.lower() in blob and ("chrom" in blob or "webrtc" in blob):
            info["chrome_on_mic_source"] = True
    except Exception:
        pass
    try:
        r = _sp.run(
            ["pactl", "list", "sink-inputs"],
            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
        )
        cur_sink = ""
        is_py = False
        for ln in (r.stdout or "").splitlines():
            s = ln.strip()
            if s.startswith("Sink Input"):
                cur_sink, is_py = "", False
            elif s.startswith("Sink:"):
                cur_sink = s.split(":", 1)[1].strip()
            elif "application.process.binary" in s or "application.name" in s:
                if "python" in s.lower():
                    is_py = True
            if is_py and cur_sink:
                info["tts_sink_actual"] = cur_sink
    except Exception:
        pass
    _ft_step(
        meeting_id,
        "audio_route",
        "VOICE-OUT diag: koov_mic_source=%s chrome_capturing_mic=%s tts_sink=%r want=%r"
        % (
            info["mic_source_state"],
            info["chrome_on_mic_source"],
            info["tts_sink_actual"] or "?",
            sink,
        ),
    )
    return info


def _url_for_log(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return ""


def _write_status(path: str, data: dict[str, Any]) -> None:
    """Atomic write via temp-file rename to avoid partial reads."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def facetime_dashboard_labels(facetime_state: str | None) -> dict[str, str]:
    """Map internal ``facetime_state`` to dashboard-friendly fields (GET /api/status).

    Returns keys: ``facetime_phase``, ``facetime_phase_label``, ``facetime_state_label``.
    ``facetime_phase`` is stable for UI branching: ``connecting``, ``waiting_for_admit``,
    ``in_meeting``, ``ended``, ``error``, or empty when state is unknown.
    """
    fs = (facetime_state or "").strip()
    if not fs:
        return {"facetime_phase": "", "facetime_phase_label": "", "facetime_state_label": ""}

    state_detail: dict[str, str] = {
        "starting": "Starting Koov…",
        "opening_link": "Opening the FaceTime link…",
        "waiting_for_name": "Entering display name…",
        "waiting_for_join_click": "Joining from the browser…",
        "waiting_for_admit": "Waiting for the host to admit Koov on their Apple device",
        "joining": "Setting up FaceTime…",
        "in_call": "In the meeting",
        "ended": "The call has ended",
        "stopped": "Koov has stopped",
        "error": "An error occurred",
    }
    state_label = state_detail.get(fs, "Setting up FaceTime…")

    if fs == "in_call":
        phase, phase_label = "in_meeting", "In the meeting"
    elif fs == "waiting_for_admit":
        phase, phase_label = "waiting_for_admit", "Waiting for the host to admit Koov"
    elif fs in ("ended", "stopped"):
        phase = "ended"
        phase_label = "The call has ended" if fs == "ended" else "Koov stopped"
    elif fs == "error":
        phase, phase_label = "error", "Something went wrong"
    else:
        phase, phase_label = "connecting", "Joining FaceTime…"

    return {
        "facetime_phase": phase,
        "facetime_phase_label": phase_label,
        "facetime_state_label": state_label,
    }


def _facetime_status_payload(
    previous: dict[str, object],
    *,
    status: str,
    meeting_id: str,
    meeting_link: str,
    now: float,
    extra: dict[str, object],
) -> dict[str, object]:
    """Merge an incremental worker update and derive stable dashboard fields."""
    payload: dict[str, object] = {
        **previous,
        "status": status,
        "meeting_id": meeting_id,
        "ts": now,
        "meeting_link": meeting_link,
        **extra,
    }
    facetime_state = str(payload.get("facetime_state") or "").strip()
    if facetime_state:
        payload.update(facetime_dashboard_labels(facetime_state))
        payload["facetime_ready"] = bool(
            payload.get("facetime_join_clicked")
            and facetime_state == "waiting_for_admit"
        )
    return payload


_ft_logger = logging.getLogger(__name__)

# Ring buffer of recent join/runtime steps. Container stdout is frequently lost
# (logs get captured after the container is already removed), so we also persist
# the last N steps into the worker-status JSON, which IS reliably written. This
# is what lets us see *where* a join died after the fact.
_RECENT_STEPS: "deque[dict[str, object]]" = deque(maxlen=60)


def _ft_step(meeting_id: str, step: str, detail: str, *, level: int = logging.INFO) -> None:
    """Grep-friendly progress: ``docker logs … 2>&1 | grep 'FaceTime:'``."""
    _ft_logger.log(level, "FaceTime: [step=%s] meeting_id=%s | %s", step, meeting_id, detail)
    try:
        _RECENT_STEPS.append(
            {
                "ts": round(time.time(), 3),
                "step": step,
                "detail": str(detail)[:300],
                "level": logging.getLevelName(level),
            }
        )
    except Exception:
        pass


def _write_final_summary_on_shutdown(agent: Any, config: Any, meeting_id: str) -> None:
    """Generate + persist final_summary.json for a finished FaceTime meeting.

    Mirrors koov/worker.py (Zoom). This is what feeds cross-meeting context the
    next time Koov joins a call. Best-effort: falls back to a state-only summary
    if the LLM call fails so something is always written when there was content.
    """
    from koov.meeting_summary import (
        generate_final_summary,
        merge_final_report_into_summary,
        minimal_final_summary_from_transcript_and_state,
        write_final_summary,
    )

    try:
        transcript_text, state = agent.get_meeting_data_for_summary()
    except Exception as exc:  # noqa: BLE001
        _ft_logger.warning("get_meeting_data_for_summary failed: %s", exc)
        transcript_text, state = "", {}

    # Nothing to summarize (e.g. nobody spoke) — skip to avoid empty files.
    if not (transcript_text or "").strip() and not state:
        _ft_logger.info("FaceTime: no transcript/state; skipping final summary for %s", meeting_id)
        return

    api_key = os.environ.get("KOOV_OPENAI_API_KEY", "").strip()
    model = (
        getattr(config, "openai_thinking_model", None)
        or getattr(config, "openai_chat_model", None)
        or os.environ.get("KOOV_OPENAI_THINKING_MODEL")
        or os.environ.get("KOOV_OPENAI_CHAT_MODEL", "gpt-4o-mini")
    )
    try:
        _summary_mode = (getattr(config, "summary_mode", None) or "full").strip().lower()
        if _summary_mode not in ("state", "fast", "full"):
            _summary_mode = "full"
        # Shutdown must finish inside docker stop grace — prefer fast LLM pass.
        _shutdown_mode = (os.environ.get("KOOV_SHUTDOWN_SUMMARY_MODE") or "fast").strip().lower()
        if _shutdown_mode in ("state", "fast", "full"):
            _summary_mode = _shutdown_mode
        summary = generate_final_summary(
            transcript_text,
            state,
            api_key=api_key or None,
            model=model,
            mode=_summary_mode,
            max_transcript_chars=getattr(config, "summary_max_transcript_chars", 28000),
            fast_max_tokens=getattr(config, "summary_fast_max_tokens", 1400),
            full_max_tokens=int(os.environ.get("KOOV_SUMMARY_FULL_MAX_TOKENS", "2400") or 2400),
            timeout_seconds=min(
                90,
                int(getattr(config, "summary_timeout_seconds", 120) or 90),
            ),
            prior_sessions_context=getattr(agent, "_cross_meeting_memory_text", None) or None,
            participant_names=state.get("participant_names"),
        )
        try:
            eng = agent._get_context_engine()
            if eng is not None:
                summary = merge_final_report_into_summary(
                    summary, eng.generate_final_report()
                )
        except Exception as merge_exc:  # noqa: BLE001
            _ft_logger.debug("Final report merge skipped: %s", merge_exc)
    except Exception as exc:  # noqa: BLE001
        _ft_logger.warning("generate_final_summary failed; using minimal fallback: %s", exc)
        summary = minimal_final_summary_from_transcript_and_state(transcript_text, state)

    try:
        summary["interactions"] = agent.get_koov_interactions()
    except Exception:  # noqa: BLE001
        summary["interactions"] = []

    path = write_final_summary(
        meeting_id, summary, meeting_link=os.environ.get("KOOV_MEETING_LINK", "")
    )
    _ft_logger.info("FaceTime: wrote final summary for %s -> %s", meeting_id, path)
    try:
        from koov.email_sender import send_meeting_emails

        _participant_emails: dict[str, str] = {}
        _pe_json = (os.environ.get("KOOV_PARTICIPANT_EMAILS_JSON") or "").strip()
        if _pe_json:
            try:
                _pe_map = json.loads(_pe_json)
                if isinstance(_pe_map, dict):
                    _participant_emails = {
                        str(k).strip(): str(v).strip().lower()
                        for k, v in _pe_map.items()
                        if str(k).strip() and str(v).strip()
                    }
            except Exception:
                pass
        _summary_recipients: list[str] = []
        _sr_json = (os.environ.get("KOOV_SUMMARY_RECIPIENTS_JSON") or "").strip()
        if _sr_json:
            try:
                _sr_list = json.loads(_sr_json)
                if isinstance(_sr_list, list):
                    _summary_recipients = [
                        str(v).strip().lower()
                        for v in _sr_list
                        if str(v).strip() and "@" in str(v)
                    ]
            except Exception:
                pass
        _collection_recipients: list[str] = []
        _cr_json = (os.environ.get("KOOV_COLLECTION_RECIPIENTS_JSON") or "").strip()
        if _cr_json:
            try:
                _cr_list = json.loads(_cr_json)
                if isinstance(_cr_list, list):
                    _collection_recipients = [
                        str(v).strip().lower()
                        for v in _cr_list
                        if str(v).strip() and "@" in str(v)
                    ]
            except Exception:
                pass
        send_meeting_emails(
            summary,
            meeting_id,
            speaker_emails=_participant_emails,
            summary_recipients=_summary_recipients,
            collection_recipients=_collection_recipients,
            invite_collection_link=(
                os.environ.get("KOOV_INVITE_EMAIL_COLLECTION_LINK") or ""
            ).strip(),
        )
    except Exception as _mail_exc:  # noqa: BLE001
        _ft_logger.warning("FaceTime: send_meeting_emails failed: %s", _mail_exc)


def _try_leave_facetime_call(driver) -> bool:
    """Click Leave/End in FaceTime web so the host's call drops Koov before browser teardown."""
    if driver is None:
        return False
    try:
        left = driver.execute_script(
            """
            function _clickLeave(root) {
                var els = root.querySelectorAll(
                    'ui-button, button, a[role="button"], [role="button"], div[role="button"]'
                );
                for (var i = 0; i < els.length; i++) {
                    var el = els[i];
                    if (!el.offsetParent) continue;
                    var txt = ((el.innerText || '') + ' ' +
                               (el.getAttribute('aria-label') || '') + ' ' +
                               (el.getAttribute('title') || '')).toLowerCase();
                    if (txt.indexOf('leave call') !== -1 || txt.indexOf('end call') !== -1 ||
                        txt.indexOf('leave meeting') !== -1) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
            if (_clickLeave(document)) return true;
            var leaveBtn = document.querySelector(
                '#extended-menu-leave-button, ui-button.close-button, button.close-button'
            );
            if (leaveBtn && leaveBtn.offsetParent) {
                leaveBtn.click();
                return true;
            }
            return false;
            """
        )
        if left:
            time.sleep(0.6)
            try:
                driver.execute_script(
                    """
                    var els = document.querySelectorAll('ui-button, button, [role="button"]');
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        if (!el.offsetParent) continue;
                        var txt = ((el.innerText || '') + ' ' +
                                   (el.getAttribute('aria-label') || '')).toLowerCase().trim();
                        if (txt === 'leave' || txt.indexOf('leave call') !== -1 ||
                            txt.indexOf('end call') !== -1) {
                            el.click();
                            return;
                        }
                    }
                    """
                )
            except Exception:
                pass
            time.sleep(0.4)
        return bool(left)
    except Exception:
        return False


_FT_ROSTER_SKIP = {
    "facetime",
    "koov",
    "leave call",
    "end call",
    "microphone",
    "camera",
    "mute",
    "unmute",
    "participants",
    "add person",
    "share link",
    "copy link",
    "grid layout",
    "video effects",
    "settings",
    "more",
    "close",
    "cancel",
    "continue",
    "join",
    "waiting to be let in",
}


def _clean_ft_roster_candidate(raw: str, *, display_name: str) -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    s = s.strip("•·-–—:|,")
    if not s:
        return None
    # FaceTime labels often include status words; keep the likely name part.
    s = re.sub(
        r"\b(is )?(muted|unmuted|speaking|camera off|video off|audio|host|you)\b",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\s+", " ", s).strip("•·-–—:|, ")
    low = s.lower()
    if not s or low in _FT_ROSTER_SKIP:
        return None
    if display_name and low == display_name.strip().lower():
        return None
    if len(s) < 2 or len(s) > 64:
        return None
    if re.search(r"https?://|facetime|apple|chrome|javascript|function|\{|\}", low):
        return None
    # Avoid button copy and whole paragraphs. Names usually have 1-4 short tokens.
    words = s.split()
    if len(words) > 4:
        return None
    if any(w.lower() in _FT_ROSTER_SKIP for w in words):
        return None
    return s


def _extract_facetime_participant_names(driver: Any, *, display_name: str) -> list[str]:
    """Best-effort FaceTime-web roster scrape.

    Apple does not expose a bot API, so this reads visible DOM labels/titles
    around participant tiles/rows. It is intentionally conservative: false
    negatives are better than polluting meeting memory with toolbar labels.
    """
    script = r"""
const out = [];
// Only structural participant containers. The broad '[aria-label]' / '[title]'
// selectors used to pull in every toolbar button + tooltip ("Mute", "Add People",
// "Share Screen", …), which inflated the roster to dozens of fake "participants".
const selectors = [
  '[class*="participant" i]',
  '[class*="roster" i]',
  '[role="listitem"]'
];
const seen = new Set();
for (const sel of selectors) {
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (rect && (rect.width <= 0 || rect.height <= 0)) continue;
    const vals = [
      el.getAttribute && el.getAttribute('aria-label'),
      el.getAttribute && el.getAttribute('title'),
      el.innerText,
      el.textContent
    ];
    for (const v of vals) {
      if (v && String(v).trim()) out.push(String(v).trim());
    }
  }
}
return out.slice(0, 200);
"""
    try:
        raw_vals = driver.execute_script(script) or []
    except Exception:
        return []
    names: list[str] = []
    seen_names: set[str] = set()
    for raw in raw_vals:
        # Split multi-line tile text into candidates.
        for part in re.split(r"[\n\r]+", str(raw or "")):
            name = _clean_ft_roster_candidate(part, display_name=display_name)
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            names.append(name)
    return names[:12]


def _extract_facetime_active_speaker(driver: Any, *, display_name: str) -> str | None:
    """Best-effort: which participant FaceTime currently shows as speaking.

    FaceTime web highlights the active speaker's tile (a glowing ring / class
    change / aria state). We look for tiles that carry a "speaking"/"talking"/
    "active" signal and return the cleaned name on that tile. This is the only
    voice→name anchor on FaceTime's single mixed-audio stream. Returns None when
    no tile is clearly marked as speaking (silence, overlap, or DOM not exposing
    the indicator).
    """
    script = r"""
const isSpeak = (s) => {
  s = (s || '').toLowerCase();
  return s.includes('speaking') || s.includes('talking') ||
         s.includes('is-active-speaker') || s.includes('active-speaker') ||
         s.includes('voiceactivity') || s.includes('audio-level');
};
const visible = (el) => {
  try {
    const r = el.getBoundingClientRect();
    return r && r.width > 0 && r.height > 0;
  } catch (e) { return false; }
};
const cands = [];
const sels = [
  '[class*="participant" i]', '[class*="tile" i]', '[class*="roster" i]',
  '[role="listitem"]', '[role="gridcell"]'
];
const seen = new Set();
for (const sel of sels) {
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const blob = [
      el.className || '',
      el.getAttribute && el.getAttribute('aria-label') || '',
      el.getAttribute && el.getAttribute('data-is-speaking') || '',
      el.getAttribute && el.getAttribute('data-speaking') || '',
      el.getAttribute && el.getAttribute('data-voice-activity') || ''
    ].join(' ');
    let speaking = isSpeak(blob);
    if (!speaking) {
      // Some clients only mark a child node (the audio ring) as speaking.
      for (const child of el.querySelectorAll('[class],[aria-label]')) {
        const cb = (child.className || '') + ' ' + (child.getAttribute && child.getAttribute('aria-label') || '');
        if (isSpeak(cb)) { speaking = true; break; }
      }
    }
    if (!speaking) continue;
    const name = (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title'))) ||
                 el.innerText || el.textContent || '';
    if (name && String(name).trim()) cands.push(String(name).trim());
  }
}
return cands.slice(0, 4);
"""
    try:
        raw_vals = driver.execute_script(script) or []
    except Exception:
        return None
    for raw in raw_vals:
        for part in re.split(r"[\n\r]+", str(raw or "")):
            name = _clean_ft_roster_candidate(part, display_name=display_name)
            if name:
                return name
    return None


def _strip_env_file_quotes(environ: dict[str, str] | None = None) -> list[str]:
    """Remove one layer of matching quotes from KOOV_* values.

    ``docker run --env-file`` passes values verbatim, so ``KEY="abc"`` in .env
    arrives as the literal string ``"abc"`` (for example an ElevenLabs voice id
    the API then rejects). Host-side ``load_env_file`` already strips these;
    this makes container workers behave the same. Returns the keys changed.
    """
    env = os.environ if environ is None else environ
    changed: list[str] = []
    for key, value in list(env.items()):
        if not key.startswith("KOOV_") or len(value) < 2:
            continue
        if value[0] == value[-1] and value[0] in ("'", '"'):
            env[key] = value[1:-1]
            changed.append(key)
    return changed


def _admit_timed_out(
    *,
    elapsed_s: float,
    stalled_s: float,
    current_pass_s: float,
    timeout_s: float,
    hard_cap_factor: float = 4.0,
) -> bool:
    """True when a never-admitted worker should give up and exit.

    Time the poll loop spent blocked (``stalled_s``, plus the current pass beyond
    30s) does not count toward ``timeout_s``, so a bot admitted while Chromium was
    saturated is not killed as stale. A hard wall-clock cap still applies: if the
    browser stays saturated on every pass, nearly all time counts as stalled and
    the bot would otherwise linger for an hour holding its camera lease.
    """
    if timeout_s <= 0:
        return False
    if elapsed_s >= timeout_s * max(1.0, hard_cap_factor):
        return True
    active_s = elapsed_s - stalled_s - max(0.0, current_pass_s - 30.0)
    return active_s >= timeout_s


def main() -> None:
    from koov.logging import setup_logging

    setup_logging(os.environ.get("KOOV_LOG_LEVEL", "INFO"))
    log = logging.getLogger(__name__)
    _unquoted = _strip_env_file_quotes()
    if _unquoted:
        log.info("FaceTime: stripped surrounding quotes from env: %s", ", ".join(sorted(_unquoted)))

    meeting_id = os.environ.get("KOOV_MEETING_ID", "").strip()
    meeting_link = os.environ.get("KOOV_MEETING_LINK", "").strip()
    display_name = os.environ.get("KOOV_DISPLAY_NAME", "Koov").strip() or "Koov"
    from koov.facetime_env import display_name_from_join_url

    _name_from_link = display_name_from_join_url(meeting_link)
    if _name_from_link:
        display_name = _name_from_link
    team_id = os.environ.get("KOOV_TEAM_ID", "").strip()
    shared_context_enabled = os.environ.get("KOOV_SHARED_CONTEXT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    participant_timezones_json = os.environ.get("KOOV_PARTICIPANT_TIMEZONES_JSON", "").strip()
    notes_dir = os.environ.get("KOOV_NOTES_DIR", "koov_notes")
    os.makedirs(notes_dir, exist_ok=True)

    if not meeting_id or not meeting_link:
        sys.stderr.write("[facetime_worker] KOOV_MEETING_ID and KOOV_MEETING_LINK must be set\n")
        sys.exit(1)

    status_path = os.path.join(notes_dir, f"worker-status-{meeting_id}.json")

    def write(status: str, **extra: object) -> None:
        # Status writes are incremental updates from several phases. Preserve
        # fields such as the navigation URL and join-click result instead of
        # replacing the entire snapshot and making them disappear on the next
        # heartbeat.
        previous: dict[str, object] = {}
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, ValueError, TypeError):
            pass
        extra.setdefault("join_backend", "facetime_web")
        payload = _facetime_status_payload(
            previous,
            status=status,
            meeting_id=meeting_id,
            meeting_link=meeting_link,
            now=time.time(),
            extra=dict(extra),
        )
        # Persist recent join/runtime steps so a failed join is diagnosable from
        # the status JSON even when container stdout was lost.
        payload["steps_recent"] = list(_RECENT_STEPS)
        _write_status(status_path, payload)

    write("starting", facetime_state="starting")
    _ft_step(meeting_id, "worker_boot", "process started; status file=starting")

    from koov.envfile import load_env_file

    env_fp = os.environ.get("KOOV_ENV_FILE", ".env")
    _ft_step(meeting_id, "env_load", f"loading env file path={env_fp!r} (if missing, worker uses env only)")
    load_env_file(env_fp)
    # Host .env often sets KOOV_JOIN_BACKEND=zoom_sdk; this process must stay FaceTime.
    os.environ["KOOV_JOIN_BACKEND"] = "facetime_web"
    # Force the meeting store onto the shared /notes volume (set AFTER load_env_file so a
    # stale .env / inherited API path can't point us at a dir that doesn't exist in this
    # container). This is the only persistent path a FaceTime worker container has, and it
    # maps to the same host dir the API server reads — so final_summary.json survives
    # teardown and powers cross-meeting memory (same link → remember last time).
    os.environ["KOOV_MEETING_STORE_DIR"] = os.path.join(notes_dir, "koov_meetings")
    # Wake on "Koov" (primary) or "computer" (legacy alias still in active use).
    # Comma-separated; STT homophones (`koo`, `cool`, `coup`, `koove`, etc.) are
    # accepted by the fuzzy matcher in koov/wake_detector.py so users don't have
    # to enunciate perfectly — see `_FUZZY_FALSE_FRIENDS` for the false-friend
    # guard list.
    from koov.facetime_env import PRODUCTION_WAKE_PHRASE

    os.environ.setdefault("KOOV_WAKE_PHRASE", PRODUCTION_WAKE_PHRASE)
    if not (os.environ.get("KOOV_BOT_PERSONA_ID") or "").strip():
        os.environ.setdefault("KOOV_PERSONAS_ENABLED", "default")
    _reassert_pulse_for_chromium()
    _ft_step(
        meeting_id,
        "pulse_route",
        "PULSE_SINK=%r PULSE_SOURCE=%r (after KOOV_FT_* reassert)"
        % (os.environ.get("PULSE_SINK", ""), os.environ.get("PULSE_SOURCE", "")),
    )
    from koov.config import KoovConfig

    # FaceTime worker must always use the container's PulseAudio graph, even if .env
    # contains blank or host-specific device names from local development.
    # Use PortAudio's "pulse" device and route via pactl defaults (call_sink.monitor in,
    # koov_sink out). Binding sounddevice to "call_sink.monitor" by name often fails silently
    # (_no_device) so the dashboard shows "joining" with zero transcript chunks.
    _reassert_agent_listen_route(meeting_id, reason="worker boot")
    _reassert_tts_output_route(meeting_id)
    os.environ["KOOV_INPUT_DEVICE"] = "pulse"
    os.environ["KOOV_OUTPUT_DEVICE"] = "pulse"
    os.environ["KOOV_AUDIO_OUTPUT_PULSE_SINK"] = _ft_tts_sink_name()
    os.environ["KOOV_CONVERSATIONAL"] = "0"
    os.environ["KOOV_LOW_LATENCY"] = "1"
    from koov.facetime_env import (
        apply_facetime_eleven_env_defaults,
        apply_facetime_worker_env_defaults,
        facetime_worker_runtime_overrides,
    )

    _eleven_model = apply_facetime_eleven_env_defaults()
    apply_facetime_worker_env_defaults()
    # Keep direct launches and API-spawned containers on one canonical bundle.
    os.environ.update(facetime_worker_runtime_overrides())
    _ft_step(meeting_id, "env", "ElevenLabs model=%r audio_tags=%r" % (
        _eleven_model,
        os.environ.get("KOOV_ELEVEN_AUDIO_TAGS"),
    ))
    _ft_step(
        meeting_id,
        "env",
        "latency: opener=%s preroll=%s transcript_preroll=%s extend_cap=%ss"
        % (
            os.environ.get("KOOV_WAKE_OPENER"),
            os.environ.get("KOOV_THINKING_PREROLL"),
            os.environ.get("KOOV_THINKING_PREROLL_TRANSCRIPT_STT"),
            os.environ.get("KOOV_WAKE_OPENER_EXTEND_CAP_S"),
        ),
    )
    # Multilingual STT: Scribe v2 handles accents + 90+ languages; local base.en cannot.
    if (os.environ.get("KOOV_ELEVEN_API_KEY") or "").strip():
        os.environ["KOOV_STT_BACKEND"] = "elevenlabs"
        os.environ.setdefault("KOOV_ELEVEN_STT_MODEL", "scribe_v2_realtime")
    else:
        _wm = (os.environ.get("KOOV_WHISPER_MODEL") or "").strip().lower()
        if _wm in ("", "base.en", "base"):
            os.environ["KOOV_WHISPER_MODEL"] = "small"
    os.environ.pop("KOOV_WHISPER_LANGUAGE", None)
    _ft_step(
        meeting_id,
        "env",
        "STT backend=%r model=%r lang=auto"
        % (
            os.environ.get("KOOV_STT_BACKEND", "faster-whisper"),
            os.environ.get("KOOV_ELEVEN_STT_MODEL")
            or os.environ.get("KOOV_WHISPER_MODEL", ""),
        ),
    )
    os.environ.setdefault("KOOV_TTS_OUTPUT_GAIN", "1.05")
    # Interruption on FaceTime is STOP-DIRECTIVE ONLY ("Koov, stop" / "hold on").
    # Raw-VAD barge-in stays off (mixed audio echoes Koov's own TTS, so VAD would
    # self-interrupt). Transcript barge-in is on but gated to stop directives, which
    # are echo-safe — Koov never says "Koov, stop", and ambient chatter / a new
    # question mid-reply will NOT cut Koov off. This gives working interruption
    # without the self-cut glitch.
    # FaceTime has a single mixed audio stream — stream attribution is meaningless.
    os.environ.setdefault("KOOV_ZOOM_STREAM_ATTRIBUTION", "0")
    # FaceTime has mixed audio only. Diarization (acoustic speaker separation) is
    # OPT-IN here and must never run on the listen/STT hot path: the local pyannote
    # backend runs *synchronously* per transcript chunk (KOOV_DIARIZATION_ASYNC only
    # wraps the cloud backend), so enabling it on a single-core worker stalls the
    # wake loop and Koov stops hearing anyone. Knowing "who's on the call" is handled
    # separately by the participant-name roster scrape below, which doesn't touch audio.
    #
    # Only auto-enable when a real cloud diarization API key is present, because that
    # path is genuinely async/non-blocking. A bare HF token (common for unrelated
    # reasons) must NOT silently turn on the blocking local diarizer.
    if os.environ.get("KOOV_DIARIZATION_ENABLED") is None:
        _cloud_key = (os.environ.get("KOOV_DIARIZATION_API_KEY") or "").strip()
        _backend = (os.environ.get("KOOV_DIARIZATION_BACKEND") or "").strip().lower()
        _is_cloud_backend = _backend in {"pyannote-cloud", "pyannoteai", "pyannote-api"}
        if _cloud_key and (_is_cloud_backend or not _backend):
            # Force the cloud async path so speaker labelling never blocks wake/TTS.
            os.environ["KOOV_DIARIZATION_ENABLED"] = "1"
            os.environ.setdefault("KOOV_DIARIZATION_BACKEND", "pyannote-cloud")
            os.environ.setdefault("KOOV_DIARIZATION_ASYNC", "1")
            _ft_step(
                meeting_id,
                "diarization",
                "enabled async cloud diarization from KOOV_DIARIZATION_API_KEY",
            )
        else:
            os.environ["KOOV_DIARIZATION_ENABLED"] = "0"
    # If the operator explicitly enabled diarization, still force async so a misconfig
    # can't fully wedge the listen loop. (Local backend ignores this — see warning below.)
    if os.environ.get("KOOV_DIARIZATION_ENABLED", "").strip() in ("1", "true", "yes"):
        os.environ.setdefault("KOOV_DIARIZATION_ASYNC", "1")
        _backend = (os.environ.get("KOOV_DIARIZATION_BACKEND") or "pyannote-local").strip().lower()
        if _backend not in {"pyannote-cloud", "pyannoteai", "pyannote-api"}:
            _ft_step(
                meeting_id,
                "diarization",
                "WARNING: local diarization backend runs synchronously and can stall "
                "the FaceTime listen loop; prefer a cloud backend + KOOV_DIARIZATION_API_KEY",
                level=logging.WARNING,
            )

    config = KoovConfig.from_env()
    _ft_step(
        meeting_id,
        "config",
        "KoovConfig loaded sample_rate_hz=%s input_device=%r output_device=%r"
        % (config.sample_rate_hz, config.input_device, config.output_device),
    )

    from koov.facetime_camera import FaceTimeCameraController
    from koov.facetime_control import read_camera_control
    from koov.facetime_video import CameraMode, VideoScene, VirtualCameraOutput
    from koov.facetime_vision import (
        VIDEO_RECTS_JS,
        VisionResult,
        VoiceVisionBridge,
        analyze_image,
        claim_next_request,
        complete_request,
        crop_to_region,
        fail_inflight,
        prepare_screenshot,
        prune_done,
        recover_stale_requests,
        usable_screenshot,
        vision_detail_from_env,
        vision_model_from_env,
    )

    def _env_on(name: str, default: str = "0") -> bool:
        return (os.environ.get(name, default) or default).strip().lower() in (
            "1", "true", "yes", "on",
        )

    # Every FaceTime worker is provisioned with a device when the feature is on;
    # KOOV_FT_CAMERA_ENABLED only decides whether the camera starts on.
    camera_feature_enabled = _env_on("KOOV_FT_CAMERA_FEATURE")
    camera_required = _env_on("KOOV_FT_CAMERA_REQUIRED")
    video_device = (os.environ.get("KOOV_FT_VIDEO_DEVICE") or "").strip() if camera_feature_enabled else ""
    camera_provisioned = bool(camera_feature_enabled and video_device)
    camera_desired_enabled = bool(camera_provisioned and _env_on("KOOV_FT_CAMERA_ENABLED"))
    camera_desired_mode = (
        os.environ.get("KOOV_FT_CAMERA_MODE")
        or os.environ.get("KOOV_FT_CAMERA_DEFAULT_MODE")
        or "avatar"
    ).strip().lower()
    if camera_desired_mode not in {mode.value for mode in CameraMode}:
        camera_desired_mode = "avatar"
    try:
        camera_width = max(160, min(int(os.environ.get("KOOV_FT_VIDEO_WIDTH", "640")), 1920))
        camera_height = max(120, min(int(os.environ.get("KOOV_FT_VIDEO_HEIGHT", "360")), 1080))
        camera_fps = max(5, min(int(os.environ.get("KOOV_FT_VIDEO_FPS", "12")), 20))
    except ValueError:
        camera_width, camera_height, camera_fps = 640, 360, 12
    vision_enabled = _env_on("KOOV_FT_VISION_ENABLED")
    vision_model = vision_model_from_env()
    vision_detail = vision_detail_from_env()
    try:
        vision_max_per_minute = max(
            1, min(int(os.environ.get("KOOV_FT_VISION_MAX_REQUESTS_PER_MINUTE", "3")), 30)
        )
    except ValueError:
        vision_max_per_minute = 3

    camera_output: VirtualCameraOutput | None = None
    camera_controller: FaceTimeCameraController | None = None
    camera_selected = False
    camera_enabled_actual = False
    camera_error_code = (os.environ.get("KOOV_FT_CAMERA_ERROR_CODE") or "").strip()
    camera_error = (os.environ.get("KOOV_FT_VIDEO_DEVICE_ERROR") or "").strip()
    if camera_error and not camera_error_code:
        camera_error_code = "camera_device_missing"
    if camera_error_code == "camera_pool_exhausted" and not camera_error:
        camera_error = "No virtual camera was free when this worker started; joined audio-only"
    camera_dom_dumped = False
    camera_disclosure_shown = False
    camera_transient_text = ""
    camera_transient_until = 0.0
    camera_control_revision = 0
    camera_actual_mode = camera_desired_mode
    camera_last_repair_at = 0.0
    camera_control_changed = False
    vision_busy = False
    vision_last_prune_at = 0.0
    vision_last_request_id = ""
    vision_last_error = ""

    # Browser join runs BEFORE KoovAgent(): agent construction loads Vosk/wake/STT and can take 20s+.
    # External clients often poll /api/status once then POST /api/stop; if Chrome never opened yet,
    # the FaceTime worker container is removed before any join knock reaches Apple.
    # Phase-1 improvement: KoovAgent boots in a background thread so browser join and model
    # loading happen concurrently — by the time the host taps "Admit", the agent is usually ready.
    stop_event = threading.Event()
    agent_holder: dict[str, Any] = {"agent": None}
    agent = None
    agent_thread: threading.Thread | None = None
    _agent_boot_thread: threading.Thread | None = None
    _agent_boot_error: list[str] = []  # captures exception text from boot thread
    driver = None

    def _on_signal(signum: int, frame: object) -> None:
        log.info("FaceTime worker received signal %s for meeting_id=%s; stopping", signum, meeting_id)
        stop_event.set()
        try:
            a = agent_holder.get("agent")
            if a is not None:
                a.stop()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    facetime_state = "opening_link"
    last_err: str | None = None

    try:
        write("joining", facetime_state="opening_link")
        _ft_step(meeting_id, "browser_phase", "begin Selenium join (KoovAgent starts only after this phase)")

        if camera_provisioned and not camera_error:
            camera_output = VirtualCameraOutput(
                video_device,
                width=camera_width,
                height=camera_height,
                fps=camera_fps,
                display_name=display_name,
            )
            camera_output.set_scene(
                VideoScene(
                    enabled=camera_desired_enabled,
                    mode=CameraMode(camera_desired_mode),
                    display_name=display_name,
                )
            )
            try:
                _camera_health = camera_output.start()
            except Exception as _camera_start_exc:  # noqa: BLE001 - never block the audio join
                camera_output.stop(timeout_s=2.0)
                _camera_health = camera_output.health()
                _camera_health.error_code = "camera_producer_failed"
                _camera_health.error = str(_camera_start_exc)[:500]
            camera_error_code = _camera_health.error_code
            camera_error = _camera_health.error
            _ft_step(
                meeting_id,
                "camera_producer_started" if _camera_health.producer_ok else "camera_degraded_audio_only",
                "device_assigned=%s producer_ok=%s error_code=%r"
                % (bool(video_device), _camera_health.producer_ok, _camera_health.error_code),
                level=logging.INFO if _camera_health.producer_ok else logging.WARNING,
            )
            write(
                "joining",
                facetime_state="opening_link",
                facetime_camera_feature_enabled=True,
                facetime_camera_available=_camera_health.available,
                facetime_camera_device_assigned=True,
                facetime_camera_producer_ok=_camera_health.producer_ok,
                facetime_camera_selected=False,
                facetime_camera_enabled=False,
                facetime_camera_desired_enabled=camera_desired_enabled,
                facetime_camera_desired_mode=camera_desired_mode,
                facetime_camera_actual_mode=camera_desired_mode,
                facetime_camera_control_revision=0,
                facetime_camera_last_frame_at=_camera_health.last_frame_at,
                facetime_camera_error_code=_camera_health.error_code,
                facetime_camera_error=_camera_health.error,
                facetime_vision_enabled=vision_enabled,
                facetime_vision_busy=False,
            )
            if camera_required and not _camera_health.producer_ok:
                raise RuntimeError(
                    "FaceTime camera was required but the virtual-camera producer could not start: "
                    + (_camera_health.error or _camera_health.error_code or "unknown error")
                )
        elif camera_required:
            raise RuntimeError(
                "FaceTime camera was required but no usable virtual camera device was assigned: "
                + (camera_error or camera_error_code or "no device")
            )
        if vision_enabled:
            try:
                _recovered = recover_stale_requests(notes_dir, meeting_id)
                if _recovered:
                    _ft_step(meeting_id, "vision_requested", "recovered %d stale request(s)" % _recovered)
            except Exception as _recover_exc:  # noqa: BLE001
                _ft_step(meeting_id, "vision_failed", "stale recovery error: %s" % _recover_exc,
                         level=logging.WARNING)

        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait

        join_url = _normalize_facetime_web_url(meeting_link)
        if join_url != meeting_link.strip():
            _ft_step(
                meeting_id,
                "url_normalize",
                "normalized facetime:// -> https host=%s" % (urlparse(join_url).hostname or ""),
            )

        chrome_opts = webdriver.ChromeOptions()
        # Debian/Ubuntu packaged Chromium path
        for _p in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if os.path.exists(_p):
                chrome_opts.binary_location = _p
                break
        chrome_opts.add_argument("--no-sandbox")
        chrome_opts.add_argument("--disable-dev-shm-usage")
        chrome_opts.add_argument("--disable-gpu")
        chrome_opts.add_argument("--window-size=1280,900")
        chrome_opts.add_argument("--lang=en-US,en")
        chrome_opts.add_argument("--autoplay-policy=no-user-gesture-required")
        chrome_opts.add_argument("--use-fake-ui-for-media-stream")
        chrome_opts.add_argument("--use-fake-ui-for-media-stream")
        chrome_opts.add_argument("--alsa-output-device=pulse")
        chrome_opts.add_argument("--alsa-input-device=pulse")
        chrome_opts.add_argument("--disable-blink-features=AutomationControlled")
        # Chrome runs its audio service in a sandboxed child process that re-execs with a
        # cleaned environment, preventing it from finding the PulseAudio socket even when
        # --no-sandbox is set for the browser process.  Without this flag Chrome's WebRTC
        # layer never opens a capture stream, so pactl finds zero source-outputs to move and
        # Koov's TTS never reaches FaceTime participants (FaceTime shows "No Device Selected").
        # AudioServiceOutOfProcess can prevent capture from using the same PA socket graph.
        chrome_opts.add_argument(
            "--disable-features=AudioServiceSandbox,AudioServiceOutOfProcess"
        )
        chrome_opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_opts.add_experimental_option("useAutomationExtension", False)
        # IMPORTANT: Do NOT use --use-fake-device-for-media-stream; we need real Pulse devices.

        _reassert_pulse_for_chromium()

        cd = _chromedriver_path()
        bin_loc = getattr(chrome_opts, "binary_location", None) or "(default)"
        _ft_step(meeting_id, "chromium_launch", "chromedriver=%r chromium_binary=%r" % (cd, bin_loc))
        try:
            service = Service(cd) if cd else Service()
        except TypeError:
            service = Service(executable_path=cd) if cd else Service()
        driver = webdriver.Chrome(service=service, options=chrome_opts)
        driver.set_page_load_timeout(120)

        # Chrome has now forked and inherited PULSE_SINK=call_sink /
        # PULSE_SOURCE=koov_mic_source from the entrypoint. Restore Python's env so that
        # sounddevice streams (opened later by KoovAgent) connect to the right endpoints:
        #   Python input  → call_sink.monitor  (hear what FaceTime plays back)
        #   Python output → koov_sink          (TTS flows through koov_mic_source → Chrome mic)
        _py_sink = (os.environ.get("KOOV_FT_DEFAULT_SINK") or "koov_sink").strip()
        _py_src = (os.environ.get("KOOV_FT_DEFAULT_SOURCE") or "call_sink.monitor").strip()
        os.environ["PULSE_SINK"] = _py_sink
        os.environ["PULSE_SOURCE"] = _py_src
        _ft_step(
            meeting_id,
            "pulse_restore",
            "Python Pulse env restored after Chrome fork: PULSE_SINK=%r PULSE_SOURCE=%r"
            % (_py_sink, _py_src),
        )

        _ft_step(meeting_id, "chromium_ready", "WebDriver session created")
        if camera_output is not None and camera_output.health().producer_ok:
            camera_controller = FaceTimeCameraController(driver, label_hint="koov")

        # ── Parallel KoovAgent boot ────────────────────────────────────────────
        # Start building KoovAgent (Vosk/wake/STT/TTS warmup) NOW, while Chrome
        # is still doing the join flow.  This overlaps 15–60s of model loading
        # with the name-entry + join-click phases so the agent is ready the moment
        # the host taps "Admit".
        def _agent_boot_worker() -> None:
            try:
                # pkg_resources shim needed by webrtcvad on Debian 12 / pip 24+.
                try:
                    import pkg_resources  # noqa: F401
                except ModuleNotFoundError:
                    import importlib.metadata as _im
                    import types as _types
                    import sys as _sys
                    _shim = _types.SimpleNamespace(
                        get_distribution=lambda n: _types.SimpleNamespace(version=_im.version(n)),
                        require=lambda *a, **k: None,
                        working_set=[],
                    )
                    _sys.modules.setdefault("pkg_resources", _shim)
                    _ft_step(meeting_id, "koov_agent", "pkg_resources shim injected (boot thread)")

                from koov.agent import KoovAgent
                _ft_step(
                    meeting_id,
                    "koov_agent",
                    "constructing KoovAgent in background thread (Vosk/wake/STT may take 15–60s)…",
                )
                _a = KoovAgent(config)
                _ft_step(meeting_id, "koov_agent", "KoovAgent constructed (background thread)")
                agent_holder["agent"] = _a
            except Exception as _exc:
                _agent_boot_error.append(str(_exc))
                _ft_step(
                    meeting_id,
                    "koov_agent",
                    "KoovAgent boot failed in background thread: %s" % _exc,
                    level=logging.ERROR,
                )

        _agent_boot_thread = threading.Thread(
            target=_agent_boot_worker,
            daemon=True,
            name=f"agent-boot-{meeting_id[:8]}",
        )
        _agent_boot_thread.start()
        _ft_step(meeting_id, "koov_agent", "KoovAgent boot thread started — running parallel with browser join")

        # Grant microphone permission to facetime.apple.com via CDP BEFORE navigating.
        # Without this, enumerateDevices() returns audioinput entries with empty labels
        # (Chrome's default privacy behavior), which makes FaceTime's React dropdown
        # render "No Device Selected" and never attach the mic track to the call's
        # RTCPeerConnection. --use-fake-ui-for-media-stream accepts the prompt but does
        # not persist a permission grant that populates device labels.
        #
        # Chromium versions vary in which permission names they accept (e.g. 147 rejects
        # "audioCapturePanTiltZoom"), so we grant each name in its own CDP call: that way
        # one unknown name does not abort the whole grant and leave audio/video ungranted.
        _grant_results: list[str] = []
        for _perm in ("audioCapture", "videoCapture"):
            try:
                driver.execute_cdp_cmd(
                    "Browser.grantPermissions",
                    {
                        "origin": "https://facetime.apple.com",
                        "permissions": [_perm],
                    },
                )
                _grant_results.append("%s=ok" % _perm)
            except Exception as _exc:
                _grant_results.append(
                    "%s=fail(%s)" % (_perm, str(_exc).splitlines()[0][:120])
                )
        _ft_step(
            meeting_id,
            "cdp_grant",
            "Browser.grantPermissions for https://facetime.apple.com: %s"
            % ", ".join(_grant_results),
        )

        _ft_step(
            meeting_id,
            "navigate",
            "GET join page host=%s" % (urlparse(join_url).hostname or ""),
        )
        driver.get(join_url)
        time.sleep(1.5)
        try:
            WebDriverWait(driver, 45).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            _ft_step(meeting_id, "navigate", "document.readyState=complete (within 45s)")
        except Exception:
            _ft_step(
                meeting_id,
                "navigate",
                "document.readyState did not reach complete in 45s; continuing",
                level=logging.WARNING,
            )
        time.sleep(1.5)
        try:
            _ft_step(
                meeting_id,
                "page_loaded",
                "url=%s title=%r"
                % (_url_for_log(driver.current_url or ""), (driver.title or "")[:120]),
            )
        except Exception:
            pass

        # FaceTime requests getUserMedia() soon after page load. Retry inspection/rewiring
        # until Chromium either appears on koov_mic_source or can be moved there.
        _koov_mic_src = (os.environ.get("KOOV_FT_KOOV_MIC_SOURCE") or "koov_mic_source").strip()

        def _rewire_with_retry() -> None:
            # Keep retrying for up to 5 minutes — FaceTime admission can be slow
            # and Chrome resets audio routing when call state changes
            for attempt in range(60):
                time.sleep(5)
                try:
                    driver.execute_script("""
                        navigator.mediaDevices.getUserMedia({audio: true, video: false})
                            .then(s => { window._koov_mic_stream = s; })
                            .catch(e => { console.log('koov mic error:', e); });
                    """)
                except Exception:
                    pass
                moved = _rewire_chrome_source_outputs(meeting_id, _koov_mic_src)
                if moved > 0:
                    _ft_step(meeting_id, "pulse_rewire",
                        "rewired %d Chrome source-output(s) on attempt %d" % (moved, attempt + 1))
                    # Select the real koov_mic_source entry in FaceTime's dropdown.
                    #
                    # FaceTime shows two options:
                    #   1. "Use System Settings"  (default/highlighted, but NOT committed)
                    #   2. "koov_mic_source"      (the actual Pulse source we want)
                    #
                    # A plain .click() on the first <ui-menu-item> picks "Use System
                    # Settings", which leaves the mic label as "No Device Selected" —
                    # i.e. no mic track is attached to the peer connection and the
                    # other side hears silence.  We must click the *second* item
                    # ("koov_mic_source") and dispatch full pointer events, because
                    # Apple's ui-menu-item / menuitemradio listens on pointerup/click,
                    # not a synthetic element.click().
                    _MIC_SELECT_JS = r"""
const wanted = (arguments[0] || 'koov').toLowerCase();
const items = Array.from(document.querySelectorAll('ui-menu-item, [role="menuitem"], [role="menuitemradio"]'));
function label(el) {
  return ((el.innerText || el.textContent || '') + ' ' +
          (el.getAttribute('aria-label') || '')).trim().toLowerCase();
}
function usable(txt) {
  if (!txt) return false;
  if (txt.includes('use system settings')) return false;
  if (txt === 'microphone') return false;
  return true;
}
let target = null;
for (const el of items) {
  if (!el.offsetParent) continue;
  const t = label(el);
  if (t.includes(wanted) && usable(t)) { target = el; break; }
}
if (!target) {
  for (const el of items) {
    if (!el.offsetParent) continue;
    const t = label(el);
    if (usable(t)) { target = el; break; }
  }
}
if (!target) return { ok: false, reason: 'no_usable_item', seen: items.map(label) };
target.scrollIntoView({block: 'center', inline: 'nearest'});
const rect = target.getBoundingClientRect();
const x = rect.left + rect.width / 2;
const y = rect.top + rect.height / 2;
function fire(type, opts) {
  const Ctor = (type.startsWith('pointer') ? PointerEvent
              : type.startsWith('mouse')   ? MouseEvent
              : Event);
  const ev = new Ctor(type, Object.assign({
    bubbles: true, cancelable: true, composed: true,
    view: window, button: 0, buttons: type.endsWith('up') ? 0 : 1,
    clientX: x, clientY: y,
    pointerId: 1, pointerType: 'mouse', isPrimary: true,
  }, opts || {}));
  target.dispatchEvent(ev);
}
try { target.focus(); } catch (e) {}
fire('pointerover');  fire('mouseover');
fire('pointerenter'); fire('mouseenter');
fire('pointermove');  fire('mousemove');
fire('pointerdown');  fire('mousedown');
fire('pointerup');    fire('mouseup');
fire('click');
return { ok: true, label: (target.innerText || '').trim(),
         checked: target.getAttribute('aria-checked'),
         selected: target.getAttribute('aria-selected') };
"""
                    def _verify_mic_selected() -> str:
                        try:
                            return (driver.execute_script(
                                """
                                const root = document.querySelector('#audio-device-selector');
                                if (!root) return '';
                                const v = root.querySelector('.value');
                                return ((v ? v.innerText : root.innerText) || '').trim();
                                """
                            ) or "").strip()
                        except Exception:
                            return ""

                    mic_ok_in_ui = False
                    for mic_attempt in range(8):
                        try:
                            mic_btn = driver.find_element(
                                By.CSS_SELECTOR, "#audio-device-selector ui-button"
                            )
                            driver.execute_script("arguments[0].click();", mic_btn)
                            time.sleep(0.6)
                            res = driver.execute_script(_MIC_SELECT_JS, "koov")
                            _ft_step(
                                meeting_id,
                                "pulse_rewire",
                                "mic-select attempt %d result=%r"
                                % (mic_attempt + 1, res),
                            )
                            time.sleep(0.8)
                            cur = _verify_mic_selected()
                            if cur and "no device selected" not in cur.lower():
                                _ft_step(
                                    meeting_id,
                                    "pulse_rewire",
                                    "mic label now shows %r — selection committed"
                                    % cur[:120],
                                )
                                mic_ok_in_ui = True
                                break
                            try:
                                driver.execute_script(
                                    """
                                    const bd = document.querySelector('ui-pane-backdrop');
                                    if (bd && bd.offsetParent) bd.click();
                                    """
                                )
                            except Exception:
                                pass
                            time.sleep(0.6)
                        except Exception as e:
                            _ft_step(
                                meeting_id,
                                "pulse_rewire",
                                "mic-select attempt %d exception: %s"
                                % (mic_attempt + 1, e),
                                level=logging.DEBUG,
                            )
                            time.sleep(0.6)
                    if not mic_ok_in_ui:
                        _ft_step(
                            meeting_id,
                            "pulse_rewire",
                            "mic-select: could not commit 'koov_mic_source' in FaceTime UI after 8 tries",
                            level=logging.WARNING,
                        )
                    time.sleep(15)
                    _rewire_chrome_source_outputs(meeting_id, _koov_mic_src)
                    time.sleep(15)
                    _rewire_chrome_source_outputs(meeting_id, _koov_mic_src)
                    return
                _ft_step(meeting_id, "pulse_rewire",
                    "retry %d/60: no Chrome source-output yet" % (attempt + 1))
                    

        threading.Thread(target=_rewire_with_retry, daemon=True, name="pulse-rewire").start()

        def _set_state(s: str) -> None:
            nonlocal facetime_state
            facetime_state = s

        _set_state("waiting_for_name")
        write(
            "joining",
            facetime_state=facetime_state,
            facetime_nav_url=_url_for_log(join_url),
        )
        _ft_step(meeting_id, "name", "facetime_state=waiting_for_name; waiting for #name-entry (45s)")

        # FaceTime's name input is a React/framework-controlled element.  Selenium
        # send_keys / clear() do not fire the native DOM events React listens for, so
        # the value appears in the DOM but the framework never sees it — the Continue
        # button stays disabled.  Use the native HTMLInputElement value setter and
        # dispatch 'input'/'change' events so the framework enables Continue.
        _JS_SET_VALUE = """
(function(input, value) {
    var nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    nativeSetter.call(input, value);
    input.dispatchEvent(new Event('input',  { bubbles: true, cancelable: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
})(arguments[0], arguments[1]);
"""

        def _find_name_input(deadline: float):
            """Return the #name-entry element once it is present and visible."""
            while time.time() < deadline:
                try:
                    driver.switch_to.default_content()
                    # Primary: Apple's stable ID
                    for sel in (
                        "#name-entry",
                        "input[autocomplete='name']",
                        "input[id*='name']",
                        "input[type='text']",
                    ):
                        els = driver.find_elements(By.CSS_SELECTOR, sel)
                        for el in els:
                            try:
                                if el.is_displayed():
                                    return el
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(0.4)
            return None

        def _fill_name_and_click_continue() -> bool:
            """
            Set the display name via native setter, wait for Continue to enable,
            then click it.  Returns True on success.
            """
            inp = _find_name_input(time.time() + 45.0)
            if not inp:
                _ft_step(
                    meeting_id, "name",
                    "no visible name input after 45s (Apple UI may skip name step)",
                    level=logging.WARNING,
                )
                return False

            _ft_step(meeting_id, "name", "name input found; injecting display_name via native setter")

            # Focus then set value through native setter so React sees the change.
            try:
                driver.execute_script("arguments[0].focus();", inp)
            except Exception:
                pass
            try:
                driver.execute_script(_JS_SET_VALUE, inp, display_name)
            except Exception as exc:
                _ft_step(meeting_id, "name", "native setter failed, falling back to send_keys: %s" % exc,
                         level=logging.WARNING)
                try:
                    inp.clear()
                    inp.send_keys(display_name)
                except Exception:
                    pass

            # Diagnostic — verify the value was accepted.
            try:
                cur_val = driver.execute_script("return arguments[0].value;", inp) or ""
                try:
                    aria_dis = (
                        driver.find_element(By.CSS_SELECTOR, "ui-button.name-entry-continue-button")
                              .get_attribute("aria-disabled") or ""
                    )
                except Exception:
                    aria_dis = "?"
                _ft_step(
                    meeting_id, "name",
                    "after inject: input.value=%r aria-disabled=%r" % (cur_val, aria_dis),
                )
            except Exception:
                pass

            # Wait up to 10s for the Continue / ui-button to become enabled.
            continue_el = None
            _t_wait = time.time() + 10.0
            while time.time() < _t_wait:
                try:
                    for _sel in (
                        "ui-button.name-entry-continue-button",
                        "ui-button.continue-button",
                        "ui-button.cta.continue-button",
                        "ui-button[aria-disabled='false']",
                        "ui-button:not([aria-disabled='true'])",
                    ):
                        candidates = driver.find_elements(By.CSS_SELECTOR, _sel)
                        for _c in candidates:
                            try:
                                _txt = ((_c.text or "") + " " + (_c.get_attribute("aria-label") or "")).lower()
                                if "continue" in _txt or "join" in _txt or "next" in _txt or not _txt.strip():
                                    _dis = (_c.get_attribute("aria-disabled") or "").strip()
                                    if _dis != "true":
                                        continue_el = _c
                                        break
                            except Exception:
                                pass
                        if continue_el:
                            break
                except Exception:
                    pass
                if continue_el:
                    break
                time.sleep(0.3)

            if continue_el:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                        continue_el,
                    )
                    _ft_step(meeting_id, "name", "Continue button clicked after name injection")
                    return True
                except Exception as exc:
                    _ft_step(meeting_id, "name", "Continue click failed: %s" % exc, level=logging.WARNING)
            else:
                # Button stayed disabled — fire Enter key on the input as last resort.
                _ft_step(
                    meeting_id, "name",
                    "Continue still disabled after 10s; firing Enter key on input as fallback",
                    level=logging.WARNING,
                )
                try:
                    driver.execute_script(
                        """
arguments[0].dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, key:'Enter', keyCode:13}));
arguments[0].dispatchEvent(new KeyboardEvent('keyup',   {bubbles:true, key:'Enter', keyCode:13}));
""",
                        inp,
                    )
                except Exception:
                    pass
                try:
                    inp.send_keys(Keys.ENTER)
                except Exception:
                    pass

            return False

        name_submitted = _fill_name_and_click_continue()
        _ft_step(
            meeting_id, "name",
            "name entry phase done: submitted=%s; proceeding to join-click poll" % name_submitted,
        )
        time.sleep(0.8)

        _set_state("waiting_for_join_click")
        write("joining", facetime_state=facetime_state)
        _ft_step(meeting_id, "join_click", "facetime_state=waiting_for_join_click; polling for Join/Continue")

        # With a live virtual camera FaceTime may default video on in the lobby.
        # Best effort only: never delays or blocks the join on failure.
        if camera_controller is not None and not camera_desired_enabled:
            try:
                _prejoin_cam = camera_controller.set_enabled(False, require_selection=False)
                _ft_step(
                    meeting_id,
                    "camera_repair",
                    "pre-join camera off: enabled=%s error_code=%r"
                    % (_prejoin_cam.enabled, _prejoin_cam.error_code),
                )
            except Exception as _prejoin_exc:  # noqa: BLE001
                _ft_step(meeting_id, "camera_repair", "pre-join camera off failed: %s" % _prejoin_exc,
                         level=logging.WARNING)

        def _element_join_label(el) -> str:
            try:
                parts = [
                    (el.text or ""),
                    (el.get_attribute("aria-label") or ""),
                    (el.get_attribute("title") or ""),
                    (el.get_attribute("value") or ""),
                ]
                return " ".join(parts).strip().lower()
            except Exception:
                return ""

        def _js_click(el) -> None:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)

        def _label_suggests_join(label: str) -> bool:
            """Match primary actions without firing on e.g. 'joining…' loading copy."""
            t = (label or "").strip().lower()
            if not t:
                return False
            toks = set(re.findall(r"[a-z0-9]+", t))
            if toks & {"continue", "next"}:
                return True
            if "join" in toks:
                return True
            if "join call" in t or "join facetime" in t or "join meeting" in t:
                return True
            return False

        def _try_js_join_click() -> bool:
            """Apple often renders controls in ways Selenium misses; click via DOM text scan.
            Skips elements that still have aria-disabled='true' so we never click a disabled
            Continue button and get stuck on the name-entry screen.
            """
            try:
                return bool(
                    driver.execute_script(
                        """
                        var sel = "ui-button, button, a, [role='button'], div[role='button'], span[role='button']";
                        var els = document.querySelectorAll(sel);
                        for (var i = 0; i < els.length; i++) {
                          var el = els[i];
                          if (!el || !el.offsetParent) continue;
                          if (el.getAttribute("aria-disabled") === "true") continue;
                          if (el.hasAttribute("disabled")) continue;
                          var t = ((el.innerText || "") + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
                          if (t.indexOf("joining") !== -1) continue;
                          if (t.indexOf("join") !== -1 || t.indexOf("continue") !== -1 || t.indexOf("next") !== -1) {
                            el.scrollIntoView({block: "center", inline: "nearest"});
                            el.click();
                            return true;
                          }
                        }
                        return false;
                        """
                    )
                )
            except Exception:
                return False

        def _try_click_join_in_current_context() -> bool:
            try:
                for el in driver.find_elements(
                    By.CSS_SELECTOR,
                    "ui-button, button, a[role='button'], [role='button'], div[role='button'], span[role='button'], a",
                ):
                    try:
                        if not el.is_displayed():
                            continue
                        # Skip disabled buttons — clicking them re-triggers name-entry loop.
                        if (el.get_attribute("aria-disabled") or "").strip() == "true":
                            continue
                        if (el.get_attribute("disabled") or "") not in ("", None):
                            continue
                    except Exception:
                        continue
                    label = _element_join_label(el)
                    if not label:
                        continue
                    if _label_suggests_join(label):
                        try:
                            el.click()
                            return True
                        except Exception:
                            try:
                                _js_click(el)
                                return True
                            except Exception:
                                continue
            except Exception:
                pass
            return _try_js_join_click()

        def _try_click_join_all_frames() -> bool:
            driver.switch_to.default_content()
            if _try_click_join_in_current_context():
                return True
            top_frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            for fr in top_frames:
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(fr)
                    if _try_click_join_in_current_context():
                        driver.switch_to.default_content()
                        return True
                    for fr2 in driver.find_elements(By.CSS_SELECTOR, "iframe, frame"):
                        try:
                            driver.switch_to.frame(fr2)
                            if _try_click_join_in_current_context():
                                driver.switch_to.default_content()
                                return True
                        except Exception:
                            pass
                        try:
                            driver.switch_to.parent_frame()
                        except Exception:
                            break
                except Exception:
                    pass
            driver.switch_to.default_content()
            return False

        def _mic_selection_label() -> str:
            try:
                return str(
                    driver.execute_script(
                        """
                        var root = document.querySelector('#audio-device-selector');
                        if (!root) return '';
                        var value = root.querySelector('.value');
                        var txt = (value ? value.innerText : root.innerText) || '';
                        return txt.trim();
                        """
                    )
                    or ""
                ).strip()
            except Exception:
                return ""

        def _ensure_facetime_mic_selected() -> bool:
            current = _mic_selection_label()
            if current and "no device selected" not in current.lower():
                return True

            try:
                devices = driver.execute_async_script(
                    """
                    const done = arguments[0];
                    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
                      done([]);
                      return;
                    }
                    navigator.mediaDevices.enumerateDevices()
                      .then(ds => done(ds.map(d => ({
                        kind: d.kind || '',
                        label: d.label || '',
                        deviceId: d.deviceId || ''
                      }))))
                      .catch(() => done([]));
                    """
                )
                audio_labels = [
                    (d.get("label") or "").strip()
                    for d in (devices or [])
                    if isinstance(d, dict) and (d.get("kind") or "") == "audioinput"
                ]
                if audio_labels:
                    _ft_step(
                        meeting_id,
                        "mic_select",
                        "enumerated audioinput devices: %s" % ", ".join(repr(x) for x in audio_labels[:6]),
                    )
            except Exception:
                pass

            try:
                opened = bool(
                    driver.execute_script(
                        """
                        var root = document.querySelector('#audio-device-selector');
                        if (!root || !root.offsetParent) return false;
                        var btn = root.querySelector('ui-button,button,[role="button"]') || root;
                        btn.scrollIntoView({block: 'center', inline: 'nearest'});
                        btn.click();
                        return true;
                        """
                    )
                )
            except Exception:
                opened = False
            if not opened:
                return False

            time.sleep(0.6)

            try:
                chosen = driver.execute_script(
                    """
                    var deny = ['no device selected', 'camera', 'done', 'leave', 'copy link'];
                    // Do NOT include generic 'microphone' — it matches the dropdown header
                    // ("Microphone / No Device Selected") before the real koov_mic_source row.
                    var preferred = [
                      'koov virtual microphone',
                      'koov_mic_source',
                      'koov_mic',
                      'koov'
                    ];
                    function usable(text) {
                      var t = text.toLowerCase().replace(/\\s+/g, ' ').trim();
                      if (!t) return false;
                      if (t.indexOf('use system settings') !== -1) return false;
                      // Reject the broken placeholder row; real devices include koov_*.
                      if (t.indexOf('no device selected') !== -1 && t.indexOf('koov') === -1)
                        return false;
                      if (t === 'microphone') return false;
                      for (var d of deny) {
                        if (t === d) return false;
                      }
                      return true;
                    }
                    // Phase A: FaceTime renders mic options as ui-menu-item with .unselected-item-label.
                    var menuCandidates = document.querySelectorAll(
                      'ui-menu-item[role="menuitem"], ui-menu-item[role="menuitemradio"], ' +
                      '[role="menuitemradio"], [role="menuitem"]'
                    );
                    for (var i = 0; i < menuCandidates.length; i++) {
                      var mi = menuCandidates[i];
                      if (!mi || !mi.offsetParent) continue;
                      var lab = mi.querySelector('.unselected-item-label, .selected-item-label');
                      var raw = lab ? (lab.innerText || '').trim() : '';
                      if (!raw) raw = (mi.innerText || '').trim();
                      if (!raw) continue;
                      var low = raw.toLowerCase().replace(/\\s+/g, ' ').trim();
                      if (low.indexOf('use system settings') !== -1) continue;
                      if (low.indexOf('koov_mic') !== -1 || low.indexOf('koov virtual') !== -1) {
                        mi.scrollIntoView({block: 'center', inline: 'nearest'});
                        mi.click();
                        return raw;
                      }
                    }
                    var sels = [
                      '[role="menuitem"]',
                      'li',
                      'button',
                      'ui-button',
                      '[role="button"]',
                      '.menu-item',
                      '.dropdown-item'
                    ];
                    var seen = new Set();
                    var items = [];
                    for (var s of sels) {
                      document.querySelectorAll(s).forEach(function(el) {
                        if (!el || !el.offsetParent) return;
                        if (seen.has(el)) return;
                        seen.add(el);
                        var txt = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
                        if (!txt) return;
                        items.push({el: el, txt: txt});
                      });
                    }
                    for (var p of preferred) {
                      for (var j = 0; j < items.length; j++) {
                        var item = items[j];
                        var t = item.txt.toLowerCase().replace(/\\s+/g, ' ').trim();
                        if (usable(item.txt) && t.indexOf(p) !== -1) {
                          item.el.scrollIntoView({block: 'center', inline: 'nearest'});
                          item.el.click();
                          return item.txt;
                        }
                      }
                    }
                    for (var k = 0; k < items.length; k++) {
                      var it = items[k];
                      if (usable(it.txt)) {
                        it.el.scrollIntoView({block: 'center', inline: 'nearest'});
                        it.el.click();
                        return it.txt;
                      }
                    }
                    return '';
                    """
                )
            except Exception:
                chosen = ""

            if chosen:
                _ft_step(meeting_id, "mic_select", "selected FaceTime microphone option %r" % str(chosen)[:120])
                time.sleep(0.6)
            else:
                _ft_step(
                    meeting_id,
                    "mic_select",
                    "microphone dropdown opened but no selectable option was found",
                    level=logging.WARNING,
                )

            try:
                driver.execute_script(
                    """
                    var doneBtn = document.querySelector('#extended-menu-leave-button, ui-button.close-button, button.close-button');
                    if (doneBtn && doneBtn.offsetParent) doneBtn.click();
                    """
                )
            except Exception:
                pass

            after = _mic_selection_label()
            if after and "no device selected" not in after.lower():
                _ft_step(meeting_id, "mic_select", "microphone selector now shows %r" % after[:120])
                return True
            return False

        # ── Harden join-click: MutationObserver hint + fast early polling ────
        # Install a MutationObserver so the browser side signals when a Join/
        # Continue button becomes enabled.  We still poll, but the fast phase
        # (first 20s) polls every 250ms instead of 1s for a snappier first-try.
        try:
            driver.execute_script(
                """
                (function() {
                    if (window._koovJoinReady) return;
                    window._koovJoinReady = false;
                    function _check() {
                        var els = document.querySelectorAll(
                            'ui-button, button, [role="button"]'
                        );
                        for (var i = 0; i < els.length; i++) {
                            var el = els[i];
                            if (!el.offsetParent) continue;
                            var txt = ((el.innerText || '') + ' ' +
                                       (el.getAttribute('aria-label') || '')).toLowerCase();
                            if ((txt.indexOf('join') !== -1 || txt.indexOf('continue') !== -1)
                                && txt.indexOf('joining') === -1
                                && el.getAttribute('aria-disabled') !== 'true') {
                                window._koovJoinReady = true;
                                return;
                            }
                        }
                    }
                    var obs = new MutationObserver(_check);
                    obs.observe(document.documentElement, {
                        childList: true, subtree: true, attributes: true,
                        attributeFilter: ['aria-disabled', 'disabled']
                    });
                    _check();
                })();
                """
            )
        except Exception:
            pass

        join_clicked = False
        join_max_s = int(os.environ.get("KOOV_FT_JOIN_TIMEOUT_S", "90"))
        _fast_phase_s = 20   # poll every 250ms for first 20s
        _t_join_start = time.monotonic()
        _attempt = 0
        while not stop_event.is_set():
            _elapsed = time.monotonic() - _t_join_start
            if _elapsed >= join_max_s:
                break
            _attempt += 1
            if _attempt == 1 or _attempt % 40 == 0:
                _ft_step(
                    meeting_id,
                    "join_click_poll",
                    "still searching Join/Continue attempt=%d elapsed=%.0fs/max=%ds"
                    % (_attempt, _elapsed, join_max_s),
                )
            if _try_click_join_all_frames():
                join_clicked = True
                _ft_step(
                    meeting_id,
                    "join_click",
                    "clicked Join/Continue attempt=%d elapsed=%.1fs"
                    % (_attempt, time.monotonic() - _t_join_start),
                )
                break
            # Fast phase: 250ms; slow phase: 1s
            time.sleep(0.25 if _elapsed < _fast_phase_s else 1.0)
        if not join_clicked:
            _ft_step(
                meeting_id,
                "join_click",
                "no Join/Continue clicked within %ss; host may never see a knock" % join_max_s,
                level=logging.WARNING,
            )
            # Save diagnostic screenshot + HTML so operator can see what blocked the click.
            try:
                shot = os.path.join(notes_dir, f"facetime-{meeting_id}-joinclick-miss.png")
                htmlp = os.path.join(notes_dir, f"facetime-{meeting_id}-joinclick-miss.html")
                driver.save_screenshot(shot)
                with open(htmlp, "w", encoding="utf-8") as _hf:
                    _hf.write(driver.page_source or "")
                _ft_step(meeting_id, "join_click",
                         "diagnostic screenshot/html saved: %s" % shot)
            except Exception as _sc_exc:
                _ft_step(meeting_id, "join_click",
                         "screenshot failed (non-fatal): %s" % _sc_exc)

        _set_state("waiting_for_admit")
        write(
            "joining",
            facetime_state=facetime_state,
            facetime_join_clicked=join_clicked,
            # facetime_ready=True means Koov has knocked and the host just needs to tap Admit.
            facetime_ready=join_clicked,
        )
        _ft_step(
            meeting_id,
            "admit_wait",
            "facetime_state=waiting_for_admit join_clicked=%s facetime_ready=%s "
            "(host must admit on Apple device)"
            % (join_clicked, join_clicked),
        )

        # ── Join boot thread ───────────────────────────────────────────────────
        # KoovAgent has been building in parallel. Wait for it here (after join
        # click) — most of the 15–60s overlap already happened.  Give it up to
        # 120s extra in case models were slow to load.
        if _agent_boot_thread is not None and _agent_boot_thread.is_alive():
            _ft_step(
                meeting_id,
                "koov_agent",
                "waiting for background KoovAgent boot to finish (up to 120s)…",
            )
            _agent_boot_thread.join(timeout=120.0)
            if _agent_boot_thread.is_alive():
                _msg = "KoovAgent boot thread still alive after 120s join; proceeding anyway"
                _ft_step(meeting_id, "koov_agent", _msg, level=logging.WARNING)
                write("joining", facetime_state=facetime_state, koov_boot_warning=_msg)

        if _agent_boot_error:
            raise RuntimeError("KoovAgent boot failed: %s" % _agent_boot_error[0])

        agent = agent_holder.get("agent")
        if agent is None:
            raise RuntimeError("KoovAgent was not constructed by boot thread")

        _ft_step(meeting_id, "koov_agent", "KoovAgent ready; binding meeting + starting run_forever thread")
        # Pin specific past meetings as memory if provided via env var
        _pinned_ids_json = (os.environ.get("KOOV_MEMORY_MEETING_IDS_JSON") or "").strip()
        if _pinned_ids_json:
            try:
                _pinned_ids = json.loads(_pinned_ids_json)
                if isinstance(_pinned_ids, list):
                    agent._pinned_memory_ids = [str(m).strip() for m in _pinned_ids if m]
            except Exception:
                pass
        agent.set_meeting(meeting_id)
        try:
            _tp_boot = getattr(getattr(agent, "_note_store", None), "path", "") or ""
            if _tp_boot:
                os.makedirs(os.path.dirname(_tp_boot) or notes_dir, exist_ok=True)
                with open(_tp_boot, "a", encoding="utf-8"):
                    pass
                _ft_step(meeting_id, "koov_agent", "transcript file ready path=%r" % _tp_boot)
        except Exception as _tp_exc:
            _ft_step(
                meeting_id,
                "koov_agent",
                "could not create transcript file: %s" % _tp_exc,
                level=logging.WARNING,
            )
        if team_id and shared_context_enabled:
            agent.set_team_context(team_id)
        if participant_timezones_json:
            try:
                _tz_map = json.loads(participant_timezones_json)
                if isinstance(_tz_map, dict):
                    agent.merge_participant_timezones({str(k): str(v) for k, v in _tz_map.items()})
            except Exception as _tz_exc:
                _ft_step(meeting_id, "timezone", "failed to parse KOOV_PARTICIPANT_TIMEZONES_JSON: %s" % _tz_exc, level=logging.WARNING)
        # KoovAgent uses sounddevice device "pulse", which typically binds to PulseAudio's
        # *default source* — not necessarily PULSE_SOURCE. The entrypoint sets default to
        # koov_mic_source for Chromium; transcript-only wake needs call_sink.monitor (FaceTime
        # playback). Switch default now so the agent's InputStream opens on the monitor; Chrome
        # capture already targets koov_mic_source via WebRTC + pactl move-source-output.
        _reassert_agent_listen_route(
            meeting_id, reason="before KoovAgent (transcript wake / STT hear FaceTime)"
        )
        _reassert_tts_output_route(meeting_id)
        try:
            agent._audio_in.start()
            if getattr(agent._audio_in, "_no_device", False):
                _ft_step(
                    meeting_id,
                    "koov_agent",
                    "audio input unavailable (PortAudio/pulse); Koov cannot hear the call",
                    level=logging.ERROR,
                )
            else:
                _ft_step(meeting_id, "koov_agent", "audio input stream opened device=%r" % config.input_device)
        except Exception as _aud_exc:
            _ft_step(
                meeting_id,
                "koov_agent",
                "audio input start check failed: %s" % _aud_exc,
                level=logging.WARNING,
            )

        agent_thread = threading.Thread(
            target=agent.run_forever, daemon=True, name=f"KoovAgent-{meeting_id}"
        )
        agent_thread.start()
        _ft_step(meeting_id, "koov_agent", "run_forever thread alive=%s" % agent_thread.is_alive())
        if vision_enabled and hasattr(agent, "set_visual_query_handler"):
            _vision_bridge = VoiceVisionBridge(
                notes_dir, meeting_id, max_per_minute=vision_max_per_minute
            )
            try:
                agent.set_visual_query_handler(_vision_bridge.ask)
                _ft_step(meeting_id, "vision_requested", "voice visual questions enabled")
            except Exception as _bridge_exc:  # noqa: BLE001
                _ft_step(meeting_id, "vision_failed", "voice bridge registration failed: %s" % _bridge_exc,
                         level=logging.WARNING)
        try:
            _boot_tp = getattr(getattr(agent, "_note_store", None), "path", "") or ""
            write(
                "joining",
                facetime_state=facetime_state,
                transcript_path=os.path.abspath(_boot_tp) if _boot_tp else "",
                koov_running=agent_thread.is_alive(),
            )
        except Exception:
            pass

        # Loop: update status based on page content; transition to in_call when admitted.
        last_logged_facetime_state: str | None = None
        poll_n = 0
        # Tracks whether Phase-2 mic rewire succeeded; exposed in worker-status for dashboards.
        pulse_rewire_ok: bool = False
        facetime_mic_ok: bool = False
        # While in_call, full ``page_source`` is expensive and can destabilize Chromium/FaceTime;
        # refresh it on a slower cadence and reuse a cache between refreshes.
        _page_text_in_call_cache: str = ""
        _last_in_call_page_text_ts: float = 0.0
        _last_pulse_maint_ts: float = 0.0
        _last_roster_poll_ts: float = 0.0
        _last_active_poll_ts: float = 0.0
        _last_active_name: str = ""
        _participant_names: list[str] = []
        _participant_name_keys: set[str] = set()
        # Names seen on the previous roster poll. A candidate must show up on two
        # consecutive polls before it counts as a real participant — transient DOM
        # text / one-off UI labels never persist, so this kills the roster overcount.
        _roster_seen_prev: set[str] = set()
        # After host admits, waiting-room copy disappears before toolbar strings appear.
        _admit_clear_streak: int = 0
        # "call ended" can appear transiently in FaceTime's join/loading DOM before
        # we ever reach the call. Require consecutive confirmations during join so a
        # flicker doesn't make Koov bail while the host is actively in the call.
        _call_ended_streak: int = 0
        _ever_in_call: bool = False

        # DOM strings that suggest we are already in the call (Apple may update copy over time;
        # using a broad set reduces fragility).
        _in_call_signals = [
            "leave call", "end call",                    # leave/end button labels
            "microphone", "camera", "participants",      # in-call toolbar strings
            "mute", "unmute",                            # mic mute/unmute labels
            "you're muted", "you are muted",
            "add person", "shareplay", "grid layout",
            "video effects",
        ]
        from koov.facetime_env import page_is_name_lobby as _page_is_name_lobby
        _last_lobby_recovery_ts = 0.0
        _lobby_recovery_interval_s = 20.0
        # DOM strings that indicate waiting-room / admission pending.
        # Do NOT use short substrings like "approved" — Apple's in-call participant rows use
        # CSS classes such as "approved-participant", so matching "approved" would always
        # classify the page as waiting and we'd never reach facetime_state=in_call (no
        # in-call Pulse rewire thread, no FaceTime mic dropdown loop — remote hears nothing).
        # Likewise avoid bare "waiting" (common in scripts / unrelated copy).
        _waiting_signals = [
            "waiting to be let in",
            "waiting to be let in…",
            "waiting to be let in...",
            "let you in when they're ready",
            "knocking",
        ]
        # DOM strings that mean the FaceTime call has ended for this participant
        # (host hung up from their iPhone, kicked us, link expired, etc.).
        # When seen, the worker exits cleanly so the API server can rm the container
        # and the box doesn't pile up zombie workers across calls.
        _call_ended_signals = [
            "call ended",
            "the call has ended",
            "you have been removed",
            "you've been removed",
            "facetime ended",
            "call has ended",
            "this call has ended",
            "the host ended the call",
            "the host left",
            "session has ended",
        ]
        # Heartbeat tracking so the user sees the bot is alive while it waits to be admitted.
        _last_heartbeat_ts = 0.0
        _heartbeat_interval_s = 30.0
        _loop_start_mono = time.monotonic()
        # Time the loop spends blocked (e.g. a WebDriver call stuck for 120s while
        # Chromium's renderer is saturated as the call connects) must not count as
        # "waiting to be admitted", or a just-admitted bot exits as stale.
        _prev_iter_mono = 0.0
        _admit_stalled_s = 0.0
        _first_in_call_mono = 0.0
        _provisional_in_call_since = 0.0

        while not stop_event.is_set():
            if agent_thread is not None and not agent_thread.is_alive():
                _ft_step(
                    meeting_id,
                    "koov_agent",
                    "run_forever thread exited; leaving browser poll loop",
                    level=logging.WARNING,
                )
                break
            page_text = ""
            _poll_now = time.time()
            _iter_mono = time.monotonic()
            if _prev_iter_mono:
                _iter_gap = _iter_mono - _prev_iter_mono
                if _iter_gap > 30.0:
                    _admit_stalled_s += _iter_gap - 5.0
                    _ft_step(
                        meeting_id,
                        "loop_stall",
                        "poll loop blocked for %.0fs (state=%s); not counted toward admit timeout"
                        % (_iter_gap, facetime_state),
                        level=logging.WARNING,
                    )
            _prev_iter_mono = _iter_mono
            try:
                _in_call_dom_s = float(
                    (os.environ.get("KOOV_FT_IN_CALL_PAGE_POLL_S") or "12").strip() or "12"
                )
            except ValueError:
                _in_call_dom_s = 12.0
            if _in_call_dom_s < 4.0:
                _in_call_dom_s = 4.0
            try:
                if (
                    facetime_state == "in_call"
                    and _page_text_in_call_cache
                    and (_poll_now - _last_in_call_page_text_ts) < _in_call_dom_s
                ):
                    page_text = _page_text_in_call_cache
                else:
                    page_text = (driver.page_source or "").lower()
                    if facetime_state == "in_call":
                        _page_text_in_call_cache = page_text
                        _last_in_call_page_text_ts = _poll_now
            except Exception:
                page_text = ""

            dbg_title = ""
            dbg_url = ""
            try:
                dbg_title = (driver.title or "")[:200]
                dbg_url = _url_for_log(driver.current_url or "")
            except Exception:
                pass

            # Detect explicit "call ended" / "removed" before the in_call sticky branch,
            # so when the host ends the call from their iPhone the worker exits cleanly
            # (and the API server's _watch_container thread can rm the container).
            # Without this, the worker stays in `in_call` forever, holding RAM/CPU/PA,
            # and stale-cleanup on the *next* call has to forcibly remove it.
            _ended_matches = [t for t in _call_ended_signals if t in page_text]
            if _ended_matches:
                _call_ended_streak += 1
            else:
                _call_ended_streak = 0
            # Once we've genuinely been in the call, a single detection means the
            # host ended it — exit promptly. During join (never in_call yet) require
            # the signal to persist across consecutive polls so a transient match in
            # FaceTime's loading DOM can't make Koov bail while the host is live.
            _ended_required = 1 if _ever_in_call else 3
            if _ended_matches and _call_ended_streak >= _ended_required:
                _ft_step(
                    meeting_id,
                    "call_state",
                    "call-ended signal confirmed (%s; streak=%d, ever_in_call=%s); exiting"
                    % (", ".join(_ended_matches[:3]), _call_ended_streak, _ever_in_call),
                )
                facetime_state = "ended"
                write("stopped", facetime_state="ended")
                stop_event.set()
                break
            if _ended_matches:
                _ft_step(
                    meeting_id,
                    "call_state",
                    "call-ended signal seen but not yet confirmed (%s; streak=%d/%d) — "
                    "treating as transient during join"
                    % (", ".join(_ended_matches[:3]), _call_ended_streak, _ended_required),
                    level=logging.WARNING,
                )

            # Name-entry lobby must not stick as in_call (admit heuristic can fire too early).
            if _page_is_name_lobby(page_text):
                if facetime_state == "in_call":
                    _ft_step(
                        meeting_id,
                        "call_state",
                        "still on name-entry lobby — correcting false in_call",
                        level=logging.WARNING,
                    )
                facetime_state = "waiting_for_name"
                _admit_clear_streak = 0
                if (_poll_now - _last_lobby_recovery_ts) >= _lobby_recovery_interval_s:
                    _last_lobby_recovery_ts = _poll_now
                    try:
                        if _fill_name_and_click_continue():
                            _set_state("waiting_for_join_click")
                            time.sleep(0.6)
                            if _try_click_join_all_frames():
                                join_clicked = True
                                _ft_step(meeting_id, "lobby_recovery", "re-submitted name + join click")
                    except Exception as _lre:
                        _ft_step(
                            meeting_id,
                            "lobby_recovery",
                            "name lobby recovery failed: %s" % _lre,
                            level=logging.DEBUG,
                        )
            elif facetime_state == "in_call":
                pass  # sticky in_call only when not on the name lobby
            elif any(t in page_text for t in _waiting_signals):
                facetime_state = "waiting_for_admit"
                _admit_clear_streak = 0
            elif any(t in page_text for t in _in_call_signals):
                facetime_state = "in_call"
                _admit_clear_streak = 0
            elif any(t in (dbg_title or "").lower() for t in _in_call_signals):
                # URL/title fallback for Apple UI changes that hide DOM strings
                facetime_state = "in_call"
                _admit_clear_streak = 0
            elif join_clicked and facetime_state in (
                "waiting_for_admit",
                "joining",
                "waiting_for_join_click",
            ):
                # Host admitted on iPhone: waiting copy vanishes before toolbar DOM is stable.
                # Guard against transient blank/loading pages; they briefly contain no
                # waiting text too, but are NOT a real admit.
                if (
                    len(page_text) > 200
                    and not any(t in page_text for t in _waiting_signals)
                    and not _page_is_name_lobby(page_text)
                ):
                    _admit_clear_streak += 1
                    if _admit_clear_streak >= 2:
                        facetime_state = "in_call"
                        _ft_step(
                            meeting_id,
                            "call_state",
                            "promoted to in_call (admit heuristic: waiting copy gone) — "
                            "provisional, awaiting toolbar/transcript confirmation",
                        )
                else:
                    _admit_clear_streak = 0
            else:
                # Keep last known state
                facetime_state = facetime_state or "joining"

            if facetime_state == "in_call":
                _positive_in_call = any(t in page_text for t in _in_call_signals) or any(
                    t in (dbg_title or "").lower() for t in _in_call_signals
                )
                try:
                    _chunks_now = int(getattr(agent, "_transcript_chunk_id", 0) or 0)
                except Exception:
                    _chunks_now = 0
                if _positive_in_call or _chunks_now > 0:
                    if not _ever_in_call:
                        _ever_in_call = True
                        _first_in_call_mono = time.monotonic()
                        if _provisional_in_call_since:
                            _ft_step(
                                meeting_id,
                                "call_state",
                                "in_call confirmed (%s)"
                                % ("toolbar DOM" if _positive_in_call else "transcript"),
                            )
                    _provisional_in_call_since = 0.0
                elif not _ever_in_call:
                    if _provisional_in_call_since == 0.0:
                        _provisional_in_call_since = time.monotonic()
                    try:
                        _confirm_s = float(
                            (os.environ.get("KOOV_FT_ADMIT_CONFIRM_S") or "45").strip() or "45"
                        )
                    except ValueError:
                        _confirm_s = 45.0
                    if (time.monotonic() - _provisional_in_call_since) >= _confirm_s:
                        _ft_step(
                            meeting_id,
                            "call_state",
                            "provisional in_call unconfirmed after %ds (no toolbar/transcripts) — "
                            "reverting to waiting_for_admit + re-clicking join"
                            % int(_confirm_s),
                            level=logging.WARNING,
                        )
                        facetime_state = "waiting_for_admit"
                        _admit_clear_streak = 0
                        _provisional_in_call_since = 0.0
                        try:
                            _try_click_join_all_frames()
                        except Exception:
                            pass

            try:
                _admit_timeout_s = float(
                    (os.environ.get("KOOV_FT_ADMIT_TIMEOUT_S") or "180").strip() or "180"
                )
            except ValueError:
                _admit_timeout_s = 180.0
            _mono_now = time.monotonic()
            if (
                not _ever_in_call
                and _admit_timed_out(
                    elapsed_s=_mono_now - _loop_start_mono,
                    stalled_s=_admit_stalled_s,
                    current_pass_s=_mono_now - _iter_mono,
                    timeout_s=_admit_timeout_s,
                )
            ):
                _ft_step(
                    meeting_id,
                    "self_cleanup",
                    "never admitted after %ds (state=%s); exiting stale FaceTime worker"
                    % (int(time.monotonic() - _loop_start_mono), facetime_state),
                    level=logging.WARNING,
                )
                facetime_state = "ended"
                write("stopped", facetime_state="ended")
                stop_event.set()
                break

            # Heartbeat: while the bot is waiting on the host to admit it on the iPhone,
            # there is otherwise long radio silence in the logs.  Emit a clear "I am alive,
            # waiting for you to admit me" message every ~30s so the operator can tell
            # the difference between "bot is hung" and "bot is waiting on a human action."
            _now = time.time()
            if (
                facetime_state in ("waiting_for_admit", "waiting_for_join_click")
                and (_now - _last_heartbeat_ts) >= _heartbeat_interval_s
            ):
                _last_heartbeat_ts = _now
                _ft_step(
                    meeting_id,
                    "heartbeat",
                    "bot alive, state=%r — admit Koov on the host iPhone "
                    "(open FaceTime app or the call screen and tap the green checkmark)"
                    % facetime_state,
                )

            poll_n += 1
            if facetime_state != last_logged_facetime_state:
                _ft_step(
                    meeting_id,
                    "call_state",
                    "facetime_state -> %r page_title=%r (poll #%s)"
                    % (facetime_state, (dbg_title or "")[:100], poll_n),
                )
                last_logged_facetime_state = facetime_state

                # When the bot enters the call, re-wire Chrome's mic and dump routing.
                if facetime_state == "in_call":
                    # Do NOT set PA default-source to koov_mic_source — that breaks KoovAgent
                    # listening on call_sink.monitor. Chrome capture is moved via pactl below;
                    # Chromium already inherits PULSE_SOURCE=koov_mic_source from the entrypoint.
                    _reassert_agent_listen_route(meeting_id, reason="in_call (keep hearing FaceTime)")
                    _reassert_tts_output_route(meeting_id)
                    _call_sink = (os.environ.get("KOOV_FT_PULSE_SINK") or "call_sink").strip()
                    _rewire_chrome_sink_inputs(meeting_id, _call_sink)
                    # Explicitly move Chrome's WebRTC capture stream to koov_mic_source.
                    # Chrome's audio service creates the source-output a few seconds after
                    # admission — retry every 3s for up to 90s until at least one is moved.
                    def _audio_rewire_loop():
                        import time as _time
                        nonlocal pulse_rewire_ok
                        for _attempt in range(30):  # 30 × 3s = 90s
                            if stop_event.is_set():
                                break
                            moved = _rewire_chrome_source_outputs(meeting_id, _koov_mic_src)
                            if moved > 0:
                                pulse_rewire_ok = True
                                _ft_step(meeting_id, "pulse_rewire",
                                         "audio-rewire-loop: success after attempt %d" % (_attempt + 1))
                                break
                            _time.sleep(3.0)
                        else:
                            _ft_step(meeting_id, "pulse_rewire",
                                     "audio-rewire-loop: gave up after 30 attempts — "
                                     "TTS may be inaudible to FaceTime participants",
                                     level=logging.WARNING)

                    _rewire_thread = threading.Thread(
                        target=_audio_rewire_loop, daemon=True,
                        name=f"audio-rewire-{meeting_id[:8]}"
                    )
                    _rewire_thread.start()
                    import subprocess as _sp
                    for _pa_cmd, _label in [
                        (["pactl", "list", "sink-inputs", "short"], "sink_inputs"),
                        (["pactl", "list", "source-outputs", "short"], "source_outputs"),
                        (["pactl", "list", "sinks", "short"], "sinks"),
                        (["pactl", "list", "sources", "short"], "sources"),
                    ]:
                        try:
                            _r = _sp.run(_pa_cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True)
                            _ft_step(meeting_id, "pulse_diag",
                                     "%s:\n%s" % (_label, (_r.stdout or "").strip()))
                        except Exception as _pe:
                            _ft_step(meeting_id, "pulse_diag",
                                     "%s: error=%s" % (_label, _pe))

            # Fast TTS-route guard: every in_call iteration, ensure Koov's (python)
            # TTS playback is on koov_sink. AudioOutput opens a fresh "pulse" stream
            # per utterance, so a clip can land on the wrong sink (then nobody hears
            # Koov). Running this on the fast loop catches replies and — via its
            # "pulse_tts" step — tells us definitively which sink TTS landed on.
            if facetime_state == "in_call":
                try:
                    # Keep koov_sink as default so each per-utterance "pulse" stream
                    # opens on it, then pin any that still landed elsewhere.
                    _reassert_tts_output_route(meeting_id)
                    _pin_koov_tts_to_sink(meeting_id, _ft_tts_sink_name())
                except Exception:
                    pass

            # Best-effort roster scrape: FaceTime exposes participant names in the
            # browser UI, while audio remains mixed. Feed names into Koov's memory
            # so it can answer "who's on the call?" and resolve owners in action
            # items. Diarization still separates voices acoustically.
            try:
                _roster_poll_s = float(
                    (os.environ.get("KOOV_FT_ROSTER_POLL_S") or "15").strip() or "15"
                )
            except ValueError:
                _roster_poll_s = 15.0
            if (
                facetime_state == "in_call"
                and (_now - _last_roster_poll_ts) >= max(5.0, _roster_poll_s)
            ):
                _last_roster_poll_ts = _now
                try:
                    _names = _extract_facetime_participant_names(
                        driver, display_name=display_name
                    )
                    _names_keys = {n.lower() for n in _names}
                    # Only accept names that also appeared on the previous poll
                    # (persistence gate) and aren't already registered.
                    _new = [
                        n for n in _names
                        if n.lower() not in _participant_name_keys
                        and n.lower() in _roster_seen_prev
                    ]
                    _roster_seen_prev = _names_keys
                    if _new:
                        _participant_names.extend(_new)
                        _participant_name_keys.update(n.lower() for n in _new)
                        if hasattr(agent, "register_call_participants"):
                            agent.register_call_participants(_new)
                        _ft_step(
                            meeting_id,
                            "participants",
                            "FaceTime roster detected: %s" % ", ".join(_participant_names),
                        )
                except Exception as _roster_exc:
                    _ft_step(
                        meeting_id,
                        "participants",
                        "FaceTime roster scrape failed: %s" % _roster_exc,
                        level=logging.DEBUG,
                    )

            # Active-speaker poll (fast): tells Koov which participant is speaking
            # right now so it can put a real name on each transcript line — the only
            # voice→name anchor on FaceTime's single mixed-audio stream. Polled much
            # faster than the roster so it tracks turn-taking. Disable with
            # KOOV_FT_ACTIVE_SPEAKER=0; tune cadence with KOOV_FT_ACTIVE_POLL_S.
            _active_enabled = (
                os.environ.get("KOOV_FT_ACTIVE_SPEAKER", "1").strip().lower()
                not in ("0", "false", "no", "off")
            )
            try:
                _active_poll_s = float(
                    (os.environ.get("KOOV_FT_ACTIVE_POLL_S") or "1.2").strip() or "1.2"
                )
            except ValueError:
                _active_poll_s = 1.2
            if (
                _active_enabled
                and facetime_state == "in_call"
                and hasattr(agent, "note_active_speaker")
                and (_now - _last_active_poll_ts) >= max(0.5, _active_poll_s)
            ):
                _last_active_poll_ts = _now
                try:
                    _speaking = _extract_facetime_active_speaker(
                        driver, display_name=display_name
                    )
                    if _speaking:
                        agent.note_active_speaker(_speaking)
                        if _speaking != _last_active_name:
                            _last_active_name = _speaking
                            _ft_step(
                                meeting_id,
                                "active_speaker",
                                "FaceTime active speaker: %s" % _speaking,
                                level=logging.DEBUG,
                            )
                except Exception as _active_exc:
                    _ft_step(
                        meeting_id,
                        "active_speaker",
                        "FaceTime active-speaker scrape failed: %s" % _active_exc,
                        level=logging.DEBUG,
                    )

            # Fast regression check (EVERY in_call poll, ~1.5s): the single biggest cause
            # of "remote can't hear Koov" is FaceTime silently flipping the mic row back to
            # "No Device Selected" mid-call (it then detaches the mic track from WebRTC).
            # Previously this was only noticed in the ~28s maintenance pass, leaving up to
            # 28s of dead air. Detect it here and flip the flag so the re-selection block
            # below re-attaches the mic within one poll.
            if facetime_state == "in_call" and facetime_mic_ok:
                try:
                    _fast_lbl = _mic_selection_label()
                    if (not _fast_lbl) or ("no device selected" in _fast_lbl.lower()):
                        facetime_mic_ok = False
                        _ft_step(
                            meeting_id,
                            "mic_select",
                            "mic regressed to %r (fast poll) — re-attaching Koov mic"
                            % ((_fast_lbl or "")[:80]),
                            level=logging.WARNING,
                        )
                except Exception:
                    pass

            # Every poll while in_call: keep re-asserting FaceTime's mic selection.
            # FaceTime's React UI renders "No Device Selected" whenever it thinks the
            # audioinput list has no usable labels, and in that state it does NOT attach
            # the mic MediaStreamTrack to its RTCPeerConnection — so remote hears nothing
            # even though Chromium's capture stream is correctly pointed at
            # koov_mic_source.  Keep clicking the dropdown and picking the Koov mic
            # until the label stops saying "No Device Selected".
            if facetime_state == "in_call" and not facetime_mic_ok:
                try:
                    current_mic_label = _mic_selection_label()
                except Exception:
                    current_mic_label = ""
                needs_select = (not current_mic_label) or (
                    "no device selected" in current_mic_label.lower()
                )
                if needs_select:
                    # Ask Chrome to re-enumerate devices (now that CDP granted
                    # audioCapture, labels should come back non-empty), and fire a
                    # devicechange event so FaceTime's React listener re-reads.
                    try:
                        driver.execute_script(
                            """
                            try {
                              navigator.mediaDevices && navigator.mediaDevices.dispatchEvent
                                  && navigator.mediaDevices.dispatchEvent(new Event('devicechange'));
                            } catch(e) {}
                            if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
                              navigator.mediaDevices.enumerateDevices().catch(function(){});
                            }
                            """
                        )
                    except Exception:
                        pass
                    try:
                        facetime_mic_ok = _ensure_facetime_mic_selected() or facetime_mic_ok
                    except Exception as _mse:
                        _ft_step(
                            meeting_id,
                            "mic_select",
                            "exception while selecting mic: %s" % _mse,
                            level=logging.WARNING,
                        )
                else:
                    facetime_mic_ok = True
                    _ft_step(
                        meeting_id,
                        "mic_select",
                        "microphone already shows %r; skipping auto-select"
                        % current_mic_label[:120],
                    )

            # Mid-call: Chrome may spawn a new capture stream after UI/network churn, and
            # FaceTime can revert the mic row to "No Device Selected" even when Pulse is
            # correct. Re-assert periodically for the whole meeting (not only until first ok).
            if facetime_state == "in_call":
                try:
                    _maint_iv = float(
                        (os.environ.get("KOOV_FT_PULSE_MAINT_INTERVAL_S") or "28").strip() or "28"
                    )
                except ValueError:
                    _maint_iv = 28.0
                if _maint_iv < 10.0:
                    _maint_iv = 10.0
                if (_poll_now - _last_pulse_maint_ts) >= _maint_iv:
                    _last_pulse_maint_ts = _poll_now
                    try:
                        driver.execute_script(
                            """
                            try {
                              if (window._koov_mic_stream) {
                                window._koov_mic_stream.getTracks().forEach(function(t) {
                                  try { t.stop(); } catch (e) {}
                                });
                              }
                            } catch (e) {}
                            navigator.mediaDevices.getUserMedia({audio: true, video: false})
                              .then(function(s) { window._koov_mic_stream = s; })
                              .catch(function(e) { console.log('koov mic maint:', e); });
                            """
                        )
                    except Exception:
                        pass
                    _n_streams = 0
                    try:
                        _call_sink = (os.environ.get("KOOV_FT_PULSE_SINK") or "call_sink").strip()
                        _n_playback = _rewire_chrome_sink_inputs(meeting_id, _call_sink)
                        _n_streams = _rewire_chrome_source_outputs(meeting_id, _koov_mic_src)
                        if _n_streams > 0:
                            pulse_rewire_ok = True
                        _ft_step(
                            meeting_id,
                            "pulse_maint",
                            "periodic in_call: playback_moves=%d mic_streams=%d sink=%r mic=%r"
                            % (_n_playback, _n_streams, _call_sink, _koov_mic_src),
                        )
                    except Exception as _pex:
                        _ft_step(
                            meeting_id,
                            "pulse_maint",
                            "periodic rewire error: %s" % _pex,
                            level=logging.DEBUG,
                        )
                    # Keep listen + TTS routes stable while fixing Chrome mic UI / rewire.
                    _reassert_agent_listen_route(meeting_id, reason="in_call maintenance")
                    _reassert_tts_output_route(meeting_id)
                    try:
                        _diagnose_ft_audio_routing(meeting_id)
                    except Exception:
                        pass
                    try:
                        _lab = _mic_selection_label()
                        if (not _lab) or ("no device selected" in _lab.lower()):
                            facetime_mic_ok = False
                            _ft_step(
                                meeting_id,
                                "pulse_maint",
                                "mic label regressed to %r; forcing FaceTime mic re-select"
                                % ((_lab or "")[:120]),
                                level=logging.WARNING,
                            )
                            facetime_mic_ok = (
                                _ensure_facetime_mic_selected() or facetime_mic_ok
                            )
                    except Exception:
                        pass

            # Camera desired state is a latest-wins atomic file. Camera failures
            # never leave this block and therefore cannot disturb audio routing.
            camera_control_changed = False
            if camera_output is not None:
                try:
                    _control = read_camera_control(notes_dir, meeting_id)
                    if _control is not None and _control.revision > camera_control_revision:
                        camera_control_revision = _control.revision
                        camera_desired_enabled = bool(_control.enabled)
                        camera_desired_mode = _control.mode
                        camera_control_changed = True
                        _ft_step(
                            meeting_id,
                            "camera_scene_changed",
                            "revision=%d enabled=%s mode=%s"
                            % (camera_control_revision, camera_desired_enabled, camera_desired_mode),
                        )
                    _scene_text = _control.text if _control is not None else ""
                    if _control is not None and _control.expires_at is not None and time.time() >= _control.expires_at:
                        _scene_text = ""
                        if camera_desired_mode in ("status_card", "message_card"):
                            camera_desired_mode = "activity"
                            camera_control_changed = True
                except ValueError as _control_exc:
                    camera_error_code = "camera_control_invalid"
                    camera_error = str(_control_exc)[:500]
                    _scene_text = ""
                except Exception as _control_exc:
                    camera_error_code = "camera_control_failed"
                    camera_error = str(_control_exc)[:500]
                    _scene_text = ""

                # One-time disclosure when visual questions are on and Koov is visible.
                if (
                    vision_enabled
                    and not camera_disclosure_shown
                    and facetime_state == "in_call"
                    and camera_enabled_actual
                ):
                    camera_disclosure_shown = True
                    camera_transient_text = "Visual questions are on. Ask \u201cKoov, look at\u2026\u201d"
                    camera_transient_until = time.monotonic() + 8.0
                    _ft_step(meeting_id, "camera_scene_changed", "vision disclosure card shown")

                camera_actual_mode = camera_desired_mode
                if camera_desired_mode == "activity":
                    _runtime: dict[str, object] = {}
                    try:
                        if hasattr(agent, "get_runtime_activity"):
                            _runtime = agent.get_runtime_activity() or {}
                    except Exception:
                        _runtime = {}
                    if bool(_runtime.get("tts_playing")):
                        camera_actual_mode = "speaking"
                    elif bool(_runtime.get("wake_cycle_active")):
                        camera_actual_mode = "thinking"
                    else:
                        camera_actual_mode = "listening" if facetime_state == "in_call" else "avatar"
                if camera_transient_text and time.monotonic() < camera_transient_until and not _scene_text:
                    _scene_mode = "status_card"
                    _scene_text = camera_transient_text
                else:
                    camera_transient_text = ""
                    _scene_mode = camera_actual_mode
                try:
                    camera_output.set_scene(
                        VideoScene(
                            enabled=camera_desired_enabled,
                            mode=CameraMode(_scene_mode),
                            display_name=display_name,
                            text=_scene_text,
                        )
                    )
                except Exception as _scene_exc:
                    camera_error_code = "camera_render_failed"
                    camera_error = str(_scene_exc)[:500]

                _camera_health_now = camera_output.health()
                if not _camera_health_now.producer_ok and _camera_health_now.error_code:
                    camera_error_code = _camera_health_now.error_code
                    camera_error = _camera_health_now.error
                if (
                    facetime_state == "in_call"
                    and camera_controller is not None
                    and _camera_health_now.producer_ok
                    and (camera_control_changed or (_poll_now - camera_last_repair_at) >= 10.0)
                ):
                    camera_last_repair_at = _poll_now
                    _browser_camera = camera_controller.set_enabled(camera_desired_enabled)
                    camera_selected = _browser_camera.selected
                    camera_enabled_actual = _browser_camera.enabled
                    if _browser_camera.error_code:
                        camera_error_code = _browser_camera.error_code
                        camera_error = _browser_camera.error
                        if not camera_dom_dumped and camera_error_code in (
                            "camera_selector_failed", "camera_toggle_failed", "camera_not_enumerated",
                        ):
                            # Once per meeting: sanitized control markup to tune selectors.
                            camera_dom_dumped = True
                            try:
                                with open(
                                    os.path.join(notes_dir, f"facetime-{meeting_id}-camera-dom.html"),
                                    "w",
                                    encoding="utf-8",
                                ) as _dom_f:
                                    _dom_f.write(camera_controller.diagnostic_markup())
                            except Exception:
                                pass
                        _ft_step(
                            meeting_id,
                            "camera_repair",
                            "selected=%s enabled=%s error_code=%s"
                            % (camera_selected, camera_enabled_actual, camera_error_code),
                            level=logging.WARNING,
                        )
                    else:
                        camera_error_code = ""
                        camera_error = ""
                        _ft_step(
                            meeting_id,
                            "camera_enabled" if camera_enabled_actual else "camera_selected",
                            "selected=%s enabled=%s mode=%s"
                            % (camera_selected, camera_enabled_actual, camera_actual_mode),
                        )

            # Claim at most one on-demand visual request. Selenium capture stays
            # on the main browser thread; provider I/O runs in a bounded daemon.
            if vision_enabled and (_poll_now - vision_last_prune_at) >= 300.0:
                vision_last_prune_at = _poll_now
                try:
                    prune_done(notes_dir, meeting_id)
                except Exception:
                    pass
            if vision_enabled and facetime_state == "in_call" and not vision_busy:
                _claimed = claim_next_request(notes_dir, meeting_id)
                if _claimed is not None:
                    _vision_path, _vision_request = _claimed
                    _vision_id = str(_vision_request.get("request_id") or _vision_path.stem)
                    _vision_question = str(_vision_request.get("question") or "").strip()
                    _vision_expiry = float(_vision_request.get("expires_at") or 0.0)
                    vision_last_request_id = _vision_id
                    if (
                        str(_vision_request.get("meeting_id") or "") != meeting_id
                        or not _vision_question
                        or (_vision_expiry and time.time() > _vision_expiry)
                    ):
                        vision_last_error = "Visual request was invalid or expired"
                        complete_request(
                            _vision_path,
                            VisionResult(
                                _vision_id,
                                "failed",
                                error_code="vision_request_invalid",
                                error=vision_last_error,
                                completed_at=time.time(),
                            ),
                        )
                    else:
                        try:
                            _vision_png = driver.get_screenshot_as_png()
                            try:
                                _vision_rects = driver.execute_script(VIDEO_RECTS_JS) or []
                                _vision_png = crop_to_region(_vision_png, _vision_rects)
                            except Exception:
                                pass  # full viewport is an acceptable fallback
                            _vision_ok, _vision_reason = usable_screenshot(_vision_png)
                        except Exception as _capture_exc:
                            _vision_ok, _vision_reason = False, str(_capture_exc)
                            _vision_png = b""
                        if not _vision_ok:
                            vision_last_error = _vision_reason
                            complete_request(
                                _vision_path,
                                VisionResult(
                                    _vision_id,
                                    "failed",
                                    error_code="vision_blank_frame",
                                    error=_vision_reason,
                                    completed_at=time.time(),
                                ),
                            )
                        else:
                            vision_busy = True
                            _vision_jpeg = prepare_screenshot(_vision_png)
                            del _vision_png

                            def _run_visual_query(
                                path=_vision_path,
                                request_id=_vision_id,
                                question=_vision_question,
                                image_bytes=_vision_jpeg,
                            ) -> None:
                                nonlocal vision_busy, vision_last_error
                                _started = time.monotonic()
                                try:
                                    answer = analyze_image(
                                        image_bytes,
                                        question,
                                        api_key=os.environ.get("KOOV_OPENAI_API_KEY", ""),
                                        model=vision_model,
                                        detail=vision_detail,
                                    )
                                    complete_request(
                                        path,
                                        VisionResult(
                                            request_id,
                                            "completed",
                                            answer=answer,
                                            completed_at=time.time(),
                                            latency_ms=int((time.monotonic() - _started) * 1000),
                                        ),
                                    )
                                    vision_last_error = ""
                                    _ft_step(meeting_id, "vision_completed", "request_id=%s" % request_id)
                                except Exception as _vision_exc:
                                    vision_last_error = str(_vision_exc)[:500]
                                    complete_request(
                                        path,
                                        VisionResult(
                                            request_id,
                                            "failed",
                                            error_code="vision_model_failed",
                                            error=vision_last_error,
                                            completed_at=time.time(),
                                            latency_ms=int((time.monotonic() - _started) * 1000),
                                        ),
                                    )
                                    _ft_step(
                                        meeting_id,
                                        "vision_failed",
                                        "request_id=%s error=%s" % (request_id, vision_last_error),
                                        level=logging.WARNING,
                                    )
                                finally:
                                    vision_busy = False

                            threading.Thread(
                                target=_run_visual_query,
                                daemon=True,
                                name=f"vision-{_vision_id[:8]}",
                            ).start()
                            _ft_step(meeting_id, "vision_frame_captured", "request_id=%s" % _vision_id)

            transcript_chunks = 0
            transcript_path = ""
            last_audio_at = 0.0
            try:
                transcript_chunks = int(getattr(agent, "_transcript_chunk_id", 0) or 0)
            except Exception:
                transcript_chunks = 0
            if (
                transcript_chunks > 0
                and facetime_state != "ended"
                and not _page_is_name_lobby(page_text)
            ):
                facetime_state = "in_call"
            try:
                last_audio_at = float(getattr(agent, "_last_audio_frame_at", 0) or 0)
            except Exception:
                last_audio_at = 0.0
            _now_audio = time.time()
            receiving_audio = last_audio_at > 0 and (_now_audio - last_audio_at) <= 15.0
            if last_audio_at > 0 and not receiving_audio:
                audio_status = "silent"
            elif receiving_audio:
                audio_status = "receiving"
            else:
                audio_status = "no_audio_yet"
            audio_input_no_device = bool(
                getattr(getattr(agent, "_audio_in", None), "_no_device", False)
            )
            try:
                p = getattr(getattr(agent, "_note_store", None), "path", "") or ""
                transcript_path = os.path.abspath(p) if p else ""
            except Exception:
                transcript_path = ""
            tz_map: dict[str, str] = {}
            tz_conflicts: list[dict] = []
            try:
                tz_ctx = agent.get_timezone_context()
                tz_map = tz_ctx.get("participant_timezones") or {}
                tz_conflicts = tz_ctx.get("timezone_conflicts") or []
            except Exception:
                pass
            _thoughts: list = []
            try:
                if agent is not None and hasattr(agent, "get_thoughts_for_status"):
                    _thoughts = agent.get_thoughts_for_status()
            except Exception:
                pass
            _live_recap: dict = {}
            try:
                if agent is not None and hasattr(agent, "get_live_recap_for_dashboard"):
                    _live_recap = agent.get_live_recap_for_dashboard() or {}
            except Exception:
                pass
            _camera_health_for_status = camera_output.health() if camera_output is not None else None
            write(
                "active" if facetime_state == "in_call" else "joining",
                facetime_state=facetime_state,
                transcript_chunks=transcript_chunks,
                last_activity=time.time(),
                transcript_path=transcript_path,
                team_id=team_id or None,
                shared_context=bool(team_id and shared_context_enabled),
                participant_names=_participant_names,
                participant_timezones=tz_map,
                timezone_conflicts=tz_conflicts,
                audio_graph_ready=True,
                pulse_rewire_ok=pulse_rewire_ok,
                facetime_mic_ok=facetime_mic_ok,
                facetime_join_clicked=join_clicked,
                last_audio_received_at=last_audio_at if last_audio_at > 0 else None,
                receiving_audio=receiving_audio,
                audio_status=audio_status,
                audio_input_no_device=audio_input_no_device,
                pulse_defaults={
                    "default_sink": os.environ.get("KOOV_FT_DEFAULT_SINK", "koov_sink"),
                    "default_source": os.environ.get("KOOV_FT_DEFAULT_SOURCE", "call_sink.monitor"),
                    "pulse_sink": os.environ.get("PULSE_SINK", ""),
                    "pulse_source": os.environ.get("PULSE_SOURCE", ""),
                },
                koov_running=agent_thread.is_alive(),
                facetime_page_title=dbg_title,
                facetime_page_url=dbg_url,
                thoughts=_thoughts,
                live_recap=_live_recap,
                facetime_camera_feature_enabled=camera_feature_enabled,
                facetime_camera_available=bool(
                    _camera_health_for_status and _camera_health_for_status.available
                ),
                facetime_camera_device_assigned=bool(video_device),
                facetime_camera_producer_ok=bool(
                    _camera_health_for_status and _camera_health_for_status.producer_ok
                ),
                facetime_camera_selected=camera_selected,
                facetime_camera_enabled=camera_enabled_actual,
                facetime_camera_desired_enabled=camera_desired_enabled,
                facetime_camera_desired_mode=camera_desired_mode,
                facetime_camera_actual_mode=camera_actual_mode,
                facetime_camera_control_revision=camera_control_revision,
                facetime_camera_last_frame_at=(
                    _camera_health_for_status.last_frame_at if _camera_health_for_status else None
                ),
                facetime_camera_error_code=(
                    camera_error_code
                    or (_camera_health_for_status.error_code if _camera_health_for_status else "")
                ),
                facetime_camera_error=(
                    camera_error
                    or (_camera_health_for_status.error if _camera_health_for_status else "")
                ),
                facetime_vision_enabled=vision_enabled,
                facetime_vision_busy=vision_busy,
                facetime_vision_last_request_id=vision_last_request_id,
                facetime_vision_last_error=vision_last_error,
            )
            # While in_call, iterate faster so active-speaker tracking follows
            # turn-taking (the heavy page_source scrape stays throttled to ~12s via
            # its own cache, so this doesn't add DOM-scrape load). Idle/joining
            # states don't need the finer cadence.
            if facetime_state == "in_call":
                try:
                    _loop_wait = float(
                        (os.environ.get("KOOV_FT_IN_CALL_LOOP_S") or "1.5").strip() or "1.5"
                    )
                except ValueError:
                    _loop_wait = 1.5
                _loop_wait = max(0.5, min(_loop_wait, 5.0))
            else:
                _loop_wait = 5.0
            stop_event.wait(_loop_wait)
    except Exception as exc:
        last_err = str(exc)
        _ft_step(meeting_id, "fatal", "uncaught exception: %s" % exc, level=logging.ERROR)
        log.error("FaceTime worker error for meeting_id=%s: %s", meeting_id, exc, exc_info=True)
        try:
            if driver is not None:
                shot = os.path.join(notes_dir, f"facetime-{meeting_id}-error.png")
                htmlp = os.path.join(notes_dir, f"facetime-{meeting_id}-error.html")
                try:
                    driver.save_screenshot(shot)
                except Exception:
                    pass
                try:
                    with open(htmlp, "w", encoding="utf-8") as f:
                        f.write(driver.page_source or "")
                except Exception:
                    pass
        except Exception:
            pass
        write("error", facetime_state="error", error=last_err)
    finally:
        # IMPORTANT ordering: when the user ends the meeting from the dashboard,
        # /api/stop gives the container a bounded grace period before SIGKILL. The
        # final summary is the durable artifact the dashboard shows + cross-meeting
        # context, so produce it EARLY (right after the agent settles) instead of
        # after a 30s thread-join + slow Chrome teardown, which used to blow past the
        # grace window and get SIGKILLed before the summary was ever written.
        if agent is not None:
            try:
                agent.stop()
            except Exception:
                pass
        stop_event.set()
        # Brief join so run_forever can flush its current turn, then persist state.
        if agent_thread is not None and agent_thread.is_alive():
            agent_thread.join(timeout=6.0)
        if agent is not None:
            try:
                agent._note_store.sync()
            except Exception:
                pass
        # Persist final_summary.json BEFORE the slow teardown below.
        if agent is not None:
            try:
                _write_final_summary_on_shutdown(agent, config, meeting_id)
            except Exception as _se:  # noqa: BLE001
                log.warning("FaceTime final summary write failed (non-fatal): %s", _se)
        # Leave the FaceTime call explicitly so the host's device drops Koov; driver.quit()
        # alone often leaves a ghost participant until Apple's session times out.
        try:
            if driver is not None:
                if _try_leave_facetime_call(driver):
                    _ft_step(meeting_id, "leave_call", "clicked Leave/End in FaceTime UI before shutdown")
                else:
                    _ft_step(
                        meeting_id,
                        "leave_call",
                        "no Leave/End control found; closing browser anyway",
                        level=logging.WARNING,
                    )
        except Exception as _lv_exc:
            _ft_step(
                meeting_id,
                "leave_call",
                "leave click failed (non-fatal): %s" % _lv_exc,
                level=logging.WARNING,
            )
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        if vision_enabled:
            try:
                fail_inflight(notes_dir, meeting_id)
            except Exception:
                pass
        if camera_output is not None:
            try:
                camera_output.stop(timeout_s=5.0)
            except Exception as _camera_stop_exc:
                _ft_step(
                    meeting_id,
                    "camera_degraded_audio_only",
                    "camera shutdown error: %s" % _camera_stop_exc,
                    level=logging.WARNING,
                )
        if agent_thread is not None and agent_thread.is_alive():
            agent_thread.join(timeout=10.0)
        _final_thoughts: list = []
        try:
            if agent is not None and hasattr(agent, "get_thoughts_for_status"):
                _final_thoughts = agent.get_thoughts_for_status()
        except Exception:
            pass
        write(
            "stopped",
            facetime_state="stopped",
            error=last_err or "",
            team_id=team_id or None,
            shared_context=bool(team_id and shared_context_enabled),
            thoughts=_final_thoughts,
            facetime_camera_feature_enabled=camera_feature_enabled,
            facetime_camera_available=False,
            facetime_camera_device_assigned=bool(video_device),
            facetime_camera_producer_ok=False,
            facetime_camera_selected=camera_selected,
            facetime_camera_enabled=False,
            facetime_camera_desired_mode=camera_desired_mode,
            facetime_camera_actual_mode=camera_actual_mode,
            facetime_camera_control_revision=camera_control_revision,
            facetime_camera_error_code=camera_error_code,
            facetime_camera_error=camera_error,
            facetime_vision_enabled=vision_enabled,
            facetime_vision_busy=vision_busy,
            facetime_vision_last_request_id=vision_last_request_id,
            facetime_vision_last_error=vision_last_error,
        )
        _ft_step(
            meeting_id,
            "shutdown",
            "worker finished last_err=%r" % (last_err or ""),
        )
        log.info("FaceTime worker stopped: meeting_id=%s", meeting_id)


if __name__ == "__main__":
    main()
