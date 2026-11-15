"""Small, dependency-free client for the Chert Messaging API.

Docs: https://docs.trychert.com/api/overview
- Host: https://console.trychert.com/api/v1
- Auth: HMAC (`x-chert-signature: v1,<ts>,<hex>` + `x-chert-tenant`) over "<ts>.<body>",
  or `authorization: Bearer <signing_secret>` (required for the binary upload PUT).

What the API supports that we rely on (verified against the docs):
- 1:1 sends via POST /send (text, attachments, Apple send `effect`, voice notes).
- Groups via POST /chats (2-31 recipients); follow-ups via POST /chats/{id}/messages.
- Tapbacks on any message id (outbound or inbound): love, like, dislike, laugh, emphasize, question.
- Typing dots on existing chats.
- Edits: 1:1 phone chats only, single text part, within 15 minutes, up to 5 edits.
- Voice notes: WAV/CAF staged via POST /voice-notes, then sent 1:1 via /send.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://console.trychert.com/api/v1"
REACTIONS = ("love", "like", "dislike", "laugh", "emphasize", "question")


class ChertError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "", body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


@dataclass(frozen=True)
class ChertConfig:
    signing_secret: str
    tenant: str
    line_phone: str = ""        # your Chert number in E.164 (used as `from` when creating chats)
    line_id: str = ""           # phone_line_id (pins sends to a line; needed for voice notes)
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "ChertConfig | None":
        secret = (os.getenv("MECHUS_CHERT_SIGNING_SECRET") or "").strip()
        tenant = (os.getenv("MECHUS_CHERT_TENANT") or "").strip()
        if not secret or not tenant:
            return None
        return cls(
            signing_secret=secret,
            tenant=tenant,
            line_phone=(os.getenv("MECHUS_CHERT_LINE_PHONE") or "").strip(),
            line_id=(os.getenv("MECHUS_CHERT_LINE_ID") or "").strip(),
            base_url=(os.getenv("MECHUS_CHERT_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        )


def sign(secret: str, body: bytes, ts: int | None = None) -> tuple[int, str]:
    """HMAC-SHA256 over "<ts>.<body>" (body = exact bytes sent; empty for GET)."""
    ts = int(time.time()) if ts is None else int(ts)
    digest = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return ts, digest


def new_idempotency_key(prefix: str = "mechus") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"[:128]


def text_part(value: str) -> dict[str, str]:
    return {"type": "text", "value": str(value)}


def media_part(attachment_id: str) -> dict[str, str]:
    return {"type": "media", "attachment_id": str(attachment_id)}


def rich_link_part(url: str, *, title: str = "", summary: str = "", image_url: str = "") -> dict[str, str]:
    part = {"type": "rich_link", "url": url}
    if title:
        part["title"] = title[:200]
    if summary:
        part["summary"] = summary[:400]
    if image_url:
        part["image_url"] = image_url
    return part


class ChertClient:
    def __init__(self, config: ChertConfig, *, timeout_s: float = 20.0, opener=None) -> None:
        self.config = config
        self.timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen

    # ---------- transport ----------
    def _headers(self, body: bytes) -> dict[str, str]:
        ts, digest = sign(self.config.signing_secret, body)
        return {
            "x-chert-signature": f"v1,{ts},{digest}",
            "x-chert-tenant": self.config.tenant,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retries: int = 2) -> dict[str, Any]:
        raw = b"" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=raw if method != "GET" else None, method=method, headers=self._headers(raw))
            try:
                with self._open(req, timeout=self.timeout_s) as resp:
                    text = resp.read().decode("utf-8") or "{}"
                    return json.loads(text)
            except urllib.error.HTTPError as exc:
                detail = exc.read(4000).decode("utf-8", errors="replace")
                try:
                    payload = json.loads(detail)
                except ValueError:
                    payload = {"raw": detail[:500]}
                code = ""
                if isinstance(payload, dict):
                    err = payload.get("error")
                    code = str((err or {}).get("code") if isinstance(err, dict) else payload.get("code") or "")
                # 5xx and 429 are retryable; 4xx are not.
                if exc.code >= 500 or exc.code == 429:
                    last_exc = ChertError(f"Chert {method} {path} -> HTTP {exc.code}", status=exc.code, code=code, body=payload)
                    if attempt < retries:
                        try:
                            wait = float(exc.headers.get("Retry-After", "")) if exc.headers else 0.0
                        except (TypeError, ValueError):
                            wait = 0.0
                        time.sleep(min(10.0, wait) if wait > 0 else 0.6 * (2 ** attempt))
                        continue
                    raise last_exc from None
                raise ChertError(f"Chert {method} {path} -> HTTP {exc.code}", status=exc.code, code=code, body=payload) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = ChertError(f"Chert unreachable: {type(exc).__name__}")
                if attempt < retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                raise last_exc from None
            except ValueError:
                raise ChertError(f"Chert {method} {path} returned invalid JSON") from None
        raise last_exc or ChertError("Chert request failed")

    # ---------- messaging ----------
    def send(
        self,
        phone: str,
        body: str = "",
        *,
        attachments: list[str] | None = None,
        effect: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """1:1 send. Returns {message_id, chat_id, status, ...}."""
        payload: dict[str, Any] = {"phone": phone, "idempotency_key": idempotency_key or new_idempotency_key("send")}
        if body:
            payload["body"] = body
        if attachments:
            payload["attachments"] = list(attachments)
        if effect:
            payload["effect"] = effect
        if self.config.line_id:
            payload["phone_line_id"] = self.config.line_id
        return self.request("POST", "send", payload)

    def create_chat(self, to: list[str], text: str, *, subject: str = "", idempotency_key: str = "") -> dict[str, Any]:
        """1:1 (one recipient) or group (2-31). First message must be text only."""
        if not to:
            raise ValueError("at least one recipient is required")
        if len(to) > 31:
            raise ValueError("groups are limited to 31 recipients")
        if not self.config.line_phone:
            raise ChertError("MECHUS_CHERT_LINE_PHONE (your Chert number) is required to create chats")
        message: dict[str, Any] = {"parts": [text_part(text)], "idempotency_key": idempotency_key or new_idempotency_key("chat")}
        if subject and len(to) > 1:
            message["subject"] = subject[:80]
        return self.request("POST", "chats", {"from": self.config.line_phone, "to": list(to), "message": message})

    def send_parts(self, chat_id: str, parts: list[dict[str, Any]], *, idempotency_key: str = "") -> dict[str, Any]:
        """Follow-up message into an existing chat (works for groups)."""
        return self.request(
            "POST",
            f"chats/{chat_id}/messages",
            {"message": {"parts": parts, "idempotency_key": idempotency_key or new_idempotency_key("msg")}},
        )

    def send_text(self, chat_id: str, text: str, **kw: Any) -> dict[str, Any]:
        return self.send_parts(chat_id, [text_part(text)], **kw)

    def get_message(self, message_id: str) -> dict[str, Any]:
        """Message status (sent/queued/failed) plus delivered_at / read_at receipts."""
        return self.request("GET", f"messages/{message_id}", retries=1)

    def react(self, message_id: str, reaction: str) -> dict[str, Any]:
        if reaction not in REACTIONS:
            raise ValueError(f"reaction must be one of {REACTIONS}")
        return self.request("POST", f"messages/{message_id}/react", {"reaction": reaction})

    def typing(self, chat_id: str, on: bool = True) -> dict[str, Any]:
        return self.request("POST", f"chats/{chat_id}/typing", {"state": "typing" if on else "stopped"}, retries=0)

    def edit(self, message_id: str, new_text: str, *, idempotency_key: str = "") -> dict[str, Any]:
        """1:1 phone chats only, within 15 minutes of sending, max 5 edits."""
        return self.request(
            "POST",
            f"messages/{message_id}/edit",
            {"new_text": new_text, "edited_part_index": 0, "idempotency_key": idempotency_key or new_idempotency_key("edit")},
            retries=0,
        )

    # ---------- media ----------
    def upload_attachment(self, data: bytes, filename: str, content_type: str) -> str:
        slot = self.request(
            "POST", "attachments", {"filename": filename, "content_type": content_type, "size_bytes": len(data)}
        )
        attachment_id = str(slot.get("attachment_id") or "")
        upload_url = str(slot.get("upload_url") or "")
        if not attachment_id or not upload_url:
            raise ChertError("Chert did not return an upload slot", body=slot)
        headers = {str(k): str(v) for k, v in (slot.get("required_headers") or {}).items()}
        headers.setdefault("authorization", f"Bearer {self.config.signing_secret}")
        headers.setdefault("content-type", content_type)
        headers["content-length"] = str(len(data))
        req = urllib.request.Request(upload_url, data=data, method="PUT", headers=headers)
        try:
            with self._open(req, timeout=max(self.timeout_s, 60)) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            raise ChertError(f"Chert upload failed: HTTP {exc.code}", status=exc.code) from None
        return attachment_id

    def download_attachment(self, attachment_id: str) -> tuple[bytes, str]:
        url = f"{self.config.base_url}/attachments/{attachment_id}/content"
        req = urllib.request.Request(url, method="GET", headers={k: v for k, v in self._headers(b"").items() if k != "content-type"})
        try:
            with self._open(req, timeout=max(self.timeout_s, 60)) as resp:
                ctype = resp.headers.get("content-type", "application/octet-stream") if hasattr(resp, "headers") else "application/octet-stream"
                return resp.read(), ctype
        except urllib.error.HTTPError as exc:
            raise ChertError(f"Chert download failed: HTTP {exc.code}", status=exc.code) from None

    def send_voice_note(self, phone: str, wav_bytes: bytes, *, idempotency_key: str = "") -> dict[str, Any]:
        import base64

        staged = self.request(
            "POST",
            "voice-notes",
            {
                "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
                "mime_type": "audio/wav",
                **({"phone_line_id": self.config.line_id} if self.config.line_id else {}),
            },
        )
        vn_id = str(staged.get("voice_note_id") or "")
        if not vn_id:
            raise ChertError("Chert did not stage the voice note", body=staged)
        payload: dict[str, Any] = {
            "phone": phone,
            "voice_note": {"voice_note_id": vn_id},
            "idempotency_key": idempotency_key or new_idempotency_key("vn"),
        }
        line = str(staged.get("phone_line_id") or self.config.line_id or "")
        if line:
            payload["phone_line_id"] = line
        return self.request("POST", "send", payload)


def client_from_env() -> ChertClient | None:
    cfg = ChertConfig.from_env()
    return ChertClient(cfg) if cfg else None
