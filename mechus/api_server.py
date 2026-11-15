"""
HTTP API server for Mechus meeting automation.

This server allows the dashboard to trigger Mechus to join meetings automatically.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_from_directory
from flask_cors import CORS

from mechus.agent import MechusAgent
from mechus.config import MechusConfig
from mechus.personas import PersonaRegistry
from mechus.envfile import load_env_file
from mechus.facetime_worker import facetime_dashboard_labels
from mechus.facetime_camera_lease import CameraLease, CameraLeaseManager
from mechus.facetime_control import (
    CAMERA_MODES,
    enqueue_vision_request,
    read_camera_control,
    vision_request_status,
    write_camera_control,
)
from mechus.json_utils import safe_parse_json
from mechus.notes_paths import apply_notes_paths_to_env
from mechus.speaker_map import (
    load_speaker_map_for_meeting,
    speaker_map_file_path,
)

log = logging.getLogger(__name__)

# Align meeting-store paths with worker containers (mechus_notes/mechus_meetings).
apply_notes_paths_to_env()
_deadline_reminder_started = False


def _docker_bin() -> str:
    return shutil.which("docker") or "docker"


def _append_docker_env(docker_cmd: list[str], env_file_abs: str, overrides: dict[str, str]) -> None:
    """Append env to ``docker run``: env-file first, then ``-e`` overrides (overrides win)."""
    docker_cmd.extend(
        _facetime_worker_docker_env_flags(env_file_abs, overrides, os.environ)
    )


def _facetime_worker_docker_env_flags(
    env_file_abs: str,
    overrides: dict[str, str],
    environ: dict[str, str],
) -> list[str]:
    """Build ``docker run`` env flags for a FaceTime worker.

    When the API runs under docker-compose ``env_file:``, vars are injected into the
    process environment but ``.env`` is often **not** mounted on disk — so we must
    always pass per-meeting ``overrides`` (meeting id, link, display name) via ``-e``.
    """
    flags: list[str] = []
    if env_file_abs and os.path.exists(env_file_abs):
        flags.extend(["--env-file", env_file_abs])
    else:
        _skip = set(overrides)
        for _k, _v in environ.items():
            if _k in _skip:
                continue
            if _k.startswith("MECHUS_") or _k in (
                "ZOOM_APP_CLIENT_ID",
                "ZOOM_APP_CLIENT_SECRET",
            ):
                flags.extend(["-e", f"{_k}={_v}"])
    for _k, _v in overrides.items():
        flags.extend(["-e", f"{_k}={_v}"])
    return flags


def join_requires_vosk_wake_model(join_backend: str) -> bool:
    """True when this join path uses microphone Vosk wake (not transcript-only Zoom/FaceTime)."""
    return (join_backend or "").strip().lower() in ("browser", "external")


# meeting_id -> per-call Chert options from the /api/join-meeting body.
_CHERT_CALL_OPTIONS: dict[str, dict[str, Any]] = {}


def _facetime_backend_for_link() -> str:
    """Which backend handles FaceTime links: Chert API (``facetime_chert``) or Chromium (``facetime_web``).

    ``MECHUS_FACETIME_BACKEND=chert|web`` forces one. Otherwise Chert is used whenever its
    credentials are configured, with the browser worker as the fallback.
    """
    forced = (os.getenv("MECHUS_FACETIME_BACKEND") or "").strip().lower()
    if forced in ("chert", "facetime_chert"):
        return "facetime_chert"
    if forced in ("web", "facetime_web", "browser"):
        return "facetime_web"
    try:
        from mechus.chert_client import chert_configured

        if chert_configured():
            return "facetime_chert"
    except Exception:
        pass
    return "facetime_web"


def infer_join_backend_from_meeting_link(meeting_link: str) -> str | None:
    """Infer join backend from URL when clients omit ``join_backend``.

    This prevents deployments that default ``MECHUS_JOIN_BACKEND=zoom_sdk`` from rejecting
    non-Zoom links (e.g. FaceTime) with HTTP 400.
    """
    link = (meeting_link or "").strip()
    if not link:
        return None
    low = link.lower()
    if low.startswith("facetime://"):
        return _facetime_backend_for_link()
    try:
        host = (urlparse(link).hostname or "").lower()
    except Exception:
        host = ""
    if host == "facetime.apple.com" or "facetime.apple.com" in low:
        return _facetime_backend_for_link()
    try:
        from mechus.chert_client import looks_like_chert_destination

        # A bare phone number / Apple ID email can only be reached by calling it via Chert.
        if looks_like_chert_destination(link):
            return "facetime_chert"
    except Exception:
        pass
    try:
        from mechus.zoom_sdk_join import is_zoom_link

        if is_zoom_link(link):
            return "zoom_sdk"
    except Exception:
        pass
    return None


app = Flask(__name__)

# Tiger Data analytics routes (/api/tiger/*, /api/meetings/<id>/analytics, /api/analytics/overview)
from mechus.tiger_api import tiger_bp  # noqa: E402

app.register_blueprint(tiger_bp)

# CORS: production-safe. Set MECHUS_CORS_ORIGINS to comma-separated origins (e.g. https://app.example.com).
_cors_origins = os.getenv("MECHUS_CORS_ORIGINS", "").strip()
if _cors_origins:
    origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]
    CORS(app, origins=origins, supports_credentials=True)
else:
    CORS(app)  # Allow all origins when not set (dev/local)

# Mechus Properties (real-estate inspections + Chert iMessage) lives in its own blueprint.
from mechus.estates.routes import register as _register_estates  # noqa: E402

_register_estates(app)

# Optional API key for production. Set MECHUS_API_KEY; clients send X-API-Key or Authorization: Bearer <key>.
_API_KEY = os.getenv("MECHUS_API_KEY", "").strip() or None

# Cap live transcript lines in /api/status so the endpoint returns quickly (avoids 10s+ timeouts when file is huge).
_MAX_LIVE_TRANSCRIPTS_IN_STATUS = 200
_TRANSCRIPT_TAIL_BYTES = 512 * 1024  # When file larger than this, only read last 512KB for status

_FACETIME_CAMERA_STATUS_KEYS = (
    "facetime_camera_feature_enabled",
    "facetime_camera_available",
    "facetime_camera_device_assigned",
    "facetime_camera_producer_ok",
    "facetime_camera_selected",
    "facetime_camera_enabled",
    "facetime_camera_desired_enabled",
    "facetime_camera_desired_mode",
    "facetime_camera_actual_mode",
    "facetime_camera_control_revision",
    "facetime_camera_last_frame_at",
    "facetime_camera_error_code",
    "facetime_camera_error",
    "facetime_vision_enabled",
    "facetime_vision_busy",
    "facetime_vision_last_request_id",
    "facetime_vision_last_error",
)


def _notes_dir() -> str:
    from mechus.notes_paths import resolve_notes_dir

    return resolve_notes_dir()


def _worker_status_path(meeting_id: str) -> str:
    return os.path.join(_notes_dir(), f"worker-status-{meeting_id}.json")


def _read_worker_status_json(meeting_id: str) -> dict[str, Any]:
    p = _worker_status_path(meeting_id)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json_truthy(val: object) -> bool:
    """Parse ``force`` (and similar) flags from JSON query/body values."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on", "force")


def _facetime_camera_feature_enabled() -> bool:
    return _json_truthy(os.getenv("MECHUS_FT_CAMERA_FEATURE", "0"))


def _facetime_vision_feature_enabled() -> bool:
    return _json_truthy(os.getenv("MECHUS_FT_VISION_ENABLED", "0"))


def _camera_lease_manager() -> CameraLeaseManager:
    return CameraLeaseManager(_notes_dir())


