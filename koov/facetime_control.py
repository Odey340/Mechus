"""Atomic control files shared by the FaceTime API and worker containers."""

from __future__ import annotations

from koov.filelock import lock_exclusive, unlock
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CAMERA_MODES = frozenset(
    {"off", "avatar", "activity", "listening", "thinking", "speaking", "status_card", "message_card"}
)
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_control_id(value: str) -> str:
    out = _SAFE_ID_RE.sub("_", str(value or "").strip())[:200]
    if not out:
        raise ValueError("meeting_id is required")
    return out


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def normalize_card_text(value: object, max_chars: int = 200) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = " ".join(text.replace("\x00", "").split())
    if len(text) > max_chars:
        raise ValueError(f"camera card text must be at most {max_chars} characters")
    return text


@dataclass(frozen=True)
class CameraDesiredState:
    schema_version: int
    revision: int
    meeting_id: str
    enabled: bool
    mode: str
    text: str
    expires_at: float | None
    requested_at: float

    @classmethod
    def from_dict(cls, value: object, *, expected_meeting_id: str = "") -> "CameraDesiredState":
        if not isinstance(value, dict):
            raise ValueError("camera control must be an object")
        meeting_id = str(value.get("meeting_id") or "").strip()
        if not meeting_id or (expected_meeting_id and meeting_id != expected_meeting_id):
            raise ValueError("camera control meeting_id mismatch")
        mode = str(value.get("mode") or "avatar").strip().lower()
        if mode not in CAMERA_MODES:
            raise ValueError(f"unknown camera mode: {mode}")
        revision = int(value.get("revision") or 0)
        if revision < 0:
            raise ValueError("camera control revision must be non-negative")
        expires_raw = value.get("expires_at")
        expires_at = float(expires_raw) if expires_raw is not None else None
        return cls(
            schema_version=int(value.get("schema_version") or 1),
            revision=revision,
            meeting_id=meeting_id,
            enabled=bool(value.get("enabled")),
            mode=mode,
            text=normalize_card_text(value.get("text") or ""),
            expires_at=expires_at,
            requested_at=float(value.get("requested_at") or 0.0),
        )


def camera_control_path(notes_dir: str | os.PathLike[str], meeting_id: str) -> Path:
    return Path(notes_dir) / f"camera-control-{safe_control_id(meeting_id)}.json"


def read_camera_control(notes_dir: str | os.PathLike[str], meeting_id: str) -> CameraDesiredState | None:
    path = camera_control_path(notes_dir, meeting_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid camera control file: {exc}") from exc
    return CameraDesiredState.from_dict(value, expected_meeting_id=meeting_id)


def write_camera_control(
    notes_dir: str | os.PathLike[str],
    meeting_id: str,
    *,
    enabled: bool,
    mode: str,
    text: object = "",
    duration_seconds: float | None = None,
) -> CameraDesiredState:
    mode = str(mode or "avatar").strip().lower()
    if mode not in CAMERA_MODES:
        raise ValueError(f"unknown camera mode: {mode}")
    card_text = normalize_card_text(text)
    duration: float | None = None
    if duration_seconds is not None:
        duration = float(duration_seconds)
        if duration < 1.0 or duration > 60.0:
            raise ValueError("duration_seconds must be between 1 and 60")
    base = Path(notes_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = camera_control_path(base, meeting_id)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        lock_exclusive(lock_handle)
        old_revision = 0
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            old_revision = int(old.get("revision") or 0) if isinstance(old, dict) else 0
        except (OSError, ValueError, TypeError):
            pass
        now = time.time()
        state = CameraDesiredState(
            schema_version=1,
            revision=old_revision + 1,
            meeting_id=str(meeting_id),
            enabled=bool(enabled),
            mode=mode,
            text=card_text,
            expires_at=(now + duration) if duration is not None else None,
            requested_at=now,
        )
        _atomic_json(path, asdict(state))
        unlock(lock_handle)
    return state


def _vision_root(notes_dir: str | os.PathLike[str], meeting_id: str) -> Path:
    return Path(notes_dir) / f"worker-commands-{safe_control_id(meeting_id)}"


def enqueue_vision_request(
    notes_dir: str | os.PathLike[str], meeting_id: str, question: object, *, ttl_s: float = 30.0
) -> dict[str, Any]:
    text = unicodedata.normalize("NFC", str(question or "")).strip()
    if not text:
        raise ValueError("question is required")
    if len(text) > 500:
        raise ValueError("question must be at most 500 characters")
    ttl = max(5.0, min(float(ttl_s), 120.0))
    now = time.time()
    request_id = str(uuid.uuid4())
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "kind": "vision_query",
        "meeting_id": str(meeting_id),
        "question": text,
        "requested_at": now,
        "expires_at": now + ttl,
    }
    _atomic_json(_vision_root(notes_dir, meeting_id) / "pending" / f"{request_id}.json", payload)
    return payload


def vision_request_status(
    notes_dir: str | os.PathLike[str], meeting_id: str, request_id: str
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(request_id or "")):
        return None
    root = _vision_root(notes_dir, meeting_id)
    for state in ("done", "processing", "pending"):
        path = root / state / f"{request_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError):
            return {"request_id": request_id, "status": "failed", "error": "invalid result file"}
        if isinstance(value, dict):
            return {**value, "status": value.get("status") or state}
    return None

