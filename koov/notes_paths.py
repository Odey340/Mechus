"""Resolve writable notes + meeting-store directories (Docker + local dev)."""

from __future__ import annotations

import os


def resolve_notes_dir() -> str:
    """Absolute path for transcript JSONL, worker-status, speaker maps."""
    raw = (os.environ.get("KOOV_NOTES_DIR") or "").strip()
    if not raw:
        raw = "/app/koov_notes" if os.path.isdir("/app/koov_notes") else "koov_notes"
    return os.path.abspath(os.path.expanduser(raw))


def resolve_meeting_store_dir(*, notes_dir: str | None = None) -> str:
    """Absolute path for per-meeting chunks, snapshots, final_summary.json."""
    explicit = (os.environ.get("KOOV_MEETING_STORE_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    base = notes_dir if notes_dir is not None else resolve_notes_dir()
    return os.path.join(base, "koov_meetings")


def apply_notes_paths_to_env(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Ensure dirs exist; set KOOV_NOTES_DIR and KOOV_MEETING_STORE_DIR. Returns (notes, store)."""
    target = os.environ if env is None else env
    notes = resolve_notes_dir() if env is None else _resolve_notes_dir_from(target)
    store = (
        resolve_meeting_store_dir(notes_dir=notes)
        if env is None
        else _resolve_meeting_store_dir_from(target, notes_dir=notes)
    )
    os.makedirs(notes, exist_ok=True)
    os.makedirs(store, exist_ok=True)
    target["KOOV_NOTES_DIR"] = notes
    target["KOOV_MEETING_STORE_DIR"] = store
    return notes, store


def _resolve_notes_dir_from(env: dict[str, str]) -> str:
    raw = (env.get("KOOV_NOTES_DIR") or "").strip()
    if not raw:
        raw = "/app/koov_notes" if os.path.isdir("/app/koov_notes") else "koov_notes"
    return os.path.abspath(os.path.expanduser(raw))


def _resolve_meeting_store_dir_from(env: dict[str, str], *, notes_dir: str) -> str:
    explicit = (env.get("KOOV_MEETING_STORE_DIR") or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(notes_dir, "koov_meetings")