def _reconcile_facetime_camera_leases() -> None:
    if not _facetime_camera_feature_enabled():
        return
    try:
        result = subprocess.run(
            [
                _docker_bin(),
                "ps",
                "--filter", "label=mechus.facetime=1",
                "--format",
                '{{.Label "mechus.meeting_id"}}\t{{.Label "mechus.video_device"}}\t{{.Names}}',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return
        claims: list[CameraLease] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            mid, device, container = (part.strip() for part in parts)
            if not mid or not device or not container:
                continue
            claims.append(CameraLease(device, mid, container, time.time()))
        _camera_lease_manager().reconcile(claims)
    except Exception:
        log.warning("Could not reconcile FaceTime camera leases", exc_info=True)


def _acquire_facetime_camera_lease(meeting_id: str, container_name: str) -> CameraLease | None:
    try:
        _reconcile_facetime_camera_leases()
        return _camera_lease_manager().acquire(meeting_id, container_name)
    except ValueError as exc:
        # Misconfigured MECHUS_FT_VIDEO_DEVICES must not break joins.
        log.warning("FaceTime camera pool misconfigured (camera_pool_misconfigured): %s", exc)
        return None
    except Exception:
        log.warning("FaceTime camera lease acquisition failed", exc_info=True)
        return None


# Worker settings forwarded from the API environment. The API container gets
# these from docker-compose; the env file passed to workers may not have them.
_FACETIME_CAMERA_FORWARD_ENV = (
    "MECHUS_FT_VIDEO_WIDTH",
    "MECHUS_FT_VIDEO_HEIGHT",
    "MECHUS_FT_VIDEO_FPS",
    "MECHUS_OPENAI_VISION_MODEL",
    "MECHUS_FT_VISION_DETAIL",
    "MECHUS_FT_VISION_MAX_REQUESTS_PER_MINUTE",
)


def _facetime_camera_launch_args(
    worker_key: str,
    container_name: str,
    *,
    provision: bool,
    required: bool,
) -> tuple[dict[str, str], list[str], CameraLease | None]:
    """Lease a camera (when provisioned) and build worker env + docker args.

    Shared by the single-bot and multi-bot launch paths so device allocation,
    labels, and env forwarding cannot drift between them. Raises RuntimeError
    only when the camera is required and no device is available.
    """
    env: dict[str, str] = {"MECHUS_FT_CAMERA_FEATURE": "1" if provision else "0"}
    for key in _FACETIME_CAMERA_FORWARD_ENV:
        value = (os.getenv(key) or "").strip()
        if value:
            env[key] = value
    args: list[str] = [
        "--label", "mechus.facetime=1",
        "--label", f"mechus.meeting_id={worker_key}",
    ]
    lease: CameraLease | None = None
    if provision:
        lease = _acquire_facetime_camera_lease(worker_key, container_name)
        if lease is None:
            if required:
                raise RuntimeError("No FaceTime virtual camera is available")
            env["MECHUS_FT_CAMERA_ERROR_CODE"] = "camera_pool_exhausted"
            log.warning(
                "FaceTime camera pool exhausted for %s; worker will join audio-only", worker_key
            )
        else:
            env["MECHUS_FT_VIDEO_DEVICE"] = lease.device
            args.extend([
                "--label", f"mechus.video_device={lease.device}",
                "--device", f"{lease.device}:{lease.device}:rwm",
            ])
    return env, args, lease


def _release_facetime_camera_lease(meeting_id: str) -> None:
    try:
        _camera_lease_manager().release(meeting_id)
    except Exception:
        log.warning("Could not release FaceTime camera lease for %s", meeting_id, exc_info=True)


def _facetime_camera_status_fields(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in _FACETIME_CAMERA_STATUS_KEYS if key in value}


def _compute_mechus_presence(
    *,
    status: str,
    join_backend: str,
    bot_in_call: bool,
    bot_waiting: bool,
    bot_connecting: bool,
    worker_error: str,
) -> str:
    """Single dashboard-friendly join state (do not infer from Supabase ``live`` alone)."""
    st = (status or "").strip().lower()
    if st == "error":
        return "failed"
    if st in ("stopped", "stopping"):
        return "left"
    if bot_waiting:
        return "waiting_room"
    if bot_in_call and st == "active":
        return "in_call"
    if bot_connecting or st in ("starting", "joining"):
        return "connecting"
    if worker_error and st != "active":
        return "failed"
    return "not_joined"


def _build_status_payload_from_worker_disk(meeting_id: str) -> dict[str, Any] | None:
    """Rebuild GET /api/status from worker-status-{id}.json when in-memory session is gone.

    Dashboard polls after API restart, worker exit, or race with session cleanup; without this
    the API returns ``idle`` and the UI thinks Mechus never joined.
    """
    ws = _read_worker_status_json(meeting_id)
    if not ws:
        return None
    st = str(ws.get("status") or "").strip().lower()
    if st not in ("starting", "joining", "active", "stopped", "error", "stopping"):
        return None

    worker_error = str(ws.get("error") or "").strip()[:800]
    join_backend = str(ws.get("join_backend") or "").strip()
    zoom_join_state = str(ws.get("join_state") or "").strip()
    started_at = ws.get("started_at") or ws.get("ts")
    try:
        started_at_f = float(started_at) if started_at is not None else time.time()
    except (TypeError, ValueError):
        started_at_f = time.time()

    tp_raw = str(ws.get("transcript_path") or "").strip()
    tp = _coerce_worker_transcript_path_to_api(tp_raw) if tp_raw else None
    if not tp:
        guessed = os.path.join(
            os.path.abspath(_notes_dir()),
            f"{_safe_notes_filename_id(meeting_id)}.jsonl",
        )
        tp = guessed if os.path.exists(guessed) else None
    speaker_map = _load_speaker_map(meeting_id)
    live_transcripts = _read_live_transcripts_for_status(tp, speaker_map) if tp else []

    bot_waiting = (
        join_backend == "zoom_sdk"
        and "WAITING" in zoom_join_state.upper()
        and st == "joining"
    )
    bot_connecting = (
        join_backend == "zoom_sdk"
        and st in ("starting", "joining")
        and not bot_waiting
    )
    bot_in_call = st == "active" and join_backend in ("zoom_sdk", "facetime_web", "facetime_chert")

    payload: dict[str, Any] = {
        "active": st in ("starting", "joining", "active"),
        "status": st,
        "meeting_id": meeting_id,
        "user_id": str(ws.get("user_id") or ""),
        "meeting_link": str(ws.get("meeting_link") or ""),
        "started_at": started_at_f,
        "uptime_seconds": max(0.0, time.time() - started_at_f),
        "transcript_chunks": int(ws.get("transcript_chunks") or 0),
        "last_activity": ws.get("last_activity") or ws.get("ts"),
        "live_transcripts": live_transcripts,
        "live_transcript_count": len(live_transcripts),
        "speaker_map": speaker_map,
        "participants": ws.get("participants") if isinstance(ws.get("participants"), dict) else {},
        "join_backend": join_backend,
        "worker_error": worker_error,
        "facetime_worker_error": worker_error,
        "zoom_join_state": zoom_join_state,
        "zoom_join_detail": str(ws.get("join_detail") or "").strip(),
        "bot_waiting": bot_waiting,
        "bot_connecting": bot_connecting,
        "bot_in_call": bot_in_call,
        "session_ended": st in ("stopped", "error", "stopping"),
    }
    payload["mechus_presence"] = _compute_mechus_presence(
        status=st,
        join_backend=join_backend,
        bot_in_call=bot_in_call,
        bot_waiting=bot_waiting,
        bot_connecting=bot_connecting,
        worker_error=worker_error,
    )
    payload.update(_facetime_camera_status_fields(ws))
    if st == "error" and worker_error:
        payload["failure_reason"] = worker_error
        payload["no_audio_hint"] = worker_error
    return payload


def _coerce_worker_transcript_path_to_api(tp: str) -> str:
    """Map transcript paths recorded inside a FaceTime worker (e.g. ``/notes/...``) to this process's notes dir.

    Returns the resolved API-side path even when the JSONL file does not exist yet (worker just started).
    """
    tp = (tp or "").strip()
    if not tp:
        return ""
    notes_abs = os.path.abspath(_notes_dir())
    if tp.startswith("/notes/"):
        rel = tp[len("/notes/") :].lstrip("/")
        if rel:
            cand = os.path.normpath(os.path.join(notes_abs, rel))
            if cand.startswith(notes_abs):
                return cand
    p = tp if os.path.isabs(tp) else os.path.abspath(tp)
    if p.startswith(notes_abs):
        return p
    if os.path.isfile(p):
        return os.path.abspath(p)
    base = os.path.basename(tp.replace("\\", "/"))
    if base:
        return os.path.join(notes_abs, base)
    return ""


def _facetime_container_name(meeting_id: str) -> str:
    """Docker ``--name`` for a FaceTime worker (must match join-meeting spawn)."""
    cname = f"mechus-facetime-{meeting_id}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", cname)[:120]


def _facetime_container_running(container_name: str) -> bool | None:
    """Return True/False if the container exists; None if inspect failed."""
    cn = str(container_name or "").strip()
    if not cn:
        return None
    try:
        rc = subprocess.run(
            [_docker_bin(), "inspect", "-f", "{{.State.Running}}", cn],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if rc.returncode != 0:
        return None
    return (rc.stdout or "").strip().lower() == "true"


def _stop_facetime_container_for_meeting(
    meeting_id: str,
    *,
    stop_timeout_s: int = 35,
) -> bool:
    """SIGTERM a FaceTime worker by meeting_id when the in-memory session is gone or incomplete."""
    mid = (meeting_id or "").strip()
    if not mid:
        return False
    cname = _facetime_container_name(mid)
    running = _facetime_container_running(cname)
    if running is None:
        return False
    if running:
        _stop_docker_worker_container(cname, mid, stop_timeout_s=stop_timeout_s)
        return True
    try:
        subprocess.run(
            [_docker_bin(), "rm", "-f", cname],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        pass
    return True


def _stop_docker_worker_container(
    container_name: str,
    meeting_id: str = "",
    *,
    stop_timeout_s: int = 35,
    save_logs: bool = True,
) -> None:
    """SIGTERM a worker container so it can flush JSONL/status, then remove it."""
    cn = str(container_name or "").strip()
    if not cn:
        return
    mid = (meeting_id or "").strip()
    try:
        subprocess.run(
            [_docker_bin(), "stop", "-t", str(max(1, int(stop_timeout_s))), cn],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("docker stop failed for %s: %s", cn, exc)
    if save_logs and mid:
        try:
            ndir = os.path.abspath(_notes_dir())
            os.makedirs(ndir, exist_ok=True)
            log_path = os.path.join(ndir, f"facetime-{mid}-container.log")
            logs_out = subprocess.run(
                [_docker_bin(), "logs", "--tail=4000", cn],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if logs_out.returncode == 0 and (logs_out.stdout or "").strip():
                with open(log_path, "w", encoding="utf-8") as lf:
                    lf.write(logs_out.stdout)
        except Exception as le:
            log.warning("Could not save FaceTime worker logs for %s: %s", mid, le)
    try:
        subprocess.run(
            [_docker_bin(), "rm", "-f", cn],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("docker rm failed for %s: %s", cn, exc)
    if mid:
        _release_facetime_camera_lease(mid)


def _facetime_early_stop_blocked(session: object, meeting_id: str, force: bool) -> tuple[bool, str]:
    """Refuse ``/api/stop`` during FaceTime join so dashboards cannot kill the worker on the first poll.

    Applies even before ``worker_container`` is set (race: client calls stop while ``docker run`` is still
    starting) so the join thread is not left fighting a session marked stopped.
    """
    if force:
        return False, ""
    if (getattr(session, "join_backend", "") or "") != "facetime_web":
        return False, ""
    ws = _read_worker_status_json(meeting_id)
    _ft_state = str(ws.get("facetime_state") or "").strip()
    # Allow stop once bot is visible to the host (waiting room or in-call).
    if _ft_state in ("in_call", "waiting_for_admit"):
        return False, ""
    if str(ws.get("status") or "") == "error":
        return False, ""
    try:
        guard = float(os.getenv("MECHUS_FT_EARLY_STOP_GUARD_S", "90"))
    except ValueError:
        guard = 90.0
    guard = max(0.0, min(guard, 3600.0))
    elapsed = time.time() - float(getattr(session, "started_at", 0) or 0)
    if elapsed >= guard:
        return False, ""
    return True, (
        "FaceTime worker is still starting up (has not yet reached the FaceTime waiting room). "
        "Poll GET /api/status until facetime_state is waiting_for_admit or in_call, "
        'or POST /api/stop with {"force": true} to cancel immediately.'
    )


def _safe_notes_filename_id(meeting_id: str) -> str:
    s = (meeting_id or "").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s[:200] if s else "session"


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        log.debug("Failed to write %s", path, exc_info=True)


def _persist_meeting_transcript_ref(meeting_id: str, transcript_path: str, meeting_link: str = "") -> None:
    """Persist transcript JSONL path so GET /api/transcript works after in-process stop (no worker subprocess)."""
    p = _worker_status_path(meeting_id)
    tp = os.path.abspath(transcript_path) if transcript_path else ""
    _atomic_write_json(
        p,
        {
            "status": "stopped",
            "meeting_id": meeting_id,
            "ts": time.time(),
            "transcript_path": tp,
            "meeting_link": meeting_link,
        },
    )


def _transcript_jsonl_path_for_ended_meeting(meeting_id: str) -> str | None:
    """After POST /api/stop, session is gone; use worker-status or {meeting_id}.jsonl under MECHUS_NOTES_DIR."""
    ws = _read_worker_status_json(meeting_id)
    tp = str(ws.get("transcript_path") or "").strip()
    candidates: list[str] = []
    coerced = _coerce_worker_transcript_path_to_api(tp)
    if coerced:
        candidates.append(coerced)
    if tp:
        candidates.append(tp if os.path.isabs(tp) else os.path.abspath(tp))
    notes_abs = os.path.abspath(_notes_dir())
    candidates.append(os.path.join(notes_abs, f"{_safe_notes_filename_id(meeting_id)}.jsonl"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    # Worker may have recorded a path before any transcript lines were written.
    if coerced:
        return coerced
    return None


def _team_shared_context_enabled(data: dict[str, Any], team_id: str) -> bool:
    if not team_id:
        return False
    if "shared_context" not in data:
        return True
    return _json_truthy(data.get("shared_context"))


def _add_meeting_to_team_context(
    *,
    team_id: str,
    meeting_id: str,
    created_by: str = "",
    title: str = "",
    final_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not team_id or not meeting_id:
        return None
    from mechus.team_context import TeamContextStore

    return TeamContextStore(team_id).add_meeting(
        meeting_id,
        title=title,
        created_by=created_by,
        final_summary=final_summary,
    )


def _load_formatted_transcripts_from_jsonl(transcript_path: str, meeting_id: str) -> tuple[list[dict], list, dict[str, str], list[dict]]:
    """Parse transcript + wake_cycle + thought lines from a session JSONL file (same shape as active get_transcript)."""
    speaker_map = _load_speaker_map(meeting_id)
    transcripts: list[dict] = []
    wake_cycles: list = []
    thought_notes: list[dict] = []
    if not os.path.exists(transcript_path):
        return [], [], speaker_map, []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            note = safe_parse_json(line.strip())
            if note.get("_parse_failed"):
                continue
            if note.get("kind") == "transcript":
                transcripts.append(note)
            elif note.get("kind") == "wake_cycle":
                wake_cycles.append(note)
            elif note.get("kind") == "thought":
                thought_notes.append(note)
    formatted_transcripts: list[dict] = []
    for note in transcripts:
        meta = note.get("meta", {})
        segments = _apply_speaker_map_to_segments(meta.get("segments", []), speaker_map)
        tags = meta.get("tags", [])
        formatted_transcripts.append(
            {
                "timestamp": note.get("ts"),
                "speaker": _speaker_for_transcript_note(note, speaker_map),
                "text": _apply_speaker_map_to_text(note.get("text"), speaker_map),
                "tags": tags,
                "segments": segments,
                "start_time": meta.get("start_ts"),
                "end_time": meta.get("end_ts"),
            }
        )
    # Format persisted thought notes into canonical thoughts shape
    _formatted_thoughts: list[dict] = []
    for tn in thought_notes:
        _meta = tn.get("meta") or {}
        _ts = tn.get("ts")
        _kind = str(_meta.get("thought_kind") or "thinking")
        _text = str(tn.get("text") or "").strip()
        if not _text:
            continue
        _entry: dict = {
            "ts": int(_ts) if _ts is not None else 0,
            "kind": _kind,
            "text": _text,
        }
        _ps = str(_meta.get("planned_speech") or "").strip()
        if _ps:
            _entry["planned_speech"] = _ps
        _conns = _meta.get("connections")
        if isinstance(_conns, list) and _conns:
            _entry["connections"] = _conns
        _formatted_thoughts.append(_entry)
    # Attach to speaker_map return as a side-channel via a mutable wrapper so callers
    # that only destructure 3 values still work; callers wanting thoughts use index [3].
    return formatted_transcripts, wake_cycles, speaker_map, _formatted_thoughts


def _speaker_map_path(meeting_id: str) -> str:
    return speaker_map_file_path(_notes_dir(), meeting_id)


def _load_speaker_map(meeting_id: str) -> dict[str, str]:
    if not meeting_id:
        return {}
    return load_speaker_map_for_meeting(_notes_dir(), meeting_id)


def _save_speaker_map(meeting_id: str, mapping: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for k, v in mapping.items():
        key = str(k or "").strip()
        value = str(v or "").strip()
        if key and value:
            clean[key] = value
    path = _speaker_map_path(meeting_id)
    tmp = path + ".tmp"
    payload = {
        "meeting_id": meeting_id,
        "speakers": clean,
        "updated_at": time.time(),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return clean


def _map_speaker_label(label: object, speaker_id: object, mapping: dict[str, str]) -> str:
    speaker_id_str = str(speaker_id or "").strip()
    label_str = str(label or "").strip()
    if speaker_id_str and speaker_id_str in mapping:
        return mapping[speaker_id_str]
    if label_str and label_str in mapping:
        return mapping[label_str]
    return label_str or speaker_id_str or "Unknown"


def _parse_speaker_prefix_from_text(text: object) -> str | None:
    """If the first line looks like 'Display Name: utterance', return the display name."""
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
        return None
    first_line = raw.split("\n", 1)[0]
    if ":" not in first_line:
        return None
    prefix, rest = first_line.split(":", 1)
    name = prefix.strip()
    if not name or len(name) > 120:
        return None
    if name.lower() in ("http", "https"):
        return None
    # Noise-only STT labels, not a person name
    if name.startswith("[") and name.endswith("]"):
        return None
    if not rest.strip():
        return None
    return name


def _speaker_for_transcript_note(note: dict, speaker_map: dict[str, str]) -> str:
    """Display name for UI: segments meta first, else parse 'Name: text' from note text."""
    meta = note.get("meta") or {}
    segments = meta.get("segments") or []
    text = note.get("text", "")
    speaker: str | None = None
    if segments and isinstance(segments[0], dict):
        speaker = _map_speaker_label(
            segments[0].get("speaker", ""),
            segments[0].get("speaker_id"),
            speaker_map,
        )
    if not speaker or speaker in ("Speaker", "Unknown", ""):
        parsed = _parse_speaker_prefix_from_text(text)
        if parsed:
            speaker = _map_speaker_label(parsed, parsed, speaker_map)
    if not speaker or speaker in ("Unknown", ""):
        return "Speaker"
    return speaker


def _apply_speaker_map_to_text(text: object, mapping: dict[str, str]) -> str:
    raw = str(text or "")
    if not raw or not mapping:
        return raw
    out_lines: list[str] = []
    for line in raw.splitlines():
        if ":" not in line:
            out_lines.append(line)
            continue
        prefix, rest = line.split(":", 1)
        mapped = mapping.get(prefix.strip())
        if mapped:
            out_lines.append(f"{mapped}: {rest.lstrip()}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _apply_speaker_map_to_segments(segments: object, mapping: dict[str, str]) -> list[dict]:
    if not isinstance(segments, list):
        return []
    out: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_out = dict(seg)
        seg_out["speaker"] = _map_speaker_label(seg.get("speaker"), seg.get("speaker_id"), mapping)
        out.append(seg_out)
    return out


@app.before_request
def _require_api_key():
    """If MECHUS_API_KEY is set, require it on all routes except /health and /ready."""
    if not _API_KEY:
        return None
    if request.path in ("/health", "/ready", "/callback", "/", "/dashboard", "/meetings", "/welcome"):
        # The dashboard pages are public; they send the key on every API call.
        return None
    if request.path.startswith("/static/"):
        return None
    from mechus.estates.routes import PUBLIC_PATHS, PUBLIC_PREFIXES

    if request.path in PUBLIC_PATHS or request.path.startswith(PUBLIC_PREFIXES):
        # Public share links + the Chert webhook (verified by its own HMAC signature).
        return None
    # Public share previews (OG/Twitter crawlers do not send API keys)
    if request.path.startswith("/share/"):
        return None
    key = request.headers.get("X-API-Key") or (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if key != _API_KEY:
        return jsonify({"error": "Unauthorized", "message": "Missing or invalid API key"}), 401
    return None


# Global state - support multiple concurrent meetings
_active_sessions: dict[str, MechusSession] = {}  # keyed by meeting_id (or per-bot worker key for multi-invite)
_session_lock = threading.Lock()
# Multi-bot: client ``meeting_id`` is the logical group id; values are worker keys (``bot_meeting_id``).
_session_group_children: dict[str, list[str]] = {}
_SESSION_ACTIVE_STATUSES = frozenset({"starting", "joining", "active"})
_vision_rate_lock = threading.Lock()
_vision_request_times: dict[str, list[float]] = {}


def _cleanup_zoom_sessions() -> None:
    """Explicitly leave + destroy Zoom SDK C++ objects before Python GC runs.
    Called by both the atexit handler and the SIGTERM handler so that the Zoom SDK
    never reaches its Python-GC-triggered destructor (which causes SIGABRT because
    the C++ runtime tries to call std::terminate during stack-unwind).
    Also terminates any live worker subprocesses.
    """
    with _session_lock:
        for _mid, _session in list(_active_sessions.items()):
            # Container mode: stop worker container
            if getattr(_session, "worker_container", None):
                _c = _session.worker_container
                _session.worker_container = None
                try:
                    subprocess.run(
                        [_docker_bin(), "rm", "-f", str(_c)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except Exception:
                    pass
                _release_facetime_camera_lease(_mid)
                continue
            # Subprocess mode: terminate worker
            if _session.worker_proc is not None:
                _proc = _session.worker_proc
                _session.worker_proc = None
                try:
                    if _proc.poll() is None:
                        _proc.terminate()
                except Exception:
                    pass
                continue
            # In-process mode: explicit Zoom SDK cleanup
            zb = _session.zoom_bot
            if zb is None:
                continue
            _session.zoom_bot = None
            try:
                zb.leave()
            except Exception:
                pass
            try:
                zb.cleanup()
            except Exception:
                pass


atexit.register(_cleanup_zoom_sessions)

# Hook SIGTERM so gunicorn graceful-shutdown triggers cleanup before GC.
# We restore the previous handler (gunicorn's) so the worker still exits normally.
_prev_sigterm = signal.getsignal(signal.SIGTERM)


def _sigterm_handler(signum: int, frame) -> None:  # type: ignore[type-arg]
    _cleanup_zoom_sessions()
    if callable(_prev_sigterm):
        _prev_sigterm(signum, frame)
    elif _prev_sigterm == signal.SIG_DFL:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)


try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
except (OSError, ValueError):
    pass  # Not in main thread (e.g. inside a gunicorn worker on some versions)


@dataclass
class MechusSession:
    """Represents an active Mechus session. agent/thread may be None while join is in progress."""
    meeting_link: str
    user_id: str  # User ID from dashboard
    team_id: str = ""
    shared_context_enabled: bool = False
    share_to_team_context: bool = False
    agent: Optional[MechusAgent] = None
    thread: Optional[threading.Thread] = None
    started_at: float = 0.0
    status: str = "starting"  # "starting", "joining", "active", "stopped", "error"
    last_activity: float = 0.0  # Timestamp of last audio activity
    transcript_chunks: int = 0  # Number of transcript chunks processed
    browser_driver: object = None  # Selenium WebDriver instance (to close browser when stopping)
    zoom_bot: object = None  # Zoom SDK bot (when join_backend=zoom_sdk inline) for leave/cleanup
    worker_proc: object = None  # subprocess.Popen for zoom_sdk subprocess mode
    worker_container: str | None = None  # docker container name/id for containerized worker backends (e.g. facetime_web)
    join_backend: str = ""  # resolved backend for this session (browser, external, zoom_sdk, facetime_web)
    participants: dict[str, str] | None = None
    participant_names: list[str] | None = None
    active_speaker_names: list[str] | None = None
    participant_timezones: dict[str, str] | None = None
    participant_emails: dict[str, str] | None = None
    summary_email_recipients: list[str] | None = None
    collection_recipients: list[str] | None = None
    invite_collection_link: str = ""
    speaker_attribution_mode: str = ""
    # Multi-bot: ``session_group_id`` matches the client's logical meeting_id; this row's key is ``bot_meeting_id``.
    session_group_id: str | None = None
    bot_persona_id: str | None = None
    bot_display_name: str | None = None
    facetime_camera_requested: bool = False
    facetime_camera_required: bool = False
    facetime_camera_mode: str = "avatar"
    facetime_camera_device: str = ""
    facetime_vision_requested: bool = False


def _prune_session_group_child(group_id: str | None, child_key: str) -> None:
    if not group_id:
        return
    with _session_lock:
        lst = _session_group_children.get(group_id)
        if not lst:
            return
        try:
            lst.remove(child_key)
        except ValueError:
            pass
        if not lst:
            _session_group_children.pop(group_id, None)


def _join_slot_busy(slot_id: str) -> bool:
    """True if ``slot_id`` is an active single-session key or an active multi-bot group id."""
    with _session_lock:
        sess = _active_sessions.get(slot_id)
        if sess is not None and sess.status in _SESSION_ACTIVE_STATUSES:
            return True
        for _mid, s in _active_sessions.items():
            if getattr(s, "session_group_id", None) == slot_id and s.status in _SESSION_ACTIVE_STATUSES:
                return True
        for wk in _session_group_children.get(slot_id, []):
            ss = _active_sessions.get(wk)
            if ss is not None and ss.status in _SESSION_ACTIVE_STATUSES:
                return True
    return False


def _normalized_meeting_link_key(meeting_link: str) -> str:
    """Stable key for duplicate-join detection by meeting link."""
    try:
        from mechus.meeting_summary import meeting_link_key

        return meeting_link_key(meeting_link)
    except Exception:
        raw = (meeting_link or "").strip().lower()
        try:
            p = urlparse(raw)
            frag_items = [
                (k, v)
                for k, v in parse_qsl(p.fragment, keep_blank_values=False)
                if k.lower() != "l"
            ]
            query_items = [
                (k, v)
                for k, v in parse_qsl(p.query, keep_blank_values=False)
                if k.lower() != "l"
            ]
            return urlunparse(
                (
                    (p.scheme or "").lower(),
                    (p.netloc or "").lower(),
                    (p.path or "").rstrip("/"),
                    "",
                    urlencode(sorted(query_items)),
                    urlencode(sorted(frag_items)),
                )
            )
        except Exception:
            return raw


def _find_active_session_for_link_locked(
    target: str, *, backend: str | None = None
) -> tuple[str, MechusSession] | None:
    """Scan for an active session on this link key. Caller MUST hold _session_lock."""
    if not target:
        return None
    for mid, sess in _active_sessions.items():
        if sess.status not in _SESSION_ACTIVE_STATUSES:
            continue
        if backend and (getattr(sess, "join_backend", "") or "") != backend:
            continue
        if _normalized_meeting_link_key(getattr(sess, "meeting_link", "") or "") == target:
            return mid, sess
    return None


def _active_session_for_meeting_link(meeting_link: str, *, backend: str | None = None) -> tuple[str, MechusSession] | None:
    """Return an active session already using this meeting link, if any."""
    target = _normalized_meeting_link_key(meeting_link)
    if not target:
        return None
    with _session_lock:
        return _find_active_session_for_link_locked(target, backend=backend)


def _cleanup_orphan_facetime_workers(*, reason: str) -> int:
    """Remove FaceTime worker containers not tracked by the API session table."""
    try:
        ps = subprocess.run(
            [_docker_bin(), "ps", "-a", "--filter", "name=mechus-facetime-", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        log.debug("FaceTime orphan cleanup skipped (%s): %s", reason, exc)
        return 0

    names = [ln.strip() for ln in (ps.stdout or "").splitlines() if ln.strip()]
    if not names:
        return 0
    with _session_lock:
        tracked = {
            str(getattr(sess, "worker_container", "") or "")
            for sess in _active_sessions.values()
            if getattr(sess, "worker_container", None)
            and sess.status in _SESSION_ACTIVE_STATUSES
        }
    stale = [name for name in names if name not in tracked]
    for name in stale:
        try:
            subprocess.run(
                [_docker_bin(), "rm", "-f", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception as exc:
            log.warning("Could not remove orphan FaceTime worker %s (%s): %s", name, reason, exc)
    if stale:
        log.warning(
            "FaceTime orphan cleanup (%s): removed %d untracked worker(s): %s",
            reason,
            len(stale),
            ", ".join(stale),
        )
        # Free camera devices held by the containers just removed.
        _reconcile_facetime_camera_leases()
    return len(stale)


def _schedule_facetime_orphan_cleanup() -> None:
    def _run() -> None:
        _cleanup_orphan_facetime_workers(reason="api_startup")

    threading.Timer(2.0, _run).start()


_schedule_facetime_orphan_cleanup()


def _parse_bots_invite(data: dict[str, Any], registry: PersonaRegistry) -> list[dict[str, str]]:
    """Validate ``bots`` array; each entry has persona_id and display_name."""
    raw = data.get("bots")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("bots must be a JSON array of objects with persona_id and optional display_name")
    if len(raw) == 0:
        raise ValueError("bots must be a non-empty array when provided")
    out: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"bots[{i}] must be an object")
        pid = str(item.get("persona_id") or item.get("id") or "").strip().lower()
        if not pid:
            raise ValueError(f"bots[{i}].persona_id is required")
        p = registry.get(pid)
        if p is None:
            raise ValueError(f"Unknown persona_id {pid!r} in bots[{i}]")
        dn = str(item.get("display_name") or "").strip() or p.name
        out.append({"persona_id": pid, "display_name": dn})
    return out


def _extract_participant_timezones(payload: dict[str, object] | None) -> dict[str, str]:
    """Best-effort parse of participant timezone metadata from join payload."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    raw_map = payload.get("participant_timezones")
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip()
            if kk and vv:
                out[kk] = vv
    raw_participants = payload.get("participants")
    if isinstance(raw_participants, list):
        for p in raw_participants:
            if not isinstance(p, dict):
                continue
            who = str(p.get("name") or p.get("id") or p.get("speaker") or "").strip()
            tz = str(p.get("timezone") or p.get("tz") or p.get("location") or "").strip()
            if who and tz and who not in out:
                out[who] = tz
    return out


def _extract_participant_emails(payload: dict[str, object] | None) -> dict[str, str]:
    """Best-effort parse of participant emails from join payload metadata.

    Supports:
    - participant_emails: {"name_or_alias": "email@x.com"}
    - zoom_participant_emails: {"zoom_name_or_alias": "email@x.com"}
    - participants: [{
        "name": "...",
        "email": "...",
        "zoom_email": "...",
        "zoom_login_email": "...",
        "aliases": ["...", "..."],
        "mechus_name": "...",
        "display_name": "...",
        "zoom_name": "..."
      }]
    """
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    raw_map = payload.get("participant_emails")
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip().lower()
            if kk and vv and "@" in vv:
                out[kk] = vv
    raw_zoom_map = payload.get("zoom_participant_emails")
    if isinstance(raw_zoom_map, dict):
        for k, v in raw_zoom_map.items():
            kk = str(k or "").strip()
            vv = str(v or "").strip().lower()
            if kk and vv and "@" in vv and kk not in out:
                out[kk] = vv
    raw_participants = payload.get("participants")
    if isinstance(raw_participants, list):
        for p in raw_participants:
            if not isinstance(p, dict):
                continue
            email = str(
                p.get("email")
                or p.get("mail")
                or p.get("zoom_email")
                or p.get("zoom_login_email")
                or ""
            ).strip().lower()
            if not email or "@" not in email:
                continue

            candidate_names: list[str] = []
            for key in ("name", "id", "speaker", "mechus_name", "display_name", "zoom_name"):
                val = str(p.get(key) or "").strip()
                if val:
                    candidate_names.append(val)
            aliases = p.get("aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    alias_val = str(alias or "").strip()
                    if alias_val:
                        candidate_names.append(alias_val)

            for who in candidate_names:
                if who and who not in out:
                    out[who] = email
    return out


def _extract_summary_opt_in_recipients(payload: dict[str, object] | None) -> list[str]:
    """Best-effort parse of summary-email recipients from dashboard payload."""
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()

    raw_list = payload.get("summary_email_recipients")
    if isinstance(raw_list, list):
        for item in raw_list:
            email = str(item or "").strip().lower()
            if email and "@" in email and email not in seen:
                out.append(email)
                seen.add(email)

    raw_participants = payload.get("participants")
    if isinstance(raw_participants, list):
        for p in raw_participants:
            if not isinstance(p, dict):
                continue
            email = str(p.get("email") or p.get("mail") or "").strip().lower()
            if not email or "@" not in email or email in seen:
                continue
            opted_in = p.get("summary_opt_in")
            if opted_in is None:
                opted_in = p.get("summary_email_opt_in")
            if opted_in is None:
                opted_in = p.get("email_summary_opt_in")
            if bool(opted_in):
                out.append(email)
                seen.add(email)
    return out


def _extract_collection_recipients(payload: dict[str, object] | None) -> list[str]:
    """Best-effort parse of recipients for unresolved-email collection notices."""
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(email: str) -> None:
        e = str(email or "").strip().lower()
        if e and "@" in e and e not in seen:
            out.append(e)
            seen.add(e)

    for key in ("meeting_host_email", "organizer_email", "created_by_email", "host_email"):
        _add(str(payload.get(key) or ""))

    raw = payload.get("collection_recipients")
    if isinstance(raw, list):
        for item in raw:
            _add(str(item or ""))

    participants = payload.get("participants")
    if isinstance(participants, list):
        for p in participants:
            if not isinstance(p, dict):
                continue
            role = str(p.get("role") or "").strip().lower()
            is_host = bool(p.get("is_host")) or role in ("host", "organizer", "admin", "owner")
            if not is_host:
                continue
            _add(str(p.get("email") or p.get("mail") or p.get("zoom_email") or p.get("zoom_login_email") or ""))
    return out


def _try_send_meeting_emails_on_stop(
    meeting_id: str,
    final_summary: dict[str, Any] | None,
    session: "MechusSession | None",
) -> None:
    """Best-effort summary email after stop (backup if the worker SMTP call failed)."""
    if not final_summary or not isinstance(final_summary, dict):
        return
    try:
        from mechus.email_sender import send_meeting_emails

        send_meeting_emails(
            final_summary,
            meeting_id,
            speaker_emails=getattr(session, "participant_emails", None) if session else None,
            summary_recipients=getattr(session, "summary_email_recipients", None) if session else None,
            collection_recipients=getattr(session, "collection_recipients", None) if session else None,
            invite_collection_link=(
                getattr(session, "invite_collection_link", "") if session else ""
            ),
        )
    except Exception as exc:
        log.warning("API stop email retry failed for meeting_id=%s: %s", meeting_id, exc)


def _read_live_transcripts_for_status(transcript_path: str, speaker_map: dict[str, str] | None = None) -> list:
    """Read transcript entries from the session file for /api/status. Uses tail of file when large so status returns fast."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []
    speaker_map = speaker_map or {}
    live = []
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "r", encoding="utf-8") as f:
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
                f.readline()  # skip partial line
            for line in f:
                if not line.strip():
                    continue
                try:
                    note = safe_parse_json(line.strip())
                    if note.get("_parse_failed") or note.get("kind") != "transcript":
                        continue
                    ts = note.get("ts")
                    text = note.get("text", "")
                    meta = note.get("meta", {})
                    note_for_speaker = {"meta": meta, "text": text}
                    speaker = _speaker_for_transcript_note(note_for_speaker, speaker_map)
                    live.append({
                        "timestamp": ts,
                        "speaker": speaker,
                        "text": _apply_speaker_map_to_text(text, speaker_map),
                        "received_at": ts,
                    })
                except (KeyError, IndexError, TypeError):
                    continue
        return live[-_MAX_LIVE_TRANSCRIPTS_IN_STATUS:]
    except Exception:
        return []


def _open_link(link: str) -> None:
    """Open a meeting link in the default browser."""
    link = link.strip()
    if not link:
        return
    
    system = platform.system().lower()
    if system == "darwin":
        subprocess.Popen(["open", link])
    elif system == "windows":
        os.startfile(link)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", link])


_MEET_ROSTER_SKIP = {
    "google meet",
    "meet",
    "mechus",
    "you",
    "join now",
    "ask to join",
    "leave call",
    "end call",
    "microphone",
    "camera",
    "turn off microphone",
    "turn on microphone",
    "turn off camera",
    "turn on camera",
    "captions",
    "activities",
    "present now",
    "more options",
    "people",
    "chat",
    "raise hand",
    "meeting details",
    "copy joining info",
    "admit",
    "deny entry",
}


def _is_google_meet_link(link: str) -> bool:
    try:
        host = (urlparse(link or "").hostname or "").lower()
    except Exception:
        host = ""
    return host == "meet.google.com" or host.endswith(".meet.google.com")


def _clean_meet_roster_candidate(raw: object, *, display_name: str = "Mechus") -> str | None:
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip("•·-–—:|,")
    if not s:
        return None
    s = re.sub(
        r"\b(is )?(muted|unmuted|speaking|presenting|camera off|video off|host|you|me)\b",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\s+", " ", s).strip("•·-–—:|, ")
    low = s.lower()
    if not s or low in _MEET_ROSTER_SKIP:
        return None
    if display_name and low == display_name.strip().lower():
        return None
    if len(s) < 2 or len(s) > 64:
        return None
    if re.search(r"https?://|meet\.google|google|chrome|javascript|function|\{|\}", low):
        return None
    words = s.split()
    if len(words) > 4:
        return None
    if any(w.lower() in _MEET_ROSTER_SKIP for w in words):
        return None
    if not re.search(r"[A-Za-z]", s):
        return None
    return s


def _extract_google_meet_people(driver: object, *, display_name: str = "Mechus") -> tuple[list[str], list[str]]:
    """Best-effort Google Meet roster + active-speaker scrape."""
    script = r"""
const out = {names: [], active: []};
const visible = (el) => {
  try {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r && r.width > 0 && r.height > 0 && st && st.visibility !== 'hidden' && st.display !== 'none';
  } catch (e) { return false; }
};
const valsFor = (el) => [
  el.getAttribute && el.getAttribute('data-self-name'),
  el.getAttribute && el.getAttribute('aria-label'),
  el.getAttribute && el.getAttribute('title'),
  el.innerText,
  el.textContent
].filter(Boolean).map(v => String(v).trim()).filter(Boolean);
const selectors = [
  '[data-participant-id]',
  '[data-self-name]',
  '[role="listitem"]',
  '[role="gridcell"]',
  '[aria-label]',
  '[title]',
  '[class*="participant" i]',
  '[class*="tile" i]',
  '[class*="people" i]'
];
const seen = new Set();
for (const sel of selectors) {
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const vals = valsFor(el);
    for (const v of vals) out.names.push(v);
    const blob = [
      el.className || '',
      el.getAttribute && el.getAttribute('aria-label') || '',
      el.getAttribute && el.getAttribute('data-is-speaking') || '',
      el.getAttribute && el.getAttribute('data-speaking') || ''
    ].join(' ').toLowerCase();
    if (blob.includes('speaking') || blob.includes('active speaker') || blob.includes('is-speaking')) {
      for (const v of vals) out.active.push(v);
    }
  }
}
return out;
"""
    try:
        payload = driver.execute_script(script) or {}
    except Exception:
        return [], []

    def _clean_list(vals: object) -> list[str]:
        names: list[str] = []
        seen_names: set[str] = set()
        raw_vals = vals if isinstance(vals, list) else []
        for raw in raw_vals:
            for part in re.split(r"[\n\r]+", str(raw or "")):
                name = _clean_meet_roster_candidate(part, display_name=display_name)
                if not name:
                    continue
                key = name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                names.append(name)
        return names[:20]

    if not isinstance(payload, dict):
        return [], []
    names = _clean_list(payload.get("names"))
    active = _clean_list(payload.get("active"))
    merged = list(names)
    seen = {n.lower() for n in merged}
    for name in active:
        if name.lower() not in seen:
            merged.append(name)
            seen.add(name.lower())
    return merged[:20], active[:5]


def _start_google_meet_context_poller(
    *,
    meeting_id: str,
    driver: object | None,
    agent: MechusAgent | None,
    display_name: str = "Mechus",
) -> None:
    """Poll Meet DOM for roster/active-speaker hints while a browser session runs."""
    if driver is None or agent is None:
        return

    def _worker() -> None:
        participant_keys: set[str] = set()
        last_active = ""
        while True:
            with _session_lock:
                session = _active_sessions.get(meeting_id)
                if session is None or session.status in ("stopped", "stopping", "error"):
                    return
            try:
                cur = str(getattr(driver, "current_url", "") or "")
            except Exception:
                return
            if cur and "meet.google.com" not in cur:
                time.sleep(5.0)
                continue
            names, active = _extract_google_meet_people(driver, display_name=display_name)
            new_names = [n for n in names if n.lower() not in participant_keys]
            if new_names:
                participant_keys.update(n.lower() for n in new_names)
                try:
                    agent.register_call_participants(new_names)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Meet participant registration failed: %s", exc)
                log.info("Google Meet roster detected: %s", ", ".join(sorted(participant_keys)))
            if active:
                active_name = active[0]
                if active_name != last_active:
                    last_active = active_name
                    try:
                        agent._get_memory().add_segment(
                            f"Google Meet active speaker hint: {active_name}"
                        )
                    except Exception:
                        pass
                    log.info("Google Meet active speaker hint: %s", active_name)
            with _session_lock:
                session = _active_sessions.get(meeting_id)
                if session is not None:
                    session.participant_names = sorted(participant_keys)
                    session.active_speaker_names = active[:5]
            time.sleep(10.0)

    threading.Thread(
        target=_worker, daemon=True, name=f"meet-context-{meeting_id[:8]}"
    ).start()


def _open_and_join_meeting(link: str):
    """
    Open meeting link and attempt to auto-join using browser automation.
    
    Returns:
        WebDriver instance if successful, None otherwise
    
    Note: Audio device setup must be done manually in Zoom/Meet settings
    (one-time setup). This function only automates clicking "Join".
    """
    link = link.strip()
    if not link:
        return None
    
    # Try browser automation first
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        
        # Set up Chrome options
        chrome_options = Options()
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        # Disable camera (but allow microphone for audio)
        prefs = {
            "profile.default_content_setting_values.media_stream_camera": 2,  # Block camera
            "profile.default_content_setting_values.media_stream_mic": 1,  # Allow microphone
        }
        chrome_options.add_experimental_option("prefs", prefs)
        log.info("Camera disabled, microphone enabled for meeting audio")
        
        # Use pre-configured Chrome profile for audio device settings
        # This profile should have audio devices pre-selected in Zoom/Meet
        profile_path = os.getenv("MECHUS_CHROME_PROFILE_PATH")
        if profile_path and os.path.exists(profile_path):
            chrome_options.add_argument(f'--user-data-dir={profile_path}')
            log.info("Using Chrome profile with pre-configured audio settings")
        else:
            # Create a dedicated profile directory for Mechus
            mechus_profile_dir = os.path.expanduser("~/.mechus-chrome-profile")
            os.makedirs(mechus_profile_dir, exist_ok=True)
            chrome_options.add_argument(f'--user-data-dir={mechus_profile_dir}')
            log.info("Using Mechus Chrome profile. Audio devices will be remembered after first setup.")
        
        # Default to visible browser so auto-join and meeting audio work (headless often fails for Meet/Zoom).
        # Set MECHUS_SHOW_BROWSER=false to run headless (no audio in headless).
        show_browser = os.getenv("MECHUS_SHOW_BROWSER", "true").lower() in ("true", "1", "yes")
        if not show_browser:
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')  # Set window size for headless
            log.info("Running browser in headless mode (invisible)")
            log.warning(
                "Headless Chrome does not output audio. Mechus will not hear the meeting or be able to play TTS. "
                "For listening and notes, set MECHUS_SHOW_BROWSER=true and minimize the window. See docs/HOW_TO_RUN_AND_JOIN.md."
            )
        else:
            log.info("Running browser in visible mode (MECHUS_SHOW_BROWSER=true)")
        
        # Set display for headless (cloud servers need Xvfb)
        display = os.getenv("DISPLAY", ":99")
        if display:
            os.environ["DISPLAY"] = display
        
        # Try to create driver
        try:
            # On macOS, try to find Chrome in common locations
            system = platform.system().lower()
            if system == "darwin":
                # Common Chrome locations on macOS
                chrome_paths = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
                for chrome_path in chrome_paths:
                    if os.path.exists(chrome_path):
                        chrome_options.binary_location = chrome_path
                        log.info(f"Using Chrome at: {chrome_path}")
                        break
            
            # Try to create driver
            try:
                driver = webdriver.Chrome(options=chrome_options)
                log.info("Chrome driver created successfully")
            except Exception as e:
                log.warning(f"Failed to create Chrome driver: {e}")
                # Try chromium if chrome not available (Linux)
                if system != "darwin":
                    chrome_options.binary_location = "/usr/bin/chromium-browser"
                    driver = webdriver.Chrome(options=chrome_options)
                else:
                    raise
        except Exception as e:
            # Chrome not available, provide helpful error
            log.error(f"Selenium/Chrome not available: {e}")
            log.error("To fix this:")
            log.error("1. Install Chrome: https://www.google.com/chrome/")
            log.error("2. Install ChromeDriver: brew install chromedriver")
            log.error("3. Or set MECHUS_SHOW_BROWSER=false to use headless mode")
            log.warning("Falling back to opening link in default browser (Safari)")
            _open_link(link)
            log.info("Please click 'Join' in the browser and ensure audio devices are set correctly")
            return None
        
        try:
            # Open meeting link
            driver.get(link)
            log.info("Opened meeting link in browser")
            
            # Wait for page to load (Meet/Zoom can be slow)
            time.sleep(10)
            
            # Turn off camera before joining (for both Google Meet and Zoom)
            camera_turned_off = False
            try:
                log.info("Attempting to turn off camera before joining...")
                
                # Google Meet camera toggle
                if "meet.google.com" in link:
                    camera_selectors = [
                        "//button[@aria-label='Turn off camera' or contains(@aria-label, 'camera')]",
                        "//button[contains(@data-tooltip, 'camera') or contains(@data-tooltip, 'Camera')]",
                        "//div[@role='button' and contains(@aria-label, 'camera')]",
                        "//button[contains(@jsname, 'camera')]",
                        # Try JavaScript approach for Google Meet
                        "Array.from(document.querySelectorAll('button, div[role=\"button\"]')).find(el => { const label = (el.getAttribute('aria-label') || el.getAttribute('data-tooltip') || '').toLowerCase(); return (label.includes('camera') || label.includes('turn off')) && el.offsetParent !== null; })?.click(); return true;",
                    ]
                    
                    for selector in camera_selectors:
                        try:
                            if selector.startswith("Array.from"):  # JavaScript approach
                                result = driver.execute_script(selector)
                                if result:
                                    log.info("Turned off camera using JavaScript")
                                    camera_turned_off = True
                                    time.sleep(0.5)
                                    break
                            else:  # XPath approach
                                camera_btn = WebDriverWait(driver, 2).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                if camera_btn.is_displayed():
                                    driver.execute_script("arguments[0].click();", camera_btn)
                                    log.info("Turned off camera in Google Meet")
                                    camera_turned_off = True
                                    time.sleep(0.5)
                                    break
                        except Exception as e:
                            log.debug(f"Camera selector failed: {e}")
                            continue
                
                # Zoom camera toggle
                elif "zoom.us" in link:
                    camera_selectors = [
                        "//button[contains(@aria-label, 'camera') or contains(@aria-label, 'Camera')]",
                        "//button[contains(text(), 'Turn off') or contains(text(), 'Camera')]",
                        "//div[@class='video-button' or contains(@class, 'camera')]//button",
                    ]
                    
                    for selector in camera_selectors:
                        try:
                            camera_btn = WebDriverWait(driver, 2).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            if camera_btn.is_displayed():
                                driver.execute_script("arguments[0].click();", camera_btn)
                                log.info("Turned off camera in Zoom")
                                camera_turned_off = True
                                time.sleep(0.5)
                                break
                        except Exception as e:
                            log.debug(f"Camera selector failed: {e}")
                            continue
                
                if not camera_turned_off:
                    log.warning("Could not find camera toggle button - camera may be on")
                else:
                    log.info("Camera turned off successfully")
                    
            except Exception as e:
                log.debug(f"Error turning off camera: {e} - continuing anyway")
            
            # Try to find and click "Join" button
            join_clicked = False
            max_attempt_time = 15  # Maximum time to spend trying to find Join button (seconds)
            start_time = time.time()
            
            # Google Meet selectors (try first since logs show Google Meet)
            if "meet.google.com" in link:
                log.info("Attempting to join Google Meet...")
                
                # Check if we need to sign in first
                try:
                    page_source = driver.page_source.lower()
                    if "sign in" in page_source or "sign-in" in page_source:
                        log.warning("Google Meet requires sign-in. Please sign in manually in the browser.")
                        log.info("After signing in, the Join button should appear automatically.")
                        log.info("For future meetings, Chrome profile will remember your sign-in.")
                        # Keep the browser driver alive so the user can sign in
                        # manually and the Meet roster poller can start once the
                        # page reaches the meeting.
                        return driver
                except Exception:
                    pass
                
                # First, try JavaScript-based approach (more reliable)
                try:
                    log.debug("Trying JavaScript-based click methods...")
                    # Google Meet often uses these button patterns
                    js_click_attempts = [
                        # Try clicking by text content (most reliable)
                        ("const el = Array.from(document.querySelectorAll('button, div[role=\"button\"]')).find(el => { const text = el.textContent || el.innerText || ''; return (text.includes('Join now') || text.includes('Ask to join') || text.includes('Join')) && el.offsetParent !== null; }); if (el) { el.click(); return true; } return false;", "text content"),
                        # Try clicking by aria-label
                        ("const el = Array.from(document.querySelectorAll('button[aria-label*=\"Join\"], div[aria-label*=\"Join\"]')).find(el => el.offsetParent !== null); if (el) { el.click(); return true; } return false;", "aria-label"),
                        # Try clicking by data attributes
                        ("const el = Array.from(document.querySelectorAll('[jscontroller], [jsname]')).find(el => { const text = (el.textContent || el.innerText || '').toLowerCase(); return (text.includes('join') || text.includes('ask')) && el.offsetParent !== null; }); if (el) { el.click(); return true; } return false;", "data attributes"),
                    ]
                    
                    for js_code, method_name in js_click_attempts:
                        # Check timeout
                        if time.time() - start_time > max_attempt_time:
                            log.warning(f"Timeout reached ({max_attempt_time}s) while trying to find Join button")
                            break
                        
                        try:
                            log.debug(f"Trying JavaScript method: {method_name}")
                            result = driver.execute_script(js_code)
                            if not result:
                                continue
                            # Wait a moment to see if page changed
                            time.sleep(1)
                            # Check if we're now in the meeting (URL changed or page content changed)
                            try:
                                current_url = driver.current_url
                                video_elements = driver.find_elements(By.TAG_NAME, "video")
                                if "meet.google.com" in current_url and (len(video_elements) > 0 or "meeting" in current_url.lower()):
                                    log.info(f"Successfully clicked Join button using JavaScript ({method_name})")
                                    join_clicked = True
                                    break
                            except:
                                # If we can't check, assume it worked if no exception
                                log.info(f"Clicked Join button using JavaScript ({method_name}) - verifying...")
                                join_clicked = True
                                break
                        except Exception as e:
                            log.debug(f"JavaScript click attempt ({method_name}) failed: {e}")
                            continue
                except Exception as e:
                    log.debug(f"JavaScript approach failed: {e}")
                
                # If JS didn't work, try XPath selectors (with timeout)
                if not join_clicked and (time.time() - start_time) < max_attempt_time:
                    log.debug("Trying XPath selectors...")
                    meet_selectors = [
                        # More specific selectors
                        "//button[contains(., 'Join now')]",
                        "//button[contains(., 'Ask to join')]",
                        "//div[@role='button' and contains(., 'Join now')]",
                        "//div[@role='button' and contains(., 'Ask to join')]",
                        "//span[contains(text(), 'Join now')]/ancestor::button",
                        "//span[contains(text(), 'Ask to join')]/ancestor::button",
                        "//span[contains(text(), 'Join now')]/ancestor::div[@role='button']",
                        "//span[contains(text(), 'Ask to join')]/ancestor::div[@role='button']",
                        # Generic join buttons
                        "//button[contains(@aria-label, 'Join')]",
                        "//div[@role='button' and contains(@aria-label, 'Join')]",
                    ]
                    
                    for selector in meet_selectors:
                        # Check timeout
                        if time.time() - start_time > max_attempt_time:
                            log.warning(f"Timeout reached while trying selectors")
                            break
                        
                        try:
                            # Use shorter timeout to avoid hanging
                            join_button = WebDriverWait(driver, 1).until(
                                EC.presence_of_element_located((By.XPATH, selector))
                            )
                            # Make sure it's visible and clickable
                            if join_button.is_displayed() and join_button.is_enabled():
                                # Scroll into view
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", join_button)
                                time.sleep(0.5)
                                # Try regular click first
                                try:
                                    join_button.click()
                                except:
                                    # If regular click fails, use JavaScript click
                                    driver.execute_script("arguments[0].click();", join_button)
                                
                                log.info(f"Clicked Join button (Google Meet) using selector: {selector}")
                                join_clicked = True
                                time.sleep(2)
                                # Verify we're in the meeting
                                current_url = driver.current_url
                                if "meet.google.com" in current_url:
                                    log.info("Successfully joined Google Meet")
                                break
                        except Exception as e:
                            log.debug(f"Selector {selector} failed: {e}")
                            continue
                
                # Last resort: try to find any clickable element with "join" text (with timeout)
                if not join_clicked and (time.time() - start_time) < max_attempt_time:
                    try:
                        log.debug("Trying text matching approach...")
                        all_buttons = driver.find_elements(By.XPATH, "//button | //div[@role='button']")
                        log.debug(f"Found {len(all_buttons)} potential buttons")
                        for btn in all_buttons[:10]:  # Limit to first 10 to avoid hanging
                            # Check timeout
                            if time.time() - start_time > max_attempt_time:
                                break
                            try:
                                if not btn.is_displayed():
                                    continue
                                text = (btn.text or btn.get_attribute("aria-label") or "").lower()
                                if 'join' in text:
                                    log.debug(f"Found button with 'join' text: {text[:50]}")
                                    driver.execute_script("arguments[0].click();", btn)
                                    log.info(f"Clicked Join button by text matching: {text[:50]}")
                                    join_clicked = True
                                    time.sleep(2)
                                    break
                            except Exception as e:
                                log.debug(f"Error clicking button: {e}")
                                continue
                    except Exception as e:
                        log.debug(f"Text matching approach failed: {e}")
            
            # Zoom selectors
            if not join_clicked and "zoom.us" in link:
                zoom_selectors = [
                    "//button[contains(text(), 'Join')]",
                    "//button[contains(text(), 'Join Meeting')]",
                    "//span[contains(text(), 'Join')]/..",
                    "//a[contains(text(), 'Join')]",
                    "//button[@id='joinBtn']",
                ]
                
                for selector in zoom_selectors:
                    try:
                        join_button = WebDriverWait(driver, 20).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        driver.execute_script("arguments[0].scrollIntoView(true);", join_button)
                        time.sleep(0.5)
                        join_button.click()
                        log.info(f"Clicked Join button (Zoom) using selector: {selector}")
                        join_clicked = True
                        time.sleep(2)
                        
                        # Try to click "Join with Computer Audio" if it appears
                        try:
                            audio_selectors = [
                                "//button[contains(text(), 'Computer Audio')]",
                                "//button[contains(text(), 'Join Audio')]",
                                "//button[contains(text(), 'Join with Computer Audio')]",
                            ]
                            for audio_sel in audio_selectors:
                                try:
                                    audio_button = WebDriverWait(driver, 3).until(
                                        EC.element_to_be_clickable((By.XPATH, audio_sel))
                                    )
                                    audio_button.click()
                                    log.info("Clicked audio join button")
                                    break
                                except:
                                    continue
                        except:
                            pass
                        
                        break
                    except Exception as e:
                        log.debug(f"Zoom selector {selector} failed: {e}")
                        continue
            
            if not join_clicked:
                log.warning("Could not automatically click Join button after trying all methods.")
                log.info("This is normal for the first time - Google Meet may require:")
                log.info("  1. Sign-in (if not already signed in)")
                log.info("  2. Manual click on 'Join now' or 'Ask to join' button")
                log.info("  3. Audio device permissions")
                log.info("")
                log.info("After you manually join once:")
                log.info("  - Chrome profile will remember your sign-in")
                log.info("  - Audio device settings will be saved")
                log.info("  - Future meetings may auto-join more reliably")
                log.info("")
                log.info("The browser window is open - please click 'Join' manually.")
            
            # Keep browser open - don't close it
            # The meeting will stay open
            log.info("Meeting should be joining. Please verify audio devices are set correctly in Zoom/Meet settings.")
            
            # Return the driver so we can close it later
            return driver
            
        except Exception as e:
            log.error("Error during auto-join: %s", e)
            # Fallback: just keep the browser open
            log.info("Browser opened. Please click 'Join' manually.")
            # Still return driver so we can close it
            try:
                return driver
            except:
                return None
            
    except ImportError:
        # Selenium not installed, fall back to normal open
        log.warning("Selenium not installed. Install with: pip install selenium")
        log.info("Opening link normally. Please click 'Join' manually.")
        _open_link(link)
        return None
    except Exception as e:
        log.error("Browser automation failed: %s", e)
        log.info("Falling back to normal link opening. Please click 'Join' manually.")
        _open_link(link)
        return None


def _maybe_with_display_name(link: str, display_name: str | None) -> str:
    """For Zoom browser links, add a display name so Mechus shows up as itself."""
    if not link or not display_name:
        return link

    parsed = urlparse(link)
    if "zoom.us" not in parsed.netloc:
        return link

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    # Do not override if caller already provided one.
    query.setdefault("uname", display_name)
    parsed = parsed._replace(query=urlencode(query))
    return urlunparse(parsed)


def _run_agent(agent: MechusAgent, meeting_id: str) -> None:
    """Run the Mechus agent in the current thread."""
    import threading
    global _active_sessions
    
    # Monkey-patch to skip signal handlers in background thread
    original_install = agent._install_signal_handlers
    def skip_signal_handlers():
        # Only install signal handlers if we're in the main thread
        if threading.current_thread() is threading.main_thread():
            original_install()
        # Otherwise skip - signal handlers can't be set in background threads
    
    agent._install_signal_handlers = skip_signal_handlers
    
    try:
        agent.run_forever()
    except Exception as exc:
        log.error("Agent error for meeting %s: %s", meeting_id, exc, exc_info=True)
        # Update session status on error
        with _session_lock:
            if meeting_id in _active_sessions:
                _active_sessions[meeting_id].status = "error"
    finally:
        agent.stop()
        # Update session status when stopped
        with _session_lock:
            if meeting_id in _active_sessions:
                _active_sessions[meeting_id].status = "stopped"


def _run_voice_test_loop(agent: MechusAgent, meeting_id: str) -> None:
    """Play a test phrase into the meeting every few seconds (Zoom SDK voice injection test)."""
    time.sleep(5)  # Let Zoom join and agent start
    # Wait for Zoom virtual mic to be ready (onMicInitialize + onMicStartSend)
    audio_out = agent._audio_out
    wait_ready = getattr(audio_out, "is_ready_to_send", None)
    if callable(wait_ready):
        for _ in range(35):  # up to 35 seconds
            if wait_ready():
                log.info("Zoom virtual mic ready for voice test, meeting %s", meeting_id)
                break
            time.sleep(1)
        else:
            log.warning(
                "Zoom virtual mic never ready (no onMicStartSend?); voice test may be silent for meeting %s",
                meeting_id,
            )
    else:
        time.sleep(5)  # non-Zoom output, short extra delay
    phrase = "Hello, this is Mechus. Voice test."
    repeat_interval = 5  # seconds between plays
    repeat_count = 12    # ~1 minute total
    gain = getattr(agent._config, "tts_output_gain", 0.9)
    for i in range(repeat_count):
        try:
            syn = agent._tts.synthesize(phrase)
            agent._audio_out.play_blocking(syn.audio_f32, syn.sample_rate_hz, gain=gain)
            log.info("Voice test play %s/%s for meeting %s", i + 1, repeat_count, meeting_id)
        except Exception as e:
            log.warning("Voice test loop stopped for meeting %s: %s", meeting_id, e)
            break
        if i < repeat_count - 1:
            time.sleep(repeat_interval)
    log.info("Voice test loop finished for meeting %s", meeting_id)


@app.route("/health", methods=["GET"])
def health():
    """Liveness: is the process up."""
    return jsonify({"status": "ok", "service": "mechus-api"})


@app.route("/ready", methods=["GET"])
def ready():
    """Readiness: can accept traffic (for load balancers / K8s)."""
    return jsonify({"status": "ready", "service": "mechus-api"})


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.route("/", methods=["GET"])
def index():
    return redirect("/dashboard#/tenants")


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Landlord portfolio dashboard."""
    resp = send_from_directory(_STATIC_DIR, "properties.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/meetings", methods=["GET"])
def meetings_dashboard():
    """Keep old meeting links working while meetings live inside the main dashboard."""
    query = urlencode(list(request.args.items(multi=True)))
    target = "/dashboard" + (f"?{query}" if query else "") + "#/meetings"
    return redirect(target)


@app.route("/welcome", methods=["GET"])
def welcome():
    """First-visit intro for the built-in dashboard."""
    resp = send_from_directory(_STATIC_DIR, "welcome.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/callback", methods=["GET"])
def oauth_callback():
    """Zoom OAuth redirect target. Accepts ?code=... and shows success. Used so app install completes."""
    code = request.args.get("code", "")
    # Optional: exchange code for tokens if needed. For RTMS, app credentials suffice.
    return (
        "<!DOCTYPE html><html><body style='font-family:sans-serif;padding:2em;'>"
        "<h1>Mechus App Authorized</h1><p>You can close this tab and return to Zoom.</p></body></html>"
    ), 200


@app.route("/api/teams", methods=["POST"])
def upsert_team():
    """Create or update a Mechus Teams / Enterprise workspace."""
    try:
        data = request.get_json() or {}
        team_id = (data.get("team_id") or data.get("id") or "").strip()
        from mechus.team_context import TeamContextStore

        team = TeamContextStore(team_id).upsert_team(
            name=str(data.get("name") or "").strip(),
            plan=str(data.get("plan") or "teams").strip(),
            created_by=str(data.get("user_id") or data.get("created_by") or "").strip(),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
        )
        return jsonify({"success": True, "team": team})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        log.exception("upsert_team failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/teams/<team_id>", methods=["GET"])
def get_team(team_id: str):
    """Return team metadata and context counts."""
    try:
        from mechus.team_context import TeamContextStore

        store = TeamContextStore(team_id)
        team = store.get_team()
        if team is None:
            return jsonify({"success": False, "error": "team not found"}), 404
        items = store.list_items()
        return jsonify({
            "success": True,
            "team": team,
            "context_count": len(items),
            "meeting_count": sum(1 for i in items if i.get("source_type") == "meeting"),
        })
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/teams/<team_id>/context", methods=["GET", "POST"])
def team_context(team_id: str):
    """List or add shared team context notes."""
    try:
        from mechus.team_context import TeamContextStore

        store = TeamContextStore(team_id)
        if request.method == "GET":
            limit_raw = request.args.get("limit")
            limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
            return jsonify({
                "success": True,
                "team_id": store.team_id,
                "items": store.list_items(limit=limit),
            })

        data = request.get_json() or {}
        item = store.append_item(
            text=str(data.get("text") or ""),
            title=str(data.get("title") or ""),
            source_type=str(data.get("source_type") or "manual"),
            source_id=str(data.get("source_id") or ""),
            created_by=str(data.get("user_id") or data.get("created_by") or ""),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
        )
        return jsonify({"success": True, "team_id": store.team_id, "item": item})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        log.exception("team_context failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/teams/<team_id>/meetings", methods=["GET", "POST"])
def team_meetings(team_id: str):
    """List or add completed meetings into shared team context."""
    try:
        from mechus.team_context import TeamContextStore

        store = TeamContextStore(team_id)
        if request.method == "GET":
            limit_raw = request.args.get("limit")
            limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
            return jsonify({
                "success": True,
                "team_id": store.team_id,
                "meetings": store.list_meetings(limit=limit),
            })

        data = request.get_json() or {}
        item = store.add_meeting(
            str(data.get("meeting_id") or "").strip(),
            title=str(data.get("title") or ""),
            created_by=str(data.get("user_id") or data.get("created_by") or ""),
        )
        return jsonify({"success": True, "team_id": store.team_id, "item": item})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        log.exception("team_meetings failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/meetings/list", methods=["GET"])
def list_past_meetings():
    """
    List all past meetings that have a final summary available.
    Used by the dashboard 'Contextual memory' picker to let the user choose
    which past meetings Mechus should remember during the current session.

    Query params:
      limit      (int, default 50) — max results
      exclude    (str)             — meeting_id to exclude (e.g. current meeting)
      link_key   (str)             — filter to meetings from the same link only

    Response:
    {
      "success": true,
      "meetings": [
        {
          "meeting_id": "...",
          "title": "Q3 Launch Planning",
          "generated_at": "2026-06-10T14:23:00Z",
          "meeting_link": "https://...",
          "link_key": "sha1...",
          "executive_summary_preview": "First 180 chars of summary...",
          "topics": ["pricing", "launch", ...],
          "has_action_items": true,
          "has_decisions": true,
          "action_item_count": 3,
          "decision_count": 2
        }, ...
      ]
    }
    """
    try:
        from mechus.agent import MechusAgent
        limit_raw = request.args.get("limit")
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else 50
        limit = max(1, min(limit, 200))
        exclude = (request.args.get("exclude") or "").strip() or None
        link_key_filter = (request.args.get("link_key") or "").strip() or None
        meetings = MechusAgent.list_past_meetings(
            exclude_meeting_id=exclude,
            max_results=limit,
        )
        if link_key_filter:
            meetings = [m for m in meetings if m.get("link_key") == link_key_filter]
        return jsonify({"success": True, "meetings": meetings})
    except Exception as exc:
        log.exception("list_past_meetings failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/sessions/<session_id>/memory", methods=["GET", "POST", "DELETE"])
def session_memory(session_id: str):
    """
    Get or set the contextual meeting memory for a live session.

    GET  — return which past meetings are currently loaded as memory.
    POST — pin specific past meetings as memory for this session.
           Body: {"meeting_ids": ["id1", "id2", ...]}
           Pass [] to clear pinned memory and fall back to auto-selection.
    DELETE — clear pinned memory and reload auto-selected memory.

    This is the live-session counterpart of the 'Contextual memory' picker:
    the dashboard calls POST here after the user selects meetings.
    Works for both in-process sessions and worker-based (Zoom/FaceTime) sessions.
    """
    try:
        with _session_lock:
            session = _active_sessions.get(session_id)
        if session is None:
            return jsonify({"success": False, "error": "session not found"}), 404

        agent = getattr(session, "agent", None)
        if agent is None:
            return jsonify({"success": False, "error": "agent not available"}), 404

        if request.method == "GET":
            pinned = getattr(agent, "_pinned_memory_ids", None)
            current_text = getattr(agent, "_cross_meeting_memory_text", "") or ""
            return jsonify({
                "success": True,
                "session_id": session_id,
                "pinned_meeting_ids": pinned,
                "memory_loaded": bool(current_text),
                "memory_chars": len(current_text),
            })

        if request.method == "DELETE":
            agent._pinned_memory_ids = None
            agent._reload_cross_meeting_memory()
            current_text = getattr(agent, "_cross_meeting_memory_text", "") or ""
            return jsonify({
                "success": True,
                "session_id": session_id,
                "pinned_meeting_ids": None,
                "memory_chars": len(current_text),
                "message": "Reverted to auto-selected memory",
            })

        # POST — pin specific meetings
        data = request.get_json() or {}
        meeting_ids = data.get("meeting_ids")
        if not isinstance(meeting_ids, list):
            return jsonify({"success": False, "error": "meeting_ids must be an array"}), 400
        meeting_ids = [str(m).strip() for m in meeting_ids if m]

        if not meeting_ids:
            # Empty list → clear pinned, revert to auto
            agent._pinned_memory_ids = None
            agent._reload_cross_meeting_memory()
            current_text = getattr(agent, "_cross_meeting_memory_text", "") or ""
            return jsonify({
                "success": True,
                "session_id": session_id,
                "pinned_meeting_ids": None,
                "memory_chars": len(current_text),
                "message": "Pinned memory cleared; using auto-selected memory",
            })

        agent._pinned_memory_ids = meeting_ids
        result = agent.set_cross_meeting_memory_from_ids(meeting_ids)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "pinned_meeting_ids": result["meeting_ids"],
            "memory_loaded": result["loaded"],
            "memory_chars": result["chars"],
        })

    except Exception as exc:
        log.exception("session_memory failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/set-meeting-for-rtms", methods=["POST"])
def set_meeting_for_rtms():
    """
    Store meeting link for RTMS webhook. Dashboard paste flow:

    1. User pastes link in dashboard
    2. Dashboard calls this with meeting_link
    3. User joins that meeting with Zoom App -> RTMS webhook fires
    4. rtms_mechus uses stored meeting for Meeting SDK join (TTS)

    Request body:
    {
        "meeting_link": "https://zoom.us/j/123456789?pwd=..." (required),
        "user_id": "user-123" (optional; default "default")
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400
        meeting_link = data.get("meeting_link")
        if not meeting_link:
            return jsonify({"success": False, "error": "meeting_link is required"}), 400
        user_id = (data.get("user_id") or "").strip() or "default"

        from mechus.rtms_meeting_store import set_meeting
        result = set_meeting(meeting_link, user_id=user_id)
        if result is None:
            return jsonify({
                "success": False,
                "error": "Invalid Zoom meeting link. Use format https://zoom.us/j/MEETING_NUMBER or add ?pwd=PASSWORD",
            }), 400
        meeting_number, _ = result
        return jsonify({
            "success": True,
            "message": "Meeting stored for RTMS. Join this meeting with your Zoom App; Mechus will connect when RTMS fires.",
            "meeting_number": meeting_number,
        })
    except Exception as e:
        log.exception("set-meeting-for-rtms error")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/personas", methods=["GET"])
def list_personas():
    """List configured Mechus personas (id, name, wake phrases, voice id).

    Reads ``mechus/personas/*.json`` (or ``MECHUS_PERSONAS_DIR``) using the same
    loader the agent uses. The response is safe to render in a dashboard;
    full system prompts are intentionally NOT returned.
    """
    try:
        env_file = os.getenv("MECHUS_ENV_FILE", ".env")
        load_env_file(env_file)
        config = MechusConfig.from_env()
        from mechus.personas import PersonaRegistry
        registry = PersonaRegistry.load(
            config,
            personas_dir=getattr(config, "personas_dir", None),
            enabled_ids=getattr(config, "personas_enabled", None) or None,
        )
        out = []
        for p in registry.personas():
            out.append({
                "id": p.id,
                "name": p.name,
                "wake_phrases": list(p.wake_phrases),
                "voice_id": p.voice_id or None,
                "is_default": bool(p.is_default),
                "audio_tags": bool(p.audio_tags),
                "max_words": int(p.max_words),
                "tools": list(p.tools),
            })
        return jsonify({
            "success": True,
            "personas_dir": getattr(config, "personas_dir", ""),
            "personas": out,
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("list-personas error")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/join-meeting", methods=["POST"])
def join_meeting():
    """
    Join a meeting and start Mechus.
    
    Request body:
    {
        "meeting_link": "https://zoom.us/j/..." (required),
        "user_id": "user-123" (optional; default "default" – dashboard can send only meeting_link),
        "team_id": "team-123" (optional; enables Mechus for Teams shared context),
        "shared_context": true (optional; default true when team_id is present),
        "share_to_team_context": false (optional; add this meeting to team context when stopped),
        "meeting_id": "meeting-123" (optional, auto-generated if not provided),
        "display_name": "Mechus" (optional),
        "join_backend": "browser" | "external" | "zoom_sdk" | "facetime_web" (optional; if omitted, inferred from meeting_link for FaceTime/Zoom URLs, else MECHUS_JOIN_BACKEND or "browser"),
        "recording_token": "..." (optional; Zoom local recording join token – pre-grants recording, no host prompt. See docs/PRODUCTION_HEADLESS.md)
        "zak_token": "..." (optional but required since Mar 2026 to join meetings outside the SDK app's own Zoom account. Host generates via GET https://api.zoom.us/v2/users/me/token?type=zak)
        "participant_emails": {"Full Name or Alias": "person@company.com"} (optional; dashboard metadata used for action-item email routing),
        "zoom_participant_emails": {"Zoom Display Name": "person@company.com"} (optional; Zoom-login-derived fallback map),
        "participants": [{"name":"Full Name","email":"person@company.com","aliases":["Zoom Nick"],"summary_opt_in":true,"timezone":"America/New_York"}] (optional; alias-aware metadata format)
        "summary_email_recipients": ["person@company.com"] (optional; opt-in summary recipients from dashboard)
        "meeting_host_email": "host@company.com" (optional; dynamic fallback recipient for unresolved owner emails),
        "collection_recipients": ["host@company.com"] (optional; explicit fallback recipients),
        "invite_email_collection_link": "https://app.mechus.ai/invite/email-collection?meeting_id=..." (optional; fallback link for unresolved owner emails)
        "bots": [{"persona_id": "penny", "display_name": "Penny"}, ...] (optional; multiple Zoom/FaceTime participants — requires join_backend zoom_sdk or facetime_web; each bot is a separate worker with MECHUS_BOT_PERSONA_ID locked to that persona. Response includes bot_meeting_id per bot and session_group_id equal to meeting_id.)
        "memory_meeting_ids": ["meeting-abc", "meeting-xyz"] (optional; pin specific past meetings as contextual memory — overrides the automatic recency-based selection. Pass [] to disable cross-meeting memory entirely.)
    }
    When join_backend is "external", no browser is opened; audio is routed to Mechus's input separately. When "zoom_sdk" (Zoom link, Linux only), Mechus joins via Zoom Meeting SDK with full audio and no browser. See docs/SCALABLE_JOIN.md and docs/ZOOM_SDK_SETUP.md.
    
    Returns:
    {
        "success": true,
        "message": "Mechus is joining the meeting",
        "status": "starting",
        "meeting_id": "meeting-123"
    }
    """
    global _active_sessions
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400
        
        meeting_link = data.get("meeting_link")
        if not meeting_link:
            # Chert FaceTime calls dial a phone number / Apple ID email instead of a link.
            meeting_link = str(data.get("destination") or "").strip()
        if not meeting_link:
            return jsonify({"success": False, "error": "meeting_link (or destination) is required"}), 400
        
        # user_id optional: default so dashboard can send only meeting_link
        user_id = (data.get("user_id") or "").strip() or "default"
        team_id = (data.get("team_id") or "").strip()
        shared_context_enabled = _team_shared_context_enabled(data, team_id)
        share_to_team_context = bool(team_id) and _json_truthy(data.get("share_to_team_context"))
        if team_id:
            from mechus.team_context import TeamContextStore
            TeamContextStore(team_id).upsert_team(
                name=str(data.get("team_name") or "").strip(),
                plan=str(data.get("plan") or "teams").strip(),
                created_by=user_id,
            )

        # memory_meeting_ids: optional list of past meeting IDs to use as
        # contextual memory.  None = auto-select (recency + same-link priority).
        # [] = disable cross-meeting memory for this session.
        _raw_memory_ids = data.get("memory_meeting_ids")
        memory_meeting_ids: list[str] | None = None
        if isinstance(_raw_memory_ids, list):
            memory_meeting_ids = [str(m).strip() for m in _raw_memory_ids if m]
        
        # Get or generate meeting_id: unique per join so every meeting is fresh for every person
        meeting_id = (data.get("meeting_id") or "").strip() or None
        if not meeting_id:
            meeting_id = str(uuid.uuid4())
        
        display_name = data.get("display_name", "Mechus")
        recording_token = (data.get("recording_token") or "").strip() or None
        zak_token = (data.get("zak_token") or "").strip() or None
        participant_timezones = _extract_participant_timezones(data)
        participant_emails = _extract_participant_emails(data)
        summary_email_recipients = _extract_summary_opt_in_recipients(data)
        collection_recipients = _extract_collection_recipients(data)
        invite_collection_link = str(data.get("invite_email_collection_link") or "").strip()
        # Chert FaceTime options: {"video": true}, {"inspect_mode": "move_in"|"move_out",
        # "inspect_unit": "apt-4b"}. Read by the facetime_chert worker spawn below.
        _CHERT_CALL_OPTIONS[str(meeting_id)] = {
            "video": bool(data.get("video")),
            "inspect_mode": str(data.get("inspect_mode") or "").strip(),
            "inspect_unit": str(data.get("inspect_unit") or "").strip(),
        }
        if not invite_collection_link:
            invite_collection_link = (os.getenv("MECHUS_INVITE_EMAIL_COLLECTION_LINK") or "").strip()

        # Per-meeting persona allow-list (single-bot joins only; multi-bot uses MECHUS_BOT_PERSONA_ID per worker).
        enabled_personas_csv: str = ""
        if data.get("bots") is None:
            _enabled_personas_raw = data.get("enabled_personas")
            if isinstance(_enabled_personas_raw, list):
                enabled_personas_csv = ",".join(
                    str(x).strip().lower()
                    for x in _enabled_personas_raw
                    if str(x).strip()
                )
            elif isinstance(_enabled_personas_raw, str) and _enabled_personas_raw.strip():
                enabled_personas_csv = _enabled_personas_raw.strip().lower()
        # Prepare the meeting link early (single-bot; multi-bot uses per-persona display names on the link)
        formatted_link = _maybe_with_display_name(meeting_link, display_name)

        raw_backend = data.get("join_backend")
        if isinstance(raw_backend, str) and raw_backend.strip():
            join_backend = raw_backend.strip().lower()
        else:
            inferred = infer_join_backend_from_meeting_link(str(meeting_link))
            join_backend = inferred or os.getenv("MECHUS_JOIN_BACKEND", "browser")
            join_backend = str(join_backend).strip().lower()
            if inferred:
                log.info(
                    "join_backend inferred from meeting_link for meeting_id=%s: %s",
                    meeting_id,
                    join_backend,
                )
        if join_backend == "chert":
            join_backend = "facetime_chert"
        if join_backend not in ("browser", "external", "zoom_sdk", "facetime_web", "facetime_chert"):
            join_backend = "browser"

        camera_value = data.get("camera")
        if camera_value is not None and not isinstance(camera_value, dict):
            return jsonify({"success": False, "error": "camera must be an object"}), 400
        camera_data = camera_value if isinstance(camera_value, dict) else {}
        vision_value = data.get("vision")
        if vision_value is not None and not isinstance(vision_value, dict):
            return jsonify({"success": False, "error": "vision must be an object"}), 400
        vision_data = vision_value if isinstance(vision_value, dict) else {}
        camera_warnings: list[str] = []
        camera_mode = str(
            camera_data.get("mode")
            or os.getenv("MECHUS_FT_CAMERA_DEFAULT_MODE", "avatar")
            or "avatar"
        ).strip().lower()
        # ``camera.enabled`` sets the *initial* on/off state. When the feature is
        # on, every FaceTime worker is provisioned with a device (if one is free)
        # so the camera can still be turned on later through the camera API.
        camera_requested = _json_truthy(
            camera_data.get("enabled")
            if "enabled" in camera_data
            else os.getenv("MECHUS_FT_CAMERA_AUTO_ON", "0")
        )
        camera_required = _json_truthy(
            camera_data.get("required")
            if "required" in camera_data
            else os.getenv("MECHUS_FT_CAMERA_REQUIRED", "0")
        )
        vision_requested = _json_truthy(vision_data.get("enabled"))
        if join_backend != "facetime_web":
            if camera_value is not None or vision_value is not None:
                camera_warnings.append(
                    "camera/vision settings are only supported for FaceTime and were ignored"
                )
            camera_requested = False
            camera_required = False
            vision_requested = False
            camera_mode = "avatar"
        elif camera_mode not in CAMERA_MODES:
            return jsonify({"success": False, "error": f"Unknown camera mode: {camera_mode}"}), 400
        camera_provision = join_backend == "facetime_web" and _facetime_camera_feature_enabled()
        if not camera_provision:
            if camera_required and join_backend == "facetime_web":
                return jsonify({
                    "success": False,
                    "error": "FaceTime camera support is disabled on this server.",
                }), 503
            if camera_requested:
                camera_warnings.append("FaceTime camera support is disabled on this server")
            camera_requested = False
            camera_required = False
        if vision_requested and not _facetime_vision_feature_enabled():
            camera_warnings.append("FaceTime visual understanding is disabled on this server")
            vision_requested = False
        camera_response = {
            "camera": {
                "provisioned": camera_provision,
                "initially_enabled": camera_requested,
                "required": camera_required,
                "mode": camera_mode,
            },
            "vision": {"enabled": vision_requested},
        }
        if camera_warnings:
            camera_response["warnings"] = camera_warnings

        # Load environment and create config (needed to validate ``bots`` persona ids)
        env_file = os.getenv("MECHUS_ENV_FILE", ".env")
        load_env_file(env_file)
        # MECHUS_FACETIME_BACKEND=chert migrates existing dashboards (which still send
        # join_backend=facetime_web) onto the Chert FaceTime API without a client change.
        if join_backend == "facetime_web" and (
            os.getenv("MECHUS_FACETIME_BACKEND") or ""
        ).strip().lower() in ("chert", "facetime_chert"):
            log.info("join_backend upgraded facetime_web -> facetime_chert for meeting_id=%s", meeting_id)
            join_backend = "facetime_chert"
        # Dashboards often send join_backend=browser; on Linux with SDK available, join as bot.
        if join_backend == "browser":
            try:
                from mechus.zoom_sdk_join import is_zoom_link, zoom_sdk_available_reason

                if is_zoom_link(str(meeting_link)):
                    _zoom_ok, _zoom_reason = zoom_sdk_available_reason()
                    if _zoom_ok:
                        log.info(
                            "join_backend upgraded browser → zoom_sdk for Zoom link meeting_id=%s",
                            meeting_id,
                        )
                        join_backend = "zoom_sdk"
                    else:
                        log.warning(
                            "Zoom link but SDK unavailable (%s); keeping browser join",
                            _zoom_reason,
                        )
            except Exception as _zexc:  # noqa: BLE001
                log.debug("Zoom SDK upgrade check skipped: %s", _zexc)
        # Single-bot: request body wins; else server .env; else default-only Mechus.
        if data.get("bots") is None and not enabled_personas_csv:
            enabled_personas_csv = (
                (os.getenv("MECHUS_PERSONAS_ENABLED") or "").strip().lower() or "default"
            )
        # Per-request persona allow-list for in-process joins (browser/external). Container
        # workers get MECHUS_PERSONAS_ENABLED via worker_env below; apply after load_env_file so
        # the join body wins over stale values in the API process environment.
        if enabled_personas_csv:
            os.environ["MECHUS_PERSONAS_ENABLED"] = enabled_personas_csv
        config = MechusConfig.from_env()

        multi_bots = False
        bot_entries: list[dict[str, str]] = []
        if data.get("bots") is not None:
            if not isinstance(data.get("bots"), list):
                return jsonify({"success": False, "error": "bots must be a JSON array"}), 400
            if len(data["bots"]) == 0:
                return jsonify({"success": False, "error": "bots must be a non-empty array when provided"}), 400
            try:
                _reg = PersonaRegistry.load(config)
                bot_entries = _parse_bots_invite(data, _reg)
            except ValueError as exc:
                return jsonify({"success": False, "error": str(exc)}), 400
            multi_bots = True
            if join_backend not in ("zoom_sdk", "facetime_web"):
                return jsonify({
                    "success": False,
                    "error": 'Multi-bot invite (body "bots") requires join_backend "zoom_sdk" or "facetime_web".',
                }), 400

        # Vosk is required only for browser/external (mic wake). Zoom SDK and FaceTime use
        # transcript-STT wake and must not block join when the model path is missing on the host.
        if (
            join_requires_vosk_wake_model(join_backend)
            and config.wake_backend == "vosk"
            and config.vosk_model_path
            and not os.path.isdir(config.vosk_model_path)
        ):
            return jsonify({
                "success": False,
                "error": "Wake word model not found. One-time setup: run 'python3 scripts/download_vosk_model.py --out models' in the repo root, then try again.",
                "setup_command": "python3 scripts/download_vosk_model.py --out models",
            }), 503

        # Zoom SDK validation (must pass before we create the placeholder session)
        if join_backend == "zoom_sdk":
            from mechus.zoom_sdk_join import (
                is_zoom_link,
                zoom_sdk_available_reason,
                parse_zoom_link,
            )
            if not is_zoom_link(meeting_link):
                return jsonify({
                    "success": False,
                    "error": "Zoom SDK join requires a Zoom meeting link (zoom.us/j/...)."
                }), 400
            zoom_ok, zoom_reason = zoom_sdk_available_reason()
            if not zoom_ok:
                return jsonify({
                    "success": False,
                    "error": "Zoom SDK is not available in this environment (needs Linux + zoom-meeting-sdk).",
                    "detail": zoom_reason,
                }), 400
            meeting_number, password = parse_zoom_link(meeting_link)
            if meeting_number is None:
                return jsonify({
                    "success": False,
                    "error": "Could not parse Zoom meeting number from link."
                }), 400
            if not os.getenv("ZOOM_APP_CLIENT_ID") or not os.getenv("ZOOM_APP_CLIENT_SECRET"):
                return jsonify({
                    "success": False,
                    "error": "Zoom SDK requires ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET in .env (create a Meeting SDK app in Zoom Marketplace)."
                }), 400
            # Best-effort ZAK/OAuth for cross-account links (e.g. us05web). Same-account us04 joins
            # still work with JWT only when no token is available — never block the join here.
            from mechus.zoom_tokens import resolve_zoom_join_tokens, resolve_zoom_oauth_access_token

            _oauth = resolve_zoom_oauth_access_token(data)
            _join_user_id = (user_id or "").strip()
            if not _oauth or not zak_token:
                try:
                    from mechus.zoom_server_credentials import resolve_zoom_credentials_for_join

                    _creds = resolve_zoom_credentials_for_join(_join_user_id or None)
                    if _creds:
                        if _creds.access_token and not _oauth:
                            _oauth = _creds.access_token
                        if _creds.zak_token and not zak_token:
                            zak_token = _creds.zak_token
                        log.info(
                            "Zoom join (%s): loaded %s credentials for user %s",
                            meeting_id,
                            _creds.source or "zoom",
                            (_join_user_id or "server")[:8],
                        )
                except Exception as _zc_exc:
                    log.warning("Zoom credential lookup failed for %s: %s", _join_user_id[:8], _zc_exc)
            recording_token, zak_token, _zoom_warn, _zoom_err = resolve_zoom_join_tokens(
                meeting_number=int(meeting_number),
                oauth_access_token=_oauth,
                recording_token=recording_token,
                zak_token=zak_token,
            )
            for _w in _zoom_warn:
                log.warning("Zoom join (%s): %s", meeting_id, _w)
            log.info(
                "Zoom join (%s): oauth=%s zak=%s recording=%s",
                meeting_id,
                bool(_oauth),
                bool(zak_token),
                bool(recording_token),
            )

        # Chert FaceTime API validation: Chert places the call and bridges it into a LiveKit room.
        if join_backend == "facetime_chert":
            from mechus.chert_client import (
                ChertConfig,
                classify_destination,
                livekit_sdk_available_reason,
            )

            if multi_bots:
                return jsonify({
                    "success": False,
                    "error": 'Multi-bot invite (body "bots") is not supported with join_backend "facetime_chert" (one call per Chert line).',
                }), 400
            _chert_missing = ChertConfig.from_env().missing()
            if _chert_missing:
                return jsonify({
                    "success": False,
                    "error": "Chert FaceTime is not configured. Missing in .env: " + ", ".join(_chert_missing),
                    "missing_env": _chert_missing,
                }), 503
            _lk_ok, _lk_reason = livekit_sdk_available_reason()
            if not _lk_ok:
                return jsonify({
                    "success": False,
                    "error": "LiveKit SDK is not installed. Run: pip install -r requirements-chert.txt",
                    "detail": _lk_reason,
                }), 503
            try:
                classify_destination(str(meeting_link))
            except ValueError as _dest_exc:
                return jsonify({"success": False, "error": str(_dest_exc)}), 400
            existing_chert = _active_session_for_meeting_link(str(meeting_link).strip(), backend="facetime_chert")
            if existing_chert is not None:
                existing_mid, existing_session = existing_chert
                return jsonify({
                    "success": True,
                    "message": "Mechus is already calling or active for this FaceTime destination",
                    "meeting_id": existing_mid,
                    "meeting_link": existing_session.meeting_link,
                    "status": existing_session.status,
                    "deduped": True,
                }), 200

        # FaceTime web validation: join is via Chromium in a worker container (worker normalizes facetime:// → https).
        if join_backend == "facetime_web":
            ml = str(meeting_link or "").strip()
            low = ml.lower()
            if not (low.startswith("https://") or low.startswith("http://") or low.startswith("facetime://")):
                return jsonify({
                    "success": False,
                    "error": "FaceTime web join requires a FaceTime link (https://facetime.apple.com/... or facetime://...).",
                }), 400
            # Clean up workers left behind by an API restart before considering a new join.
            # Without this, the dashboard can show no active session while an invisible
            # Chromium bot keeps knocking/joining from an old request.
            _cleanup_orphan_facetime_workers(reason="pre_join")
            existing_ft = _active_session_for_meeting_link(ml, backend="facetime_web")
            if existing_ft is not None:
                existing_mid, existing_session = existing_ft
                return jsonify({
                    "success": True,
                    "message": "FaceTime bot is already joining or active for this link",
                    "meeting_id": existing_mid,
                    "meeting_link": existing_session.meeting_link,
                    "status": existing_session.status,
                    "deduped": True,
                }), 200
            # Pre-flight: verify the worker Docker image exists so we get a helpful 503 now
            # rather than a silent container-not-found error 30s into the join attempt.
            _ft_image = os.getenv("MECHUS_FACETIME_WORKER_IMAGE", "mechus-facetime-worker:latest").strip() or "mechus-facetime-worker:latest"
            try:
                _inspect_result = subprocess.run(
                    [_docker_bin(), "image", "inspect", _ft_image],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=8,
                )
                if _inspect_result.returncode != 0:
                    return jsonify({
                        "success": False,
                        "error": (
                            f"FaceTime worker image '{_ft_image}' not found on this host. "
                            "Build it first: docker build -t mechus-facetime-worker:latest "
                            "-f docker/facetime-worker/Dockerfile . "
                            "(or run ./run.sh deploy which builds automatically)."
                        ),
                    }), 503
            except (FileNotFoundError, subprocess.TimeoutExpired):
                # Docker CLI unavailable or timeout — proceed and let the join thread fail with details.
                pass
            except Exception:
                pass

        if _join_slot_busy(meeting_id):
            existing_link = None
            with _session_lock:
                ex = _active_sessions.get(meeting_id)
                if ex is not None:
                    existing_link = ex.meeting_link
                else:
                    for _mid, _sx in _active_sessions.items():
                        if getattr(_sx, "session_group_id", None) == meeting_id:
                            existing_link = _sx.meeting_link
                            break
            return jsonify({
                "success": False,
                "error": f"Meeting {meeting_id} is already active",
                "current_link": existing_link,
            }), 409

        group_id = meeting_id
        bot_specs: list[dict[str, Any]] = []
        if multi_bots:
            for b in bot_entries:
                suffix = uuid.uuid4().hex[:8]
                wkey = f"{group_id}__bot__{b['persona_id']}__{suffix}"
                wkey = re.sub(r"[^a-zA-Z0-9._-]+", "_", wkey)[:200]
                fl = _maybe_with_display_name(str(meeting_link), b["display_name"])
                bot_specs.append({**b, "worker_key": wkey, "formatted_link": fl})

        # Store session(s) immediately so status/stop work while join is in progress (join can take up to 120s).
        _dedup_existing: tuple[str, MechusSession] | None = None
        with _session_lock:
            # Close the check-then-register race: two rapid FaceTime join clicks can both
            # pass the earlier dedup check (2177) before either registers a session, which
            # spawns duplicate Chromium bots that all knock on the same link. Re-check the
            # link key while holding the lock and bail before registering a second session.
            if join_backend == "facetime_web" and not multi_bots:
                _dedup_existing = _find_active_session_for_link_locked(
                    _normalized_meeting_link_key(str(meeting_link or "")),
                    backend="facetime_web",
                )
            if _dedup_existing is not None:
                pass  # handled after the lock is released
            elif multi_bots:
                _session_group_children[group_id] = [s["worker_key"] for s in bot_specs]
                for spec in bot_specs:
                    _active_sessions[spec["worker_key"]] = MechusSession(
                        meeting_link=spec["formatted_link"],
                        user_id=user_id,
                        team_id=team_id,
                        shared_context_enabled=shared_context_enabled,
                        share_to_team_context=share_to_team_context,
                        agent=None,
                        thread=None,
                        started_at=time.time(),
                        status="starting",
                        last_activity=time.time(),
                        transcript_chunks=0,
                        browser_driver=None,
                        zoom_bot=None,
                        join_backend=join_backend,
                        participant_timezones=participant_timezones,
                        participant_emails=participant_emails,
                        summary_email_recipients=summary_email_recipients or None,
                        collection_recipients=collection_recipients or None,
                        invite_collection_link=invite_collection_link,
                        session_group_id=group_id,
                        bot_persona_id=spec["persona_id"],
                        bot_display_name=spec.get("display_name"),
                        facetime_camera_requested=camera_requested,
                        facetime_camera_required=camera_required,
                        facetime_camera_mode=camera_mode,
                        facetime_vision_requested=vision_requested,
                    )
            else:
                _active_sessions[meeting_id] = MechusSession(
                    meeting_link=formatted_link,
                    user_id=user_id,
                    team_id=team_id,
                    shared_context_enabled=shared_context_enabled,
                    share_to_team_context=share_to_team_context,
                    agent=None,
                    thread=None,
                    started_at=time.time(),
                    status="starting",
                    last_activity=time.time(),
                    transcript_chunks=0,
                    browser_driver=None,
                    zoom_bot=None,
                    join_backend=join_backend,
                    participant_timezones=participant_timezones,
                    participant_emails=participant_emails,
                    summary_email_recipients=summary_email_recipients or None,
                    collection_recipients=collection_recipients or None,
                    invite_collection_link=invite_collection_link,
                    facetime_camera_requested=camera_requested,
                    facetime_camera_required=camera_required,
                    facetime_camera_mode=camera_mode,
                    facetime_vision_requested=vision_requested,
                )

        # A concurrent FaceTime join for the same link won the race: reuse it instead
        # of spawning a duplicate bot.
        if _dedup_existing is not None:
            existing_mid, existing_session = _dedup_existing
            return jsonify({
                "success": True,
                "message": "FaceTime bot is already joining or active for this link",
                "meeting_id": existing_mid,
                "meeting_link": existing_session.meeting_link,
                "status": existing_session.status,
                "deduped": True,
            }), 200

        def do_multi_join(
            _bot_specs=bot_specs,
            _group_id=group_id,
            _meeting_link=meeting_link,
            _join_backend=join_backend,
            _recording_token=recording_token,
            _zak_token=zak_token,
            _participant_timezones=participant_timezones,
            _participant_emails=participant_emails,
            _summary_email_recipients=summary_email_recipients,
            _collection_recipients=collection_recipients,
            _invite_collection_link=invite_collection_link,
            _team_id=team_id,
            _shared_context_enabled=shared_context_enabled,
        ):
            nonlocal config, user_id
            log.info(
                "Multi-bot join thread started group_id=%s bots=%d backend=%s",
                _group_id,
                len(_bot_specs),
                _join_backend,
            )
            sys.stderr.flush()
            for spec in _bot_specs:
                wk = spec["worker_key"]
                dname = spec["display_name"]
                flink = spec["formatted_link"]
                persona_id = spec["persona_id"]
                try:
                    if _join_backend == "facetime_web":
                        env_file = os.getenv("MECHUS_ENV_FILE", ".env")
                        env_file_abs = os.path.abspath(env_file)
                        notes_in_container = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                        os.makedirs(notes_in_container, exist_ok=True)
                        notes_host = (os.getenv("MECHUS_NOTES_HOST_PATH") or "").strip()
                        vol_src = notes_host if notes_host else notes_in_container
                        if not notes_host:
                            log.warning(
                                "MECHUS_NOTES_HOST_PATH unset; using %s for worker docker -v (often wrong for "
                                "docker-out-of-docker — set to the host's absolute notes directory).",
                                vol_src,
                            )
                        image = os.getenv("MECHUS_FACETIME_WORKER_IMAGE", "mechus-facetime-worker:latest").strip() or "mechus-facetime-worker:latest"
                        container_name = f"mechus-facetime-{wk}"
                        container_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", container_name)[:120]
                        try:
                            _ls = subprocess.run(
                                [_docker_bin(), "ps", "-a", "--filter", "name=mechus-facetime-",
                                 "--format", "{{.Names}}\t{{.Status}}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            )
                            _live_names = set()
                            with _session_lock:
                                for _mid, _sess in _active_sessions.items():
                                    _wc = getattr(_sess, "worker_container", "") or ""
                                    if _wc:
                                        _live_names.add(_wc)
                            _stale: list[tuple[str, str]] = []
                            for _ln in (_ls.stdout or "").splitlines():
                                _parts = _ln.split("\t")
                                if len(_parts) < 2:
                                    continue
                                _cn, _st = _parts[0].strip(), _parts[1].strip()
                                if not _cn or _cn == container_name:
                                    continue
                                if _cn in _live_names:
                                    continue
                                _stale.append((_cn, _st))
                            if _stale:
                                for _cn, _ in _stale:
                                    try:
                                        subprocess.run(
                                            [_docker_bin(), "rm", "-f", _cn],
                                            check=False,
                                            stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL,
                                            timeout=15,
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        from mechus.facetime_env import (
                            PRODUCTION_WAKE_PHRASE,
                            facetime_worker_eleven_overrides,
                            facetime_worker_runtime_overrides,
                        )

                        _worker_env_overrides = {
                            "MECHUS_NOTES_DIR": "/notes",
                            "MECHUS_MEETING_ID": wk,
                            "MECHUS_MEETING_LINK": _meeting_link,
                            "MECHUS_DISPLAY_NAME": dname,
                            "MECHUS_JOIN_BACKEND": "facetime_web",
                            "MECHUS_BOT_PERSONA_ID": persona_id,
                            "MECHUS_WAKE_PHRASE": PRODUCTION_WAKE_PHRASE,
                            **facetime_worker_eleven_overrides(),
                            "MECHUS_TTS_OUTPUT_GAIN": "1.05",
                            # Stop-directive-only barge-in: echo-safe on mixed audio.
                            **facetime_worker_runtime_overrides(),
                            "MECHUS_FT_CAMERA_ENABLED": "1" if camera_requested else "0",
                            "MECHUS_FT_CAMERA_REQUIRED": "1" if camera_required else "0",
                            "MECHUS_FT_CAMERA_MODE": camera_mode,
                            "MECHUS_FT_VISION_ENABLED": "1" if vision_requested else "0",
                        }
                        if _team_id:
                            _worker_env_overrides["MECHUS_TEAM_ID"] = _team_id
                            _worker_env_overrides["MECHUS_SHARED_CONTEXT_ENABLED"] = "1" if _shared_context_enabled else "0"
                            _worker_env_overrides.setdefault("MECHUS_TEAM_STORE_DIR", "/notes/team_context")
                        if _participant_timezones:
                            _worker_env_overrides["MECHUS_PARTICIPANT_TIMEZONES_JSON"] = json.dumps(_participant_timezones)
                        if _participant_emails:
                            _worker_env_overrides["MECHUS_PARTICIPANT_EMAILS_JSON"] = json.dumps(_participant_emails)
                        if _summary_email_recipients:
                            _worker_env_overrides["MECHUS_SUMMARY_RECIPIENTS_JSON"] = json.dumps(_summary_email_recipients)
                        if _collection_recipients:
                            _worker_env_overrides["MECHUS_COLLECTION_RECIPIENTS_JSON"] = json.dumps(_collection_recipients)
                        if _invite_collection_link:
                            _worker_env_overrides["MECHUS_INVITE_EMAIL_COLLECTION_LINK"] = _invite_collection_link
                        if _memory_meeting_ids is not None:
                            _worker_env_overrides["MECHUS_MEMORY_MEETING_IDS_JSON"] = json.dumps(_memory_meeting_ids)
                        _camera_env, _camera_docker_args, _camera_lease = _facetime_camera_launch_args(
                            wk,
                            container_name,
                            provision=camera_provision,
                            required=camera_required,
                        )
                        _worker_env_overrides.update(_camera_env)
                        docker_cmd = [
                            _docker_bin(), "run",
                            "--name", container_name,
                            *_camera_docker_args,
                            "-v", f"{vol_src}:/notes",
                        ]
                        if os.path.exists(env_file_abs):
                            _append_docker_env(docker_cmd, env_file_abs, _worker_env_overrides)
                        else:
                            _skip = set(_worker_env_overrides)
                            for _k, _v in os.environ.items():
                                if _k in _skip:
                                    continue
                                if _k.startswith("MECHUS_") or _k in (
                                    "ZOOM_APP_CLIENT_ID", "ZOOM_APP_CLIENT_SECRET",
                                ):
                                    docker_cmd.extend(["-e", f"{_k}={_v}"])
                            for _k, _v in _worker_env_overrides.items():
                                docker_cmd.extend(["-e", f"{_k}={_v}"])
                        docker_cmd.extend(["-d", image])
                        try:
                            _out = subprocess.check_output(docker_cmd, stderr=subprocess.STDOUT, text=True).strip()
                            log.info(
                                "FaceTime API (multi-bot): meeting_id=%s persona=%s container_id=%s",
                                wk,
                                persona_id,
                                (_out or "")[:80],
                            )
                        except FileNotFoundError as exc:
                            _release_facetime_camera_lease(wk)
                            raise RuntimeError(
                                "Docker CLI not found inside the API container. "
                                "Rebuild the `mechus-zoom` image (it installs /usr/local/bin/docker) and ensure "
                                "`docker-compose.zoom.yml` mounts /var/run/docker.sock + sets DOCKER_GID."
                            ) from exc
                        except subprocess.CalledProcessError as exc:
                            _release_facetime_camera_lease(wk)
                            msg = (exc.output or "").strip()[-1200:]
                            raise RuntimeError(
                                "Failed to start FaceTime worker container. "
                                f"Details: {msg}"
                            ) from exc
                        with _session_lock:
                            if wk not in _active_sessions:
                                log.warning(
                                    "FaceTime docker started but session meeting_id=%s is gone; removing orphan %s",
                                    wk,
                                    container_name,
                                )
                                subprocess.run([_docker_bin(), "rm", "-f", container_name], check=False)
                                _release_facetime_camera_lease(wk)
                                continue
                            _active_sessions[wk].worker_container = container_name
                            _active_sessions[wk].status = "joining"
                            _active_sessions[wk].facetime_camera_device = (
                                _camera_lease.device if _camera_lease is not None else ""
                            )

                        def _watch_container(mid=wk, cname=container_name, ndir=notes_in_container):
                            while True:
                                try:
                                    rc = subprocess.run(
                                        [_docker_bin(), "inspect", "-f", "{{.State.Running}}", cname],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        text=True,
                                    )
                                    if rc.returncode != 0:
                                        running = "false"
                                    else:
                                        running = (rc.stdout or "").strip().lower()
                                    if running != "true":
                                        break
                                except Exception:
                                    break
                                time.sleep(2)
                            try:
                                log_path = os.path.join(ndir, f"facetime-{mid}-container.log")
                                logs_out = subprocess.run(
                                    [_docker_bin(), "logs", "--tail=2000", cname],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True,
                                )
                                with open(log_path, "w", encoding="utf-8") as _lf:
                                    _lf.write(logs_out.stdout or "")
                            except Exception:
                                pass
                            try:
                                subprocess.run([_docker_bin(), "rm", "-f", cname], check=False, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass
                            _release_facetime_camera_lease(mid)
                            with _session_lock:
                                if mid in _active_sessions:
                                    sess = _active_sessions[mid]
                                    if sess.status not in ("stopped", "stopping"):
                                        sess.status = "stopped"
                                _prune_session_group_child(_group_id, mid)

                        threading.Thread(target=_watch_container, daemon=True).start()
                    elif _join_backend == "zoom_sdk":
                        from mechus.facetime_env import (
                            apply_zoom_worker_env_defaults,
                            production_latency_env_dict,
                        )

                        from mechus.notes_paths import apply_notes_paths_to_env

                        worker_env = os.environ.copy()
                        apply_notes_paths_to_env(worker_env)
                        apply_zoom_worker_env_defaults(worker_env)
                        worker_env.update(production_latency_env_dict(worker_env))
                        from mechus.facetime_env import apply_zoom_tts_clarity_defaults

                        apply_zoom_tts_clarity_defaults(worker_env)
                        from mechus.facetime_env import apply_zoom_meeting_interaction_defaults

                        apply_zoom_meeting_interaction_defaults(worker_env)
                        worker_env["MECHUS_JOIN_BACKEND"] = "zoom_sdk"
                        worker_env["MECHUS_MEETING_ID"] = wk
                        worker_env["MECHUS_MEETING_LINK"] = _meeting_link
                        worker_env["MECHUS_DISPLAY_NAME"] = dname
                        worker_env["MECHUS_BOT_PERSONA_ID"] = persona_id
                        worker_env["MECHUS_ELEVEN_TTS_LATENCY_OPT"] = worker_env.get(
                            "MECHUS_ELEVEN_TTS_LATENCY_OPT", "2"
                        )
                        if _team_id:
                            worker_env["MECHUS_TEAM_ID"] = _team_id
                            worker_env["MECHUS_SHARED_CONTEXT_ENABLED"] = "1" if _shared_context_enabled else "0"
                        if _recording_token:
                            worker_env["MECHUS_RECORDING_TOKEN"] = _recording_token
                        if _zak_token:
                            worker_env["MECHUS_ZAK_TOKEN"] = _zak_token
                        if _participant_timezones:
                            worker_env["MECHUS_PARTICIPANT_TIMEZONES_JSON"] = json.dumps(_participant_timezones)
                        if _participant_emails:
                            worker_env["MECHUS_PARTICIPANT_EMAILS_JSON"] = json.dumps(_participant_emails)
                        if _summary_email_recipients:
                            worker_env["MECHUS_SUMMARY_RECIPIENTS_JSON"] = json.dumps(_summary_email_recipients)
                        if _collection_recipients:
                            worker_env["MECHUS_COLLECTION_RECIPIENTS_JSON"] = json.dumps(_collection_recipients)
                        if _invite_collection_link:
                            worker_env["MECHUS_INVITE_EMAIL_COLLECTION_LINK"] = _invite_collection_link
                        if _memory_meeting_ids is not None:
                            worker_env["MECHUS_MEMORY_MEETING_IDS_JSON"] = json.dumps(_memory_meeting_ids)
                        worker_proc = subprocess.Popen(
                            [sys.executable, "-m", "mechus.worker"],
                            env=worker_env,
                            stdout=sys.stdout,
                            stderr=sys.stderr,
                        )
                        log.info("Multi-bot Zoom worker started pid=%s meeting_id=%s persona=%s", worker_proc.pid, wk, persona_id)
                        with _session_lock:
                            if wk in _active_sessions:
                                _active_sessions[wk].worker_proc = worker_proc
                                _active_sessions[wk].status = "joining"

                        def _watch_worker(proc=worker_proc, mid=wk, gid=_group_id):
                            proc.wait()
                            with _session_lock:
                                if mid in _active_sessions:
                                    sess = _active_sessions[mid]
                                    if sess.status not in ("stopped", "stopping"):
                                        sess.status = "stopped" if proc.returncode == 0 else "error"
                                _prune_session_group_child(gid, mid)
                            log.info("Multi-bot worker exited: pid=%s rc=%s meeting_id=%s", proc.pid, proc.returncode, mid)

                        threading.Thread(target=_watch_worker, daemon=True).start()
                except Exception as e:
                    log.error("Multi-bot join error meeting_id=%s persona=%s: %s", wk, persona_id, e, exc_info=True)
                    with _session_lock:
                        if wk in _active_sessions:
                            _active_sessions[wk].status = "error"
                        _prune_session_group_child(_group_id, wk)

        def do_join(
            _meeting_link=meeting_link,
            _display_name=display_name,
            _formatted_link=formatted_link,
            _meeting_id=meeting_id,
            _join_backend=join_backend,
            _recording_token=recording_token,
            _zak_token=zak_token,
            _participant_timezones=participant_timezones,
            _participant_emails=participant_emails,
            _summary_email_recipients=summary_email_recipients,
            _collection_recipients=collection_recipients,
            _invite_collection_link=invite_collection_link,
            _team_id=team_id,
            _shared_context_enabled=shared_context_enabled,
            _memory_meeting_ids=memory_meeting_ids,
        ):
            nonlocal config, user_id
            browser_driver = None
            zoom_bot = None
            agent = None
            log.info("Join thread started for meeting_id=%s backend=%s", _meeting_id, _join_backend)
            sys.stderr.flush()
            try:
                if _join_backend == "facetime_web":
                    # Spawn a dedicated docker worker container so each meeting gets its own
                    # browser + PulseAudio graph, enabling true concurrent meetings.
                    env_file = os.getenv("MECHUS_ENV_FILE", ".env")
                    env_file_abs = os.path.abspath(env_file)
                    notes_in_container = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                    os.makedirs(notes_in_container, exist_ok=True)
                    # Bind-mount source is interpreted by the host Docker daemon, not this container's
                    # filesystem. Use MECHUS_NOTES_HOST_PATH when set (see docker-compose.zoom.yml + restart script).
                    notes_host = (os.getenv("MECHUS_NOTES_HOST_PATH") or "").strip()
                    vol_src = notes_host if notes_host else notes_in_container
                    if not notes_host:
                        log.warning(
                            "MECHUS_NOTES_HOST_PATH unset; using %s for worker docker -v (often wrong for "
                            "docker-out-of-docker — set to the host's absolute notes directory).",
                            vol_src,
                        )

                    image = os.getenv("MECHUS_FACETIME_WORKER_IMAGE", "mechus-facetime-worker:latest").strip() or "mechus-facetime-worker:latest"
                    container_name = f"mechus-facetime-{_meeting_id}"
                    # Avoid collisions if user supplies meeting_id multiple times.
                    container_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", container_name)[:120]

                    # Stale-worker cleanup: when the host ends a FaceTime call from their iPhone,
                    # nothing tells the worker container to die — it stays alive holding
                    # mechus_mic_source / Chromium / Vosk in memory.  On AWS t3.medium-ish boxes,
                    # piling up 2-3 of these silently OOM-kills the whole stack and the *new*
                    # worker either never finishes booting or gets killed mid-join.  Auto-remove
                    # any leftover mechus-facetime-* containers from previous sessions before the
                    # new worker starts.  We only remove containers NOT tracked in _active_sessions
                    # so live, in-flight meetings are never disturbed.
                    try:
                        _ls = subprocess.run(
                            [_docker_bin(), "ps", "-a", "--filter", "name=mechus-facetime-",
                             "--format", "{{.Names}}\t{{.Status}}"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        )
                        _live_names = set()
                        with _session_lock:
                            for _mid, _sess in _active_sessions.items():
                                _wc = getattr(_sess, "worker_container", "") or ""
                                if _wc:
                                    _live_names.add(_wc)
                        _stale: list[tuple[str, str]] = []
                        for _ln in (_ls.stdout or "").splitlines():
                            _parts = _ln.split("\t")
                            if len(_parts) < 2:
                                continue
                            _cn, _st = _parts[0].strip(), _parts[1].strip()
                            if not _cn or _cn == container_name:
                                continue
                            if _cn in _live_names:
                                continue
                            _stale.append((_cn, _st))
                        if _stale:
                            log.warning(
                                "FaceTime API: [step=stale_cleanup] meeting_id=%s found %d stale "
                                "FaceTime worker container(s) from prior call(s); removing: %s",
                                _meeting_id,
                                len(_stale),
                                ", ".join("%s(%s)" % (c, s[:30]) for c, s in _stale),
                            )
                            for _cn, _ in _stale:
                                try:
                                    subprocess.run(
                                        [_docker_bin(), "rm", "-f", _cn],
                                        check=False,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        timeout=15,
                                    )
                                except Exception as _re:
                                    log.warning(
                                        "Could not remove stale FaceTime container %s: %s",
                                        _cn, _re,
                                    )
                    except Exception as _se:
                        log.warning("Stale FaceTime container scan failed: %s", _se)

                    try:
                        _link_host = (urlparse(str(_meeting_link)).hostname or "").lower()
                    except Exception:
                        _link_host = ""
                    log.info(
                        "FaceTime API: [step=docker_prepare] meeting_id=%s container=%s image=%s "
                        "notes_vol=%s env_file=%s link_host=%s display_name_len=%s",
                        _meeting_id,
                        container_name,
                        image,
                        vol_src,
                        env_file_abs if os.path.exists(env_file_abs) else "(none)",
                        _link_host,
                        len(str(_display_name or "")),
                    )

                    # Per-worker overrides — set first so env-file cannot clobber them.
                    from mechus.facetime_env import (
                        PRODUCTION_WAKE_PHRASE,
                        facetime_worker_eleven_overrides,
                        facetime_worker_runtime_overrides,
                    )

                    _worker_env_overrides = {
                        "MECHUS_NOTES_DIR": "/notes",
                        "MECHUS_MEETING_ID": _meeting_id,
                        "MECHUS_MEETING_LINK": _meeting_link,
                        "MECHUS_DISPLAY_NAME": _display_name,
                        # Ensure the worker identifies itself as facetime_web so agent.py
                        # uses the correct wake/audio mode and does not inherit zoom_sdk from host .env.
                        "MECHUS_JOIN_BACKEND": "facetime_web",
                        "MECHUS_WAKE_PHRASE": PRODUCTION_WAKE_PHRASE,
                        **facetime_worker_eleven_overrides(),
                        "MECHUS_TTS_OUTPUT_GAIN": "1.05",
                        **facetime_worker_runtime_overrides(),
                        "MECHUS_FT_CAMERA_ENABLED": "1" if camera_requested else "0",
                        "MECHUS_FT_CAMERA_REQUIRED": "1" if camera_required else "0",
                        "MECHUS_FT_CAMERA_MODE": camera_mode,
                        "MECHUS_FT_VISION_ENABLED": "1" if vision_requested else "0",
                    }
                    if _team_id:
                        _worker_env_overrides["MECHUS_TEAM_ID"] = _team_id
                        _worker_env_overrides["MECHUS_SHARED_CONTEXT_ENABLED"] = "1" if _shared_context_enabled else "0"
                        _worker_env_overrides.setdefault("MECHUS_TEAM_STORE_DIR", "/notes/team_context")
                    _worker_env_overrides["MECHUS_PERSONAS_ENABLED"] = enabled_personas_csv or "default"
                    if _participant_timezones:
                        _worker_env_overrides["MECHUS_PARTICIPANT_TIMEZONES_JSON"] = json.dumps(_participant_timezones)
                    if _participant_emails:
                        _worker_env_overrides["MECHUS_PARTICIPANT_EMAILS_JSON"] = json.dumps(_participant_emails)
                    if _summary_email_recipients:
                        _worker_env_overrides["MECHUS_SUMMARY_RECIPIENTS_JSON"] = json.dumps(_summary_email_recipients)
                    if _collection_recipients:
                        _worker_env_overrides["MECHUS_COLLECTION_RECIPIENTS_JSON"] = json.dumps(_collection_recipients)
                    if _invite_collection_link:
                        _worker_env_overrides["MECHUS_INVITE_EMAIL_COLLECTION_LINK"] = _invite_collection_link
                    _camera_env, _camera_docker_args, _camera_lease = _facetime_camera_launch_args(
                        _meeting_id,
                        container_name,
                        provision=camera_provision,
                        required=camera_required,
                    )
                    _worker_env_overrides.update(_camera_env)
                    docker_cmd = [
                        _docker_bin(), "run",
                        "--name", container_name,
                        *_camera_docker_args,
                        # Share notes dir so /api/status and /api/stop can read worker-status files.
                        "-v", f"{vol_src}:/notes",
                    ]

                    docker_cmd.extend(
                        _facetime_worker_docker_env_flags(
                            env_file_abs, _worker_env_overrides, os.environ
                        )
                    )

                    # Run detached and let the API server monitor via docker inspect/logs + worker-status json.
                    docker_cmd.extend(["-d", image])

                    try:
                        out = subprocess.check_output(docker_cmd, stderr=subprocess.STDOUT, text=True).strip()
                        try:
                            _host = (urlparse(str(_meeting_link)).hostname or "").lower()
                        except Exception:
                            _host = ""
                        log.info(
                            "FaceTime API: [step=docker_started] meeting_id=%s container_id=%s link_host=%s",
                            _meeting_id,
                            (out or "")[:80],
                            _host,
                        )
                    except FileNotFoundError as exc:
                        _release_facetime_camera_lease(_meeting_id)
                        raise RuntimeError(
                            "Docker CLI not found inside the API container. "
                            "Rebuild the `mechus-zoom` image (it installs /usr/local/bin/docker) and ensure "
                            "`docker-compose.zoom.yml` mounts /var/run/docker.sock + sets DOCKER_GID."
                        ) from exc
                    except subprocess.CalledProcessError as exc:
                        _release_facetime_camera_lease(_meeting_id)
                        msg = (exc.output or "").strip()[-1200:]
                        hint = ""
                        if "permission denied" in msg.lower() and "docker.sock" in msg.lower():
                            hint = (
                                " If this mentions permission denied on /var/run/docker.sock, set DOCKER_GID "
                                "to `stat -c '%g' /var/run/docker.sock` for the API container (see docker-compose.zoom.yml)."
                            )
                        elif "unable to find image" in msg.lower() or "pull access denied" in msg.lower():
                            hint = (
                                " Build the worker image on the Docker host: "
                                "docker build -t mechus-facetime-worker:latest -f docker/facetime-worker/Dockerfile ."
                                " (scripts/restart-on-server.sh does this on deploy.)"
                            )
                        raise RuntimeError(
                            "Failed to start FaceTime worker container. Ensure docker is installed and the API "
                            f"container can access /var/run/docker.sock.{hint} Details: {msg}"
                        )

                    with _session_lock:
                        if _meeting_id not in _active_sessions:
                            log.warning(
                                "FaceTime docker started but session meeting_id=%s is gone (client stop/cancel); "
                                "removing orphan container %s",
                                _meeting_id,
                                container_name,
                            )
                            try:
                                subprocess.run([_docker_bin(), "rm", "-f", container_name], check=False)
                            except Exception:
                                pass
                            _release_facetime_camera_lease(_meeting_id)
                            return
                        _active_sessions[_meeting_id].worker_container = container_name
                        _active_sessions[_meeting_id].status = "joining"
                        _active_sessions[_meeting_id].facetime_camera_device = (
                            _camera_lease.device if _camera_lease is not None else ""
                        )
                    log.info(
                        "FaceTime API: [step=session_attached] meeting_id=%s worker_container=%s status=joining",
                        _meeting_id,
                        container_name,
                    )

                    def _watch_container(mid=_meeting_id, cname=container_name, ndir=notes_in_container):
                        # Poll until container exits; then dump logs, update status, and rm the container.
                        while True:
                            try:
                                rc = subprocess.run(
                                    [_docker_bin(), "inspect", "-f", "{{.State.Running}}", cname],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    text=True,
                                )
                                if rc.returncode != 0:
                                    running = "false"
                                else:
                                    running = (rc.stdout or "").strip().lower()
                                if running != "true":
                                    break
                            except Exception:
                                break
                            time.sleep(2)
                        # Container has exited — save full logs to the notes dir before removing.
                        try:
                            log_path = os.path.join(ndir, f"facetime-{mid}-container.log")
                            logs_out = subprocess.run(
                                [_docker_bin(), "logs", "--tail=2000", cname],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True,
                            )
                            with open(log_path, "w", encoding="utf-8") as _lf:
                                _lf.write(logs_out.stdout or "")
                            log.info(
                                "FaceTime worker logs saved: container=%s log_path=%s", cname, log_path
                            )
                        except Exception as _le:
                            log.warning("Could not save FaceTime worker logs: %s", _le)
                        # Remove the stopped container so docker ps stays clean.
                        try:
                            subprocess.run([_docker_bin(), "rm", "-f", cname], check=False, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                        _release_facetime_camera_lease(mid)
                        with _session_lock:
                            if mid in _active_sessions:
                                sess = _active_sessions[mid]
                                if sess.status not in ("stopped", "stopping"):
                                    sess.status = "stopped"
                        log.info("FaceTime worker container exited: container=%s meeting_id=%s", cname, mid)

                    threading.Thread(target=_watch_container, daemon=True).start()
                    return
                if _join_backend == "zoom_sdk":
                    # Spawn a dedicated worker subprocess so each meeting gets its own
                    # Zoom SDK C++ instance, enabling true concurrent meetings.
                    from mechus.facetime_env import (
                        apply_zoom_worker_env_defaults,
                        production_latency_env_dict,
                    )

                    from mechus.notes_paths import apply_notes_paths_to_env

                    worker_env = os.environ.copy()
                    apply_notes_paths_to_env(worker_env)
                    apply_zoom_worker_env_defaults(worker_env)
                    worker_env.update(production_latency_env_dict(worker_env))
                    from mechus.facetime_env import apply_zoom_tts_clarity_defaults

                    apply_zoom_tts_clarity_defaults(worker_env)
                    from mechus.facetime_env import apply_zoom_meeting_interaction_defaults

                    apply_zoom_meeting_interaction_defaults(worker_env)
                    worker_env["MECHUS_JOIN_BACKEND"] = "zoom_sdk"
                    worker_env["MECHUS_MEETING_ID"] = _meeting_id
                    worker_env["MECHUS_MEETING_LINK"] = _meeting_link
                    worker_env["MECHUS_DISPLAY_NAME"] = _display_name
                    worker_env["MECHUS_ELEVEN_TTS_LATENCY_OPT"] = worker_env.get(
                        "MECHUS_ELEVEN_TTS_LATENCY_OPT", "2"
                    )
                    if _team_id:
                        worker_env["MECHUS_TEAM_ID"] = _team_id
                        worker_env["MECHUS_SHARED_CONTEXT_ENABLED"] = "1" if _shared_context_enabled else "0"
                    worker_env["MECHUS_PERSONAS_ENABLED"] = enabled_personas_csv or "default"
                    if _recording_token:
                        worker_env["MECHUS_RECORDING_TOKEN"] = _recording_token
                    if _zak_token:
                        worker_env["MECHUS_ZAK_TOKEN"] = _zak_token
                    if _participant_timezones:
                        worker_env["MECHUS_PARTICIPANT_TIMEZONES_JSON"] = json.dumps(_participant_timezones)
                    if _participant_emails:
                        worker_env["MECHUS_PARTICIPANT_EMAILS_JSON"] = json.dumps(_participant_emails)
                    if _summary_email_recipients:
                        worker_env["MECHUS_SUMMARY_RECIPIENTS_JSON"] = json.dumps(_summary_email_recipients)
                    if _collection_recipients:
                        worker_env["MECHUS_COLLECTION_RECIPIENTS_JSON"] = json.dumps(_collection_recipients)
                    if _invite_collection_link:
                        worker_env["MECHUS_INVITE_EMAIL_COLLECTION_LINK"] = _invite_collection_link
                    worker_proc = subprocess.Popen(
                        [sys.executable, "-m", "mechus.worker"],
                        env=worker_env,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                    )
                    log.info(
                        "Zoom SDK worker subprocess started: pid=%s meeting_id=%s",
                        worker_proc.pid, _meeting_id,
                    )
                elif _join_backend == "facetime_chert":
                    # Chert FaceTime API: a plain subprocess (no Chromium/PulseAudio container).
                    # The worker joins a LiveKit room, asks Chert to place the call, and runs
                    # MechusAgent on the room's audio. Voice-agent env tuning happens in the worker.
                    from mechus.notes_paths import apply_notes_paths_to_env

                    worker_env = os.environ.copy()
                    apply_notes_paths_to_env(worker_env)
                    worker_env["MECHUS_JOIN_BACKEND"] = "facetime_chert"
                    _chert_opts = _CHERT_CALL_OPTIONS.pop(str(_meeting_id), {})
                    if _chert_opts.get("video"):
                        worker_env["MECHUS_CHERT_VIDEO"] = "1"
                    if _chert_opts.get("inspect_mode"):
                        worker_env["MECHUS_INSPECT_MODE"] = _chert_opts["inspect_mode"]
                    if _chert_opts.get("inspect_unit"):
                        worker_env["MECHUS_INSPECT_UNIT"] = _chert_opts["inspect_unit"]
                    worker_env["MECHUS_MEETING_ID"] = _meeting_id
                    # Raw destination (FaceTime link / phone / email): Chert needs the bare
                    # link, without the display-name prefill the browser join appends.
                    worker_env["MECHUS_MEETING_LINK"] = str(_meeting_link).strip()
                    worker_env["MECHUS_DISPLAY_NAME"] = _display_name
                    worker_env["MECHUS_PERSONAS_ENABLED"] = enabled_personas_csv or "default"
                    if _team_id:
                        worker_env["MECHUS_TEAM_ID"] = _team_id
                        worker_env["MECHUS_SHARED_CONTEXT_ENABLED"] = "1" if _shared_context_enabled else "0"
                    if _participant_timezones:
                        worker_env["MECHUS_PARTICIPANT_TIMEZONES_JSON"] = json.dumps(_participant_timezones)
                    if _participant_emails:
                        worker_env["MECHUS_PARTICIPANT_EMAILS_JSON"] = json.dumps(_participant_emails)
                    if _summary_email_recipients:
                        worker_env["MECHUS_SUMMARY_RECIPIENTS_JSON"] = json.dumps(_summary_email_recipients)
                    if _collection_recipients:
                        worker_env["MECHUS_COLLECTION_RECIPIENTS_JSON"] = json.dumps(_collection_recipients)
                    if _invite_collection_link:
                        worker_env["MECHUS_INVITE_EMAIL_COLLECTION_LINK"] = _invite_collection_link
                    if _memory_meeting_ids is not None:
                        worker_env["MECHUS_MEMORY_MEETING_IDS_JSON"] = json.dumps(_memory_meeting_ids)
                    worker_proc = subprocess.Popen(
                        [sys.executable, "-m", "mechus.chert_worker"],
                        env=worker_env,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                    )
                    log.info(
                        "Chert FaceTime worker subprocess started: pid=%s meeting_id=%s",
                        worker_proc.pid, _meeting_id,
                    )
                else:
                    worker_proc = None

                if worker_proc is not None:
                    with _session_lock:
                        if _meeting_id in _active_sessions:
                            _active_sessions[_meeting_id].worker_proc = worker_proc
                            _active_sessions[_meeting_id].status = "joining"

                    def _watch_worker(proc=worker_proc, mid=_meeting_id):
                        proc.wait()
                        with _session_lock:
                            if mid in _active_sessions:
                                sess = _active_sessions[mid]
                                if sess.status not in ("stopped", "stopping"):
                                    sess.status = "stopped" if proc.returncode == 0 else "error"
                        log.info("Worker process exited: pid=%s rc=%s meeting_id=%s", proc.pid, proc.returncode, mid)

                    threading.Thread(target=_watch_worker, daemon=True).start()
                    return
                elif _join_backend == "browser":
                    os.environ["MECHUS_JOIN_BACKEND"] = "browser"
                    agent = MechusAgent(config)
                    if _memory_meeting_ids is not None:
                        agent._pinned_memory_ids = _memory_meeting_ids
                    agent.set_meeting(_meeting_id)
                    if _team_id and _shared_context_enabled:
                        agent.set_team_context(_team_id)
                    if _participant_timezones:
                        agent.merge_participant_timezones(_participant_timezones)
                    log.info("Opening meeting link in browser (URL redacted for privacy)")
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug("Meeting link: %s", _formatted_link)
                    browser_driver = _open_and_join_meeting(_formatted_link)
                    if _is_google_meet_link(_formatted_link):
                        _start_google_meet_context_poller(
                            meeting_id=_meeting_id,
                            driver=browser_driver,
                            agent=agent,
                            display_name=str(_display_name or "Mechus"),
                        )
                    time.sleep(2)
                    show_browser = os.getenv("MECHUS_SHOW_BROWSER", "false").lower() in ("true", "1", "yes")
                    if show_browser:
                        log.info("IMPORTANT: Please click 'Join' in the browser window that opened (or it may auto-join).")
                    log.info("Ensure Zoom/Meet audio settings are: Microphone = BlackHole 2ch (or your input), Speaker = Multi-Output (or your output).")
                else:
                    agent = MechusAgent(config)
                    if _memory_meeting_ids is not None:
                        agent._pinned_memory_ids = _memory_meeting_ids
                    agent.set_meeting(_meeting_id)
                    if _team_id and _shared_context_enabled:
                        agent.set_team_context(_team_id)
                    if _participant_timezones:
                        agent.merge_participant_timezones(_participant_timezones)
                    log.info("Join backend is 'external'; not opening browser. Route meeting audio to Mechus's input device (see docs/SCALABLE_JOIN.md).")
                
                agent_thread = threading.Thread(
                    target=_run_agent,
                    args=(agent, _meeting_id),
                    daemon=True,
                    name=f"MechusAgent-{_meeting_id}"
                )
                agent_thread.start()
                
                with _session_lock:
                    if _meeting_id in _active_sessions:
                        s = _active_sessions[_meeting_id]
                        s.agent = agent
                        s.thread = agent_thread
                        s.browser_driver = browser_driver
                        s.zoom_bot = zoom_bot
                        s.status = "joining"
                        s.last_activity = time.time()
                        log.info("Session updated with agent for meeting_id=%s", _meeting_id)
                
                def monitor_activity():
                    time.sleep(3)
                    with _session_lock:
                        if _meeting_id not in _active_sessions:
                            return
                        session = _active_sessions[_meeting_id]
                        if session.thread is None or not session.thread.is_alive():
                            return
                    max_wait = 30
                    check_interval = 2
                    waited = 0
                    while waited < max_wait:
                        time.sleep(check_interval)
                        waited += check_interval
                        with _session_lock:
                            if _meeting_id not in _active_sessions:
                                return
                            session = _active_sessions[_meeting_id]
                            if session.thread is None or not session.thread.is_alive():
                                return
                            try:
                                if session.agent and hasattr(session.agent, '_audio_in'):
                                    audio_in = session.agent._audio_in
                                    if hasattr(audio_in, '_running') and audio_in._running:
                                        session.status = "active"
                                        log.info("Mechus is now active and listening for meeting %s", _meeting_id)
                                        return
                            except Exception:
                                pass
                            if waited >= 5:
                                session.status = "active"
                                log.info("Mechus is now active for meeting %s", _meeting_id)
                                return
                threading.Thread(target=monitor_activity, daemon=True).start()
            except Exception as e:
                log.error("Join thread error for meeting_id=%s: %s", _meeting_id, e, exc_info=True)
                with _session_lock:
                    if _meeting_id in _active_sessions:
                        _active_sessions[_meeting_id].status = "error"
        
        if multi_bots:
            threading.Thread(target=do_multi_join, daemon=True).start()
            return jsonify({
                "success": True,
                "message": "Mechus assistants are joining the meeting",
                "status": "starting",
                "multi_bot": True,
                "session_group_id": group_id,
                "meeting_id": group_id,
                "join_backend": join_backend,
                "team_id": team_id or None,
                "shared_context": shared_context_enabled,
                "bots": [
                    {
                        "bot_meeting_id": s["worker_key"],
                        "persona_id": s["persona_id"],
                        "display_name": s["display_name"],
                    }
                    for s in bot_specs
                ],
                **camera_response,
            })
        threading.Thread(target=do_join, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Mechus is joining the meeting",
            "status": "starting",
            "meeting_link": formatted_link,
            "meeting_id": meeting_id,
            "join_backend": join_backend,
            "team_id": team_id or None,
            "shared_context": shared_context_enabled,
            **camera_response,
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 400
    except Exception as exc:
        log.error("Error joining meeting: %s", exc, exc_info=True)
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


@app.route("/api/test-voice", methods=["POST"])
def test_voice():
    """
    Play a test phrase into the active meeting (voice injection test).
    Mechus must already be in a meeting (e.g. via zoom_sdk join).
    
    Body: { "meeting_id": "<id>", "text": "Optional phrase (default: Hello, this is a voice test from Mechus.)" }
    """
    global _active_sessions
    body = request.get_json(silent=True) or {}
    meeting_id = body.get("meeting_id") or request.args.get("meeting_id")
    if not meeting_id:
        return jsonify({"success": False, "error": "meeting_id required"}), 400
    text = (body.get("text") or request.args.get("text") or "").strip()
    if not text:
        text = "Hello, this is a voice test from Mechus."
    with _session_lock:
        if meeting_id not in _active_sessions:
            return jsonify({
                "success": False,
                "error": "No active session for this meeting. Join a meeting first.",
                "meeting_id": meeting_id,
            }), 404
        session = _active_sessions[meeting_id]
        if session.agent is None or session.status not in ("joining", "active"):
            return jsonify({
                "success": False,
                "error": f"Session not ready (status={session.status}). Wait until Mechus is active.",
                "meeting_id": meeting_id,
            }), 409
    try:
        agent = session.agent
        syn = agent._tts.synthesize(text)
        # TTS can return silence when API key missing, wrong voice, or request failed
        if syn.audio_f32 is None or syn.audio_f32.size == 0:
            return jsonify({
                "success": False,
                "error": "TTS returned no audio. Set MECHUS_ELEVEN_API_KEY and MECHUS_ELEVEN_VOICE_ID in .env (and MECHUS_TTS_BACKEND=elevenlabs), then restart.",
                "meeting_id": meeting_id,
            }), 503
        audio_out = getattr(agent, "_audio_out", None)
        ready_fn = getattr(audio_out, "is_ready_to_send", None) if audio_out else None
        if callable(ready_fn) and not ready_fn():
            return jsonify({
                "success": False,
                "error": "Zoom virtual mic not ready yet. Wait a few seconds after the bot joins and try again, or check status virtual_mic_ready.",
                "meeting_id": meeting_id,
            }), 503
        agent._audio_out.play_blocking(
            syn.audio_f32,
            syn.sample_rate_hz,
            gain=getattr(agent._config, "tts_output_gain", 0.9),
        )
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "message": "Test phrase played into the meeting.",
            "text": text,
        })
    except Exception as e:
        log.exception("Test voice failed for meeting %s", meeting_id)
        return jsonify({
            "success": False,
            "error": str(e),
            "meeting_id": meeting_id,
        }), 500


_INSPECTION_STORE: Any = None


@app.route("/api/inspection/findings", methods=["GET"])
def api_inspection_findings():
    """Saved inspection findings: ``?unit=apt-4b[&mode=move_in|move_out]`` (default both)."""
    global _INSPECTION_STORE
    try:
        from mechus.envfile import load_env_file as _lef
        from mechus.inspection import list_findings, make_store
        from mechus.notes_paths import apply_notes_paths_to_env

        _lef(os.getenv("MECHUS_ENV_FILE", ".env"))
        unit = re.sub(r"[^a-z0-9_.-]+", "-", (request.args.get("unit") or "default").strip().lower()).strip("-") or "default"
        mode = (request.args.get("mode") or "").strip().lower().replace("-", "_")
        modes = [mode] if mode in ("move_in", "move_out") else ["move_in", "move_out"]
        if _INSPECTION_STORE is None:
            _notes_dir, _ = apply_notes_paths_to_env()
            _INSPECTION_STORE = make_store(os.path.join(_notes_dir, "inspections"))
        findings: list[dict[str, Any]] = []
        frames = 0
        for m in modes:
            recs = _INSPECTION_STORE.load_records(unit, m)
            frames += len(recs)
            findings.extend(list_findings(recs))
        return jsonify({
            "success": True,
            "unit": unit,
            "modes": modes,
            "store": getattr(_INSPECTION_STORE, "backend", ""),
            "frames": frames,
            "count": len(findings),
            "findings": findings,
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("inspection findings route failed")
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/api/status", methods=["GET"])
def get_status():
    """
    Get the status of Mechus sessions.

    Query parameters:
    - meeting_id (optional): Get status for specific meeting (also returns live_transcripts)
    - session_group_id (optional): Multi-bot join — summary of each ``bot_meeting_id`` (no ``meeting_id`` required)
    - user_id (optional): Get all meetings for a user
    - Neither: Returns all active sessions
    """
    global _active_sessions

    meeting_id = request.args.get("meeting_id")
    user_id = request.args.get("user_id")
    session_group_id = (request.args.get("session_group_id") or "").strip()

    if session_group_id and not meeting_id:
        with _session_lock:
            children = list(_session_group_children.get(session_group_id, []))
        bots: list[dict[str, Any]] = []
        for bid in children:
            with _session_lock:
                s = _active_sessions.get(bid)
            if s is None:
                bots.append({"bot_meeting_id": bid, "status": "idle", "persona_id": None,
                              "display_name": None, "live_transcripts": [], "thoughts": []})
                continue
            _bot_status = s.status
            _bot_persona_id = getattr(s, "bot_persona_id", None)
            _bot_display_name = getattr(s, "bot_display_name", None) or _bot_persona_id
            # Read worker-status for transcript path + thoughts snapshot
            _bot_ws = _read_worker_status_json(bid)
            _bot_tp_raw = str(_bot_ws.get("transcript_path") or "").strip()
            _bot_tp = _coerce_worker_transcript_path_to_api(_bot_tp_raw) if _bot_tp_raw else None
            if not _bot_tp:
                _notes_d = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                _guessed_tp = os.path.join(_notes_d, f"{_safe_notes_filename_id(bid)}.jsonl")
                _bot_tp = _guessed_tp if os.path.exists(_guessed_tp) else None
            _bot_speaker_map = _load_speaker_map(bid)
            _bot_live_transcripts = (
                _read_live_transcripts_for_status(_bot_tp, _bot_speaker_map) if _bot_tp else []
            )
            # Thoughts: live agent > worker-status
            _bot_thoughts: list = []
            if s.agent is not None and hasattr(s.agent, "get_thoughts_for_status"):
                try:
                    _bot_thoughts = s.agent.get_thoughts_for_status()
                except Exception:
                    pass
            if not _bot_thoughts:
                _bwt = _bot_ws.get("thoughts")
                if isinstance(_bwt, list):
                    _bot_thoughts = _bwt
            bots.append({
                "bot_meeting_id": bid,
                "status": _bot_status,
                "persona_id": _bot_persona_id,
                "display_name": _bot_display_name,
                "live_transcripts": _bot_live_transcripts,
                "thoughts": _bot_thoughts,
                "camera": _facetime_camera_status_fields(_bot_ws),
            })
        active = any(b.get("status") in _SESSION_ACTIVE_STATUSES for b in bots)
        return jsonify({
            "active": active,
            "session_group_id": session_group_id,
            "bots": bots,
        })

    # ── Branch 1: specific meeting ─────────────────────────────────────────
    if meeting_id:
        transcript_path = None
        snap: dict = {}
        with _session_lock:
            if meeting_id not in _active_sessions:
                disk_status = _build_status_payload_from_worker_disk(meeting_id)
                if disk_status is not None:
                    return jsonify(disk_status)
                return jsonify({"active": False, "status": "idle", "meeting_id": meeting_id})

            session = _active_sessions[meeting_id]

            _worker_transcript_path = None
            facetime_state = ""
            zoom_join_state = ""
            zoom_join_detail = ""
            worker_error = ""
            pulse_defaults: dict[str, object] | None = None
            facetime_page_title = ""
            facetime_page_url = ""
            facetime_nav_url = ""
            facetime_worker_error = ""
            facetime_join_clicked: bool | None = None
            facetime_pulse_rewire_ok: bool | None = None
            facetime_mic_ok: bool | None = None
            facetime_ready: bool | None = None
            timezone_conflicts: list[dict] = []
            worker_receiving_audio: bool | None = None
            worker_last_audio_at: float | None = None
            worker_audio_status: str = ""
            worker_audio_input_no_device: bool | None = None
            worker_thoughts: list = []
            worker_camera_status: dict[str, Any] = {}

            # Container worker mode: sync status from worker status file
            if getattr(session, "worker_container", None):
                notes_dir = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                worker_status_path = os.path.join(notes_dir, f"worker-status-{meeting_id}.json")
                try:
                    with open(worker_status_path, "r", encoding="utf-8") as _f:
                        _ws = json.load(_f)
                    if _ws.get("status") in ("joining", "active", "stopped", "error"):
                        session.status = _ws["status"]
                    if _ws.get("transcript_chunks"):
                        session.transcript_chunks = _ws["transcript_chunks"]
                    if _ws.get("last_activity"):
                        session.last_activity = _ws["last_activity"]
                    if isinstance(_ws.get("participant_names"), list):
                        session.participant_names = [str(x) for x in _ws.get("participant_names") or []]
                    facetime_state = str(_ws.get("facetime_state") or "").strip()
                    pulse_defaults = _ws.get("pulse_defaults") if isinstance(_ws.get("pulse_defaults"), dict) else None
                    _raw_tp = str(_ws.get("transcript_path") or "").strip()
                    _coerced_tp = _coerce_worker_transcript_path_to_api(_raw_tp)
                    _worker_transcript_path = _coerced_tp if _coerced_tp else None
                    facetime_page_title = str(_ws.get("facetime_page_title") or "").strip()[:400]
                    facetime_page_url = str(_ws.get("facetime_page_url") or "").strip()[:600]
                    facetime_nav_url = str(_ws.get("facetime_nav_url") or "").strip()[:600]
                    facetime_worker_error = str(_ws.get("error") or "").strip()[:800]
                    worker_error = facetime_worker_error
                    zoom_join_state = str(_ws.get("join_state") or "").strip()
                    zoom_join_detail = str(_ws.get("join_detail") or "").strip()
                    _jc = _ws.get("facetime_join_clicked")
                    facetime_join_clicked = bool(_jc) if _jc is not None else None
                    _pr = _ws.get("pulse_rewire_ok")
                    facetime_pulse_rewire_ok = bool(_pr) if _pr is not None else None
                    _fm = _ws.get("facetime_mic_ok")
                    facetime_mic_ok = bool(_fm) if _fm is not None else None
                    _fr = _ws.get("facetime_ready")
                    facetime_ready = bool(_fr) if _fr is not None else None
                    _ra = _ws.get("receiving_audio")
                    worker_receiving_audio = bool(_ra) if _ra is not None else None
                    _laa = _ws.get("last_audio_received_at")
                    if _laa is not None:
                        try:
                            worker_last_audio_at = float(_laa)
                        except (TypeError, ValueError):
                            worker_last_audio_at = None
                    worker_audio_status = str(_ws.get("audio_status") or "").strip()
                    _aind = _ws.get("audio_input_no_device")
                    worker_audio_input_no_device = bool(_aind) if _aind is not None else None
                    _wt = _ws.get("thoughts")
                    if isinstance(_wt, list):
                        worker_thoughts = _wt
                    worker_camera_status = _facetime_camera_status_fields(_ws)
                except Exception:
                    pass

                if _worker_transcript_path and os.path.exists(_worker_transcript_path):
                    transcript_path = _worker_transcript_path
                else:
                    notes_dir = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                    _guessed = os.path.join(notes_dir, f"{_safe_notes_filename_id(meeting_id)}.jsonl")
                    transcript_path = _guessed if os.path.exists(_guessed) else None

            # Subprocess mode: sync status from worker status file
            elif session.worker_proc is not None:
                if session.worker_proc.poll() is not None and session.status not in ("stopped", "stopping"):
                    session.status = "stopped" if session.worker_proc.returncode == 0 else "error"
                else:
                    # Read latest status from worker's status file
                    try:
                        with open(_worker_status_path(meeting_id), "r", encoding="utf-8") as _f:
                            _ws = json.load(_f)
                        if _ws.get("status") in ("joining", "active", "stopped", "error"):
                            session.status = _ws["status"]
                        if _ws.get("transcript_chunks"):
                            session.transcript_chunks = _ws["transcript_chunks"]
                        if _ws.get("last_activity"):
                            session.last_activity = _ws["last_activity"]
                        session.participants = _ws.get("participants") or {}
                        if isinstance(_ws.get("participant_names"), list):
                            session.participant_names = [str(x) for x in _ws.get("participant_names") or []]
                        session.participant_timezones = _ws.get("participant_timezones") or session.participant_timezones or {}
                        timezone_conflicts = _ws.get("timezone_conflicts") or []
                        session.speaker_attribution_mode = _ws.get("speaker_attribution_mode") or ""
                        worker_error = str(_ws.get("error") or "").strip()[:800]
                        zoom_join_state = str(_ws.get("join_state") or "").strip()
                        zoom_join_detail = str(_ws.get("join_detail") or "").strip()
                        # Chert FaceTime worker reports call phase + audio health the same
                        # way the facetime_web container worker does.
                        facetime_state = str(_ws.get("facetime_state") or "").strip()
                        _fr2 = _ws.get("facetime_ready")
                        facetime_ready = bool(_fr2) if _fr2 is not None else None
                        _ra2 = _ws.get("receiving_audio")
                        worker_receiving_audio = bool(_ra2) if _ra2 is not None else None
                        _laa2 = _ws.get("last_audio_received_at")
                        if _laa2 is not None:
                            try:
                                worker_last_audio_at = float(_laa2)
                            except (TypeError, ValueError):
                                worker_last_audio_at = None
                        worker_audio_status = str(_ws.get("audio_status") or "").strip()
                        # Worker now writes the actual transcript path into its status file
                        _raw_tp2 = str(_ws.get("transcript_path") or "").strip()
                        _coerced_tp2 = _coerce_worker_transcript_path_to_api(_raw_tp2)
                        _worker_transcript_path = _coerced_tp2 if _coerced_tp2 else None
                        _wt2 = _ws.get("thoughts")
                        if isinstance(_wt2, list):
                            worker_thoughts = _wt2
                    except Exception:
                        pass
                # Use path from worker status file; fall back to UUID-based guess for old workers
                if _worker_transcript_path and os.path.exists(_worker_transcript_path):
                    transcript_path = _worker_transcript_path
                else:
                    notes_dir = os.path.abspath(os.getenv("MECHUS_NOTES_DIR", "mechus_notes"))
                    _guessed = os.path.join(notes_dir, f"{_safe_notes_filename_id(meeting_id)}.jsonl")
                    transcript_path = _guessed if os.path.exists(_guessed) else None
            else:
                # In-process mode
                if session.thread is not None and not session.thread.is_alive():
                    session.status = "stopped"
                if session.agent is not None:
                    try:
                        if hasattr(session.agent, "_transcript_chunk_id") and session.agent._transcript_chunk_id > 0:
                            session.transcript_chunks = session.agent._transcript_chunk_id
                            session.last_activity = time.time()
                    except Exception:
                        pass
                    try:
                        if hasattr(session.agent, "get_timezone_context"):
                            _tz_ctx = session.agent.get_timezone_context() or {}
                            session.participant_timezones = _tz_ctx.get("participant_timezones") or session.participant_timezones or {}
                            timezone_conflicts = _tz_ctx.get("timezone_conflicts") or []
                    except Exception:
                        pass
                try:
                    transcript_path = getattr(
                        getattr(session.agent, "_note_store", None), "path", None
                    ) or None
                except Exception:
                    pass

            _jb = getattr(session, "join_backend", "") or ""
            # True only while the worker is still trying to open the web UI and click Join (not while waiting for host admit).
            _fs = (facetime_state or "").strip()
            facetime_join_in_progress = _jb == "facetime_web" and facetime_join_clicked is not True and _fs in (
                "",
                "opening_link",
                "waiting_for_name",
                "waiting_for_join_click",
            )
            _ftdash = (
                facetime_dashboard_labels(facetime_state)
                if _jb in ("facetime_web", "facetime_chert")
                else {"facetime_phase": "", "facetime_phase_label": "", "facetime_state_label": ""}
            )

            snap = {
                "status": session.status,
                "user_id": session.user_id,
                "team_id": getattr(session, "team_id", "") or "",
                "shared_context": bool(getattr(session, "shared_context_enabled", False)),
                "share_to_team_context": bool(getattr(session, "share_to_team_context", False)),
                "meeting_link": session.meeting_link,
                "started_at": session.started_at,
                "transcript_chunks": session.transcript_chunks,
                "last_activity": session.last_activity,
                "participants": getattr(session, "participants", {}) or {},
                "participant_names": getattr(session, "participant_names", None) or [],
                "active_speaker_names": getattr(session, "active_speaker_names", None) or [],
                "participant_timezones": getattr(session, "participant_timezones", {}) or {},
                "timezone_conflicts": timezone_conflicts,
                "speaker_attribution_mode": getattr(session, "speaker_attribution_mode", "") or "",
                "facetime_state": facetime_state,
                "facetime_phase": _ftdash.get("facetime_phase") or "",
                "facetime_phase_label": _ftdash.get("facetime_phase_label") or "",
                "facetime_state_label": _ftdash.get("facetime_state_label") or "",
                "pulse_defaults": pulse_defaults or {},
                "join_backend": _jb,
                "facetime_page_title": facetime_page_title,
                "facetime_page_url": facetime_page_url,
                "facetime_nav_url": facetime_nav_url,
                "facetime_worker_error": facetime_worker_error or worker_error,
                "worker_error": worker_error or facetime_worker_error,
                "zoom_join_state": zoom_join_state,
                "zoom_join_detail": zoom_join_detail,
                "facetime_join_clicked": facetime_join_clicked,
                "facetime_join_in_progress": facetime_join_in_progress,
                "facetime_pulse_rewire_ok": facetime_pulse_rewire_ok,
                "facetime_mic_ok": facetime_mic_ok,
                # facetime_ready=True: join knock sent, host just needs to tap Admit.
                "facetime_ready": facetime_ready,
                "receiving_audio": worker_receiving_audio,
                "last_audio_received_at": worker_last_audio_at,
                "audio_status": worker_audio_status,
                "audio_input_no_device": worker_audio_input_no_device,
                "thoughts": worker_thoughts,
                "camera_status": worker_camera_status,
            }

        # Slow file I/O outside the lock
        speaker_map = _load_speaker_map(meeting_id)
        live_transcripts = _read_live_transcripts_for_status(transcript_path, speaker_map) if transcript_path else []

        with _session_lock:
            if meeting_id not in _active_sessions:
                disk_status = _build_status_payload_from_worker_disk(meeting_id)
                if disk_status is not None:
                    return jsonify(disk_status)
                return jsonify({"active": False, "status": "idle", "meeting_id": meeting_id})
            session = _active_sessions[meeting_id]

            payload: dict = {
                "active": snap["status"] in ("starting", "joining", "active"),
                "status": snap["status"],
                "meeting_id": meeting_id,
                "user_id": snap["user_id"],
                "team_id": snap.get("team_id") or None,
                "shared_context": bool(snap.get("shared_context")),
                "share_to_team_context": bool(snap.get("share_to_team_context")),
                "meeting_link": snap["meeting_link"],
                "started_at": snap["started_at"],
                "uptime_seconds": time.time() - snap["started_at"],
                "transcript_chunks": snap["transcript_chunks"],
                "last_activity": snap["last_activity"],
                "live_transcripts": live_transcripts,
                "live_transcript_count": len(live_transcripts),
                "speaker_map": speaker_map,
                "participants": snap["participants"],
                "participant_timezones": snap.get("participant_timezones") or {},
                "timezone_conflicts": snap.get("timezone_conflicts") or [],
                "speaker_attribution_mode": snap["speaker_attribution_mode"],
                "facetime_state": snap.get("facetime_state") or "",
                "facetime_phase": snap.get("facetime_phase") or "",
                "facetime_phase_label": snap.get("facetime_phase_label") or "",
                "facetime_state_label": snap.get("facetime_state_label") or "",
                "pulse_defaults": snap.get("pulse_defaults") or {},
                "join_backend": snap.get("join_backend") or "",
                "facetime_page_title": snap.get("facetime_page_title") or "",
                "facetime_page_url": snap.get("facetime_page_url") or "",
                "facetime_nav_url": snap.get("facetime_nav_url") or "",
                "facetime_worker_error": snap.get("facetime_worker_error") or snap.get("worker_error") or "",
                "worker_error": snap.get("worker_error") or snap.get("facetime_worker_error") or "",
                "zoom_join_state": snap.get("zoom_join_state") or "",
                "zoom_join_detail": snap.get("zoom_join_detail") or "",
                "facetime_join_clicked": snap.get("facetime_join_clicked"),
                "facetime_join_in_progress": bool(snap.get("facetime_join_in_progress")),
                "facetime_pulse_rewire_ok": snap.get("facetime_pulse_rewire_ok"),
                "facetime_mic_ok": snap.get("facetime_mic_ok"),
                "facetime_ready": snap.get("facetime_ready"),
                "receiving_audio": snap.get("receiving_audio"),
                "last_audio_received_at": snap.get("last_audio_received_at"),
                "audio_status": snap.get("audio_status") or "",
                "audio_input_no_device": snap.get("audio_input_no_device"),
                # Simple booleans for easy dashboard integration:
                # bot_in_call → true only when Mechus is actually in the meeting (not merely "joining")
                # bot_waiting → true while the bot is in FaceTime's waiting room (before admit)
                # or Zoom SDK waiting room (host must admit "Mechus")
                "bot_waiting": (snap.get("facetime_phase") == "waiting_for_admit")
                    or (snap.get("facetime_state") == "waiting_for_admit")
                    or (
                        snap.get("join_backend") == "zoom_sdk"
                        and "WAITING" in (snap.get("zoom_join_state") or "").upper()
                        and snap.get("status") == "joining"
                    ),
                "bot_connecting": (
                    snap.get("join_backend") == "facetime_web"
                    and snap.get("facetime_phase") == "connecting"
                ) or (
                    snap.get("join_backend") == "zoom_sdk"
                    and snap.get("status") in ("starting", "joining")
                    and not (
                        snap.get("join_backend") == "zoom_sdk"
                        and "WAITING" in (snap.get("zoom_join_state") or "").upper()
                    )
                ),
            }
            payload.update(snap.get("camera_status") or {})
            _st = str(snap.get("status") or "")
            _we = str(payload.get("worker_error") or "").strip()
            payload["bot_in_call"] = _st not in ("error", "stopped", "stopping", "starting") and (
                (snap.get("facetime_phase") == "in_meeting")
                or (snap.get("facetime_state") == "in_call")
                or (
                    _st == "active"
                    and snap.get("join_backend") in ("zoom_sdk", "facetime_web", "facetime_chert")
                )
                or bool(snap.get("receiving_audio"))
                or int(snap.get("transcript_chunks") or 0) > 0
            )
            payload["mechus_presence"] = _compute_mechus_presence(
                status=_st,
                join_backend=str(snap.get("join_backend") or ""),
                bot_in_call=bool(payload.get("bot_in_call")),
                bot_waiting=bool(payload.get("bot_waiting")),
                bot_connecting=bool(payload.get("bot_connecting")),
                worker_error=_we,
            )
            if _st == "error" and _we:
                payload["failure_reason"] = _we
            now = time.time()
            if session.agent is not None and hasattr(session.agent, "_last_audio_frame_at"):
                payload["last_audio_received_at"] = session.agent._last_audio_frame_at
                payload["receiving_audio"] = (now - session.agent._last_audio_frame_at) <= 15.0
                zoom_frames = 0
                audio_in = getattr(session.agent, "_audio_in", None)
                if audio_in is not None and hasattr(audio_in, "_frames_received"):
                    zoom_frames = audio_in._frames_received
                    payload["zoom_frames_received"] = zoom_frames
                payload["audio_status"] = (
                    "receiving" if payload["receiving_audio"]
                    else ("no_audio_yet" if zoom_frames == 0 else "silent")
                )
                if not payload["receiving_audio"]:
                    payload["no_audio_hint"] = (
                        "Host may need to allow recording when Zoom prompted. "
                        "Check Docker logs for 'Zoom audio: first frame received'."
                    )
                audio_out = getattr(session.agent, "_audio_out", None)
                payload["virtual_mic_ready"] = (
                    getattr(audio_out, "is_ready_to_send", lambda: False)()
                    if audio_out else False
                )
            elif snap.get("join_backend") == "facetime_web" and not payload.get("receiving_audio"):
                _ast = payload.get("audio_status") or ""
                if snap.get("audio_input_no_device"):
                    payload["no_audio_hint"] = (
                        "Mechus could not open the PulseAudio input device in the FaceTime worker. "
                        "Redeploy the facetime-worker image and check ./run.sh ft-logs <meeting_id>."
                    )
                elif _ast == "no_audio_yet":
                    payload["no_audio_hint"] = (
                        "Mechus is not receiving call audio yet. On status, check facetime_mic_ok and "
                        "facetime_pulse_rewire_ok; on the server run ./run.sh ft-logs <meeting_id>."
                    )
                elif _ast == "silent":
                    payload["no_audio_hint"] = (
                        "Mechus hears silence on the FaceTime audio tap — speak in the meeting to test."
                    )
            elif snap.get("join_backend") == "zoom_sdk":
                _we = (payload.get("worker_error") or "").strip()
                if _we:
                    payload["no_audio_hint"] = _we
                elif payload.get("bot_waiting"):
                    payload["no_audio_hint"] = (
                        "Mechus is in the Zoom waiting room — admit 'Mechus' from the Participants panel."
                    )
                elif snap.get("status") == "joining" and (payload.get("zoom_join_state") or "").strip():
                    payload["no_audio_hint"] = (
                        f"Mechus is connecting via Zoom SDK ({payload.get('zoom_join_state')}). "
                        "If this lasts more than a minute, check waiting room and recording permission."
                    )
            if session.agent is not None and getattr(session.agent, "_last_latency", None):
                payload["latency"] = session.agent._last_latency
            # Thoughts: prefer live agent stream; fall back to worker-status snapshot
            _thoughts: list = []
            if session.agent is not None and hasattr(session.agent, "get_thoughts_for_status"):
                try:
                    _thoughts = session.agent.get_thoughts_for_status()
                except Exception:
                    pass
            if not _thoughts:
                _thoughts = snap.get("thoughts") or []
            payload["thoughts"] = _thoughts
        return jsonify(payload)

    # ── Branch 2: all meetings for a user ──────────────────────────────────
    if user_id:
        with _session_lock:
            user_sessions = []
            for mid, session in _active_sessions.items():
                if session.user_id != user_id:
                    continue
                if session.thread is not None and not session.thread.is_alive():
                    session.status = "stopped"
                is_active = session.status in ("starting", "joining", "active")
                user_sessions.append({
                    "meeting_id": mid,
                    "active": is_active,
                    "status": session.status,
                    "team_id": getattr(session, "team_id", "") or None,
                    "meeting_link": session.meeting_link,
                    "started_at": session.started_at,
                    "uptime_seconds": time.time() - session.started_at,
                })
        return jsonify({
            "user_id": user_id,
            "sessions": user_sessions,
            "total_active": sum(1 for s in user_sessions if s["active"]),
        })

    # ── Branch 3: all sessions ──────────────────────────────────────────────
    with _session_lock:
        all_sessions = []
        for mid, session in _active_sessions.items():
            if session.thread is not None and not session.thread.is_alive():
                session.status = "stopped"
            is_active = session.status in ("starting", "joining", "active")
            all_sessions.append({
                "meeting_id": mid,
                "user_id": session.user_id,
                "team_id": getattr(session, "team_id", "") or None,
                "active": is_active,
                "status": session.status,
                "meeting_link": session.meeting_link,
                "started_at": session.started_at,
                "uptime_seconds": time.time() - session.started_at,
            })
    return jsonify({
        "sessions": all_sessions,
        "total_active": sum(1 for s in all_sessions if s["active"]),
    })


@app.route("/api/email-reminders", methods=["GET"])
def get_email_reminders():
    """Inspect queued/sent deadline reminder emails."""
    try:
        from mechus.email_sender import _load_reminders  # local import avoids startup coupling

        reminders = _load_reminders()
        meeting_id = (request.args.get("meeting_id") or "").strip()
        sent_param = (request.args.get("sent") or "").strip().lower()

        if meeting_id:
            reminders = [r for r in reminders if str(r.get("meeting_id") or "") == meeting_id]
        if sent_param in ("1", "true", "yes"):
            reminders = [r for r in reminders if bool(r.get("sent_at"))]
        elif sent_param in ("0", "false", "no"):
            reminders = [r for r in reminders if not bool(r.get("sent_at"))]

        reminders.sort(
            key=lambda r: (
                str(r.get("sent_at") or ""),
                str(r.get("deadline") or ""),
                str(r.get("created_at") or ""),
            ),
            reverse=True,
        )
        return jsonify({
            "success": True,
            "count": len(reminders),
            "reminders": reminders,
        })
    except Exception as e:
        log.exception("email-reminders read error")
        return jsonify({"success": False, "error": str(e)}), 500


def _stop_single_worker_like_session(meeting_id: str, data: dict) -> tuple[dict[str, Any], int] | None:
    """Stop one ``zoom_sdk`` / ``facetime_web`` worker (docker or subprocess). Returns None if not applicable."""
    cname: str | None = None
    proc = None
    session: MechusSession | None = None
    gid: str | None = None

    with _session_lock:
        if meeting_id not in _active_sessions:
            return None
        session = _active_sessions[meeting_id]
        gid = getattr(session, "session_group_id", None)

        if getattr(session, "worker_container", None):
            force_stop = _json_truthy(data.get("force"))
            blocked, block_msg = _facetime_early_stop_blocked(session, meeting_id, force_stop)
            if blocked:
                return ({
                    "success": False,
                    "error": "facetime_join_guard",
                    "message": block_msg,
                    "meeting_id": meeting_id,
                }, 200)
            cname = str(session.worker_container)
            session.status = "stopping"
            session.worker_container = None
        elif session.worker_proc is not None:
            proc = session.worker_proc
            session.status = "stopping"
        elif (getattr(session, "join_backend", "") or "") == "facetime_web" and (
            session.agent is None or session.thread is None
        ):
            force_stop = _json_truthy(data.get("force"))
            blocked, block_msg = _facetime_early_stop_blocked(session, meeting_id, force_stop)
            if blocked:
                return ({
                    "success": False,
                    "error": "facetime_join_guard",
                    "message": block_msg,
                    "meeting_id": meeting_id,
                }, 200)
            wc = getattr(session, "worker_container", None)
            if wc:
                _stop_docker_worker_container(str(wc), meeting_id)
                session.worker_container = None
            session.status = "stopped"
            del _active_sessions[meeting_id]
            _prune_session_group_child(gid, meeting_id)
            return ({
                "success": True,
                "message": "Session stopped (join was still in progress)",
                "meeting_id": meeting_id,
            }, 200)
        else:
            return None

    # Unlocked continuation for container / subprocess (matches existing stop_session behavior).
    if cname is not None:
        _stop_docker_worker_container(str(cname), meeting_id, stop_timeout_s=120)
        final_summary = None
        try:
            from mechus.meeting_summary import wait_for_final_summary

            final_summary = wait_for_final_summary(meeting_id, timeout_s=90.0)
        except Exception:
            pass
        team_context_item = None
        if getattr(session, "share_to_team_context", False) and getattr(session, "team_id", ""):
            try:
                team_context_item = _add_meeting_to_team_context(
                    team_id=session.team_id,
                    meeting_id=meeting_id,
                    created_by=session.user_id,
                    final_summary=final_summary,
                )
            except Exception as exc:
                log.warning("Could not add meeting to team context: %s", exc)
        _ws_stop = _read_worker_status_json(meeting_id)
        _tp = str(_ws_stop.get("transcript_path") or "").strip() or None
        _mlink = str(_ws_stop.get("meeting_link") or "").strip() or None
        _tp_api = _coerce_worker_transcript_path_to_api(_tp or "")
        if _tp_api:
            _persist_meeting_transcript_ref(
                meeting_id,
                _tp_api,
                _mlink or getattr(session, "meeting_link", "") or "",
            )
        _try_send_meeting_emails_on_stop(meeting_id, final_summary, session)
        with _session_lock:
            if meeting_id in _active_sessions:
                _active_sessions[meeting_id].status = "stopped"
                del _active_sessions[meeting_id]
            _prune_session_group_child(gid, meeting_id)
        return ({
            "success": True,
            "message": "Meeting stopped",
            "meeting_id": meeting_id,
            "final_summary": final_summary,
            "team_context_item": team_context_item,
            "transcript_path": _tp_api or _tp,
            "meeting_link": _mlink,
        }, 200)

    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=120)
        except Exception as _e:
            log.warning("Worker terminate error for meeting_id=%s: %s", meeting_id, _e)
            try:
                proc.kill()
            except Exception:
                pass
        final_summary = None
        try:
            from mechus.meeting_summary import wait_for_final_summary

            final_summary = wait_for_final_summary(meeting_id, timeout_s=90.0)
        except Exception:
            pass
        team_context_item = None
        if getattr(session, "share_to_team_context", False) and getattr(session, "team_id", ""):
            try:
                team_context_item = _add_meeting_to_team_context(
                    team_id=session.team_id,
                    meeting_id=meeting_id,
                    created_by=session.user_id,
                    final_summary=final_summary,
                )
            except Exception as exc:
                log.warning("Could not add meeting to team context: %s", exc)
        _ws_stop = _read_worker_status_json(meeting_id)
        _tp = str(_ws_stop.get("transcript_path") or "").strip() or None
        _mlink = str(_ws_stop.get("meeting_link") or "").strip() or None
        _try_send_meeting_emails_on_stop(meeting_id, final_summary, session)
        with _session_lock:
            if meeting_id in _active_sessions:
                _active_sessions[meeting_id].status = "stopped"
                del _active_sessions[meeting_id]
            _prune_session_group_child(gid, meeting_id)
        return ({
            "success": True,
            "message": "Meeting stopped",
            "meeting_id": meeting_id,
            "final_summary": final_summary,
            "team_context_item": team_context_item,
            "transcript_path": _tp,
            "meeting_link": _mlink,
        }, 200)

    return None


def _facetime_worker_lookup(meeting_id: str) -> tuple[bool, bool, dict[str, Any], str | None]:
    """Return ``(exists, is_facetime, worker_status, assigned_device_or_None)``.

    ``assigned_device`` is None when unknown (worker not started yet), "" when
    the worker started without a camera device.
    """
    with _session_lock:
        session = _active_sessions.get(meeting_id)
        if session is not None:
            is_ft = getattr(session, "join_backend", "") == "facetime_web"
            device: str | None = None
            if getattr(session, "worker_container", None):
                device = str(getattr(session, "facetime_camera_device", "") or "")
            ws = _read_worker_status_json(meeting_id)
            if "facetime_camera_device_assigned" in ws:
                device = device or ("assigned" if ws.get("facetime_camera_device_assigned") else "")
            return True, is_ft, ws, device
    ws = _read_worker_status_json(meeting_id)
    if not ws:
        return False, False, {}, None
    device = None
    if "facetime_camera_device_assigned" in ws:
        device = "assigned" if ws.get("facetime_camera_device_assigned") else ""
    return True, str(ws.get("join_backend") or "") == "facetime_web", ws, device


def _known_facetime_worker(meeting_id: str) -> tuple[bool, dict[str, Any]]:
    exists, is_ft, ws, _ = _facetime_worker_lookup(meeting_id)
    return exists and is_ft, ws


@app.route("/api/meetings/<meeting_id>/camera", methods=["POST"])
def control_facetime_camera(meeting_id: str):
    """Set idempotent desired camera state for one FaceTime worker."""
    if not _facetime_camera_feature_enabled():
        return jsonify({"success": False, "error": "FaceTime camera support is disabled"}), 503
    exists, is_ft, ws, device = _facetime_worker_lookup(meeting_id)
    if not exists:
        return jsonify({"success": False, "error": "FaceTime meeting not found"}), 404
    if not is_ft:
        return jsonify({
            "success": False,
            "error": "Camera control is only available for FaceTime workers",
            "error_code": "camera_not_facetime",
        }), 409
    if device == "":
        return jsonify({
            "success": False,
            "error": "This meeting's worker has no virtual camera (pool exhausted or feature was off at join)",
            "error_code": "camera_not_provisioned",
        }), 409
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        action = str(data.get("action") or "").strip().lower()
        current = read_camera_control(_notes_dir(), meeting_id)
        enabled = current.enabled if current is not None else bool(ws.get("facetime_camera_enabled"))
        mode = current.mode if current is not None else str(ws.get("facetime_camera_desired_mode") or "avatar")
        text = ""
        duration = None
        if action == "turn_on":
            enabled = True
            if mode == "off":
                mode = "avatar"
        elif action == "turn_off":
            enabled = False
            mode = "off"
        elif action == "set_mode":
            mode = str(data.get("mode") or "").strip().lower()
            if mode not in CAMERA_MODES:
                raise ValueError(f"unknown camera mode: {mode}")
            enabled = mode != "off"
        elif action == "show_card":
            mode = str(data.get("mode") or "status_card").strip().lower()
            if mode not in ("status_card", "message_card"):
                raise ValueError("show_card mode must be status_card or message_card")
            enabled = True
            text = data.get("text") or ""
            duration = data.get("duration_seconds", 10)
        else:
            raise ValueError("action must be turn_on, turn_off, set_mode, or show_card")
        desired = write_camera_control(
            _notes_dir(),
            meeting_id,
            enabled=enabled,
            mode=mode,
            text=text,
            duration_seconds=duration,
        )
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    acknowledged = int(ws.get("facetime_camera_control_revision") or 0) >= desired.revision
    return jsonify({
        "success": True,
        "status": "applied" if acknowledged else "accepted",
        "meeting_id": meeting_id,
        "camera": {
            "revision": desired.revision,
            "enabled": desired.enabled,
            "mode": desired.mode,
            "text": desired.text,
            "expires_at": desired.expires_at,
        },
    }), 200 if acknowledged else 202


@app.route("/api/meetings/<meeting_id>/vision/query", methods=["POST"])
def request_facetime_vision(meeting_id: str):
    if not _facetime_vision_feature_enabled():
        return jsonify({"success": False, "error": "FaceTime visual understanding is disabled"}), 503
    exists, is_ft, ws, _ = _facetime_worker_lookup(meeting_id)
    if not exists:
        return jsonify({"success": False, "error": "FaceTime meeting not found"}), 404
    if not is_ft:
        return jsonify({"success": False, "error": "Visual questions are only available for FaceTime workers"}), 409
    with _session_lock:
        session = _active_sessions.get(meeting_id)
        allowed_for_meeting = bool(
            (session is not None and getattr(session, "facetime_vision_requested", False))
            or ws.get("facetime_vision_enabled")
        )
    if not allowed_for_meeting:
        return jsonify({"success": False, "error": "Visual understanding is not enabled for this meeting"}), 409
    try:
        data = request.get_json(silent=True) or {}
        question = data.get("question") if isinstance(data, dict) else ""
        try:
            limit = max(1, min(int(os.getenv("MECHUS_FT_VISION_MAX_REQUESTS_PER_MINUTE", "3")), 30))
        except ValueError:
            limit = 3
        now = time.time()
        with _vision_rate_lock:
            recent = [ts for ts in _vision_request_times.get(meeting_id, []) if now - ts < 60.0]
            if len(recent) >= limit:
                return jsonify({"success": False, "error": "Visual request rate limit exceeded"}), 429
            recent.append(now)
            _vision_request_times[meeting_id] = recent
        queued = enqueue_vision_request(_notes_dir(), meeting_id, question)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({
        "success": True,
        "status": "queued",
        "meeting_id": meeting_id,
        "request_id": queued["request_id"],
        "expires_at": queued["expires_at"],
    }), 202


@app.route("/api/meetings/<meeting_id>/vision/requests/<request_id>", methods=["GET"])
def get_facetime_vision_request(meeting_id: str, request_id: str):
    known, _ = _known_facetime_worker(meeting_id)
    result = vision_request_status(_notes_dir(), meeting_id, request_id)
    if not known and result is None:
        return jsonify({"success": False, "error": "FaceTime meeting or request not found"}), 404
    if result is None:
        return jsonify({"success": False, "error": "Visual request not found"}), 404
    return jsonify({"success": True, "meeting_id": meeting_id, **result})


@app.route("/api/stop", methods=["POST"])
def stop_session():
    """
    Stop a Mechus session.
    
    Called by the dashboard when the user clicks "End meeting", "Delete meeting",
    or "Cancel" (joining). Stops the agent, leaves the meeting, and clears the
    session so the user can start another.
    
    Request body:
    {
        "meeting_id": "meeting-123" (optional, stops all if not provided),
        "session_group_id": "same-as-join-meeting_id-for-multi-bot" (optional; same as logical group),
        "force": true (optional; FaceTime web only — required to stop during the
            first MECHUS_FT_EARLY_STOP_GUARD_S seconds while not yet in_call, so stray
            dashboard /api/stop calls do not kill the worker mid-join)
    }
    """
    global _active_sessions
    
    try:
        # Handle empty body gracefully
        try:
            data = request.get_json() or {}
        except Exception:
            data = {}
        meeting_id_raw = data.get("meeting_id") or data.get("session_group_id")
        meeting_id = str(meeting_id_raw).strip() if meeting_id_raw else None

        if meeting_id:
            with _session_lock:
                _direct = meeting_id in _active_sessions
                _child_list = list(_session_group_children.get(meeting_id, [])) if not _direct else []
            if not _direct and _child_list:
                results: list[dict[str, Any]] = []
                statuses: list[int] = []
                for _cid in _child_list:
                    hit = _stop_single_worker_like_session(_cid, data)
                    if hit is not None:
                        results.append(hit[0])
                        statuses.append(hit[1])
                with _session_lock:
                    _session_group_children.pop(meeting_id, None)
                primary = 200
                if statuses:
                    primary = 200 if all(s == 200 for s in statuses) else statuses[0]
                return jsonify({
                    "success": True,
                    "message": f"Stopped {len(results)} bot(s) for session group",
                    "session_group_id": meeting_id,
                    "bots": results,
                }), primary

        with _session_lock:
            # If meeting_id provided, stop that specific session
            if meeting_id:
                if meeting_id not in _active_sessions:
                    # Session table may be empty after API restart while the FaceTime docker
                    # worker is still in the call — stop by container name before claiming done.
                    if _stop_facetime_container_for_meeting(meeting_id, stop_timeout_s=75):
                        log.info(
                            "Stopped orphaned FaceTime worker container for meeting_id=%s",
                            meeting_id,
                        )
                        return jsonify({
                            "success": True,
                            "message": "Stopped FaceTime worker (session was not in memory)",
                            "meeting_id": meeting_id,
                        })
                    return jsonify({
                        "success": True,
                        "message": "No active session (already stopped or never started)",
                        "meeting_id": meeting_id,
                    })
                
                session = _active_sessions[meeting_id]
                _sgid = getattr(session, "session_group_id", None)

                if session.status == "stopped":
                    _prune_session_group_child(_sgid, meeting_id)
                    del _active_sessions[meeting_id]
                    return jsonify({
                        "success": True,
                        "message": "Session already stopped",
                        "meeting_id": meeting_id,
                    })
                
                # Container mode: stop docker worker container (worker writes worker-status json in MECHUS_NOTES_DIR).
                if getattr(session, "worker_container", None):
                    force_stop = _json_truthy(data.get("force"))
                    blocked, block_msg = _facetime_early_stop_blocked(session, meeting_id, force_stop)
                    if blocked:
                        log.info("FaceTime early stop refused meeting_id=%s", meeting_id)
                        # 200 (not 4xx): some dashboards treat any non-2xx as a hard failure and abort polling.
                        return jsonify({
                            "success": False,
                            "error": "facetime_join_guard",
                            "message": block_msg,
                            "meeting_id": meeting_id,
                        }), 200
                    cname = session.worker_container
                    session.status = "stopping"
                    session.worker_container = None
                    _stop_docker_worker_container(str(cname), meeting_id, stop_timeout_s=120)
                    final_summary = None
                    try:
                        from mechus.meeting_summary import wait_for_final_summary
                        final_summary = wait_for_final_summary(meeting_id, timeout_s=90.0)
                    except Exception:
                        pass
                    team_context_item = None
                    if getattr(session, "share_to_team_context", False) and getattr(session, "team_id", ""):
                        try:
                            team_context_item = _add_meeting_to_team_context(
                                team_id=session.team_id,
                                meeting_id=meeting_id,
                                created_by=session.user_id,
                                final_summary=final_summary,
                            )
                        except Exception as exc:
                            log.warning("Could not add meeting to team context: %s", exc)
                    _ws_stop = _read_worker_status_json(meeting_id)
                    _tp = str(_ws_stop.get("transcript_path") or "").strip() or None
                    _mlink = str(_ws_stop.get("meeting_link") or "").strip() or None
                    _tp_api = _coerce_worker_transcript_path_to_api(_tp or "")
                    if _tp_api:
                        _persist_meeting_transcript_ref(
                            meeting_id,
                            _tp_api,
                            _mlink or getattr(session, "meeting_link", "") or "",
                        )
                    session.status = "stopped"
                    log.info("Worker container stopped: meeting_id=%s", meeting_id)
                    _try_send_meeting_emails_on_stop(meeting_id, final_summary, session)
                    _prune_session_group_child(_sgid, meeting_id)
                    del _active_sessions[meeting_id]
                    return jsonify({
                        "success": True,
                        "message": "Meeting stopped",
                        "meeting_id": meeting_id,
                        "final_summary": final_summary,
                        "team_context_item": team_context_item,
                        "transcript_path": _tp_api or _tp,
                        "meeting_link": _mlink,
                    })

                # Subprocess mode: send SIGTERM to worker process (worker writes final_summary.json before exit)
                if session.worker_proc is not None:
                    proc = session.worker_proc
                    session.status = "stopping"
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                            # Worker joins the agent thread, writes worker-status "stopped" + summary, then
                            # may block on zoom_bot.leave/cleanup — allow longer than summary generation alone.
                            proc.wait(timeout=120)
                    except Exception as _e:
                        log.warning("Worker terminate error for meeting_id=%s: %s", meeting_id, _e)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    final_summary = None
                    try:
                        from mechus.meeting_summary import wait_for_final_summary
                        final_summary = wait_for_final_summary(meeting_id, timeout_s=90.0)
                    except Exception:
                        pass
                    team_context_item = None
                    if getattr(session, "share_to_team_context", False) and getattr(session, "team_id", ""):
                        try:
                            team_context_item = _add_meeting_to_team_context(
                                team_id=session.team_id,
                                meeting_id=meeting_id,
                                created_by=session.user_id,
                                final_summary=final_summary,
                            )
                        except Exception as exc:
                            log.warning("Could not add meeting to team context: %s", exc)
                    # Same pointers in-process stop returns — dashboards persist these to Supabase for recap UI.
                    _ws_stop = _read_worker_status_json(meeting_id)
                    _tp = str(_ws_stop.get("transcript_path") or "").strip() or None
                    _mlink = str(_ws_stop.get("meeting_link") or "").strip() or None
                    session.status = "stopped"
                    log.info("Worker subprocess stopped: meeting_id=%s", meeting_id)
                    _try_send_meeting_emails_on_stop(meeting_id, final_summary, session)
                    _prune_session_group_child(_sgid, meeting_id)
                    del _active_sessions[meeting_id]
                    return jsonify({
                        "success": True,
                        "message": "Meeting stopped",
                        "meeting_id": meeting_id,
                        "final_summary": final_summary,
                        "team_context_item": team_context_item,
                        "transcript_path": _tp,
                        "meeting_link": _mlink,
                    })

                # If join is still in progress (no agent yet), just mark stopped
                if session.agent is None or session.thread is None:
                    if (getattr(session, "join_backend", "") or "") == "facetime_web":
                        force_stop = _json_truthy(data.get("force"))
                        blocked, block_msg = _facetime_early_stop_blocked(session, meeting_id, force_stop)
                        if blocked:
                            log.info("FaceTime early stop refused (no agent path) meeting_id=%s", meeting_id)
                            return jsonify({
                                "success": False,
                                "error": "facetime_join_guard",
                                "message": block_msg,
                                "meeting_id": meeting_id,
                            }), 200
                        wc = getattr(session, "worker_container", None)
                        if wc:
                            _stop_docker_worker_container(str(wc), meeting_id, stop_timeout_s=75)
                            session.worker_container = None
                        else:
                            _stop_facetime_container_for_meeting(
                                meeting_id, stop_timeout_s=75
                            )
                        session.status = "stopped"
                        _prune_session_group_child(_sgid, meeting_id)
                        del _active_sessions[meeting_id]
                        return jsonify({
                            "success": True,
                            "message": "Session stopped (join was still in progress)",
                            "meeting_id": meeting_id,
                        })
                    session.status = "stopped"
                    return jsonify({
                        "success": True,
                        "message": "Session stopped (join was still in progress)",
                        "meeting_id": meeting_id,
                    })

                # Generate end-of-meeting summary for dashboard (in-process sessions only)
                final_summary_inproc = None
                try:
                    transcript_text, state = session.agent.get_meeting_data_for_summary()
                    from mechus.meeting_summary import generate_final_summary, merge_final_report_into_summary
                    api_key = os.environ.get("MECHUS_OPENAI_API_KEY", "").strip() or None
                    _cfg = session.agent._config
                    model = (
                        getattr(_cfg, "openai_thinking_model", None)
                        or getattr(_cfg, "openai_chat_model", None)
                        or os.environ.get("MECHUS_OPENAI_THINKING_MODEL")
                        or os.environ.get("MECHUS_OPENAI_CHAT_MODEL", "gpt-4o-mini")
                    )
                    final_summary_inproc = generate_final_summary(
                        transcript_text,
                        state,
                        api_key=api_key,
                        model=model,
                        mode=getattr(_cfg, "summary_mode", "full"),
                        max_transcript_chars=getattr(_cfg, "summary_max_transcript_chars", 28000),
                        fast_max_tokens=getattr(_cfg, "summary_fast_max_tokens", 1400),
                        full_max_tokens=int(os.environ.get("MECHUS_SUMMARY_FULL_MAX_TOKENS", "2400") or 2400),
                        timeout_seconds=getattr(_cfg, "summary_timeout_seconds", 120),
                        prior_sessions_context=getattr(session.agent, "_cross_meeting_memory_text", None) or None,
                        participant_names=state.get("participant_names"),
                    )
                    try:
                        _eng = session.agent._get_context_engine()
                        if _eng is not None:
                            final_summary_inproc = merge_final_report_into_summary(
                                final_summary_inproc,
                                _eng.generate_final_report(),
                            )
                    except Exception as _merge_exc:
                        log.debug("Final report merge skipped: %s", _merge_exc)
                    final_summary_inproc["interactions"] = session.agent.get_mechus_interactions()
                except Exception as e:
                    log.warning("Could not generate final summary for in-process session: %s", e)

                # Stop the agent
                session.agent.stop()
                session.status = "stopping"
                
                # Wait for graceful shutdown (agent may be mid–wake cycle: STT/TTS can take 10–15s)
                stop_timeout = 20.0
                session.thread.join(timeout=stop_timeout)
                
                if session.thread.is_alive():
                    session.status = "error"
                    log.warning("Agent did not stop within %ss; leaving meeting anyway", stop_timeout)
                    # Still leave Zoom and clean up; don't return 500 so dashboard can show ended
                
                # Close browser and leave meeting
                if session.browser_driver:
                    try:
                        # Try to leave the meeting gracefully
                        try:
                            # For Google Meet: Try to find and click "Leave call" button
                            if "meet.google.com" in session.meeting_link:
                                leave_selectors = [
                                    "//button[contains(@aria-label, 'Leave')]",
                                    "//button[contains(., 'Leave')]",
                                    "//span[contains(text(), 'Leave')]/ancestor::button",
                                ]
                                for selector in leave_selectors:
                                    try:
                                        leave_btn = session.browser_driver.find_element(By.XPATH, selector)
                                        if leave_btn.is_displayed():
                                            session.browser_driver.execute_script("arguments[0].click();", leave_btn)
                                            log.info("Clicked Leave button in Google Meet")
                                            time.sleep(1)
                                            break
                                    except:
                                        continue
                            # For Zoom: Try to find and click "Leave" button
                            elif "zoom.us" in session.meeting_link:
                                leave_selectors = [
                                    "//button[contains(text(), 'Leave')]",
                                    "//button[contains(@aria-label, 'Leave')]",
                                ]
                                for selector in leave_selectors:
                                    try:
                                        leave_btn = session.browser_driver.find_element(By.XPATH, selector)
                                        if leave_btn.is_displayed():
                                            session.browser_driver.execute_script("arguments[0].click();", leave_btn)
                                            log.info("Clicked Leave button in Zoom")
                                            time.sleep(1)
                                            break
                                    except:
                                        continue
                        except Exception as e:
                            log.debug(f"Could not click Leave button: {e}")
                        
                        # Close the browser
                        try:
                            session.browser_driver.quit()
                            log.info("Browser closed and meeting left")
                        except Exception as e:
                            log.warning(f"Error closing browser: {e}")
                    except Exception as e:
                        log.warning(f"Error leaving meeting: {e}")
                
                # Leave Zoom SDK meeting if applicable
                if session.zoom_bot:
                    try:
                        session.zoom_bot.leave()
                        session.zoom_bot.cleanup()
                        log.info("Zoom SDK: left meeting and cleaned up")
                    except Exception as e:
                        log.warning("Zoom SDK leave/cleanup: %s", e)
                
                session.status = "stopped"
                stopped_link = session.meeting_link
                team_context_item = None
                transcript_ref = ""
                try:
                    if session.agent is not None and getattr(session.agent, "_note_store", None):
                        session.agent._note_store.sync()
                        transcript_ref = os.path.abspath(session.agent._note_store.path)
                except Exception:
                    pass
                _persist_meeting_transcript_ref(meeting_id, transcript_ref, stopped_link)
                if getattr(session, "share_to_team_context", False) and getattr(session, "team_id", ""):
                    try:
                        team_context_item = _add_meeting_to_team_context(
                            team_id=session.team_id,
                            meeting_id=meeting_id,
                            created_by=session.user_id,
                            final_summary=final_summary_inproc,
                        )
                    except Exception as exc:
                        log.warning("Could not add meeting to team context: %s", exc)
                _try_send_meeting_emails_on_stop(meeting_id, final_summary_inproc, session)
                _prune_session_group_child(_sgid, meeting_id)
                del _active_sessions[meeting_id]

                return jsonify({
                    "success": True,
                    "message": "Mechus session stopped and meeting left",
                    "meeting_id": meeting_id,
                    "meeting_link": stopped_link,
                    "final_summary": final_summary_inproc,
                    "team_context_item": team_context_item,
                    "transcript_path": transcript_ref or None,
                })
            
            # No meeting_id: Stop all sessions
            stopped_sessions = []
            for mid, session in list(_active_sessions.items()):
                if session.status != "stopped":
                    if session.agent is not None and session.thread is not None:
                        session.agent.stop()
                        session.status = "stopping"
                        session.thread.join(timeout=5.0)
                    
                    # Close browser and leave meeting
                    if session.browser_driver:
                        try:
                            # Try to leave meeting gracefully
                            try:
                                from selenium.webdriver.common.by import By
                                
                                if "meet.google.com" in session.meeting_link:
                                    leave_btn = session.browser_driver.find_element(By.XPATH, "//button[contains(@aria-label, 'Leave')]")
                                    if leave_btn.is_displayed():
                                        session.browser_driver.execute_script("arguments[0].click();", leave_btn)
                                        time.sleep(1)
                                elif "zoom.us" in session.meeting_link:
                                    leave_btn = session.browser_driver.find_element(By.XPATH, "//button[contains(text(), 'Leave')]")
                                    if leave_btn.is_displayed():
                                        session.browser_driver.execute_script("arguments[0].click();", leave_btn)
                                        time.sleep(1)
                            except:
                                pass
                            
                            # Close browser
                            session.browser_driver.quit()
                            log.info(f"Closed browser for meeting {mid}")
                        except Exception as e:
                            log.warning(f"Error closing browser for meeting {mid}: {e}")
                    
                    if session.zoom_bot:
                        try:
                            session.zoom_bot.leave()
                            session.zoom_bot.cleanup()
                            log.info("Zoom SDK: left meeting %s and cleaned up", mid)
                        except Exception as e:
                            log.warning("Zoom SDK leave/cleanup for %s: %s", mid, e)
                    
                    session.status = "stopped"
                    stopped_sessions.append(mid)
                    del _active_sessions[mid]
            
            return jsonify({
                "success": True,
                "message": f"Stopped {len(stopped_sessions)} session(s)",
                "stopped_meetings": stopped_sessions
            })
            
    except Exception as exc:
        log.error("Error stopping session: %s", exc, exc_info=True)
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


@app.route("/api/meeting/speakers", methods=["GET", "POST"])
def meeting_speakers():
    """
    Get or set a manual speaker mapping for a meeting.

    Storage: ``<MECHUS_NOTES_DIR>/speaker_maps/<safe_meeting_id>.json`` (also reads legacy
    ``speaker-map-<meeting_id>.json`` in the notes root when loading).

    GET query params:
    - meeting_id (required)

    POST JSON body:
    - meeting_id (required)
    - speaker_map or speakers: { "<speaker_label_or_id>": "<display_name>" } (merged with existing)

    Zoom SDK workers poll the canonical file every few seconds; in-process sessions update the live agent immediately.
    """
    if request.method == "GET":
        meeting_id = (request.args.get("meeting_id") or "").strip()
        if not meeting_id:
            return jsonify({"success": False, "error": "meeting_id parameter is required"}), 400
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "speaker_map": _load_speaker_map(meeting_id),
        })

    body = request.get_json(silent=True) or {}
    meeting_id = str(body.get("meeting_id") or "").strip()
    if not meeting_id:
        return jsonify({"success": False, "error": "meeting_id is required"}), 400
    raw_mapping = body.get("speaker_map", body.get("speakers", {}))
    if not isinstance(raw_mapping, dict):
        return jsonify({"success": False, "error": "speaker_map must be an object"}), 400
    merged = dict(_load_speaker_map(meeting_id))
    for k, v in raw_mapping.items():
        key = str(k or "").strip()
        value = str(v or "").strip()
        if key and value:
            merged[key] = value
    saved = _save_speaker_map(meeting_id, merged)
    with _session_lock:
        sess = _active_sessions.get(meeting_id)
        live_agent = sess.agent if sess else None
    if live_agent is not None:
        try:
            live_agent.merge_speaker_aliases(saved)
        except Exception as e:
            log.debug("meeting/speakers: merge on agent failed: %s", e)
    return jsonify({
        "success": True,
        "meeting_id": meeting_id,
        "speaker_map": saved,
        "path": _speaker_map_path(meeting_id),
    })


def _ended_transcript_summary_fields(meeting_id: str) -> dict[str, Any]:
    """Attach executive recap from final_summary.json when the session has ended."""
    from mechus.meeting_summary import (
        actionable_items_from_summary,
        is_executive_summary_raw_transcript_dump,
        read_final_summary,
    )

    fs = read_final_summary(meeting_id)
    if not fs:
        return {"summary": "", "final_summary": None, "actionable_items": []}
    exec_sum = str(fs.get("executive_summary") or "").strip()
    if not exec_sum or is_executive_summary_raw_transcript_dump(exec_sum):
        exec_sum = ""
    return {
        "summary": exec_sum,
        "final_summary": fs,
        "actionable_items": actionable_items_from_summary(fs),
    }


def _live_transcript_recap_fields(
    *,
    agent: Any = None,
    worker_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rolling recap for an in-progress meeting (worker-status snapshot or live agent)."""
    from mechus.meeting_summary import build_live_partial_summary, live_recap_to_transcript_payload

    fs: dict[str, Any] | None = None
    if agent is not None:
        try:
            if hasattr(agent, "get_live_recap_for_dashboard"):
                fs = agent.get_live_recap_for_dashboard()
            elif hasattr(agent, "get_live_recap_state_light"):
                transcript_text, state = agent.get_live_recap_state_light()
                fs = build_live_partial_summary(transcript_text, state)
        except Exception:
            fs = None
    elif worker_status:
        lr = worker_status.get("live_recap")
        if isinstance(lr, dict) and lr:
            fs = lr
    return live_recap_to_transcript_payload(fs)


@app.route("/api/transcript", methods=["GET"])
def get_transcript():
    """
    Get the transcript for a meeting session.
    
    Query parameters:
    - meeting_id (required): The meeting ID to get transcript for
    - tag (optional): If set, only return transcript rows whose tags list contains this string (case-insensitive).
    
    After POST /api/stop, the in-memory session is removed; if worker-status-<id>.json
    still lists transcript_path (JSONL on disk), the same payload is returned with
    session_ended=true instead of 404.
    """
    global _active_sessions
    
    meeting_id = request.args.get("meeting_id")
    if not meeting_id:
        return jsonify({
            "success": False,
            "error": "meeting_id parameter is required"
        }), 400
    tag_filter = (request.args.get("tag") or "").strip().lower()

    with _session_lock:
        session = _active_sessions.get(meeting_id)

    if session is None:
        tp = _transcript_jsonl_path_for_ended_meeting(meeting_id)
        ws = _read_worker_status_json(meeting_id)
        if not tp and ws.get("transcript_path"):
            tp = _coerce_worker_transcript_path_to_api(str(ws.get("transcript_path") or ""))
        if tp or ws.get("status") in ("joining", "active", "stopped", "error"):
            if not tp:
                tp = os.path.join(
                    os.path.abspath(_notes_dir()),
                    f"{_safe_notes_filename_id(meeting_id)}.jsonl",
                )
            formatted_transcripts, wake_cycles, speaker_map, _thoughts_hist = _load_formatted_transcripts_from_jsonl(tp, meeting_id)
            if tag_filter:
                formatted_transcripts = [
                    row for row in formatted_transcripts
                    if tag_filter in [str(x).lower() for x in (row.get("tags") or [])]
                ]
            participants = ws.get("participants")
            if not isinstance(participants, dict):
                participants = {}
            ws_status = str(ws.get("status") or "").strip().lower()
            is_live_ws = ws_status in ("starting", "joining", "active")
            if is_live_ws:
                _recap_fields = _live_transcript_recap_fields(worker_status=ws)
                session_ended = False
            else:
                _recap_fields = _ended_transcript_summary_fields(meeting_id)
                session_ended = True
            return jsonify({
                "success": True,
                "meeting_id": meeting_id,
                "session_id": None,
                "meeting_link": str(ws.get("meeting_link") or ""),
                "transcripts": formatted_transcripts,
                "wake_cycles": wake_cycles,
                "thoughts": _thoughts_hist,
                "actionable_items": _recap_fields.get("actionable_items") or [],
                "summary": _recap_fields.get("summary") or "",
                "final_summary": _recap_fields.get("final_summary"),
                "live_recap": bool(_recap_fields.get("live_recap")) if is_live_ws else False,
                "speaker_map": speaker_map,
                "participants": participants,
                "speaker_attribution_mode": str(ws.get("speaker_attribution_mode") or ""),
                "total_chunks": len(formatted_transcripts),
                "total_wake_cycles": len(wake_cycles),
                "session_ended": session_ended,
            })
        return jsonify({
            "success": False,
            "error": f"No active session for meeting {meeting_id}"
        }), 404

    if session.agent is None:
        # FaceTime (and similar): MechusAgent runs in a worker container; read JSONL via worker-status path.
        if getattr(session, "worker_container", None):
            ws_live = _read_worker_status_json(meeting_id)
            tp_use = _coerce_worker_transcript_path_to_api(str(ws_live.get("transcript_path") or "").strip())
            if tp_use:
                formatted_transcripts, wake_cycles, speaker_map, _thoughts_hist = _load_formatted_transcripts_from_jsonl(
                    tp_use, meeting_id
                )
                if tag_filter:
                    formatted_transcripts = [
                        row for row in formatted_transcripts
                        if tag_filter in [str(x).lower() for x in (row.get("tags") or [])]
                    ]
                participants = ws_live.get("participants")
                if not isinstance(participants, dict):
                    participants = {}
                _live = _live_transcript_recap_fields(worker_status=ws_live)
                return jsonify({
                    "success": True,
                    "meeting_id": meeting_id,
                    "session_id": None,
                    "meeting_link": session.meeting_link,
                    "transcripts": formatted_transcripts,
                    "wake_cycles": wake_cycles,
                    "thoughts": _thoughts_hist,
                    "actionable_items": _live.get("actionable_items") or [],
                    "summary": _live.get("summary") or "",
                    "final_summary": _live.get("final_summary"),
                    "live_recap": bool(_live.get("live_recap")),
                    "speaker_map": speaker_map,
                    "participants": participants,
                    "speaker_attribution_mode": str(ws_live.get("speaker_attribution_mode") or ""),
                    "total_chunks": len(formatted_transcripts),
                    "total_wake_cycles": len(wake_cycles),
                })
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "session_id": None,
            "meeting_link": session.meeting_link,
            "transcripts": [],
            "wake_cycles": [],
            "actionable_items": [],
            "summary": "",
            "total_chunks": 0,
            "total_wake_cycles": 0,
        })

    try:
        # Get the note store path from the agent
        note_store = session.agent._note_store
        transcript_path = note_store.path
        formatted_transcripts, wake_cycles, speaker_map, _thoughts_hist = _load_formatted_transcripts_from_jsonl(
            transcript_path, meeting_id
        )
        if tag_filter:
            formatted_transcripts = [
                row for row in formatted_transcripts
                if tag_filter in [str(x).lower() for x in (row.get("tags") or [])]
            ]

        # Extract actionable items from memory using Mechus's memory system (meeting-scoped)
        _live = _live_transcript_recap_fields(agent=session.agent)
        actionable_items = list(_live.get("actionable_items") or [])
        if not actionable_items:
            memory = session.agent.get_memory(meeting_id)
            if memory:
                action_items_text = memory.retrieve("action items next steps todo tasks")
                if action_items_text:
                    items = re.split(r'[•\-\n]+', action_items_text)
                    for item in items:
                        item = item.strip()
                        if item and len(item) > 10:
                            actionable_items.append(item)
                    actionable_items = list(dict.fromkeys(actionable_items))[:10]

        summary = str(_live.get("summary") or "").strip()
        if not summary:
            memory = session.agent.get_memory(meeting_id)
            if memory:
                summary_text = memory.retrieve("summary of everything discussed so far")
                if summary_text:
                    summary = summary_text

        final_summary = _live.get("final_summary")
        # Prefer live stream thoughts (in memory); fall back to JSONL-persisted history
        _live_thoughts = session.agent.get_thoughts_for_status() if hasattr(session.agent, "get_thoughts_for_status") else []
        return jsonify({
            "success": True,
            "meeting_id": meeting_id,
            "session_id": session.agent._session_id,
            "meeting_link": session.meeting_link,
            "transcripts": formatted_transcripts,
            "wake_cycles": wake_cycles,
            "thoughts": _live_thoughts or _thoughts_hist,
            "actionable_items": list(set(actionable_items))[:10],
            "summary": summary,
            "final_summary": final_summary,
            "live_recap": bool(_live.get("live_recap")),
            "speaker_map": speaker_map,
            "participants": getattr(session, "participants", {}) or {},
            "speaker_attribution_mode": getattr(session, "speaker_attribution_mode", "") or "",
            "total_chunks": len(formatted_transcripts),
            "total_wake_cycles": len(wake_cycles),
        })

    except Exception as exc:
        log.error("Error getting transcript: %s", exc, exc_info=True)
        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


# --- Share link (Open Graph page + PNG card) ---------------------------------

_SHARE_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{{ title }}</title>
  <meta property="og:title" content="{{ title }}"/>
  <meta property="og:description" content="{{ description }}"/>
  <meta property="og:image" content="{{ image_url }}"/>
  <meta property="og:type" content="website"/>
  <meta name="twitter:card" content="summary_large_image"/>
  <meta name="twitter:title" content="{{ title }}"/>
  <meta name="twitter:description" content="{{ description }}"/>
  <meta name="twitter:image" content="{{ image_url }}"/>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: #1a1a2e; color: #f4f4f5; margin: 0; padding: 2rem; min-height: 100vh; }
    .card { max-width: 640px; margin: 0 auto; }
    .badge { display: inline-block; background: #6366f1; color: #fff; padding: 0.35rem 0.85rem; border-radius: 8px; font-size: 0.85rem; font-weight: 600; }
    h1 { font-size: 1.35rem; font-weight: 600; margin: 1rem 0 0.5rem; line-height: 1.3; }
    .when { color: #a1a1aa; font-size: 0.95rem; margin-bottom: 1rem; }
    ul { color: #d4d4d8; padding-left: 1.25rem; }
    li { margin: 0.35rem 0; }
    .preview { margin-top: 1.5rem; border-radius: 12px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
    .preview img { display: block; width: 100%; height: auto; vertical-align: middle; }
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">{{ badge }}</span>
    <h1>{{ title }}</h1>
    <p class="when">{{ when_line }}</p>
    {% if details %}
    <ul>
      {% for d in details %}<li>{{ d }}</li>{% endfor %}
    </ul>
    {% endif %}
    <div class="preview">
      <img src="{{ image_url }}" alt="" width="1200" height="630" loading="lazy"/>
    </div>
  </div>
</body>
</html>"""


def _safe_meeting_id_for_share(meeting_id: str) -> bool:
    if not meeting_id or len(meeting_id) > 128:
        return False
    return bool(re.match(r"^[a-zA-Z0-9._-]+$", meeting_id))


def _absolute_public_url(path: str) -> str:
    """Absolute URL for OG tags. Set MECHUS_PUBLIC_BASE_URL when the API is behind a public HTTPS host."""
    path = path if path.startswith("/") else "/" + path
    base = os.getenv("MECHUS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}{path}"
    return request.url_root.rstrip("/") + path


def _share_context_for_meeting(meeting_id: str) -> dict[str, Any]:
    from mechus.meeting_summary import read_final_summary
    from mechus.share_card import build_share_context

    fs = read_final_summary(meeting_id)
    session_meeting_link = None
    session_started_at: Optional[float] = None
    session_status = None
    with _session_lock:
        sess = _active_sessions.get(meeting_id)
        if sess:
            session_meeting_link = sess.meeting_link
            session_started_at = sess.started_at
            session_status = sess.status
    ws = _read_worker_status_json(meeting_id)
    return build_share_context(
        meeting_id,
        final_summary=fs,
        session_meeting_link=session_meeting_link,
        session_started_at=session_started_at,
        session_status=session_status,
        worker_status=ws,
    )


@app.route("/share/<meeting_id>", methods=["GET"])
def share_meeting_page(meeting_id: str):
    """Landing page with Open Graph / Twitter Card meta tags and recap preview image."""
    if not _safe_meeting_id_for_share(meeting_id):
        return jsonify({"error": "Invalid meeting_id"}), 400
    ctx = _share_context_for_meeting(meeting_id)
    desc_parts = [ctx["when_line"]]
    if ctx.get("detail_lines"):
        desc_parts.append(" · ".join(ctx["detail_lines"]))
    description = " · ".join(desc_parts)[:500]
    image_url = _absolute_public_url(f"/share/{meeting_id}/card.png")
    return render_template_string(
        _SHARE_PAGE_HTML,
        title=ctx["title"],
        description=description,
        image_url=image_url,
        when_line=ctx["when_line"],
        details=ctx.get("detail_lines") or [],
        badge=ctx.get("badge") or "Recap",
    )


@app.route("/share/<meeting_id>/card.png", methods=["GET"])
def share_meeting_card_png(meeting_id: str):
    """1200×630 PNG for chat/social link previews (meeting title, time, summary hints)."""
    if not _safe_meeting_id_for_share(meeting_id):
        return jsonify({"error": "Invalid meeting_id"}), 400
    from mechus.share_card import render_share_card_png

    ctx = _share_context_for_meeting(meeting_id)
    png = render_share_card_png(ctx)
    if png is None:
        return jsonify({"error": "Pillow (pillow) is required for share images: pip install pillow"}), 503
    return Response(
        png,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.route("/api/meetings/<meeting_id>/share", methods=["GET"])
def api_meeting_share_urls(meeting_id: str):
    """Return share_url and image_url for embedding in a dashboard copy-link flow."""
    if not _safe_meeting_id_for_share(meeting_id):
        return jsonify({"success": False, "error": "Invalid meeting_id"}), 400
    share_url = _absolute_public_url(f"/share/{meeting_id}")
    image_url = _absolute_public_url(f"/share/{meeting_id}/card.png")
    ctx = _share_context_for_meeting(meeting_id)
    return jsonify({
        "success": True,
        "meeting_id": meeting_id,
        "share_url": share_url,
        "image_url": image_url,
        "title": ctx.get("title"),
        "when_line": ctx.get("when_line"),
        "detail_lines": ctx.get("detail_lines") or [],
    })


def main() -> None:
    """Run the API server. For production use gunicorn (see PRODUCTION.md)."""
    env_file = os.getenv("MECHUS_ENV_FILE", ".env")
    load_env_file(env_file)
    if not logging.getLogger().handlers:
        level_name = os.getenv("MECHUS_LOG_LEVEL", "INFO").strip().upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    port = int(os.getenv("MECHUS_API_PORT", "5000"))
    host = os.getenv("MECHUS_API_HOST", "127.0.0.1")
    debug = os.getenv("MECHUS_API_DEBUG", "false").lower() in ("true", "1", "yes")
    if _API_KEY:
        log.info("API key auth enabled (MECHUS_API_KEY set)")
    if _cors_origins:
        log.info("CORS restricted to: %s", _cors_origins)
    global _deadline_reminder_started
    if not _deadline_reminder_started:
        _deadline_reminder_started = True
        def _deadline_reminder_loop() -> None:
            from mechus.email_sender import process_due_deadline_reminders
            while True:
                try:
                    process_due_deadline_reminders()
                except Exception:
                    log.debug("Deadline reminder loop error", exc_info=True)
                try:
                    sleep_s = int(os.getenv("MECHUS_EMAIL_REMINDER_POLL_SECONDS", "300"))
                except ValueError:
                    sleep_s = 300
                time.sleep(max(30, sleep_s))
        threading.Thread(
            target=_deadline_reminder_loop,
            daemon=True,
            name="MechusDeadlineReminderLoop",
        ).start()
        log.info("Deadline reminder loop started")
    log.info("Starting Mechus API server on %s:%s", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    main()
