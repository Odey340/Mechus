"""
Store for meeting links set by the dashboard, used when RTMS webhook fires.

Dashboard pastes link -> POST /api/set-meeting-for-rtms -> store.
RTMS webhook -> look up stored meeting for SDK join (TTS).
File-based so api_server and rtms_koov can share state across processes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from koov.zoom_sdk_join import parse_zoom_link

log = logging.getLogger(__name__)

_STORE_PATH = os.getenv("KOOV_RTMS_MEETING_STORE", ".rtms_meetings.json")
_STORE_LOCK = threading.Lock()
_TTL_SECONDS = float(os.getenv("KOOV_RTMS_MEETING_TTL", "600"))  # 10 min default


def _load() -> dict:
    if not os.path.isfile(_STORE_PATH):
        return {}
    try:
        with open(_STORE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log.warning("RTMS meeting store load: %s", e)
        return {}


def _save(data: dict) -> None:
    try:
        with open(_STORE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("RTMS meeting store save: %s", e)


def set_meeting(meeting_link: str, user_id: str = "default") -> Optional[tuple[int, Optional[str]]]:
    """
    Store meeting link from dashboard. Returns (meeting_number, password) or None if invalid.
    """
    link = (meeting_link or "").strip()
    if not link:
        return None
    meeting_number, password = parse_zoom_link(link)
    if meeting_number is None:
        return None
    with _STORE_LOCK:
        data = _load()
        data["pending"] = {
            "meeting_link": link,
            "meeting_number": meeting_number,
            "password": password,
            "user_id": user_id,
            "set_at": time.time(),
        }
        _save(data)
    log.info("RTMS meeting store: set meeting %s for user %s", meeting_number, user_id)
    return meeting_number, password


def get_meeting() -> Optional[tuple[int, Optional[str]]]:
    """
    Get stored meeting for RTMS webhook. Returns (meeting_number, password) or None.
    Only returns if set within TTL and link is valid.
    """
    with _STORE_LOCK:
        data = _load()
        pending = data.get("pending")
        if not pending:
            return None
        set_at = pending.get("set_at", 0)
        if time.time() - set_at > _TTL_SECONDS:
            log.info("RTMS meeting store: pending expired (set %.0fs ago)", time.time() - set_at)
            return None
        num = pending.get("meeting_number")
        pwd = pending.get("password")
        if num is not None:
            return int(num), (pwd if pwd else None)
    return None
