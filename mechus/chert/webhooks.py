"""Verify and parse Chert webhooks.

- Signature: `X-Webhook-Signature: t=<ts>,v1=<hex>` = HMAC-SHA256(subscription secret, "<ts>.<raw body>"),
  rejected when |now - ts| > 300 s. Legacy header `x-chert-signature: v1,<ts>,<hex>` is also accepted.
- Delivery is at-least-once: dedupe on `event_id`.
- Events used: message.received, reaction.added (reaction.removed is ignored).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SKEW_S = 300

_EMOJI_TO_REACTION = {
    "❤️": "love", "❤": "love", "👍": "like", "👎": "dislike", "😂": "laugh", "‼️": "emphasize", "‼": "emphasize",
    "❓": "question", "?": "question",
}


def _parse_signature(headers: dict[str, str]) -> tuple[int, str] | None:
    lower = {k.lower(): v for k, v in headers.items()}
    value = lower.get("x-webhook-signature", "")
    if value:
        parts = dict(p.split("=", 1) for p in value.split(",") if "=" in p)
        try:
            return int(parts.get("t", "")), parts.get("v1", "")
        except ValueError:
            return None
    legacy = lower.get("x-chert-signature", "")
    if legacy:
        bits = legacy.split(",")
        if len(bits) == 3 and bits[0] == "v1":
            try:
                return int(bits[1]), bits[2]
            except ValueError:
                return None
    return None


def verify_signature(secret: str, raw_body: bytes, headers: dict[str, str], *, now: float | None = None) -> bool:
    if not secret:
        return False
    parsed = _parse_signature(headers)
    if not parsed:
        return False
    ts, given = parsed
    current = time.time() if now is None else now
    if abs(current - ts) > MAX_SKEW_S:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given.strip().lower())


class EventDeduper:
    """Marker-file dedupe that survives restarts and works across processes."""

    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def first_time(self, event_id: str) -> bool:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id or "")[:160]
        if not safe:
            return True
        try:
            fd = os.open(self.dir / safe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.close(fd)
        return True


@dataclass
class InboundAttachment:
    attachment_id: str
    mime_type: str = ""
    filename: str = ""
    size_bytes: int | None = None


@dataclass
class InboundMessage:
    event_id: str
    chat_id: str
    message_id: str
    sender: str
    is_group: bool
    participants: list[str] = field(default_factory=list)
    text: str = ""
    attachments: list[InboundAttachment] = field(default_factory=list)
    sent_at: str = ""
    line: str = ""


@dataclass
class InboundReaction:
    event_id: str
    target_message_id: str
    reaction: str          # one of client.REACTIONS, or "emoji"
    emoji: str
    sender: str
    removed: bool = False


def _handle(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("handle") or value.get("address") or "")
    return str(value or "")


def parse_event(payload: dict[str, Any]) -> InboundMessage | InboundReaction | None:
    kind = str(payload.get("event_type") or payload.get("type") or "")
    event_id = str(payload.get("event_id") or payload.get("id") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if kind in ("message.received", "lead_reply"):
        chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        if str(msg.get("direction") or "inbound") == "outbound":
            return None
        texts: list[str] = []
        atts: list[InboundAttachment] = []
        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("value"):
                texts.append(str(part["value"]))
            elif part.get("type") == "media" and part.get("attachment_id"):
                size = part.get("size_bytes")
                atts.append(InboundAttachment(
                    attachment_id=str(part["attachment_id"]),
                    mime_type=str(part.get("mime_type") or ""),
                    filename=str(part.get("filename") or ""),
                    size_bytes=int(size) if isinstance(size, (int, float)) else None,
                ))
        return InboundMessage(
            event_id=event_id,
            chat_id=str(chat.get("id") or msg.get("chat_id") or ""),
            message_id=str(msg.get("id") or ""),
            sender=_handle(msg.get("sender_handle")),
            is_group=bool(chat.get("is_group")),
            participants=[_handle(h) for h in chat.get("handles") or [] if _handle(h)],
            text="\n".join(texts).strip(),
            attachments=atts,
            sent_at=str(msg.get("sent_at") or ""),
            line=_handle(chat.get("owner_handle")),
        )
    if kind in ("reaction.added", "reaction.removed"):
        reaction = data.get("reaction") if isinstance(data.get("reaction"), dict) else {}
        nested = reaction.get("reaction") if isinstance(reaction.get("reaction"), dict) else {}
        raw_kind = str(nested.get("kind") or reaction.get("kind") or "")
        emoji = str(nested.get("emoji") or "")
        name = raw_kind if raw_kind in _EMOJI_TO_REACTION.values() else _EMOJI_TO_REACTION.get(emoji, "emoji")
        return InboundReaction(
            event_id=event_id,
            target_message_id=str(reaction.get("target_message_id") or ""),
            reaction=name,
            emoji=emoji,
            sender=_handle(data.get("sender_handle") or reaction.get("sender_handle")),
            removed=kind == "reaction.removed",
        )
    return None
