"""Server-level Zoom OAuth (no per-user Connect Zoom required).

Uses, in order:
  1. Cached Server-to-Server OAuth (``ZOOM_ACCOUNT_ID`` + S2S client id/secret)
  2. ``ZOOM_OAUTH_ACCESS_TOKEN`` / refresh in env
  3. Fetches ZAK for ``ZOOM_BOT_USER_ID`` or ``ZOOM_BOT_USER_EMAIL``
"""

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

_s2s_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_env_oauth_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0, "refresh_token": ""}
_zak_cache: dict[str, Any] = {"token": "", "fetched_at": 0.0}


@dataclass
class ZoomServerCredentials:
    access_token: str | None
    zak_token: str | None
    source: str = ""


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
        log.warning("Zoom server HTTP %s %s: %s %s", method, url, exc.code, detail)
        return None
    except Exception as exc:
        log.warning("Zoom server HTTP %s %s failed: %s", method, url, exc)
        return None


def _s2s_client() -> tuple[str, str, str] | None:
    account_id = (os.getenv("ZOOM_ACCOUNT_ID") or os.getenv("ZOOM_S2S_ACCOUNT_ID") or "").strip()
    if not account_id:
        return None
    for id_key, secret_key in (
        ("ZOOM_S2S_CLIENT_ID", "ZOOM_S2S_CLIENT_SECRET"),
        ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
        ("ZOOM_OAUTH_CLIENT_ID", "ZOOM_OAUTH_CLIENT_SECRET"),
    ):
        cid = (os.getenv(id_key) or "").strip()
        sec = (os.getenv(secret_key) or "").strip()
        if cid and sec:
            return account_id, cid, sec
    return None


def _fetch_s2s_access_token() -> str | None:
    now = time.time()
    cached = (_s2s_cache.get("access_token") or "").strip()
    if cached and now < float(_s2s_cache.get("expires_at") or 0) - TOKEN_PAD_S:
        return cached

    cfg = _s2s_client()
    if not cfg:
        return None
    account_id, client_id, client_secret = cfg
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "account_credentials", "account_id": account_id}
    ).encode()
    data = _http_json(
        "https://zoom.us/oauth/token",
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {auth}",
        },
        body=body,
    )
    if not data or not data.get("access_token"):
        return None
    tok = str(data["access_token"]).strip()
    expires_in = int(data.get("expires_in") or 3600)
    _s2s_cache["access_token"] = tok
    _s2s_cache["expires_at"] = now + expires_in
    log.info("Zoom: obtained Server-to-Server OAuth token (account %s…)", account_id[:8])
    return tok


def _refresh_env_oauth_token(refresh_token: str) -> str | None:
    for id_key, secret_key in (
        ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
        ("ZOOM_OAUTH_CLIENT_ID", "ZOOM_OAUTH_CLIENT_SECRET"),
        ("ZOOM_S2S_CLIENT_ID", "ZOOM_S2S_CLIENT_SECRET"),
    ):
        cid = (os.getenv(id_key) or "").strip()
        sec = (os.getenv(secret_key) or "").strip()
        if not cid or not sec:
            continue
        auth = base64.b64encode(f"{cid}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        ).encode()
        data = _http_json(
            "https://zoom.us/oauth/token",
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth}",
            },
            body=body,
        )
        if data and data.get("access_token"):
            tok = str(data["access_token"]).strip()
            expires_in = int(data.get("expires_in") or 3600)
            _env_oauth_cache["access_token"] = tok
            _env_oauth_cache["expires_at"] = time.time() + expires_in
            if data.get("refresh_token"):
                _env_oauth_cache["refresh_token"] = str(data["refresh_token"]).strip()
            return tok
    return None


def _fetch_env_oauth_access_token() -> str | None:
    now = time.time()
    cached = (_env_oauth_cache.get("access_token") or os.getenv("ZOOM_OAUTH_ACCESS_TOKEN") or "").strip()
    expires = float(_env_oauth_cache.get("expires_at") or 0)
    if cached and (expires == 0 or now < expires - TOKEN_PAD_S):
        return cached

    refresh = (
        (_env_oauth_cache.get("refresh_token") or "").strip()
        or (os.getenv("ZOOM_OAUTH_REFRESH_TOKEN") or "").strip()
    )
    if refresh:
        refreshed = _refresh_env_oauth_token(refresh)
        if refreshed:
            return refreshed
    return cached or None


def _resolve_bot_zoom_user_id(access_token: str) -> str | None:
    uid = (os.getenv("ZOOM_BOT_USER_ID") or "").strip()
    if uid:
        return uid
    email = (os.getenv("ZOOM_BOT_USER_EMAIL") or os.getenv("ZOOM_BOT_USER") or "").strip()
    if not email:
        return None
    enc = urllib.parse.quote(email)
    data = _http_json(
        f"https://api.zoom.us/v2/users/{enc}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not data:
        return None
    out = (data.get("id") or "").strip()
    return out or None


def _fetch_zak_for_user(access_token: str, zoom_user_id: str) -> str | None:
    now = time.time()
    cached = (_zak_cache.get("token") or "").strip()
    if cached and now - float(_zak_cache.get("fetched_at") or 0) < ZAK_TTL_S:
        return cached

    data = _http_json(
        f"https://api.zoom.us/v2/users/{urllib.parse.quote(zoom_user_id)}/token?type=zak",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not data:
        data = _http_json(
            "https://api.zoom.us/v2/users/me/token?type=zak",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if not data:
        return None
    tok = (data.get("token") or "").strip()
    if tok:
        _zak_cache["token"] = tok
        _zak_cache["fetched_at"] = now
    return tok or None


def fetch_server_zoom_credentials() -> ZoomServerCredentials | None:
    """Best-effort org-level Zoom tokens (no dashboard Connect Zoom)."""
    access = _fetch_s2s_access_token() or _fetch_env_oauth_access_token()
    if not access:
        return None

    source = "server_s2s" if _s2s_client() else "server_env_oauth"
    zak: str | None = None
    bot_uid = _resolve_bot_zoom_user_id(access)
    if bot_uid:
        zak = _fetch_zak_for_user(access, bot_uid)
    if not zak:
        zak = _fetch_zak_for_user(access, "me")

    if not access and not zak:
        return None
    return ZoomServerCredentials(access_token=access, zak_token=zak, source=source)


def resolve_zoom_credentials_for_join(user_id: str | None = None) -> ZoomServerCredentials | None:
    """User tokens first, then server-level (no reconnect required)."""
    uid = (user_id or "").strip()
    if uid and uid != "default":
        try:
            from koov.zoom_user_credentials import fetch_zoom_credentials_for_user

            user_creds = fetch_zoom_credentials_for_user(uid)
            if user_creds and (user_creds.access_token or user_creds.zak_token):
                return ZoomServerCredentials(
                    access_token=user_creds.access_token,
                    zak_token=user_creds.zak_token,
                    source=user_creds.source or "user",
                )
        except Exception as exc:
            log.warning("Zoom user credential lookup failed: %s", exc)

    return fetch_server_zoom_credentials()
