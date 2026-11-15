"""Move-in / move-out inspection over FaceTime Video (Chert).

Every few seconds the newest camera frame goes to Gemini Flash, which returns JSON
findings (surface, issue, severity, bounding box, one sentence to speak). New findings
are spoken through Koov's ElevenLabs TTS; a finding is announced once.

  move_in   every analysed frame and its findings are saved (MongoDB) as the baseline
  move_out  the saved move-in frames are sent alongside the live frame and Gemini is
            asked what is new since move-in

Env:
  KOOV_INSPECT_MODE        move_in | move_out   (unset = normal Koov call)
  KOOV_INSPECT_UNIT        key for the unit being inspected (default "default")
  KOOV_INSPECT_INTERVAL_S  seconds between frames (default 3)
  GEMINI_API_KEY           Google AI Studio key (also KOOV_GEMINI_API_KEY / GOOGLE_API_KEY)
  KOOV_GEMINI_MODEL        default "gemini-flash-lite-latest" (about 1 s per frame; the
                           full Flash models took over 40 s per frame when tested)
  MONGODB_URI              MongoDB connection string (also KOOV_MONGODB_URI)
  KOOV_MONGODB_DB          database name (default "koov")

Without GEMINI_API_KEY the analyser falls back to OpenAI vision; without MONGODB_URI the
store falls back to files under ``<notes>/inspections/<unit>/``. Both fallbacks log a
warning so a demo never silently runs on the wrong backend.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from koov.brand import ASSISTANT_NAME

log = logging.getLogger(__name__)

_SEVERITIES = ("low", "medium", "high")


def inspect_mode_from_env() -> str:
    """"move_in", "move_out" or "" (inspection off)."""
    raw = (os.environ.get("KOOV_INSPECT_MODE") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in ("move_in", "movein", "in"):
        return "move_in"
    if raw in ("move_out", "moveout", "out"):
        return "move_out"
    return ""


def inspect_unit_from_env() -> str:
    raw = (os.environ.get("KOOV_INSPECT_UNIT") or "default").strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-") or "default"


def _env_first(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


# ── findings ────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    surface: str
    issue: str
    severity: str
    box: list[int]  # [ymin, xmin, ymax, xmax], each 0-1000 of the frame
    say: str
    at: float = field(default_factory=time.time)
    scene: str = ""
    photo: str = ""  # evidence image file name (damage boxed), "" when none was saved
    room: str = ""   # tour stop the finding was made on
    item: str = ""

    def key_tokens(self) -> set[str]:
        return _tokens(f"{self.surface} {self.issue}")


_STOP = {
    "a", "an", "the", "on", "in", "of", "at", "to", "and", "with", "near", "is", "are",
    "there", "some", "small", "large", "visible", "appears", "appear", "area", "side",
}


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in _STOP or len(w) < 3:
            continue
        # crude stemming so "cracks"/"cracked"/"crack" match
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def is_duplicate(finding: Finding, announced: list[Finding], threshold: float = 0.5) -> bool:
    """True when ``finding`` describes something already announced on this call."""
    a = finding.key_tokens()
    if not a:
        return True
    for old in announced:
        b = old.key_tokens()
        if not b:
            continue
        if len(a & b) / float(len(a | b)) >= threshold:
            return True
    return False


_VIEWS = ("clear", "partial", "unclear")


def _load_reply(raw: Any) -> Any:
    """Model reply (JSON text, possibly fenced or with junk around it) -> parsed object."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
        try:
            return json.loads(text) if text else {}
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                return json.loads(m.group(0)) if m else {}
            except Exception:
                return {}
    return raw


def parse_target_seen(raw: Any) -> bool:
    """Model JSON -> whether the live image shows the tour's current stop."""
    raw = _load_reply(raw)
    value = raw.get("target_seen") if isinstance(raw, dict) else None
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def parse_talk(raw: Any) -> tuple[str, str, str]:
    """Model JSON -> (view, ask, ok): how well the area can be seen, a question or request
    for the person holding the camera, and a spoken all-clear. Missing fields come back
    as "" (view defaults to "clear" so replies in the older shape behave as before)."""
    raw = _load_reply(raw)
    if not isinstance(raw, dict):
        return "clear", "", ""
    view = str(raw.get("view") or "clear").strip().lower()
    if view not in _VIEWS:
        view = "clear"
    ask = str(raw.get("ask") or "").strip()[:160]
    ok = str(raw.get("ok") or "").strip()[:160]
    return view, ask, ok


