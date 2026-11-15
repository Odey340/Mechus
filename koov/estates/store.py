"""File-backed store for properties, inspections, media and timelines.

Layout under ``KOOV_ESTATES_DIR`` (default ``$KOOV_NOTES_DIR/estates``)::

    properties/<pid>.json
    inspections/<iid>.json
    media/<iid>/<mid><ext>          originals (never modified)
    frames/<iid>/<fid>.jpg          keyframes / normalized photos
    evidence/<iid>/<name>.jpg       highlighted evidence crops
    audio/<iid>-<lang>.wav          spoken summaries
    timeline/<pid>.jsonl            iMessage + system events per property
    chats.json                      Chert chat id -> property id
    shares/<token>                  public report token -> inspection id

Production replaces this with Postgres + S3 (see docs/PROPERTIES.md); the API
surface stays the same.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

PROPERTY_KINDS = ("residential", "commercial", "warehouse", "land")
INSPECTION_KINDS = ("move_in", "move_out", "routine", "job", "survey")
CATEGORIES = ("defect", "new_damage", "pre_existing", "wear_and_tear", "cleaning", "missing", "resolved")
CHARGEABLE = ("new_damage", "cleaning", "missing")
SEVERITIES = ("minor", "moderate", "major")
REVIEW_STATES = ("pending", "confirmed", "dismissed")

STAGES = (
    ("ingest", "Reading your footage"),
    ("transcribe", "Transcribing narration (ElevenLabs)"),
    ("rooms", "Finding the rooms"),
    ("inspect", "Inspecting each room"),
    ("costs", "Estimating repair costs"),
    ("evidence", "Marking up the evidence"),
    ("summary", "Recording the audio summary (ElevenLabs)"),
)

_lock = threading.RLock()
_ID_RE = re.compile(r"^[a-z]_[A-Za-z0-9_-]{6,40}$")


def base_dir() -> Path:
    explicit = (os.getenv("KOOV_ESTATES_DIR") or "").strip()
    root = Path(explicit) if explicit else Path(os.getenv("KOOV_NOTES_DIR", "koov_notes")) / "estates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(9).replace('-', 'x').replace('_', 'y')}"


def valid_id(value: str, prefix: str) -> bool:
    return bool(value) and value.startswith(prefix + "_") and bool(_ID_RE.match(value))


def now() -> float:
    return time.time()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        # Windows refuses to replace a file another thread has open for reading
        # (the dashboard polls while the pipeline writes); retry briefly.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _clean_contact(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"name": "", "phone": "", "language": "en"}
    phone = re.sub(r"[^\d+@a-zA-Z._-]", "", str(value.get("phone") or ""))[:64]
    lang = str(value.get("language") or "en").strip().lower()[:8] or "en"
    return {"name": str(value.get("name") or "").strip()[:80], "phone": phone, "language": lang}


def create_property(data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()[:120]
    if not name:
        raise ValueError("Property name is required")
    kind = str(data.get("kind") or "residential")
    if kind not in PROPERTY_KINDS:
        raise ValueError(f"kind must be one of {', '.join(PROPERTY_KINDS)}")
    prop = {
        "id": new_id("p"),
        "name": name,
        "address": str(data.get("address") or "").strip()[:200],
        "kind": kind,
        "unit": str(data.get("unit") or "").strip()[:40],
        "landlord": _clean_contact(data.get("landlord")),
        "tenant": _clean_contact(data.get("tenant")),
        "contractors": [],
        "chat_id": "",
        "created_at": now(),
        "updated_at": now(),
    }
    with _lock:
        _atomic_write_json(base_dir() / "properties" / f"{prop['id']}.json", prop)
    return prop


def get_property(pid: str) -> dict[str, Any] | None:
    if not valid_id(pid, "p"):
        return None
    value = _read_json(base_dir() / "properties" / f"{pid}.json")
    return value if isinstance(value, dict) else None


def list_properties() -> list[dict[str, Any]]:
    out = []
    for path in (base_dir() / "properties").glob("p_*.json"):
        value = _read_json(path)
        if isinstance(value, dict):
            out.append(value)
    return sorted(out, key=lambda p: p.get("created_at", 0), reverse=True)


def update_property(pid: str, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _lock:
        prop = get_property(pid)
        if prop is None:
            raise KeyError(pid)
        fn(prop)
        prop["updated_at"] = now()
        _atomic_write_json(base_dir() / "properties" / f"{pid}.json", prop)
        return prop


def patch_property(pid: str, data: dict[str, Any]) -> dict[str, Any]:
    def apply(p: dict[str, Any]) -> None:
        for key in ("name", "address", "unit"):
            if key in data:
                p[key] = str(data[key] or "").strip()[:200]
        for key in ("landlord", "tenant"):
            if key in data:
                p[key] = _clean_contact(data[key])
    return update_property(pid, apply)


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------

def _inspection_path(iid: str) -> Path:
    return base_dir() / "inspections" / f"{iid}.json"


def create_inspection(pid: str, data: dict[str, Any]) -> dict[str, Any]:
    prop = get_property(pid)
    if prop is None:
        raise KeyError(pid)
    kind = str(data.get("kind") or "move_in")
    if kind not in INSPECTION_KINDS:
        raise ValueError(f"kind must be one of {', '.join(INSPECTION_KINDS)}")
    compare_to = str(data.get("compare_to") or "")
    if kind == "move_out" and not compare_to:
        # Default baseline: the latest ready move-in for this property.
        previous = [i for i in list_inspections(pid) if i["kind"] == "move_in" and i["status"] == "ready"]
        compare_to = previous[0]["id"] if previous else ""
    if compare_to:
        base = get_inspection(compare_to)
        if base is None or base.get("property_id") != pid:
            raise ValueError("compare_to must be an inspection of the same property")
    insp = {
        "id": new_id("i"),
        "property_id": pid,
        "kind": kind,
        "label": str(data.get("label") or "").strip()[:80],
        "status": "draft",
        "stage": "",
        "progress": 0.0,
        "stages": [{"key": k, "label": label, "state": "pending", "detail": ""} for k, label in STAGES],
        "error": "",
        "compare_to": compare_to,
        "media": [],
        "frames": [],
        "transcript": None,
        "rooms": [],
        "findings": [],
        "totals": {},
        "summary_text": "",
        "audio": {},
        "share_token": secrets.token_urlsafe(18),
        "reviewed": False,
        "created_at": now(),
        "processed_at": None,
        "warnings": [],
    }
    with _lock:
        _atomic_write_json(_inspection_path(insp["id"]), insp)
        shares = base_dir() / "shares"
        shares.mkdir(parents=True, exist_ok=True)
        (shares / insp["share_token"]).write_text(insp["id"], encoding="utf-8")
    return insp


def get_inspection(iid: str) -> dict[str, Any] | None:
    if not valid_id(iid, "i"):
        return None
    value = _read_json(_inspection_path(iid))
    return value if isinstance(value, dict) else None


def inspection_for_token(token: str) -> dict[str, Any] | None:
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        return None
    try:
        iid = (base_dir() / "shares" / token).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    insp = get_inspection(iid)
    return insp if insp and insp.get("share_token") == token else None


def list_inspections(pid: str) -> list[dict[str, Any]]:
    out = []
    for path in (base_dir() / "inspections").glob("i_*.json"):
        value = _read_json(path)
        if isinstance(value, dict) and value.get("property_id") == pid:
            out.append(value)
    return sorted(out, key=lambda i: i.get("created_at", 0), reverse=True)


def update_inspection(iid: str, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _lock:
        insp = get_inspection(iid)
        if insp is None:
            raise KeyError(iid)
        fn(insp)
        _atomic_write_json(_inspection_path(iid), insp)
        return insp


def media_dir(iid: str) -> Path:
    return base_dir() / "media" / iid


def frames_dir(iid: str) -> Path:
    return base_dir() / "frames" / iid


def evidence_dir(iid: str) -> Path:
    return base_dir() / "evidence" / iid


def audio_dir() -> Path:
    path = base_dir() / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_asset(iid: str, kind: str, name: str) -> Path | None:
    """Safe path for a served asset (frames/evidence/audio) or None."""
    if not valid_id(iid, "i") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name or "") or ".." in name:
        return None
    roots = {"frames": frames_dir(iid), "evidence": evidence_dir(iid), "audio": audio_dir()}
    root = roots.get(kind)
    if root is None:
        return None
    if kind == "audio" and not name.startswith(iid + "-"):
        return None
    path = (root / name).resolve()
    if not str(path).startswith(str(root.resolve())) or not path.is_file():
        return None
    return path


# ---------------------------------------------------------------------------
# Timeline + chats
# ---------------------------------------------------------------------------

def append_event(pid: str, event: dict[str, Any]) -> dict[str, Any]:
    entry = {"id": new_id("e"), "ts": now(), **event}
    path = base_dir() / "timeline" / f"{pid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_events(pid: str, limit: int = 200) -> list[dict[str, Any]]:
    path = base_dir() / "timeline" / f"{pid}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, limit):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def update_event(pid: str, event_id: str, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
    path = base_dir() / "timeline" / f"{pid}.jsonl"
    with _lock:
        events = list_events(pid, limit=100000)
        found = None
        for ev in events:
            if ev.get("id") == event_id:
                fn(ev)
                found = ev
        if found is not None:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
            os.replace(tmp, path)
        return found


def _chats_path() -> Path:
    return base_dir() / "chats.json"


def link_chat(chat_id: str, pid: str, *, kind: str = "group") -> None:
    with _lock:
        chats = _read_json(_chats_path()) or {}
        chats[chat_id] = {"property_id": pid, "kind": kind, "linked_at": now()}
        _atomic_write_json(_chats_path(), chats)


def property_for_chat(chat_id: str) -> str:
    chats = _read_json(_chats_path()) or {}
    entry = chats.get(chat_id) if isinstance(chats, dict) else None
    return str(entry.get("property_id") or "") if isinstance(entry, dict) else ""


def property_for_handle(handle: str) -> str:
    """Route a 1:1 message to a property by the sender's phone (landlord/tenant/contractor)."""
    if not handle:
        return ""
    for prop in list_properties():
        people = [prop.get("landlord") or {}, prop.get("tenant") or {}, *(prop.get("contractors") or [])]
        if any(_same_handle(p.get("phone", ""), handle) for p in people if isinstance(p, dict)):
            return prop["id"]
    return ""


