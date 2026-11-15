"""Mechus in the property's iMessage group (via Chert).

Inbound: every message/photo/voice note is filed on the property timeline and
classified. Mechus tapbacks what it logged (👍), flags urgent issues (‼️) and pings
the landlord, asks for clarity (❓), files photos into the open inspection, and
answers questions addressed to it from the log.

Outbound: every send goes through the durable outbox (mechus/estates/outbox.py):
tried immediately, retried with backoff on transient failures, idempotent, with
delivered/read receipts written back onto the timeline event.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from mechus.brand import ASSISTANT_NAME, WAKE_WORD
from mechus.chert.client import ChertClient, ChertError, client_from_env, rich_link_part, text_part
from mechus.chert.webhooks import InboundMessage, InboundReaction
from mechus.estates import ai, media, outbox, scribe, store

log = logging.getLogger(__name__)

_state_lock = threading.Lock()
_last_webhook: dict[str, Any] = {}


class NotConfigured(RuntimeError):
    pass


class SendFailed(RuntimeError):
    def __init__(self, message: str, *, code: str = "", outbox_id: str = "", event: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.outbox_id = outbox_id
        self.event = event


def _factory() -> ChertClient | None:
    # Looked up at call time so tests can monkeypatch `client_from_env` on this module.
    return client_from_env()


outbox.set_client_factory(lambda: _factory())


def client() -> ChertClient:
    c = client_from_env()
    if c is None:
        raise NotConfigured("Chert isn't connected: set MECHUS_CHERT_SIGNING_SECRET and MECHUS_CHERT_TENANT")
    return c


def status() -> dict[str, Any]:
    c = client_from_env()
    pending = outbox.list_entries(statuses=("pending", "retrying"))
    failed = outbox.list_entries(statuses=("failed",))
    return {
        "connected": c is not None,
        "tenant": c.config.tenant if c else "",
        "line_phone": c.config.line_phone if c else "",
        "line_id": bool(c and c.config.line_id),
        "webhook_secret": bool((os.getenv("MECHUS_CHERT_WEBHOOK_SECRET") or "").strip()),
        "last_webhook": dict(_last_webhook),
        "ai_mode": ai.mode(),
        "elevenlabs": scribe.configured(),
        "outbox": {"pending": len(pending), "failed": len(failed)},
    }


def note_webhook(kind: str) -> None:
    with _state_lock:
        _last_webhook.update(kind=kind, at=store.now())


def public_url(path: str) -> str:
    base = (os.getenv("MECHUS_PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}{path}" if base else path


def _send(pid: str, op: str, args: dict[str, Any], *, text: str, kind: str = "message", chat_id: str = "",
          on_success: str = "", raise_on_fail: bool = True, **extra: Any) -> dict[str, Any]:
    """Create the outbound timeline event, then hand the send to the outbox."""
    ev = store.append_event(pid, {"kind": kind, "direction": "out", "actor": "Mechus", "role": "mechus", "chat_id": chat_id,
                                  "message_id": "", "text": text, "tapbacks": [], "delivery": "sending", **extra})
    entry = outbox.submit(pid, op, args, event_id=ev["id"], on_success=on_success)
    ev = store.update_event(pid, ev["id"], lambda e: e.update(outbox_id=entry["id"])) or ev
    if entry["status"] == "failed" and raise_on_fail:
        raise SendFailed(entry["error"], code=entry["error_code"], outbox_id=entry["id"], event=ev)
    return ev


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

def handle_message(msg: InboundMessage) -> dict[str, Any] | None:
    pid = store.property_for_chat(msg.chat_id) or store.property_for_handle(msg.sender)
    if not pid:
        store.append_event("unrouted", {"kind": "message", "direction": "in", "actor": msg.sender, "chat_id": msg.chat_id,
                                        "message_id": msg.message_id, "text": msg.text})
        return None
    prop = store.get_property(pid) or {}
    if msg.chat_id and not store.property_for_chat(msg.chat_id):
        store.link_chat(msg.chat_id, pid, kind="group" if msg.is_group else "direct")
    role = store.role_for_handle(prop, msg.sender)
    c = client_from_env()

    attachments: list[dict[str, Any]] = []
    transcribed = ""
    for att in msg.attachments:
        if c is None:
            break
        try:
            data, ctype = c.download_attachment(att.attachment_id)
        except ChertError as exc:
            log.warning("attachment %s download failed: %s", att.attachment_id, exc)
            attachments.append({"attachment_id": att.attachment_id, "kind": "unknown", "file": "", "error": "download failed"})
            continue
        ext = media.sniff_extension(data[:32], Path(att.filename).suffix.lower())
        kind = media.media_kind("x" + ext, att.mime_type or ctype)
        inbox = store.base_dir() / "inbox" / pid
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{re.sub(r'[^A-Za-z0-9_-]', '_', att.attachment_id)}{ext}"
        path.write_bytes(data)
        entry = {"attachment_id": att.attachment_id, "kind": kind, "file": path.name, "sha256": media.sha256_file(path)}
        if kind == "photo" and ext in (".heic", ".heif"):
            try:  # browsers can't show HEIC; keep a JPEG twin for the dashboard
                jpg = path.with_suffix(".jpg")
                media.normalize_photo(path, jpg)
                entry["file"] = jpg.name
            except Exception as exc:  # noqa: BLE001
                log.info("HEIC conversion failed: %s", exc)
        if kind == "audio" and scribe.configured():  # voice note -> text
            wav = path.with_suffix(".wav")
            if media.extract_audio(path, wav):
                try:
                    transcribed = scribe.transcribe(wav)["text"]
                    entry["transcript"] = transcribed
                except RuntimeError as exc:
                    log.info("voice note not transcribed: %s", exc)
        if kind in ("photo", "video"):
            entry["inspection_id"] = _file_into_open_inspection(pid, path, kind, msg.sender)
        attachments.append(entry)

    text = msg.text or transcribed
    try:
        cls = ai.classify_message(text, has_media=bool(attachments), role=role)
    except RuntimeError as exc:  # AI down: still log the message
        log.warning("classification failed: %s", exc)
        cls = {"kind": "evidence" if attachments else "chitchat", "urgent": False, "summary": text[:140], "due": None,
               "room": "", "needs_clarification": False, "addressed_to_mechus": bool(re.search(rf"\b{re.escape(WAKE_WORD)}\b", text, re.I))}
    event = store.append_event(pid, {
        "kind": "message", "direction": "in", "actor": msg.sender, "role": role, "chat_id": msg.chat_id,
        "message_id": msg.message_id, "text": text, "voice": bool(transcribed), "attachments": attachments,
        "classification": cls, "tapbacks": [],
    })
    if c is None:
        return event

    reaction = ""
    if cls["urgent"]:
        reaction = "emphasize"
    elif cls["needs_clarification"]:
        reaction = "question"
    elif cls["kind"] in ("maintenance_request", "evidence", "agreement", "payment") or attachments:
        reaction = "like"
    if reaction and msg.message_id:
        entry = outbox.submit(pid, "react", {"message_id": msg.message_id, "reaction": reaction})
        if entry["status"] != "failed":
            store.update_event(pid, event["id"], lambda e: e["tapbacks"].append({"by": "Mechus", "reaction": reaction}))

    landlord = (prop.get("landlord") or {}).get("phone", "")
    if cls["urgent"] and landlord and role != "landlord":
        _send(pid, "direct", {"phone": landlord, "text": f"‼️ Urgent at {prop.get('name')}: {cls['summary'] or text[:140]}"},
              text=f"Alerted landlord: {cls['summary'] or text[:140]}", kind="alert", on_success="link_direct", raise_on_fail=False)

    if cls["addressed_to_mechus"] and (cls["kind"] == "question" or text.strip().endswith("?")):
        try:
            c.typing(msg.chat_id, True)
        except ChertError:
            pass
        try:
            answer = ai.answer_question(text, store.list_events(pid, limit=400), prop.get("name", ""))
        except RuntimeError:
            answer = "I'm having trouble looking that up right now. I'll keep it logged and get back to you."
        _send(pid, "group_text", {"chat_id": msg.chat_id, "text": answer}, text=answer, chat_id=msg.chat_id, raise_on_fail=False)
    return event


def handle_reaction(r: InboundReaction) -> None:
    if not r.target_message_id:
        return
    for prop in store.list_properties():
        for ev in store.list_events(prop["id"], limit=2000):
            if ev.get("message_id") == r.target_message_id:
                def apply(e: dict[str, Any]) -> None:
                    taps = e.setdefault("tapbacks", [])
                    if r.removed:
                        e["tapbacks"] = [t for t in taps if not (t.get("by") == r.sender and t.get("reaction") == r.reaction)]
                    else:
                        taps.append({"by": r.sender, "reaction": r.reaction, "emoji": r.emoji})
                store.update_event(prop["id"], ev["id"], apply)
                return


def _file_into_open_inspection(pid: str, path: Path, kind: str, sender: str) -> str:
    """Photos texted during an open (draft) inspection become part of it."""
    drafts = [i for i in store.list_inspections(pid) if i["status"] == "draft"]
    if not drafts:
        return ""
    insp = drafts[0]
    mid = store.new_id("m")
    dest_dir = store.media_dir(insp["id"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{mid}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    rec = {"id": mid, "kind": kind, "filename": path.name, "path": dest.name, "size": dest.stat().st_size,
           "sha256": media.sha256_file(dest), "uploaded_at": media.iso_now(), "source": "imessage", "sender": sender}
    store.update_inspection(insp["id"], lambda i: i["media"].append(rec))
    return insp["id"]


# ---------------------------------------------------------------------------
# Outbound (called from the dashboard)
# ---------------------------------------------------------------------------

def _require_group(prop: dict[str, Any]) -> str:
    if not prop.get("chat_id"):
        raise ValueError("Create the iMessage group first")
    return prop["chat_id"]


def _people(prop: dict[str, Any]) -> list[str]:
    phones = [(prop.get("landlord") or {}).get("phone"), (prop.get("tenant") or {}).get("phone")]
    phones += [c.get("phone") for c in prop.get("contractors") or [] if isinstance(c, dict)]
    seen, out = set(), []
    for p in phones:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def create_group(pid: str, intro: str = "") -> dict[str, Any]:
    prop = store.get_property(pid)
    if prop is None:
        raise KeyError(pid)
    people = _people(prop)
    if not people:
        raise ValueError("Add a landlord or tenant phone number first")
    client()
    if any(e["status"] in ("pending", "retrying", "sending") and e["op"] == "group_create" for e in outbox.list_entries(pid=pid)):
        raise ValueError("A group is already being created for this property")
    text = intro.strip() or (
        f"Hi! I'm {ASSISTANT_NAME}, the assistant for {prop['name']}. I'll keep a shared log of this chat for everyone: "
        "repairs, photos, promises and dates, plus move-in and move-out reports. Text me photos anytime, "
        f"or ask \"{ASSISTANT_NAME}, what did we agree about…?\""
    )
    return _send(pid, "group_create", {"to": people, "text": text, "subject": prop["name"][:60] if len(people) > 1 else ""},
                 text=text, on_success="link_group")


def send_text(pid: str, text: str) -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    chat_id = _require_group(prop)
    client()
    return _send(pid, "group_text", {"chat_id": chat_id, "text": text}, text=text, chat_id=chat_id)


def send_photo(pid: str, data: bytes, filename: str, content_type: str, caption: str = "") -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    chat_id = _require_group(prop)
    client()
    suffix = media.sniff_extension(data[:32], Path(filename).suffix.lower() or ".jpg")
    stashed = outbox.stash_file(data, suffix)
    return _send(pid, "group_photo", {"chat_id": chat_id, "file": stashed, "filename": filename[:120], "content_type": content_type, "caption": caption},
                 text=caption or "Photo", chat_id=chat_id, attachments=[{"kind": "photo", "outbox_file": stashed}])


def send_report(pid: str, iid: str) -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    insp = store.get_inspection(iid)
    if insp is None or insp["property_id"] != pid:
        raise KeyError(iid)
    if insp.get("status") != "ready":
        raise ValueError("This report isn't finished yet")
    chat_id = _require_group(prop)
    client()
    tot = insp.get("totals") or {}
    url = public_url(f"/r/{insp['share_token']}")
    title = f"{insp['kind'].replace('_', '-').capitalize()} report · {prop.get('name')}"
    summary = (f"{tot.get('new_issues', 0)} new issue(s), est. ${tot.get('chargeable_low', 0):,}–${tot.get('chargeable_high', 0):,}"
               if insp["kind"] == "move_out" else f"{len(insp.get('rooms') or [])} rooms documented")
    parts = [text_part(f"{title} is ready. {summary}.")]
    parts.append(rich_link_part(url, title=title, summary=summary) if url.startswith("http") else text_part(url))
    return _send(pid, "group_parts", {"chat_id": chat_id, "parts": parts}, text=f"{title}: {url}", chat_id=chat_id, inspection_id=iid)


def _phone_for(prop: dict[str, Any], role: str) -> str:
    phone = ((prop.get(role) or {}) if role in ("landlord", "tenant") else {}).get("phone", "")
    if not phone:
        raise ValueError(f"Add the {role}'s phone number first")
    return phone


def send_voice_note(pid: str, iid: str, role: str = "tenant", language: str = "") -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    phone = _phone_for(prop, role)
    client()
    lang = language or (prop.get(role) or {}).get("language") or "en"
    audio = (insp.get("audio") or {}).get(lang)
    if not audio:
        from mechus.estates.pipeline import regenerate_audio

        audio = regenerate_audio(iid, lang)
    path = store.resolve_asset(iid, "audio", audio["file"])
    if path is None:
        raise FileNotFoundError("Audio summary missing; regenerate it first")
    return _send(pid, "voice_note", {"phone": phone, "path": str(path)},
                 text=f"🎙 Voice summary to {role} ({lang}, {audio['duration_s']}s)", kind="voice_note", inspection_id=iid,
                 to=role, on_success="link_direct")


def send_direct(pid: str, role: str, text: str, effect: str = "") -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    phone = _phone_for(prop, role)
    client()
    return _send(pid, "direct", {"phone": phone, "text": text, "effect": effect}, text=text, kind="direct", to=role,
                 effect=effect, on_success="link_direct")


def react(pid: str, event_id: str, reaction: str) -> dict[str, Any]:
    ev = next((e for e in store.list_events(pid, limit=5000) if e.get("id") == event_id), None)
    if not ev or not ev.get("message_id"):
        raise ValueError("That message has no iMessage id to react to yet")
    client()
    entry = outbox.submit(pid, "react", {"message_id": ev["message_id"], "reaction": reaction})
    if entry["status"] == "failed":
        raise SendFailed(entry["error"], code=entry["error_code"], outbox_id=entry["id"])
    return store.update_event(pid, event_id, lambda e: e.setdefault("tapbacks", []).append({"by": "Mechus", "reaction": reaction})) or ev


def typing(pid: str) -> dict[str, Any]:
    prop = store.get_property(pid) or {}
    return client().typing(_require_group(prop), True)


def edit(pid: str, event_id: str, new_text: str) -> dict[str, Any]:
    ev = next((e for e in store.list_events(pid, limit=5000) if e.get("id") == event_id), None)
    if not ev or ev.get("direction") != "out" or not ev.get("message_id"):
        raise ValueError("Only Mechus's own sent messages can be edited")
    if ev.get("kind") != "direct":
        raise ValueError("iMessage edits only work in 1:1 chats (not groups), within 15 minutes")
    if store.now() - float(ev.get("ts") or 0) > 15 * 60:
        raise ValueError("iMessage only allows edits within 15 minutes of sending")
    if len(ev.get("edits") or []) >= 5:
        raise ValueError("iMessage allows at most 5 edits per message")
    client()
    entry = outbox.submit(pid, "edit", {"message_id": ev["message_id"], "text": new_text}, max_attempts=3)
    if entry["status"] == "failed":
        raise SendFailed(entry["error"], code=entry["error_code"], outbox_id=entry["id"])
    return store.update_event(pid, event_id, lambda e: (e.setdefault("edits", []).append({"text": e.get("text"), "at": store.now()}),
                                                         e.update(text=new_text, edited=True))) or ev


INSPECT_MODES = ("", "move_in", "move_out")


def facetime(
    pid: str,
    role: str,
    video: bool = True,
    *,
    inspect_mode: str = "",
    start_call: Any = None,
) -> dict[str, Any]:
    """FaceTime the landlord or tenant with the live agent on the line.

    ``start_call(payload) -> {"meeting_id": ...}`` starts a Chert FaceTime worker
    (``mechus/chert_worker.py``) through the API server's ``/api/join-meeting``: the agent
    joins the LiveKit room, Chert dials, and the agent talks. ``inspect_mode`` turns the
    call into a move-in or move-out walkthrough keyed to this property, so a later
    move-out call compares against this property's move-in frames.

    Without ``start_call`` only the call is allocated (``chert.facetime.place_call``);
    nobody is in the room to speak, so use that only when an agent is started separately.
    """
    from mechus.chert import facetime as ft

    mode = (inspect_mode or "").strip().lower().replace("-", "_")
    if mode not in INSPECT_MODES:
        raise ValueError("inspect_mode must be move_in or move_out")
    prop = store.get_property(pid) or {}
    phone = _phone_for(prop, role)
    missing = ft.missing_config()
    if missing:
        raise RuntimeError("FaceTime is not configured: set " + ", ".join(missing))
    label = {"move_in": "move-in walkthrough", "move_out": "move-out walkthrough"}.get(mode, "call")
    text = f"FaceTime {'video' if video or mode else 'audio'} {label} with the {role}"
    if start_call is None:
        res = ft.place_call(phone, video=video, room=f"mechus-{pid}")
        store.append_event(pid, {"kind": "facetime", "direction": "out", "actor": ASSISTANT_NAME, "text": text,
                                 "call_id": res["call_id"]})
        return {"call_id": res["call_id"], "room": res["room"]}
    res = start_call({
        "destination": phone, "join_backend": "facetime_chert", "user_id": f"property-{pid}",
        "video": bool(video or mode), "inspect_mode": mode, "inspect_unit": pid if mode else "",
    })
    meeting_id = str(res.get("meeting_id") or "")
    store.append_event(pid, {"kind": "facetime", "direction": "out", "actor": ASSISTANT_NAME, "text": text,
                             "meeting_id": meeting_id, "inspect_mode": mode})
    return {"meeting_id": meeting_id, "inspect_mode": mode, "status_url": f"/api/status?meeting_id={meeting_id}"}


_E164 = re.compile(r"^\+?\d{8,15}$")


def valid_handle(value: str) -> bool:
    v = (value or "").strip()
    return bool(_E164.match(re.sub(r"[\s()-]", "", v))) or bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v))
