"""Durable outbox for everything Mechus sends through Chert.

Every outbound action is written to disk first, tried once immediately (so the
dashboard gets instant feedback), and retried in the background with
exponential backoff when the failure is transient (network, 5xx, 429). The
idempotency key is persisted with the entry, so a retry can never double-send.
After a send succeeds, the worker polls the message a few times to pick up
delivered/read receipts for the timeline.

Entry lifecycle: pending -> sent | queued -> (delivered | read);
                 pending -> retrying -> ... -> failed (retry from the dashboard).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mechus.chert.client import ChertClient, ChertError, client_from_env, media_part, new_idempotency_key, text_part
from mechus.estates import store

log = logging.getLogger(__name__)

BACKOFF_S = (3, 10, 30, 90, 300, 900)
STATUS_POLLS_S = (8, 45, 300)          # delivered/read receipt checks after a send
NON_IDEMPOTENT = {"react"}               # Chert reactions aren't idempotent: retry only network failures once

_wake = threading.Event()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_client_factory: Callable[[], ChertClient | None] = client_from_env


def set_client_factory(factory: Callable[[], ChertClient | None]) -> None:
    """Tests and the messaging layer share one way to get a client."""
    global _client_factory
    _client_factory = factory


def _dir() -> Path:
    path = store.base_dir() / "outbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(oid: str) -> Path:
    return _dir() / f"{oid}.json"


def _save(entry: dict[str, Any]) -> None:
    entry["updated_at"] = time.time()
    with store._lock:
        store._atomic_write_json(_path(entry["id"]), entry)


def get(oid: str) -> dict[str, Any] | None:
    if not store.valid_id(oid, "o"):
        return None
    value = store._read_json(_path(oid))
    return value if isinstance(value, dict) else None


def list_entries(*, pid: str = "", statuses: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for p in _dir().glob("o_*.json"):
        e = store._read_json(p)
        if not isinstance(e, dict):
            continue
        if pid and e.get("pid") != pid:
            continue
        if statuses and e.get("status") not in statuses:
            continue
        out.append(e)
    return sorted(out, key=lambda e: e.get("created_at", 0))


def stash_file(data: bytes, suffix: str) -> str:
    """Keep outbound media on disk so retries can re-upload (Chert upload slots expire in 15 min)."""
    d = _dir() / "files"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{store.new_id('f')}{suffix}"
    (d / name).write_bytes(data)
    return name


def _set_event(entry: dict[str, Any], **fields: Any) -> None:
    if entry.get("pid") and entry.get("event_id"):
        store.update_event(entry["pid"], entry["event_id"], lambda e: e.update(fields))


# ---------------------------------------------------------------------------
# Submit + execute
# ---------------------------------------------------------------------------

def submit(pid: str, op: str, args: dict[str, Any], *, event_id: str = "", chat_key: str = "",
           max_attempts: int = 6, on_success: str = "") -> dict[str, Any]:
    """Persist, try once now, and hand transient failures to the background worker."""
    entry = {
        "id": store.new_id("o"), "pid": pid, "op": op, "args": args, "status": "pending", "attempts": 0,
        "max_attempts": 2 if op in NON_IDEMPOTENT else max_attempts, "next_at": time.time(),
        "idempotency_key": new_idempotency_key(op), "error": "", "error_code": "", "result": {},
        "event_id": event_id, "chat_key": chat_key or args.get("chat_id") or args.get("phone") or "",
        "on_success": on_success, "polls_left": len(STATUS_POLLS_S), "created_at": time.time(),
    }
    _save(entry)
    _attempt(entry)
    if entry["status"] in ("pending", "retrying", "queued", "sent"):
        _ensure_worker()
        _wake.set()
    return entry


def retry(oid: str) -> dict[str, Any]:
    entry = get(oid)
    if entry is None:
        raise KeyError(oid)
    if entry["status"] not in ("failed", "retrying"):
        raise ValueError("Only failed or retrying sends can be retried")
    entry.update(status="pending", attempts=0, next_at=time.time(), error="", error_code="")
    _save(entry)
    _set_event(entry, delivery="sending", error="")
    _attempt(entry)
    _ensure_worker()
    _wake.set()
    return entry


def cancel(oid: str) -> dict[str, Any]:
    entry = get(oid)
    if entry is None:
        raise KeyError(oid)
    if entry["status"] in ("sent", "queued", "delivered", "read"):
        raise ValueError("Already sent")
    entry.update(status="cancelled")
    _save(entry)
    _set_event(entry, delivery="cancelled")
    return entry


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, ChertError):
        return exc.status == 0 or exc.status == 429 or exc.status >= 500 and exc.code not in ("4103",)
    return False


def _attempt(entry: dict[str, Any]) -> None:
    client = _client_factory()
    if client is None:
        entry.update(status="failed", error="Chert isn't connected (set MECHUS_CHERT_SIGNING_SECRET and MECHUS_CHERT_TENANT)",
                     error_code="chert_not_configured")
        _save(entry)
        _set_event(entry, delivery="failed", error=entry["error"])
        return
    entry["attempts"] += 1
    entry["status"] = "sending"
    _save(entry)
    try:
        result = _execute(client, entry)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the entry
        retry_ok = _retryable(exc) and entry["attempts"] < entry["max_attempts"]
        if entry["op"] in NON_IDEMPOTENT and isinstance(exc, ChertError) and exc.status != 0:
            retry_ok = False  # the server saw it; a retry could duplicate the tapback
        entry["error"] = friendly(exc)
        entry["error_code"] = getattr(exc, "code", "") or type(exc).__name__
        if retry_ok:
            delay = BACKOFF_S[min(entry["attempts"] - 1, len(BACKOFF_S) - 1)]
            entry.update(status="retrying", next_at=time.time() + delay)
            _set_event(entry, delivery="retrying", error=entry["error"])
        else:
            entry.update(status="failed")
            _set_event(entry, delivery="failed", error=entry["error"])
        _save(entry)
        log.warning("outbox %s %s attempt %d failed: %s", entry["id"], entry["op"], entry["attempts"], entry["error"])
        return
    entry["result"] = result
    queued = str(result.get("status") or "") == "queued"
    entry.update(status="queued" if queued else "sent", error="", error_code="",
                 next_at=time.time() + (5 if queued else STATUS_POLLS_S[0]))
    if entry["op"] in ("react", "edit", "facetime"):
        entry["polls_left"] = 0
    _save(entry)
    _set_event(entry, delivery="queued" if queued else "sent", error="",
               message_id=result.get("message_id") or "", chat_id=result.get("chat_id") or "")
    _after_success(entry)


def _execute(client: ChertClient, entry: dict[str, Any]) -> dict[str, Any]:
    op, a, key = entry["op"], entry["args"], entry["idempotency_key"]
    if op == "group_create":
        res = client.create_chat(a["to"], a["text"], subject=a.get("subject", ""), idempotency_key=key)
        return {"chat_id": str((res.get("chat") or {}).get("id") or ""), "message_id": str((res.get("message") or {}).get("id") or "")}
    if op == "group_text":
        res = client.send_parts(a["chat_id"], [text_part(a["text"])], idempotency_key=key)
        return _msg(res, a["chat_id"])
    if op == "group_parts":
        res = client.send_parts(a["chat_id"], a["parts"], idempotency_key=key)
        return _msg(res, a["chat_id"])
    if op == "group_photo":
        data = (_dir() / "files" / a["file"]).read_bytes()
        att = client.upload_attachment(data, a["filename"], a["content_type"])
        parts = ([text_part(a["caption"])] if a.get("caption") else []) + [media_part(att)]
        res = client.send_parts(a["chat_id"], parts, idempotency_key=key)
        return {**_msg(res, a["chat_id"]), "attachment_id": att}
    if op == "direct":
        res = client.send(a["phone"], a["text"], effect=a.get("effect", ""), idempotency_key=key)
        return {"message_id": str(res.get("message_id") or ""), "chat_id": str(res.get("chat_id") or ""), "status": res.get("status", "sent")}
    if op == "voice_note":
        wav = Path(a["path"])
        res = client.send_voice_note(a["phone"], wav.read_bytes(), idempotency_key=key)
        return {"message_id": str(res.get("message_id") or ""), "chat_id": str(res.get("chat_id") or ""), "status": res.get("status", "sent")}
    if op == "react":
        client.react(a["message_id"], a["reaction"])
        return {"message_id": a["message_id"]}
    if op == "edit":
        client.edit(a["message_id"], a["text"], idempotency_key=key)
        return {"message_id": a["message_id"]}
    raise ValueError(f"unknown outbox op {op}")


def _msg(res: dict[str, Any], chat_id: str) -> dict[str, Any]:
    m = res.get("message") if isinstance(res.get("message"), dict) else res
    return {"message_id": str(m.get("id") or m.get("message_id") or ""), "chat_id": chat_id, "status": m.get("status", "sent")}


def _after_success(entry: dict[str, Any]) -> None:
    if entry.get("on_success") == "link_group":
        chat_id = entry["result"].get("chat_id")
        if chat_id:
            store.link_chat(chat_id, entry["pid"], kind="group" if len(entry["args"].get("to", [])) > 1 else "direct")
            store.update_property(entry["pid"], lambda p: p.update(chat_id=chat_id))
    elif entry.get("on_success") == "link_direct":
        chat_id = entry["result"].get("chat_id")
        if chat_id and not store.property_for_chat(chat_id):
            store.link_chat(chat_id, entry["pid"], kind="direct")


# ---------------------------------------------------------------------------
# Background worker: retries + delivery receipts
# ---------------------------------------------------------------------------

def _poll_status(client: ChertClient, entry: dict[str, Any]) -> None:
    mid = entry["result"].get("message_id")
    entry["polls_left"] = max(0, int(entry.get("polls_left", 0)) - 1)
    if not mid:
        entry["polls_left"] = 0
        _save(entry)
        return
    try:
        res = client.get_message(mid)
    except ChertError as exc:
        log.info("status poll for %s failed: %s", mid, exc)
        res = {}
    m = res.get("message") if isinstance(res.get("message"), dict) else res
    state = str(m.get("status") or "")
    delivery = "read" if m.get("read_at") else "delivered" if m.get("delivered_at") else ""
    if state == "failed":
        entry.update(status="failed", error="Chert reported the message as not delivered", polls_left=0)
        _set_event(entry, delivery="failed", error=entry["error"])
    elif delivery:
        entry["status"] = delivery
        _set_event(entry, delivery=delivery)
        if delivery == "read":
            entry["polls_left"] = 0
    elif entry["status"] == "queued" and state == "sent":
        entry["status"] = "sent"
        _set_event(entry, delivery="sent")
    idx = len(STATUS_POLLS_S) - entry["polls_left"]
    entry["next_at"] = time.time() + (STATUS_POLLS_S[idx] if entry["polls_left"] else 0)
    _save(entry)


def process_due(now: float | None = None) -> int:
    """One worker pass. Keeps per-chat order: a chat's later sends wait for its earlier ones."""
    now = time.time() if now is None else now
    entries = list_entries(statuses=("pending", "retrying", "queued", "sent", "delivered"))
    blocked: set[str] = set()
    done = 0
    client = None
    for e in entries:
        key = e.get("chat_key") or ""
        if e["status"] in ("pending", "retrying"):
            if key in blocked:
                continue
            if e.get("next_at", 0) <= now:
                _attempt(e)
                done += 1
            if e["status"] in ("pending", "retrying") and key:
                blocked.add(key)
        elif e.get("polls_left", 0) > 0 and e.get("next_at", 0) <= now:
            client = client or _client_factory()
            if client is not None:
                _poll_status(client, e)
                done += 1
    return done