def _same_handle(a: str, b: str) -> bool:
    da, db = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    if da and db:
        return da[-10:] == db[-10:]
    return bool(a) and a.strip().lower() == (b or "").strip().lower()


def role_for_handle(prop: dict[str, Any], handle: str) -> str:
    if _same_handle((prop.get("landlord") or {}).get("phone", ""), handle):
        return "landlord"
    if _same_handle((prop.get("tenant") or {}).get("phone", ""), handle):
        return "tenant"
    for c in prop.get("contractors") or []:
        if isinstance(c, dict) and _same_handle(c.get("phone", ""), handle):
            return "contractor"
    return "guest"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def delete_inspection(iid: str) -> None:
    import shutil

    insp = get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    with _lock:
        for d in (media_dir(iid), frames_dir(iid), evidence_dir(iid)):
            shutil.rmtree(d, ignore_errors=True)
        for f in audio_dir().glob(f"{iid}-*.wav"):
            f.unlink(missing_ok=True)
        (base_dir() / "shares" / str(insp.get("share_token") or "x")).unlink(missing_ok=True)
        _inspection_path(iid).unlink(missing_ok=True)
        # Later move-outs that compared against this one lose their baseline link, not their data.
        for other in (base_dir() / "inspections").glob("i_*.json"):
            value = _read_json(other)
            if isinstance(value, dict) and value.get("compare_to") == iid:
                value["compare_to"] = ""
                _atomic_write_json(other, value)


def delete_property(pid: str) -> None:
    import shutil

    prop = get_property(pid)
    if prop is None:
        raise KeyError(pid)
    for insp in list_inspections(pid):
        delete_inspection(insp["id"])
    with _lock:
        (base_dir() / "timeline" / f"{pid}.jsonl").unlink(missing_ok=True)
        shutil.rmtree(base_dir() / "inbox" / pid, ignore_errors=True)
        chats = _read_json(_chats_path()) or {}
        if isinstance(chats, dict):
            chats = {k: v for k, v in chats.items() if not (isinstance(v, dict) and v.get("property_id") == pid)}
            _atomic_write_json(_chats_path(), chats)
        (base_dir() / "properties" / f"{pid}.json").unlink(missing_ok=True)
