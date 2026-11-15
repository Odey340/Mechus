"""Load Zoom OAuth + ZAK from Supabase auth app_metadata (dashboard Zoom connect)."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ZAK_TTL_S = 115 * 60
TOKEN_PAD_S = 5 * 60


@dataclass
class ZoomUserCredentials:
    access_token: str | None
    zak_token: str | None
    source: str = ""


def _supabase_admin_config() -> tuple[str, str] | None:
    url = (os.getenv("KOOV_SUPABASE_URL") or "").strip()
    key = (os.getenv("KOOV_SUPABASE_SERVICE_KEY") or "").strip()
    if url and key:
        return url.rstrip("/"), key
    return None


def _zoom_oauth_client() -> tuple[str, str] | None:
    for id_key, secret_key in (
        ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
        ("ZOOM_OAUTH_CLIENT_ID", "ZOOM_OAUTH_CLIENT_SECRET"),
    ):
        cid = (os.getenv(id_key) or "").strip()
        sec = (os.getenv(secret_key) or "").strip()
        if cid and sec:
            return cid, sec
    return None


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any] | None:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
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
        log.warning("HTTP %s %s failed: %s %s", method, url, exc.code, detail)
        return None
    except Exception as exc:
        log.warning("HTTP %s %s failed: %s", method, url, exc)
        return None


def _refresh_zoom_access_token(refresh_token: str) -> dict[str, Any] | None:
    oauth = _zoom_oauth_client()
    if not oauth:
        return None
    client_id, client_secret = oauth
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    return _http_json(
        "https://zoom.us/oauth/token",
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {auth}",
        },
        body=body,
    )


def _fetch_zak(access_token: str) -> str | None:
    data = _http_json(
        "https://api.zoom.us/v2/users/me/token?type=zak",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not data:
        return None
    tok = (data.get("token") or "").strip()
    return tok or None


def _supabase_get_user(user_id: str) -> dict[str, Any] | None:
    cfg = _supabase_admin_config()
    if not cfg:
        return None
    base, key = cfg
    data = _http_json(
        f"{base}/auth/v1/admin/users/{urllib.parse.quote(user_id)}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
        },
    )
    return data


def _supabase_patch_app_metadata(user_id: str, patch: dict[str, Any]) -> None:
    cfg = _supabase_admin_config()
    if not cfg:
        return
    base, key = cfg
    current = _supabase_get_user(user_id) or {}
    meta = dict(current.get("app_metadata") or {})
    meta.update(patch)
    body = json.dumps({"app_metadata": meta}).encode()
    _http_json(
        f"{base}/auth/v1/admin/users/{urllib.parse.quote(user_id)}",
        method="PUT",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        body=body,
    )


def _looks_like_supabase_encrypted_token(value: str | None) -> bool:
    """user_metadata tokens are encrypted by Supabase and unusable with Zoom."""
    if not value or not value.startswith("eyJ"):
        return False
    try:
        padded = value.split(".")[0] + "=="
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return False
    return decoded.startswith('{"sv"') or decoded.startswith('{"v"')


def fetch_zoom_credentials_for_user(user_id: str) -> ZoomUserCredentials | None:
    """Best-effort Zoom tokens for ``user_id`` from Supabase auth app_metadata."""
    uid = (user_id or "").strip()
    if not uid or uid == "default":
        return None
    if not _supabase_admin_config():
        return None

    user = _supabase_get_user(uid)
    if not user:
        return None

    user_meta = user.get("user_metadata") or {}
    if not isinstance(user_meta, dict):
        user_meta = {}

    meta = user.get("app_metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    # Prefer app_metadata; fall back to legacy user_metadata if tokens are plain (not Supabase-encrypted).
    access = (meta.get("zoom_access_token") or "").strip() or None
    refresh = (meta.get("zoom_refresh_token") or "").strip() or None
    if not access and not refresh and user_meta.get("zoom_connected"):
        um_access = (user_meta.get("zoom_access_token") or "").strip() or None
        um_refresh = (user_meta.get("zoom_refresh_token") or "").strip() or None
        if um_access and not _looks_like_supabase_encrypted_token(um_access):
            access, refresh = um_access, um_refresh
            if not meta.get("zoom_expires_at") and user_meta.get("zoom_expires_at"):
                meta = {**meta, "zoom_expires_at": user_meta.get("zoom_expires_at")}
        elif um_refresh and not _looks_like_supabase_encrypted_token(um_refresh):
            refresh = um_refresh

    if not access and not refresh:
        return None

    if _looks_like_supabase_encrypted_token(access):
        log.debug("Zoom: user %s has encrypted legacy tokens; using server credentials", uid[:8])
        return None

    expires_at_ms = meta.get("zoom_expires_at")
    try:
        expires_at_ms = int(expires_at_ms) if expires_at_ms is not None else 0
    except (TypeError, ValueError):
        expires_at_ms = 0

    now_ms = int(time.time() * 1000)
    if access and expires_at_ms and now_ms >= expires_at_ms - TOKEN_PAD_S * 1000:
        if refresh:
            refreshed = _refresh_zoom_access_token(refresh)
            if refreshed and refreshed.get("access_token"):
                access = str(refreshed["access_token"]).strip()
                new_refresh = (refreshed.get("refresh_token") or refresh).strip()
                expires_in = int(refreshed.get("expires_in") or 3600)
                _supabase_patch_app_metadata(
                    uid,
                    {
                        "zoom_access_token": access,
                        "zoom_refresh_token": new_refresh,
                        "zoom_expires_at": now_ms + expires_in * 1000,
                    },
                )
                log.info("Zoom: refreshed access token for user %s", uid[:8])
            else:
                log.warning("Zoom: access token expired for user %s and refresh failed", uid[:8])
                access = None
        else:
            access = None

    zak = (meta.get("zak_token") or "").strip() or None
    zak_fetched_at = meta.get("zak_fetched_at")
    try:
        zak_fetched_at = int(zak_fetched_at) if zak_fetched_at is not None else 0
    except (TypeError, ValueError):
        zak_fetched_at = 0

    zak_stale = not zak or (time.time() * 1000 - zak_fetched_at) > ZAK_TTL_S * 1000
    if access and zak_stale:
        fetched = _fetch_zak(access)
        if fetched:
            zak = fetched
            _supabase_patch_app_metadata(
                uid,
                {"zak_token": zak, "zak_fetched_at": int(time.time() * 1000)},
            )
            log.info("Zoom: fetched ZAK for user %s", uid[:8])

    if not access and not zak:
        return None

    return ZoomUserCredentials(access_token=access, zak_token=zak, source="supabase_app_metadata")
