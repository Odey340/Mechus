"""Chert client request shapes + webhook verification (no network)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import time

import pytest

from mechus.chert.client import ChertClient, ChertConfig, ChertError, sign
from mechus.chert.webhooks import EventDeduper, InboundMessage, InboundReaction, parse_event, verify_signature


class FakeResp(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(responses):
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        nxt = responses.pop(0) if responses else {}
        if isinstance(nxt, Exception):
            raise nxt
        return FakeResp(nxt)

    cfg = ChertConfig(signing_secret="s3cret", tenant="mechus", line_phone="+15550000000", line_id="line-1", base_url="https://chert.test/api/v1")
    return ChertClient(cfg, opener=opener), calls


def body(req):
    return json.loads(req.data.decode()) if req.data else None


def test_hmac_signature_matches_docs():
    ts, digest = sign("s3cret", b'{"a":1}', ts=1700000000)
    assert digest == hmac.new(b"s3cret", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()


def test_send_text_with_effect_and_signed_headers():
    c, calls = make_client([{"status": "sent", "message_id": "m1", "chat_id": "c1"}])
    res = c.send("+15551234567", "hi", effect="celebration")
    req = calls[0]
    assert req.full_url == "https://chert.test/api/v1/send" and req.get_method() == "POST"
    b = body(req)
    assert b["phone"] == "+15551234567" and b["body"] == "hi" and b["effect"] == "celebration" and b["phone_line_id"] == "line-1"
    assert b["idempotency_key"]
    sig = req.get_header("X-chert-signature")
    v, ts, hexd = sig.split(",")
    assert v == "v1" and hexd == hmac.new(b"s3cret", f"{ts}.".encode() + req.data, hashlib.sha256).hexdigest()
    assert req.get_header("X-chert-tenant") == "mechus"
    assert res["chat_id"] == "c1"


def test_create_group_and_follow_up_parts():
    c, calls = make_client([{"chat": {"id": "g1"}, "message": {"id": "m0"}}, {"message": {"id": "m2"}}])
    c.create_chat(["+15551110000", "+15552220000"], "Hello group", subject="Harbor Lofts")
    b = body(calls[0])
    assert calls[0].full_url.endswith("/chats")
    assert b["from"] == "+15550000000" and len(b["to"]) == 2
    assert b["message"]["parts"] == [{"type": "text", "value": "Hello group"}] and b["message"]["subject"] == "Harbor Lofts"
    c.send_parts("g1", [{"type": "rich_link", "url": "https://x"}])
    assert calls[1].full_url.endswith("/chats/g1/messages") and "parts" in body(calls[1])["message"]


def test_react_typing_edit_shapes():
    c, calls = make_client([{"status": "sent"}, {"status": "sent"}, {"message_id": "m1"}])
    c.react("m1", "love")
    c.typing("g1", True)
    c.edit("m1", "fixed")
    assert calls[0].full_url.endswith("/messages/m1/react") and body(calls[0]) == {"reaction": "love"}
    assert calls[1].full_url.endswith("/chats/g1/typing") and body(calls[1]) == {"state": "typing"}
    e = body(calls[2])
    assert calls[2].full_url.endswith("/messages/m1/edit") and e["new_text"] == "fixed" and e["edited_part_index"] == 0 and e["idempotency_key"]
    with pytest.raises(ValueError):
        c.react("m1", "wow")


def test_upload_then_voice_note():
    c, calls = make_client([
        {"attachment_id": "att1", "upload_url": "https://upload.test/x", "required_headers": {"content-type": "image/jpeg"}},
        {},
        {"voice_note_id": "vn_1", "phone_line_id": "line-1"},
        {"status": "sent", "message_id": "m9"},
    ])
    assert c.upload_attachment(b"\xff\xd8\xff", "a.jpg", "image/jpeg") == "att1"
    assert body(calls[0]) == {"filename": "a.jpg", "content_type": "image/jpeg", "size_bytes": 3}
    assert calls[1].get_method() == "PUT" and calls[1].get_header("Authorization") == "Bearer s3cret"
    c.send_voice_note("+15551234567", b"RIFF....WAVE")
    staged = body(calls[2])
    assert calls[2].full_url.endswith("/voice-notes") and staged["mime_type"] == "audio/wav" and staged["audio_base64"]
    assert body(calls[3])["voice_note"] == {"voice_note_id": "vn_1"}


def test_4xx_raises_without_retry():
    import urllib.error

    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":{"code":"1002","message":"invalid"}}'))
    c, calls = make_client([err])
    with pytest.raises(ChertError) as exc:
        c.react("m1", "like")
    assert exc.value.status == 400 and exc.value.code == "1002" and len(calls) == 1


def _signed(secret, raw, ts=None):
    ts = int(time.time()) if ts is None else ts
    return {"X-Webhook-Signature": f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()}


def test_webhook_signature_verification():
    raw = b'{"event_type":"message.received"}'
    assert verify_signature("whsec", raw, _signed("whsec", raw))
    assert not verify_signature("whsec", raw, _signed("other", raw))
    assert not verify_signature("whsec", raw + b" ", _signed("whsec", raw))
    assert not verify_signature("whsec", raw, _signed("whsec", raw, ts=int(time.time()) - 1000))
    ts = int(time.time())
    legacy = {"x-chert-signature": f"v1,{ts}," + hmac.new(b"whsec", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()}
    assert verify_signature("whsec", raw, legacy)
    assert not verify_signature("", raw, _signed("", raw))


def test_parse_message_and_reaction():
    msg = parse_event({"event_type": "message.received", "event_id": "ev1", "data": {
        "chat": {"id": "c1", "is_group": True, "handles": [{"handle": "+1555"}], "owner_handle": {"handle": "+1000"}},
        "message": {"id": "m1", "direction": "inbound", "sender_handle": {"handle": "+15551234567"},
                    "parts": [{"type": "text", "value": "Sink is leaking"}, {"type": "media", "attachment_id": "a1", "mime_type": "image/heic"}]}}})
    assert isinstance(msg, InboundMessage)
    assert (msg.chat_id, msg.message_id, msg.sender, msg.is_group, msg.text) == ("c1", "m1", "+15551234567", True, "Sink is leaking")
    assert msg.attachments[0].attachment_id == "a1"
    rx = parse_event({"event_type": "reaction.added", "event_id": "ev2", "data": {"sender_handle": {"handle": "+1"},
                     "reaction": {"target_message_id": "m1", "reaction": {"kind": "emoji", "emoji": "❤️"}}}})
    assert isinstance(rx, InboundReaction) and rx.reaction == "love" and rx.target_message_id == "m1"
    assert parse_event({"event_type": "message.received", "data": {"message": {"direction": "outbound"}}}) is None


def test_deduper(tmp_path):
    d = EventDeduper(tmp_path)
    assert d.first_time("ev1") and not d.first_time("ev1") and d.first_time("ev2")
