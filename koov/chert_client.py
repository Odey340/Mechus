"""Chert FaceTime Calls API client + LiveKit token minting (stdlib only).

Chert's FaceTime product is "bring your own room": Koov joins a LiveKit room, Chert's
media bridge joins the same room and carries the FaceTime call. Caller audio arrives as
the ``facetime-remote-audio`` track; whatever Koov publishes is heard by the caller.

Docs: https://docs.trychert.com/facetime/bring-your-own-room
      https://docs.trychert.com/facetime/calls-api
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://facetime.trychert.com/api"
# Chert rejects bridge tokens whose lifetime (exp - nbf) exceeds two hours.
MAX_BRIDGE_TOKEN_TTL_S = 7200

REMOTE_AUDIO_TRACK = "facetime-remote-audio"
REMOTE_VIDEO_TRACK = "facetime-remote-video"

_REQUIRED_ENV = (
    "CHERT_API_KEY",
    "CHERT_PROJECT_ID",
    "CHERT_LINE_ID",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
)


# The properties dashboard (koov/chert/facetime.py) names the FaceTime key and line
# KOOV_CHERT_FACETIME_*; KOOV_CHERT_LINE_ID there is the iMessage line, so it is tried last.
_ENV_ALIASES = {
    "CHERT_API_KEY": ("KOOV_CHERT_FACETIME_API_KEY",),
    "CHERT_LINE_ID": ("KOOV_CHERT_FACETIME_LINE_ID",),
}


def _env(name: str) -> str:
    """Read ``NAME``, a dashboard alias, or ``KOOV_NAME`` (KOOV_-prefixed vars survive
    docker ``-e`` passthrough)."""
    for candidate in (name, *_ENV_ALIASES.get(name, ()), f"KOOV_{name}"):
        value = (os.environ.get(candidate) or "").strip()
        if value:
            return value
    return ""


class ChertError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status: int = 0,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id

    def __str__(self) -> str:
        bits = [super().__str__()]
        if self.code:
            bits.append(f"code={self.code}")
        if self.status:
            bits.append(f"http={self.status}")
        if self.request_id:
            bits.append(f"request_id={self.request_id}")
        return " ".join(bits)


@dataclass(frozen=True)
class ChertConfig:
    api_key: str
    project_id: str
    line_id: str
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    api_base: str = DEFAULT_API_BASE

    @classmethod
    def from_env(cls) -> "ChertConfig":
        return cls(
            # Accept both the Telemachus worker names and the existing dashboard
            # FaceTime settings names so the merged branches share one config.
            api_key=_env("CHERT_API_KEY") or _env("CHERT_FACETIME_API_KEY"),
            project_id=_env("CHERT_PROJECT_ID"),
            line_id=_env("CHERT_LINE_ID") or _env("CHERT_FACETIME_LINE_ID"),
            livekit_url=_env("LIVEKIT_URL"),
            livekit_api_key=_env("LIVEKIT_API_KEY"),
            livekit_api_secret=_env("LIVEKIT_API_SECRET"),
            api_base=(
                _env("CHERT_API_BASE")
                or _env("CHERT_FACETIME_BASE_URL")
                or DEFAULT_API_BASE
            ).rstrip("/"),
        )

    def missing(self) -> list[str]:
        values = {
            "CHERT_API_KEY": self.api_key,
            "CHERT_PROJECT_ID": self.project_id,
            "CHERT_LINE_ID": self.line_id,
            "LIVEKIT_URL": self.livekit_url,
            "LIVEKIT_API_KEY": self.livekit_api_key,
            "LIVEKIT_API_SECRET": self.livekit_api_secret,
        }
        return [k for k in _REQUIRED_ENV if not values[k]]


def chert_configured() -> bool:
    """True when every Chert + LiveKit credential needed for a call is present."""
    return not ChertConfig.from_env().missing()


def livekit_sdk_available_reason() -> tuple[bool, str]:
    try:
        import livekit.rtc  # noqa: F401

        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"livekit import failed: {type(exc).__name__}: {exc}"


# ── LiveKit access tokens ────────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_livekit_token(
    *,
    api_key: str,
    api_secret: str,
    identity: str,
    room: str,
    ttl_s: int = 3600,
    name: str = "",
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    """Sign a room-scoped LiveKit access token (HS256 JWT) without the livekit-api package."""
    if not api_key or not api_secret:
        raise ValueError("LiveKit api_key and api_secret are required")
    if not identity or not room:
        raise ValueError("LiveKit identity and room are required")
    # Small backdate so the token is already valid on a host with a slightly fast clock.
    nbf = int(time.time()) - 10
    payload: dict[str, Any] = {
        "iss": api_key,
        "sub": identity,
        "nbf": nbf,
        "exp": nbf + int(ttl_s),
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": bool(can_publish),
            "canSubscribe": bool(can_subscribe),
        },
    }
    if name:
        payload["name"] = name
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(api_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def room_name_for_meeting(meeting_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", meeting_id or "").strip("-")[:80]
    return f"koov-{safe or uuid.uuid4().hex[:12]}"


# ── Destinations ─────────────────────────────────────────────────────────────


def classify_destination(destination: str) -> tuple[str, str]:
    """Return ``(destination_type, normalized_value)`` for a Chert outbound call.

    ``facetime_link`` for https://facetime.apple.com/join#… links (experimental on Chert,
    enabled per line), ``email`` for Apple ID emails, otherwise ``phone`` (E.164).
    """
    d = (destination or "").strip()
    if not d:
        raise ValueError("destination is empty")
    low = d.lower()
    if low.startswith("facetime://"):
        rest = d[len("facetime://"):].lstrip("/")
        d = rest if rest.lower().startswith("https://") else "https://" + rest
        low = d.lower()
    if "facetime.apple.com" in low:
        # Chert requires the bare join link: fragment only, no Koov display-name prefill.
        if "#" in d:
            base, frag = d.split("#", 1)
            parts = [p for p in frag.split("&") if p and not p.startswith("l=")]
            d = base.split("?", 1)[0] + "#" + "&".join(parts)
        return "facetime_link", d
    if "@" in d and not low.startswith(("http://", "https://")):
        return "email", low
    digits = re.sub(r"[^\d+]", "", d)
    if digits and not digits.startswith("+"):
        # Bare 10-digit numbers are assumed US/Canada; anything else must be E.164 already.
        digits = "+1" + digits if len(digits) == 10 else "+" + digits
    if not re.fullmatch(r"\+\d{7,15}", digits or ""):
        raise ValueError(
            f"destination {destination!r} is not a FaceTime link, email, or E.164 phone number"
        )
    return "phone", digits


def looks_like_chert_destination(value: str) -> bool:
    """Phone number or email (not a URL) — only reachable through Chert."""
    v = (value or "").strip()
    if not v or "://" in v or "facetime.apple.com" in v.lower():
        return False
    try:
        kind, _ = classify_destination(v)
    except ValueError:
        return False
    return kind in ("phone", "email")


# ── REST client ──────────────────────────────────────────────────────────────


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


class ChertClient:
    def __init__(self, config: ChertConfig, *, timeout_s: float = 20.0) -> None:
        self._cfg = config
        self._timeout_s = timeout_s
        self._ctx = _ssl_context()

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self._cfg.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self._cfg.api_key}",
            "X-Chert-Project-Id": self._cfg.project_id,
            "Accept": "application/json",
            "User-Agent": "koov-chert-client",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s, context=self._ctx) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            parsed = _parse_json(raw)
            err = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            raise ChertError(
                str(err.get("message") or raw[:300].decode("utf-8", "replace") or exc.reason),
                code=str(err.get("code") or ""),
                status=int(exc.code),
                request_id=str(parsed.get("request_id") or ""),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChertError(f"Chert API unreachable: {exc}") from exc
        return status, _parse_json(raw)

    def create_call(
        self,
        *,
        destination: str,
        destination_type: str,
        participant_token: str,
        remote_participant_identity: str,
        video: bool = False,
        idempotency_key: str | None = None,
        when_busy: str | None = None,
    ) -> dict[str, Any]:
        """Allocate an outbound FaceTime call bridged into our LiveKit room.

        HTTP 201 means *allocated*, not answered — wait for the bridge's audio track
        (or the ``bridge_ready`` event) before speaking. Returns the response envelope
        with ``_http_status`` added; the call id is ``envelope["data"]["id"]``.
        """
        body: dict[str, Any] = {
            "direction": "outbound",
            "mode": "customer_managed",
            "line_id": self._cfg.line_id,
            "destination": destination,
            "destination_type": destination_type,
            "media": {"audio": True, "video": bool(video)},
            "livekit_url": self._cfg.livekit_url,
            "participant_token": participant_token,
            "remote_participant_identity": remote_participant_identity,
        }
        if when_busy:
            body["when_busy"] = when_busy
        status, envelope = self._request(
            "POST",
            "/v1/calls",
            body=body,
            idempotency_key=idempotency_key or f"koov-create-{uuid.uuid4().hex}",
        )
        envelope["_http_status"] = status
        return envelope

    def get_call(self, call_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/calls/{call_id}")[1]

    def get_call_events(self, call_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/calls/{call_id}/events")[1]

    def end_call(self, call_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/calls/{call_id}/end",
            body={},
            idempotency_key=f"koov-end-{uuid.uuid4().hex}",
        )[1]

    def list_lines(self) -> dict[str, Any]:
        return self._request("GET", "/v1/lines")[1]


def _parse_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def call_id_from_envelope(envelope: dict[str, Any]) -> str:
    data = envelope.get("data")
    if isinstance(data, dict):
        return str(data.get("id") or data.get("call_id") or "").strip()
    return ""


# Chert does not publish its status enum; these are the terminal-looking values we
# treat as "the call is over" when polling GET /v1/calls/{id}.
_TERMINAL_HINTS = (
    "end",
    "fail",
    "complete",
    "terminal",
    "cancel",
    "declin",
    "no_answer",
    "unanswered",
    "busy",
    "error",
    "timeout",
    "timed_out",
    "rejected",
    "missed",
)


def call_status_from_envelope(envelope: dict[str, Any]) -> tuple[str, str]:
    """Return ``(status, reason)`` from a GET /v1/calls/{id} envelope (best effort)."""
    data = envelope.get("data")
    if not isinstance(data, dict):
        return "", ""
    status = str(data.get("status") or data.get("state") or "").strip().lower()
    reason = str(
        data.get("terminal_reason")
        or data.get("end_reason")
        or data.get("reason")
        or data.get("error")
        or ""
    ).strip()
    return status, reason


def is_terminal_status(status: str) -> bool:
    s = (status or "").strip().lower()
    return bool(s) and any(h in s for h in _TERMINAL_HINTS)