def parse_findings(raw: Any) -> tuple[str, int | None, list[Finding]]:
    """Model JSON -> (scene, matched_move_in index or None, findings). Tolerant of junk."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
        try:
            raw = json.loads(text) if text else {}
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                raw = json.loads(m.group(0)) if m else {}
            except Exception:
                raw = {}
    if isinstance(raw, list):
        raw = {"findings": raw}
    if not isinstance(raw, dict):
        return "", None, []
    scene = str(raw.get("scene") or "").strip()[:80]
    matched = raw.get("matched_move_in")
    try:
        matched = int(matched) if matched is not None else None
    except (TypeError, ValueError):
        matched = None
    out: list[Finding] = []
    for item in raw.get("findings") or []:
        if not isinstance(item, dict):
            continue
        surface = str(item.get("surface") or "").strip()[:60]
        issue = str(item.get("issue") or "").strip()[:160]
        say = str(item.get("say") or "").strip()[:240]
        if not issue or not say:
            continue
        sev = str(item.get("severity") or "low").strip().lower()
        if sev not in _SEVERITIES:
            sev = "low"
        box_raw = item.get("box") or item.get("bounding_box") or item.get("box_2d") or []
        box: list[int] = []
        if isinstance(box_raw, (list, tuple)) and len(box_raw) == 4:
            try:
                box = [max(0, min(1000, int(round(float(v))))) for v in box_raw]
            except (TypeError, ValueError):
                box = []
        out.append(Finding(surface=surface or "surface", issue=issue, severity=sev, box=box, say=say, scene=scene))
    return scene, matched, out


# ── model calls ─────────────────────────────────────────────────────────────

_RULES = (
    f"You are {ASSISTANT_NAME}, a sharp, curious property inspector walking through a rental "
    "unit with someone over a live phone camera. You are actively hunting for problems: "
    "you look closely, you ask to see things again, and you say so when an area checks "
    "out. Look for real, visible damage or defects a landlord could charge for: cracks, "
    "holes, dents, stains, water damage, mold, burns, scratches, chipped or peeling "
    "paint, broken or missing fixtures, torn carpet or screens, damaged doors, windows, "
    "appliances or blinds. Do not report clutter, furniture, normal wear, people, or "
    "lighting. Ignore the small dark rounded box in a lower corner and the small icon in "
    "the top-left: they are call interface overlays.\n\n"
    "Reply with JSON only, in this shape:\n"
    '{"scene": "<3-6 words naming the area in view>", "view": "clear|partial|unclear", '
    '"matched_move_in": null, '
    '"findings": [{"surface": "<wall|floor|ceiling|door|window|counter|...>", '
    '"issue": "<what and where, under 12 words>", "severity": "low|medium|high", '
    '"box": [ymin, xmin, ymax, xmax], "say": "<one short spoken sentence>"}], '
    '"ask": "<one short spoken question or request, or empty>", '
    '"ok": "<one short spoken all-clear, or empty>"}\n\n'
    '"view": "clear" when you can see a surface of the unit well enough to judge it; '
    '"partial" when it is too far, too dark, at a bad angle, or moving too fast to be '
    'sure; "unclear" when the image is blurred or shows no part of the unit (a face, a '
    "hand, the ceiling of a car).\n"
    '"findings": only damage you are sure of. box values are 0-1000 relative to the LIVE '
    "image. Most frames are clean; an empty list is the normal answer.\n"
    '"ask": speak up like an inspector who wants a better look. Use it when (a) something '
    "MIGHT be damage but you cannot confirm it, so ask to see it again, closer, or in "
    'better light ("Can you show me that wall again?", "Hold on, move in closer on that '
    'corner by the window."); (b) the view is partial or unclear ("Slow down for me, I '
    'lost that last bit.", "Flip the camera around so I can see the room."); or (c) the '
    "area in view is fine and something nearby has not been checked yet (\"Can you open "
    'that cabinet under the sink?", "Tilt up so I can see the ceiling.", "What\'s behind '
    'that door?"). Refer to what is actually in the image. Never put a suspicion in '
    '"findings"; ask instead.\n'
    '"ok": when the view is clear and the area has no damage, a brief confirmation that '
    'names the area ("Okay, everything looks good there.", "Alright, that wall checks '
    'out.", "Floor looks fine to me."). Leave it empty when the view is not clear or '
    "there are findings.\n"
    "Spoken lines are under 14 words, plain and conversational, no lists. Vary the "
    "wording; do not reuse a line you already said."
)


def build_prompt(
    mode: str,
    announced: list[Finding],
    baseline: list[dict[str, Any]],
    said: list[str] | None = None,
    cleared: list[str] | None = None,
    target: dict[str, Any] | None = None,
) -> str:
    parts = [_RULES]
    if target:
        parts.append(
            "GUIDED TOUR. You are leading the person through the unit one stop at a time. "
            f"The current stop is: the {target['item']} in the {target['room']}.\n"
            'Add "target_seen" to your JSON: true when the LIVE image shows the '
            f"{target['item']} well enough to judge its condition, false otherwise (wrong "
            "area, too far, too dark, blurred, or a person instead of the unit).\n"
            'Leave "ok" empty: the tour announces what passed. Use "ask" only to get a '
            f"better look at the {target['item']} (\"Move in closer on that for me.\", "
            '"Hold it steady, I lost that."); never ask about a different area. Still '
            "report any real damage you can see anywhere in the image."
        )
    if mode == "move_out":
        if baseline:
            lines = []
            for i, b in enumerate(baseline, start=1):
                known = "; ".join(
                    f"{f.get('surface', '')}: {f.get('issue', '')}" for f in (b.get("findings") or [])
                ) or "no damage recorded"
                lines.append(f"MOVE-IN {i} ({b.get('scene') or 'area'}): {known}")
            parts.append(
                "This is a MOVE-OUT inspection. The first image is the LIVE camera. The images "
                "after it are photos saved at MOVE-IN, in this order:\n" + "\n".join(lines) + "\n"
                'Set "matched_move_in" to the number of the move-in photo that shows the same '
                "area as the live image, or null if none does. Report ONLY damage in the live "
                "image that was NOT there at move-in. Damage already visible in the matching "
                "move-in photo, or listed above for it, is pre-existing: leave it out. In "
                '"say", tell the tenant it is new since move-in.'
            )
        else:
            parts.append(
                "This is a MOVE-OUT inspection. There is no move-in record for this unit, so "
                "report all visible damage as it is now. Do not mention move-in."
            )
    else:
        parts.append(
            "This is a MOVE-IN inspection: the tenant is documenting pre-existing damage. "
            'In "say", describe the damage plainly so it is on the record.'
        )
    if announced:
        parts.append(
            "Already announced on this call, do NOT report these again:\n"
            + "\n".join(f"- {f.surface}: {f.issue}" for f in announced[-12:])
        )
    if mode == "move_out" and baseline:
        parts.append('In "ok", say the area matches move-in ("Okay, that wall matches move-in.").')
    if cleared:
        parts.append(
            "Areas you have already confirmed are fine (no need to confirm them again; "
            "steer toward what has not been checked):\n" + "\n".join(f"- {c}" for c in cleared[-10:])
        )
    if said:
        parts.append(
            "Your most recent spoken lines, oldest first. Do not repeat them or ask for the "
            "same thing again:\n" + "\n".join(f"- {s}" for s in said[-6:])
        )
    return "\n\n".join(parts)


def _post_json(url: str, payload: dict, headers: dict, timeout_s: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def analyze_with_gemini(prompt: str, images: list[bytes], *, api_key: str, model: str, timeout_s: float = 8.0) -> str:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for jpeg in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode("ascii")}})
    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        },
        {"x-goog-api-key": api_key},
        timeout_s,
    )
    cands = data.get("candidates") or []
    if not cands:
        return ""
    return "".join(str(p.get("text") or "") for p in (cands[0].get("content") or {}).get("parts") or [])


def analyze_with_openai(prompt: str, images: list[bytes], *, api_key: str, model: str, timeout_s: float = 15.0) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for jpeg in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")},
            }
        )
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": model,
            "temperature": 0.1,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        },
        {"Authorization": f"Bearer {api_key}"},
        timeout_s,
    )
    return str(data["choices"][0]["message"]["content"] or "")


def make_analyzer() -> tuple[str, Callable[[str, list[bytes]], str]]:
    """(backend name, fn(prompt, images) -> raw JSON text). Gemini Flash when its key is set."""
    gem = _env_first("GEMINI_API_KEY", "KOOV_GEMINI_API_KEY", "GOOGLE_API_KEY")
    if gem:
        model = _env_first("KOOV_GEMINI_MODEL") or "gemini-flash-lite-latest"
        return f"gemini:{model}", lambda p, imgs: analyze_with_gemini(p, imgs, api_key=gem, model=model)
    oai = _env_first("KOOV_OPENAI_API_KEY", "OPENAI_API_KEY")
    if oai:
        model = _env_first("KOOV_VISION_MODEL") or "gpt-4.1-mini"
        log.warning("Inspection: GEMINI_API_KEY is not set; falling back to OpenAI vision (%s)", model)
        return f"openai:{model}", lambda p, imgs: analyze_with_openai(p, imgs, api_key=oai, model=model)
    raise RuntimeError("Inspection needs GEMINI_API_KEY (or an OpenAI key as fallback)")


# ── storage ─────────────────────────────────────────────────────────────────


class LocalInspectionStore:
    """File fallback: ``<root>/<unit>/<mode>/NNNN.jpg`` + ``records.jsonl``."""

    backend = "local-files"

    def __init__(self, root: str) -> None:
        self._root = root

    def _dir(self, unit: str, mode: str) -> str:
        d = os.path.join(self._root, unit, mode)
        os.makedirs(d, exist_ok=True)
        return d

    def save(self, *, unit: str, mode: str, meeting_id: str, jpeg: bytes, scene: str, findings: list[Finding]) -> None:
        d = self._dir(unit, mode)
        name = f"{int(time.time() * 1000)}.jpg"
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(jpeg)
        rec = {
            "unit": unit, "mode": mode, "meeting_id": meeting_id, "at": time.time(),
            "scene": scene, "frame": name, "findings": [asdict(f) for f in findings],
        }
        with open(os.path.join(d, "records.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def load_records(self, unit: str, mode: str, with_images: bool = False) -> list[dict[str, Any]]:
        if with_images and mode == "move_in":
            return self.load_baseline(unit)
        path = os.path.join(self._root, unit, mode, "records.jsonl")
        out: list[dict[str, Any]] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
        return out

    def load_baseline(self, unit: str) -> list[dict[str, Any]]:
        d = os.path.join(self._root, unit, "move_in")
        path = os.path.join(d, "records.jsonl")
        out: list[dict[str, Any]] = []
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    with open(os.path.join(d, rec["frame"]), "rb") as jf:
                        rec["jpeg"] = jf.read()
                    out.append(rec)
                except Exception:  # noqa: BLE001
                    continue
        return out


class MongoInspectionStore:
    """MongoDB: one document per saved frame in ``inspection_frames`` (JPEG inline)."""

    backend = "mongodb"

    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import MongoClient

        self._client = MongoClient(uri, serverSelectionTimeoutMS=6000, appname="koov-inspection")
        self._client.admin.command("ping")
        self._frames = self._client[db_name]["inspection_frames"]
        self._frames.create_index([("unit", 1), ("mode", 1), ("at", 1)])

    def save(self, *, unit: str, mode: str, meeting_id: str, jpeg: bytes, scene: str, findings: list[Finding]) -> None:
        from bson.binary import Binary

        self._frames.insert_one(
            {
                "unit": unit, "mode": mode, "meeting_id": meeting_id, "at": time.time(),
                "scene": scene, "jpeg": Binary(jpeg), "findings": [asdict(f) for f in findings],
            }
        )

    def load_records(self, unit: str, mode: str, with_images: bool = False) -> list[dict[str, Any]]:
        proj = None if with_images else {"jpeg": 0, "_id": 0}
        out = []
        for doc in self._frames.find({"unit": unit, "mode": mode}, proj).sort("at", 1).limit(500):
            doc.pop("_id", None)
            out.append(doc)
        return out

    def load_baseline(self, unit: str) -> list[dict[str, Any]]:
        out = []
        for doc in self._frames.find({"unit": unit, "mode": "move_in"}).sort("at", 1).limit(200):
            doc["jpeg"] = bytes(doc.get("jpeg") or b"")
            doc.pop("_id", None)
            out.append(doc)
        return out


def list_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten stored frame records into one row per finding (no image bytes)."""
    rows: list[dict[str, Any]] = []
    for rec in records:
        for f in rec.get("findings") or []:
            rows.append(
                {
                    "unit": rec.get("unit"), "mode": rec.get("mode"),
                    "meeting_id": rec.get("meeting_id"), "scene": rec.get("scene"),
                    "at": f.get("at") or rec.get("at"),
                    "surface": f.get("surface"), "issue": f.get("issue"),
                    "severity": f.get("severity"), "box": f.get("box"), "say": f.get("say"),
                }
            )
    return rows


