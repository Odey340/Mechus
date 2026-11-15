"""Chert FaceTime (Bring Your Own Room) helpers.

Outbound: POST https://facetime.trychert.com/api/v1/calls with a project API key,
`X-Chert-Project-Id`, `Idempotency-Key`, and a LiveKit room the agent has already
joined (publishing exactly one audio track). See docs.trychert.com/facetime/outbound.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

DEFAULT_BASE = "https://facetime.trychert.com/api/v1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def livekit_token(api_key: str, api_secret: str, identity: str, room: str, *, ttl_s: int = 3600) -> str:
    """HS256 LiveKit access token with join/publish/subscribe for one room."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": api_key, "sub": identity, "nbf": now - 5, "exp": now + min(ttl_s, 7200),
        "video": {"roomJoin": True, "room": room, "canPublish": True, "canSubscribe": True},
    }
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    sig = hmac.new(api_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def _first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def config() -> dict[str, str]:
    # Two naming schemes exist for the same credentials: the dashboard's
    # (KOOV_CHERT_FACETIME_*, KOOV_LIVEKIT_*) and the call worker's (CHERT_*, LIVEKIT_*,
    # see koov/chert_client.py). Either works for both.
    return {
        "api_key": _first("KOOV_CHERT_FACETIME_API_KEY", "CHERT_API_KEY", "KOOV_CHERT_API_KEY"),
        "project_id": _first("KOOV_CHERT_PROJECT_ID", "CHERT_PROJECT_ID"),
        "line_id": _first("KOOV_CHERT_FACETIME_LINE_ID", "CHERT_LINE_ID", "KOOV_CHERT_LINE_ID"),
        "livekit_url": _first("KOOV_LIVEKIT_URL", "LIVEKIT_URL"),
        "livekit_key": _first("KOOV_LIVEKIT_API_KEY", "LIVEKIT_API_KEY"),
        "livekit_secret": _first("KOOV_LIVEKIT_API_SECRET", "LIVEKIT_API_SECRET"),
        "base": (os.getenv("KOOV_CHERT_FACETIME_BASE_URL") or DEFAULT_BASE).rstrip("/"),
    }


def missing_config() -> list[str]:
    c = config()
    names = {
        "api_key": "KOOV_CHERT_FACETIME_API_KEY", "project_id": "KOOV_CHERT_PROJECT_ID", "line_id": "KOOV_CHERT_FACETIME_LINE_ID",
        "livekit_url": "KOOV_LIVEKIT_URL", "livekit_key": "KOOV_LIVEKIT_API_KEY", "livekit_secret": "KOOV_LIVEKIT_API_SECRET",
    }
    return [env for key, env in names.items() if not c[key]]


def place_call(destination: str, *, video: bool = True, room: str = "", agent_identity: str = "koov-agent") -> dict[str, Any]:
    """Allocate an outbound FaceTime call into our LiveKit room. The Koov LiveKit agent must already be in `room`."""
    missing = missing_config()
    if missing:
        raise RuntimeError("FaceTime is not configured: set " + ", ".join(missing))
    c = config()
    room = room or f"koov-{uuid.uuid4().hex[:10]}"
    bridge = livekit_token(c["livekit_key"], c["livekit_secret"], f"chert-bridge-{uuid.uuid4().hex[:6]}", room, ttl_s=3600)
    body = {
        "direction": "outbound", "mode": "customer_managed", "line_id": c["line_id"],
        "destination": destination, "destination_type": "email" if "@" in destination else "phone",
        "media": {"audio": True, "video": bool(video)},
        "livekit_url": c["livekit_url"], "participant_token": bridge, "remote_participant_identity": agent_identity,
    }
    raw = json.dumps(body).encode()
    # Same key + same body on every retry: Chert returns the original call instead of dialing twice.
    headers = {"Authorization": f"Bearer {c['api_key']}", "X-Chert-Project-Id": c["project_id"],
               "Idempotency-Key": f"koov-call-{uuid.uuid4().hex}", "Content-Type": "application/json"}
    data: dict[str, Any] = {}
    for attempt in range(3):
        req = urllib.request.Request(f"{c['base']}/calls", data=raw, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode() or "{}")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace")
            if exc.code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            hint = {401: "check KOOV_CHERT_FACETIME_API_KEY", 403: "the key needs the facetime:write scope",
                    409: "a call with these details already exists", 422: "the line isn't ready or the recipient isn't eligible"}.get(exc.code, "")
            raise RuntimeError(f"Chert FaceTime HTTP {exc.code}{' (' + hint + ')' if hint else ''}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"Couldn't reach Chert FaceTime: {type(exc).__name__}") from None
    call = data.get("data") if isinstance(data.get("data"), dict) else data
    return {"call_id": str(call.get("id") or ""), "room": room, "agent_token": livekit_token(
        c["livekit_key"], c["livekit_secret"], agent_identity, room, ttl_s=3600)}
