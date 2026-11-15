"""Canonical speaker label override file path + JSON parsing (API server + Zoom worker).

On disk (per meeting): ``<MECHUS_NOTES_DIR>/speaker_maps/<safe_meeting_id>.json`` with shape::

    {"meeting_id": "...", "speakers": {"zoom:123": "Alice", "Speaker A": "Bob"}, "updated_at": <unix>}

Legacy flat files (same directory or root notes dir) are still readable.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


def safe_meeting_id_component(meeting_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", (meeting_id or "").strip())


def speaker_maps_dir(notes_dir: str) -> str:
    path = os.path.join(notes_dir, "speaker_maps")
    os.makedirs(path, exist_ok=True)
    return path


def speaker_map_file_path(notes_dir: str, meeting_id: str) -> str:
    """Canonical path written by ``_save_speaker_map`` / ``POST /api/meeting/speakers``."""
    safe = safe_meeting_id_component(meeting_id)
    return os.path.join(speaker_maps_dir(notes_dir), f"{safe}.json")


def legacy_speaker_map_path(notes_dir: str, meeting_id: str) -> str:
    """Pre-unification path (flat JSON in notes root). Still read for migration."""
    return os.path.join(notes_dir, f"speaker-map-{meeting_id}.json")


def parse_speaker_map_json(data: Any) -> dict[str, str]:
    """Parse wrapped ``{speakers: {...}}`` or flat ``{k: v}`` into a string map."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("speakers", data)
    if not isinstance(inner, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in inner.items():
        key = str(k or "").strip()
        if not key or key in ("meeting_id", "updated_at"):
            continue
        if isinstance(v, (dict, list)):
            continue
        value = str(v).strip()
        if value:
            out[key] = value
    return out


def load_speaker_map_from_path(path: str) -> dict[str, str]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}
    return parse_speaker_map_json(data)


def load_speaker_map_for_meeting(notes_dir: str, meeting_id: str) -> dict[str, str]:
    """Load legacy root file if present, then canonical ``speaker_maps/`` (canonical wins on key clash)."""
    canonical = speaker_map_file_path(notes_dir, meeting_id)
    legacy = legacy_speaker_map_path(notes_dir, meeting_id)
    merged: dict[str, str] = {}
    if os.path.isfile(legacy):
        merged.update(load_speaker_map_from_path(legacy))
    if os.path.isfile(canonical):
        merged.update(load_speaker_map_from_path(canonical))
    return merged


def speaker_map_watch_paths(notes_dir: str, meeting_id: str) -> list[str]:
    """Paths to poll for mtime changes (canonical first, then legacy)."""
    canonical = speaker_map_file_path(notes_dir, meeting_id)
    legacy = legacy_speaker_map_path(notes_dir, meeting_id)
    out: list[str] = []
    for p in (canonical, legacy):
        if p not in out:
            out.append(p)
    return out