def make_store(fallback_root: str) -> Any:
    uri = _env_first("MONGODB_URI", "KOOV_MONGODB_URI", "MONGO_URI")
    if uri:
        try:
            return MongoInspectionStore(uri, _env_first("KOOV_MONGODB_DB") or "koov")
        except Exception as exc:  # noqa: BLE001
            log.warning("Inspection: MongoDB unavailable (%s: %s); using local files", type(exc).__name__, exc)
    else:
        log.warning("Inspection: MONGODB_URI is not set; saving to local files under %s", fallback_root)
    return LocalInspectionStore(fallback_root)


def pick_baseline(records: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Up to ``limit`` move-in frames for a move-out comparison.

    Prefers one frame per distinct scene, frames with recorded damage first, so the model
    sees as many different areas of the unit as fit in one request.
    """
    usable = [r for r in records if r.get("jpeg")]
    usable.sort(key=lambda r: (0 if r.get("findings") else 1, r.get("at") or 0))
    picked: list[dict[str, Any]] = []
    seen: list[set[str]] = []
    for r in usable:
        toks = _tokens(str(r.get("scene") or ""))
        if toks and any(len(toks & s) / float(len(toks | s)) >= 0.6 for s in seen):
            continue
        picked.append(r)
        seen.append(toks)
        if len(picked) >= limit:
            return picked
    for r in usable:  # fill any remaining slots
        if len(picked) >= limit:
            break
        if r not in picked:
            picked.append(r)
    return picked


# ── the loop ────────────────────────────────────────────────────────────────


# ── the guided tour ─────────────────────────────────────────────────────────

DEFAULT_TOUR = (
    "living room: walls, floor, windows and doors; "
    "kitchen: counters and sink, appliances, floor; "
    "bathroom: sink and toilet, tub or shower, floor; "
    "bedroom: walls, floor, closet"
)


def parse_tour(text: str | None) -> list[tuple[str, list[str]]]:
    """"kitchen: counters, floor; bathroom: sink" -> [("kitchen", ["counters", "floor"]), ...].

    ``None`` or "" means the default tour; "off" / "none" / "0" means no tour (free roam).
    A room with no items gets walls and floor.
    """
    raw = (text if text is not None else os.environ.get("KOOV_INSPECT_TOUR") or "").strip()
    if raw.lower() in ("off", "none", "0", "false", "no"):
        return []
    rooms: list[tuple[str, list[str]]] = []
    for part in (raw or DEFAULT_TOUR).split(";"):
        room, _, items = part.partition(":")
        room = re.sub(r"\s+", " ", room).strip().lower()[:40]
        if not room:
            continue
        names = [re.sub(r"\s+", " ", i).strip().lower()[:40] for i in items.split(",")]
        rooms.append((room, [n for n in names if n] or ["walls", "floor"]))
    return rooms


class Tour:
    """The point of order for a walkthrough: one room at a time, one thing at a time.

    The inspector reports what it saw of the current stop (``seen`` / ``found`` /
    ``tick``); the tour decides when to move on and what to say next. A stop is done
    after two clear looks, noted when damage was found on it, and skipped as "not
    shown" when it has not appeared after one reminder.
    """

    def __init__(self, rooms: list[tuple[str, list[str]]], *, mode: str, item_timeout_s: float = 25.0) -> None:
        self.mode = mode
        # True when there are move-in photos to compare against ("matches move-in").
        self.compare = mode == "move_out"
        self.item_timeout_s = float(item_timeout_s)
        self.steps: list[dict[str, Any]] = [
            {"room": room, "item": item, "status": "pending", "issues": 0, "looks": 0, "photo": ""}
            for room, items in rooms
            for item in items
        ]
        self.index = 0
        self.done = not self.steps
        self._prompted_at = time.monotonic()
        self._reminded = False

    # what is being checked right now
    def current(self) -> dict[str, Any] | None:
        return None if self.done else self.steps[self.index]

    def label(self) -> str:
        step = self.current()
        return f"{step['room'].title()}: {step['item']}" if step else "Tour complete"

    def opening(self) -> str:
        step = self.steps[0]
        return f"Let's start in the {step['room']}. Show me the {step['item']}, nice and slow."

    # reports from the inspector, each returns a line to speak (or "")
    def found(self, count: int) -> None:
        step = self.current()
        if step is not None:
            step["issues"] += count
            step["looks"] += 1

    def seen(self) -> str:
        step = self.current()
        if step is None:
            return ""
        step["looks"] += 1
        if step["looks"] < 2:
            return ""
        return self._advance("issue" if step["issues"] else "ok")

    def tick(self) -> str:
        """Called on a frame that did not show the current stop."""
        step = self.current()
        if step is None:
            return ""
        if step["issues"]:  # damage already noted here: do not wait for a second clean look
            return self._advance("issue")
        if time.monotonic() - self._prompted_at < self.item_timeout_s:
            return ""
        if not self._reminded:
            self._reminded = True
            self._prompted_at = time.monotonic()
            return f"I still need to see the {step['item']} in the {step['room']}. Point the camera at it for me."
        return self._advance("not_shown")

    def _advance(self, status: str) -> str:
        step = self.steps[self.index]
        step["status"] = status
        item = step["item"]
        verb = "look" if item.endswith("s") else "looks"
        if status == "ok":
            closing = (
                f"Okay, the {item} {'match' if verb == 'look' else 'matches'} move-in."
                if self.compare
                else f"Okay, the {item} {verb} good."
            )
        elif status == "issue":
            closing = f"Got it, that's noted for the {item}."
        else:
            closing = f"I'll mark the {item} as not shown."
        self.index += 1
        self._reminded = False
        self._prompted_at = time.monotonic()
        if self.index >= len(self.steps):
            self.done = True
            return f"{closing} {self.closing()}"
        nxt = self.steps[self.index]
        if nxt["room"] != step["room"]:
            return f"{closing} That's the {step['room']} done. Now take me to the {nxt['room']}, and show me the {nxt['item']}."
        return f"{closing} Next, show me the {nxt['item']}."

    def closing(self) -> str:
        issues = sum(s["issues"] for s in self.steps)
        missed = sum(1 for s in self.steps if s["status"] == "not_shown")
        if issues:
            result = f"I noted {issues} issue{'s' if issues != 1 else ''}"
        else:
            result = "I found no issues"
        if missed:
            result += f", and {missed} area{'s' if missed != 1 else ''} I couldn't see"
        return f"That's the whole tour. {result}. Thanks for walking me through. You can hang up now."

    def verdict(self) -> str:
        """passed | needs_review | incomplete | in_progress."""
        if any(s["issues"] for s in self.steps):
            return "needs_review"
        if not self.done:
            return "in_progress"
        if any(s["status"] != "ok" for s in self.steps):
            return "incomplete"
        return "passed"

    def summary(self) -> dict[str, Any]:
        rooms: list[dict[str, Any]] = []
        for s in self.steps:
            if not rooms or rooms[-1]["room"] != s["room"]:
                rooms.append({"room": s["room"], "items": []})
            rooms[-1]["items"].append(
                {"item": s["item"], "status": s["status"], "issues": s["issues"], "photo": s.get("photo", "")}
            )
        return {
            "done": self.done, "current": self.label(), "verdict": self.verdict(), "rooms": rooms,
            "not_shown": [f"{s['room']}: {s['item']}" for s in self.steps if s["status"] == "not_shown"],
        }


class Inspector:
    """Background loop: frame -> model -> new findings -> speak (+ save on move-in)."""

    def __init__(
        self,
        *,
        mode: str,
        unit: str,
        meeting_id: str,
        get_frame: Callable[[], bytes | None],
        speak: Callable[[str], None],
        store: Any,
        analyzer: Callable[[str, list[bytes]], str] | None = None,
        interval_s: float | None = None,
        tour: str | None = None,
        evidence_dir: str | None = None,
    ) -> None:
        # Photos for the landlord's report: one per finding (damage boxed) and one per
        # tour stop that passes. None = keep no photos.
        self._evidence_dir = evidence_dir
        self._photo_n = 0
        # Guided tour (default on): Mechus leads room by room. tour="off" = free roam.
        _rooms = parse_tour(tour)
        self.tour: Tour | None = (
            Tour(_rooms, mode=mode, item_timeout_s=float(os.environ.get("KOOV_INSPECT_ITEM_TIMEOUT_S", "25") or "25"))
            if _rooms else None
        )
        self._tour_opened = False
        self._step_asks = 0  # "closer look" requests on the current tour stop
        self.mode = mode
        self.unit = unit
        self._meeting_id = meeting_id
        self._get_frame = get_frame
        self._speak = speak
        self._store = store
        if analyzer is None:
            self.backend, analyzer = make_analyzer()
        else:
            self.backend = "custom"
        self._analyze = analyzer
        self._interval_s = float(interval_s if interval_s is not None else os.environ.get("KOOV_INSPECT_INTERVAL_S", "3") or "3")
        # Seconds of quiet required before a question about a poor view / before any other
        # unprompted line (all-clear, "show me the next thing"). Findings are never held back.
        self._ask_gap_s = float(os.environ.get("KOOV_INSPECT_ASK_GAP_S", "5") or "5")
        self._chat_gap_s = float(os.environ.get("KOOV_INSPECT_CHAT_GAP_S", "9") or "9")
        self.announced: list[Finding] = []
        # Everything said aloud that is not a finding: {"kind": "ask"|"ok", "text", "scene", "at"}.
        self.said: list[dict[str, Any]] = []
        self._cleared: list[tuple[set[str], str]] = []  # (scene tokens, scene) confirmed fine
        self._streak_scene: set[str] = set()
        self._clean_streak = 0  # consecutive clear, damage-free frames of the same scene
        self._scene_asks = 0  # questions asked since the scene last changed
        self._last_talk_at = time.monotonic()  # the greeting has just played
        self.frames_analysed = 0
        self.frames_saved = 0
        self._last_saved_scene: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="koov-inspector")
        self._baseline: list[dict[str, Any]] = []
        if mode == "move_out":
            try:
                self._baseline = pick_baseline(store.load_baseline(unit))
            except Exception as exc:  # noqa: BLE001
                log.warning("Inspection: could not load move-in record: %s", exc)
            log.info("Inspection: move-out for unit %r compares against %d move-in frame(s)", unit, len(self._baseline))
        if self.tour is not None:
            self.tour.compare = bool(self._baseline)

    @property
    def baseline_count(self) -> int:
        return len(self._baseline)

    def start(self) -> None:
        log.info(
            "Inspection started: mode=%s unit=%s every %.1fs model=%s store=%s",
            self.mode, self.unit, self._interval_s, self.backend, getattr(self._store, "backend", "?"),
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "unit": self.unit, "model": self.backend,
            "store": getattr(self._store, "backend", ""),
            "frames_analysed": self.frames_analysed, "frames_saved": self.frames_saved,
            "baseline_frames": len(self._baseline),
            "findings": [asdict(f) for f in self.announced],
            "areas_cleared": [name for _, name in self._cleared],
            "said": list(self.said),
            "tour": self.tour.summary() if self.tour is not None else None,
            "verdict": self.tour.verdict() if self.tour is not None else ("needs_review" if self.announced else ""),
        }

    def _save_photo(self, jpeg: bytes, findings: list[Finding] | None = None) -> str:
        """Write the frame to the evidence folder (findings' boxes drawn on it). Returns the
        file name, or "" when photos are off or the write failed."""
        if not self._evidence_dir or not jpeg:
            return ""
        try:
            os.makedirs(self._evidence_dir, exist_ok=True)
            self._photo_n += 1
            name = f"{self._photo_n:03d}.jpg"
            data = jpeg
            boxes = [f.box for f in (findings or []) if len(f.box) == 4]
            if boxes:
                try:
                    import io

                    from PIL import Image, ImageDraw

                    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
                    draw = ImageDraw.Draw(img)
                    w, h = img.size
                    for ymin, xmin, ymax, xmax in boxes:
                        x0, x1 = sorted((xmin * w / 1000.0, xmax * w / 1000.0))
                        y0, y1 = sorted((ymin * h / 1000.0, ymax * h / 1000.0))
                        for pad, colour in ((0, (255, 255, 255)), (3, (220, 38, 38))):
                            draw.rectangle((x0 + pad, y0 + pad, x1 - pad, y1 - pad), outline=colour, width=3)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    data = buf.getvalue()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Inspection: could not mark the photo: %s", exc)
            with open(os.path.join(self._evidence_dir, name), "wb") as fh:
                fh.write(data)
            return name
        except Exception as exc:  # noqa: BLE001
            log.warning("Inspection: could not save a report photo: %s", exc)
            return ""

    def card_status(self) -> str:
        """Short line for Mechus's video tile: where the tour is."""
        return self.tour.label() if self.tour is not None else "Inspection active"

    def _tour_line(self, view: str, ask: str, target_seen: bool, fresh: list[Finding]) -> str:
        """Advance the guided tour by one frame; returns what to say (or "")."""
        tour = self.tour
        if tour is None or tour.done:
            return ""
        before = tour.index
        if fresh:
            tour.found(len(fresh))
            return ""  # the finding itself was just spoken
        if target_seen and view == "clear":
            line = tour.seen()
        else:
            line = tour.tick()
        if tour.done or tour.index != before:
            self._step_asks = 0
            return line
        if line:
            return line
        # Still on the same stop: ask for a better look when the view is poor.
        quiet_s = time.monotonic() - self._last_talk_at
        if ask and view != "clear" and self._step_asks < 1 and quiet_s >= self._ask_gap_s:
            self._step_asks += 1
            return ask
        return ""

    @staticmethod
    def _similar(a: set[str], b: set[str], threshold: float = 0.6) -> bool:
        return bool(a) and bool(b) and len(a & b) / float(len(a | b)) >= threshold

    def _pick_line(self, scene: str, view: str, ask: str, ok: str, has_findings: bool) -> tuple[str, str]:
        """Choose what to say on a frame with nothing new to report: ("ask"|"ok"|"", text).

        The model proposes lines on every frame; this keeps the pace human. A request for
        a better look comes first, then one all-clear per area (only after two clean looks
        in a row, so a passing glance is not signed off), then a nudge toward something
        that has not been checked.
        """
        toks = _tokens(scene)
        same_scene = self._similar(toks, self._streak_scene)
        if not same_scene:
            self._scene_asks = 0
        if view == "clear" and not has_findings:
            self._clean_streak = self._clean_streak + 1 if same_scene else 1
        else:
            self._clean_streak = 0
        self._streak_scene = toks

        quiet_s = time.monotonic() - self._last_talk_at
        ask_toks = _tokens(ask)
        ask_ok = (
            bool(ask_toks)
            and self._scene_asks < 2
            and not any(
                self._similar(ask_toks, _tokens(s["text"]), 0.5) for s in self.said[-6:] if s["kind"] == "ask"
            )
        )
        if ask_ok and view != "clear" and quiet_s >= self._ask_gap_s:
            return "ask", ask
        if quiet_s < self._chat_gap_s:
            return "", ""
        if (
            view == "clear"
            and not has_findings
            and self._clean_streak >= 2
            and toks
            and not any(self._similar(toks, c) for c, _ in self._cleared)
        ):
            self._cleared.append((toks, scene))
            return "ok", ok or "Okay, everything looks good there."
        if ask_ok:
            return "ask", ask
        return "", ""

    def _say(self, kind: str, text: str, scene: str) -> None:
        self.said.append({"kind": kind, "text": text, "scene": scene, "at": time.time()})
        if kind == "ask":
            self._scene_asks += 1
        log.info("[%s] scene=%r say=%s", kind.upper(), scene, text)
        try:
            self._speak(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Inspection: could not speak: %s", exc)
        self._last_talk_at = time.monotonic()

    def step(self) -> list[Finding]:
        """Analyse one frame. Returns the findings that were new (and spoken)."""
        jpeg = self._get_frame()
        if not jpeg:
            return []
        images = [jpeg] + [b["jpeg"] for b in self._baseline]
        prompt = build_prompt(
            self.mode, self.announced, self._baseline,
            said=[s["text"] for s in self.said], cleared=[name for _, name in self._cleared],
            target=self.tour.current() if self.tour is not None else None,
        )
        t0 = time.monotonic()
        raw = self._analyze(prompt, images)
        scene, matched, findings = parse_findings(raw)
        view, ask, ok = parse_talk(raw)
        self.frames_analysed += 1
        fresh: list[Finding] = []
        for f in findings:
            if not is_duplicate(f, self.announced + fresh):
                fresh.append(f)
        log.info(
            "Inspection frame %d: scene=%r view=%s matched_move_in=%s findings=%d new=%d (%d ms)",
            self.frames_analysed, scene, view, matched, len(findings), len(fresh),
            int((time.monotonic() - t0) * 1000),
        )
        if self.mode == "move_in":
            scene_toks = _tokens(scene)
            new_scene = bool(scene_toks) and (
                not self._last_saved_scene
                or len(scene_toks & self._last_saved_scene) / float(len(scene_toks | self._last_saved_scene)) < 0.6
            )
            if (fresh or new_scene) and self.frames_saved < 80:
                try:
                    self._store.save(
                        unit=self.unit, mode=self.mode, meeting_id=self._meeting_id,
                        jpeg=jpeg, scene=scene, findings=fresh,
                    )
                    self.frames_saved += 1
                    self._last_saved_scene = scene_toks or self._last_saved_scene
                except Exception as exc:  # noqa: BLE001
                    log.warning("Inspection: could not save frame: %s", exc)
        elif fresh:
            try:  # keep move-out evidence too
                self._store.save(
                    unit=self.unit, mode=self.mode, meeting_id=self._meeting_id,
                    jpeg=jpeg, scene=scene, findings=fresh,
                )
                self.frames_saved += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Inspection: could not save frame: %s", exc)
        fresh.sort(key=lambda f: _SEVERITIES.index(f.severity), reverse=True)
        if fresh:
            _stop = self.tour.current() if self.tour is not None else None
            _photo = self._save_photo(jpeg, fresh[:2])
            for f in fresh[:2]:
                f.photo = _photo
                if _stop is not None:
                    f.room, f.item = _stop["room"], _stop["item"]
            if _stop is not None and _photo:
                _stop["photo"] = _photo
        for f in fresh[:2]:  # at most two sentences per frame so Koov never monologues
            self.announced.append(f)
            log.info("[FINDING] %s | %s | %s | box=%s | say=%s", f.severity, f.surface, f.issue, f.box, f.say)
            if self._stop.is_set():
                break
            try:
                self._speak(f.say)
            except Exception as exc:  # noqa: BLE001
                log.warning("Inspection: could not speak finding: %s", exc)
            self._last_talk_at = time.monotonic()
        if self.tour is not None:
            # Guided tour: the tour decides what comes next, not the model.
            was_done = self.tour.done
            _stop = self.tour.current()
            line = self._tour_line(view, ask, parse_target_seen(raw), fresh[:2])
            if _stop is not None and _stop["status"] == "ok" and not _stop["photo"]:
                _stop["photo"] = self._save_photo(jpeg)  # what the stop looked like when it passed
            if line and not self._stop.is_set():
                self._say("tour", line, scene)
            if was_done:
                pass  # tour finished earlier: stay quiet apart from new findings
            return fresh[:2]
        # Free roam: ask for a better look, or sign the area off.
        kind, line = self._pick_line(scene, view, ask, ok, has_findings=bool(findings))
        if kind and not fresh and not self._stop.is_set():
            self._say(kind, line, scene)
        return fresh[:2]

    def _run(self) -> None:
        if self.tour is not None and not self.tour.done and not self._tour_opened:
            self._tour_opened = True
            self._say("tour", self.tour.opening(), "")
            self.tour._prompted_at = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.step()
            except urllib.error.HTTPError as exc:
                log.warning("Inspection: model HTTP %s: %s", exc.code, exc.read()[:300])
                # 429 = rate limit / quota: hammering every few seconds only prolongs it.
                self._stop.wait(10.0 if exc.code == 429 else 2.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("Inspection: frame analysis failed: %s: %s", type(exc).__name__, exc)
                self._stop.wait(1.0)
            self._stop.wait(max(0.0, self._interval_s - (time.monotonic() - t0)))