def _loop() -> None:
    while True:
        try:
            process_due()
        except Exception:  # noqa: BLE001
            log.exception("outbox worker pass failed")
        pending = list_entries(statuses=("pending", "retrying", "queued", "sent", "delivered"))
        waits = [max(0.5, e.get("next_at", 0) - time.time()) for e in pending
                 if e["status"] in ("pending", "retrying") or e.get("polls_left", 0) > 0]
        _wake.wait(timeout=min(waits) if waits else 60)
        _wake.clear()


def _ensure_worker() -> None:
    global _worker
    if (os.getenv("MECHUS_OUTBOX_WORKER") or "1").strip() == "0":  # tests drive process_due() directly
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True, name="chert-outbox")
            _worker.start()


def start() -> None:
    """Resume unfinished sends after a restart."""
    if list_entries(statuses=("pending", "retrying", "queued", "sent")):
        _ensure_worker()


def friendly(exc: Exception) -> str:
    """Human message for the dashboard."""
    if isinstance(exc, ChertError):
        code = exc.code
        body = exc.body if isinstance(exc.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        hints = {
            "4098": "Apple doesn't support that effect" + (f" (try: {', '.join(err.get('allowed_effects') or body.get('allowed_effects') or [])})" if (err.get("allowed_effects") or body.get("allowed_effects")) else ""),
            "4099": "Edits aren't available on this Chert line",
            "4001": "Delivery failed (for edits: the 15-minute window may have passed)",
            "4103": "Chert couldn't confirm the outcome; check the phone before retrying",
            "2003": "Chert couldn't find that message or chat",
            "1002": "Chert rejected the request as invalid",
        }
        if code in hints:
            return hints[code]
        if exc.status == 429:
            return "Rate limited by Chert; retrying shortly"
        if exc.status >= 500:
            return f"Chert is having trouble (HTTP {exc.status}); retrying"
        if exc.status == 0:
            return "Couldn't reach Chert; retrying"
        message = err.get("message") if isinstance(err, dict) else ""
        return message or str(exc)
    if isinstance(exc, FileNotFoundError):
        return "The file to send is missing"
    return str(exc)[:300]


def purge_files(older_than_s: float = 7 * 86400) -> None:
    """Remove stashed media for finished sends."""
    cutoff = time.time() - older_than_s
    keep = {e["args"].get("file") for e in list_entries(statuses=("pending", "retrying", "failed"))}
    files = _dir() / "files"
    if files.exists():
        for f in files.iterdir():
            if f.name not in keep and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass
    shutil.rmtree(_dir() / "tmp", ignore_errors=True)
