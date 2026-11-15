"""Fetch Zoom join tokens (recording + ZAK) for Meeting SDK cross-account joins."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


def is_likely_external_zoom_link(link: str) -> bool:
    """True for vanity Zoom hosts (e.g. osu.zoom.us) that usually need OAuth/ZAK to join."""
    m = re.search(r"https?://([^/]+)/j/", (link or "").strip(), re.I)
    if not m:
        return False
    host = m.group(1).lower().split(":")[0]
    if host in ("zoom.us", "www.zoom.us"):
        return False
    if re.fullmatch(r"us\d+web\.zoom\.us", host):
        return False
    return host.endswith(".zoom.us")


def _zoom_api_get_json(url: str, access_token: str, *, timeout_s: float = 15.0) -> dict[str, Any] | None:
    token = (access_token or "").strip()
    if not token:
        return None
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        log.warning("Zoom API GET %s failed: HTTP %s %s", url, exc.code, detail)
        return None
    except Exception as exc:
        log.warning("Zoom API GET %s failed: %s", url, exc)
        return None


def fetch_local_recording_token(meeting_number: int, access_token: str) -> str | None:
    """Pre-grant local recording + optional waiting-room bypass (~120s TTL)."""
    if meeting_number <= 0:
        return None
    url = (
        f"https://api.zoom.us/v2/meetings/{int(meeting_number)}"
        "/jointoken/local_recording?bypass_waiting_room=true"
    )
    data = _zoom_api_get_json(url, access_token)
    if not data:
        return None
    tok = (data.get("token") or "").strip()
    return tok or None


def fetch_zak_token(access_token: str) -> str | None:
    """ZAK token — required for many cross-account Meeting SDK joins (2026+)."""
    data = _zoom_api_get_json(
        "https://api.zoom.us/v2/users/me/token?type=zak",
        access_token,
    )
    if not data:
        return None
    tok = (data.get("token") or "").strip()
    return tok or None


def resolve_zoom_oauth_access_token(data: dict | None) -> str:
    """OAuth access token from join body or server env (dashboard Zoom connect)."""
    if isinstance(data, dict):
        for key in (
            "zoom_access_token",
            "zoom_oauth_access_token",
            "oauth_access_token",
            "access_token",
        ):
            raw = (data.get(key) or "").strip()
            if raw:
                return raw
    import os

    return (os.getenv("ZOOM_OAUTH_ACCESS_TOKEN") or "").strip()


def resolve_zoom_join_tokens(
    *,
    meeting_number: int,
    oauth_access_token: str,
    recording_token: str | None = None,
    zak_token: str | None = None,
) -> tuple[str | None, str | None, list[str], list[str]]:
    """Fill missing recording/ZAK tokens using Zoom REST when OAuth is available.

    Returns (recording_token, zak_token, warnings, errors).
    """
    warnings: list[str] = []
    errors: list[str] = []
    rec = (recording_token or "").strip() or None
    zak = (zak_token or "").strip() or None
    oauth = (oauth_access_token or "").strip()
    if not oauth:
        if not rec and not zak:
            warnings.append(
                "No Zoom OAuth token — same-account meetings may still join with Meeting SDK JWT only. "
                "External org links need server OAuth (ZOOM_ACCOUNT_ID) or zak_token in the join body."
            )
        return rec, zak, warnings, errors

    if not rec:
        rec = fetch_local_recording_token(meeting_number, oauth)
        if rec:
            log.info("Zoom: fetched local_recording join token for meeting %s", meeting_number)
        else:
            warnings.append(
                "Could not fetch Zoom recording token — host may need to allow recording "
                "or admit Mechus from the waiting room."
            )
    if not zak:
        zak = fetch_zak_token(oauth)
        if zak:
            log.info("Zoom: fetched ZAK token for cross-account join")
        else:
            msg = (
                "Could not fetch Zoom ZAK token — check server Zoom OAuth config "
                "(ZOOM_ACCOUNT_ID + S2S credentials) or pass zak_token in the join body."
            )
            warnings.append(msg)
            errors.append(msg)
    return rec, zak, warnings, errors
