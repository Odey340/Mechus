"""Vision + language calls for Mechus Properties.

Live mode uses an OpenAI-compatible chat endpoint (``MECHUS_OPENAI_API_KEY``) with
JSON output. Offline mode (``MECHUS_ESTATES_FAKE_AI=1`` or no key) returns
deterministic results so the dashboard, tests and demos always work.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image

from mechus.brand import WAKE_WORD
from mechus.estates import examples

log = logging.getLogger(__name__)

ROOM_TYPES = ("Entry", "Living room", "Kitchen", "Dining room", "Bedroom", "Bathroom", "Hallway", "Laundry",
              "Closet", "Garage", "Office", "Balcony", "Exterior", "Basement", "Warehouse floor", "Loading dock", "Lot")
FINDING_CATEGORIES = ("new_damage", "pre_existing", "wear_and_tear", "cleaning", "missing", "resolved")

_WEAR_GUIDE = (
    "Normal wear and tear (NOT chargeable): light scuffs, minor paint fading, small nail holes from hanging pictures, "
    "light carpet traffic wear, loose grout from age, minor sun fading. Damage (chargeable): holes larger than a nail, "
    "burns, large or set-in stains, broken or missing fixtures, cracked tiles or glass, water damage caused by misuse, "
    "pet damage, unapproved paint colors, items left behind, rooms needing more than routine cleaning."
)


def mode() -> str:
    if (os.getenv("MECHUS_ESTATES_FAKE_AI") or "").strip().lower() in ("1", "true", "yes"):
        return "offline"
    return "live" if (os.getenv("MECHUS_OPENAI_API_KEY") or "").strip() else "offline"


def _model(kind: str = "vision") -> str:
    if kind == "text":
        return (os.getenv("MECHUS_ESTATES_TEXT_MODEL") or os.getenv("MECHUS_OPENAI_CHAT_MODEL") or "gpt-4o-mini").strip()
    return (os.getenv("MECHUS_ESTATES_VISION_MODEL") or "gpt-4o").strip()


def _image_part(path: str | Path, *, detail: str = "high", max_side: int = 1024) -> dict[str, Any]:
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), "detail": detail}}


def _chat_json(system: str, content: list[dict[str, Any]] | str, *, model: str, max_tokens: int = 1800,
               timeout_s: float = 90.0, retries: int = 2) -> dict[str, Any]:
    key = (os.getenv("MECHUS_OPENAI_API_KEY") or "").strip()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload).encode("utf-8")
    last = "unknown error"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            (os.getenv("MECHUS_OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                value = json.loads(resp.read().decode("utf-8"))
            text = value["choices"][0]["message"]["content"] or "{}"
            return _loads(text)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read(300).decode('utf-8', 'replace')}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"unreachable: {type(exc).__name__}"
        except (KeyError, IndexError, ValueError) as exc:
            last = f"bad response: {exc}"
        time.sleep(1.2 * (2 ** attempt))
    raise RuntimeError(f"AI request failed ({last})")


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        value = json.loads(m.group(0)) if m else {}
    return value if isinstance(value, dict) else {}


def _box(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _sev(value: Any) -> str:
    v = str(value or "").lower()
    return v if v in ("minor", "moderate", "major") else "moderate"


def _conf(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 2)
    except (TypeError, ValueError):
        return 0.6


def _seed(*parts: Any) -> int:
    return int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def label_rooms(frames: list[dict[str, Any]], *, property_kind: str = "residential") -> list[dict[str, Any]]:
    """frames: [{path, hint}] -> [{room, view, quality}] (same order)."""
    if not frames:
        return []
    if mode() == "offline":
        names = ["Living room", "Kitchen", "Bathroom", "Bedroom 1", "Bedroom 2"]
        per = max(1, len(frames) // min(len(names), max(1, len(frames))))
        return [{"room": names[min(i // per, len(names) - 1)], "view": "overview", "quality": "good"} for i in range(len(frames))]
    out: list[dict[str, Any]] = []
    seen: list[str] = []
    for start in range(0, len(frames), 16):
        batch = frames[start:start + 16]
        content: list[dict[str, Any]] = [{"type": "text", "text": (
            f"Property type: {property_kind}. Rooms named so far: {', '.join(seen) or 'none'}. "
            "For each numbered image say which room it shows. Reuse an existing name when it is the same room; "
            "number repeated room types (Bedroom 1, Bedroom 2, Bathroom 2). Use narration hints when present. "
            'Return JSON {"frames":[{"i":<index>,"room":"...","view":"<what part of the room>","quality":"good|blurry|dark|unusable"}]}.'
        )}]
        for j, fr in enumerate(batch):
            hint = f' Narration nearby: "{fr["hint"]}"' if fr.get("hint") else ""
            content.append({"type": "text", "text": f"Image {j}.{hint}"})
            content.append(_image_part(fr["path"], detail="low", max_side=640))
        data = _chat_json(
            "You label rooms in property walkthrough footage. Known room types: " + ", ".join(ROOM_TYPES) + ".",
            content, model=_model(), max_tokens=1200,
        )
        by_i = {int(f.get("i", -1)): f for f in data.get("frames") or [] if isinstance(f, dict) and str(f.get("i", "")).lstrip("-").isdigit()}
        for j in range(len(batch)):
            f = by_i.get(j, {})
            room = " ".join(str(f.get("room") or (seen[-1] if seen else "Unlabeled")).split())[:40].strip() or "Unlabeled"
            if room not in seen:
                seen.append(room)
            q = str(f.get("quality") or "good").lower()
            out.append({"room": room, "view": str(f.get("view") or "")[:80], "quality": q if q in ("good", "blurry", "dark", "unusable") else "good"})
    return out


# ---------------------------------------------------------------------------
# Move-in inventory
# ---------------------------------------------------------------------------

def inventory_room(room: str, frames: list[dict[str, Any]], *, notes: str = "") -> dict[str, Any]:
    """frames: [{path}] (<=8). Returns {summary, condition, items[], defects[]} with frame indexes into `frames`."""
    if mode() == "offline":
        s = _seed(room, len(frames))
        defects = []
        if s % 3 != 0 and frames:
            defects.append({"item": "Wall", "type": "scuff", "description": f"Light scuff marks on the {room.lower()} wall near the door.",
                            "severity": "minor", "frame": 0, "box": [0.18, 0.32, 0.42, 0.55], "confidence": 0.74})
        return {"summary": f"{room} is clean and in good condition overall.", "condition": "good",
                "items": [{"name": "Walls", "condition": "good"}, {"name": "Floor", "condition": "good"}, {"name": "Fixtures", "condition": "good"}],
                "defects": defects}
    content: list[dict[str, Any]] = [{"type": "text", "text": (
        f"{examples.inventory_examples()} "
        f"Room: {room}. This is a MOVE-IN baseline. Document the condition of everything visible: walls, ceiling, floor, "
        "doors, windows, blinds, fixtures, cabinets, countertops, appliances. List every existing defect so the tenant is "
        "not charged for it later. Only report what is visible; do not guess. Boxes are normalized [x0,y0,x1,y1] on the "
        f"numbered image. Landlord/tenant narration: \"{notes[:600]}\". "
        'Return JSON {"summary":"one sentence","condition":"excellent|good|fair|poor","items":[{"name":"","condition":"","notes":""}],'
        '"defects":[{"item":"","type":"scratch|stain|hole|crack|chip|dent|water_damage|mold|broken|missing|dirty|other",'
        '"description":"","severity":"minor|moderate|major","frame":<image index>,"box":[x0,y0,x1,y1],"confidence":0-1}]}'
    )}]
    for j, fr in enumerate(frames[:8]):
        content += [{"type": "text", "text": f"Image {j}"}, _image_part(fr["path"])]
    data = _chat_json("You are a meticulous property inspector writing an evidence-grade condition report. "
                      "Never identify people.", content, model=_model())
    defects = []
    for d in data.get("defects") or []:
        if not isinstance(d, dict) or not d.get("description"):
            continue
        frame = d.get("frame")
        defects.append({
            "item": str(d.get("item") or "")[:60], "type": str(d.get("type") or "other")[:30],
            "description": str(d.get("description"))[:300], "severity": _sev(d.get("severity")),
            "frame": int(frame) if isinstance(frame, (int, float)) and 0 <= int(frame) < len(frames) else 0,
            "box": _box(d.get("box")), "confidence": _conf(d.get("confidence")),
        })
    cond = str(data.get("condition") or "good").lower()
    return {"summary": str(data.get("summary") or "")[:300],
            "condition": cond if cond in ("excellent", "good", "fair", "poor") else "good",
            "items": [i for i in data.get("items") or [] if isinstance(i, dict)][:30], "defects": defects}


# ---------------------------------------------------------------------------
# Move-out comparison
# ---------------------------------------------------------------------------

def compare_room(room: str, before: list[dict[str, Any]], after: list[dict[str, Any]], *,
                 baseline_defects: list[dict[str, Any]], notes: str = "") -> dict[str, Any]:
    """before/after: [{path}] (<=4 each). Returns {summary, findings[]} with frame indexes into before/after."""
    if mode() == "offline":
        s = _seed(room, len(before), len(after))
        findings = []
        if after:
            findings.append({"item": "Wall", "category": "new_damage", "description": f"New hole in the {room.lower()} wall, about 2 inches wide.",
                             "severity": "moderate", "after_frame": 0, "after_box": [0.55, 0.30, 0.70, 0.46],
                             "before_frame": 0 if before else None, "before_box": [0.55, 0.30, 0.70, 0.46] if before else None,
                             "confidence": 0.81, "reason": "Not visible in the move-in footage of the same wall."})
        if s % 2 == 0 and after:
            findings.append({"item": "Floor", "category": "wear_and_tear", "description": "Light traffic wear on the floor finish.",
                             "severity": "minor", "after_frame": min(1, len(after) - 1), "after_box": [0.2, 0.68, 0.6, 0.95],
                             "before_frame": None, "before_box": None, "confidence": 0.7, "reason": "Expected from normal use."})
        for d in baseline_defects[:1]:
            findings.append({"item": d.get("item", ""), "category": "pre_existing", "description": d.get("description", ""),
                             "severity": d.get("severity", "minor"), "after_frame": 0, "after_box": d.get("box"),
                             "before_frame": d.get("frame", 0), "before_box": d.get("box"), "confidence": 0.77,
                             "reason": "Documented at move-in."})
        return {"summary": f"{room}: {sum(1 for f in findings if f['category'] == 'new_damage')} new issue(s) since move-in.", "findings": findings}
    baseline = "; ".join(f"{d.get('item')}: {d.get('description')}" for d in baseline_defects[:20]) or "none recorded"
    content: list[dict[str, Any]] = [{"type": "text", "text": (
        f"{examples.compare_examples()} "
        f"Room: {room}. Images B0..B{max(0, len(before) - 1)} are from MOVE-IN, images A0..A{max(0, len(after) - 1)} from MOVE-OUT. "
        f"Defects already documented at move-in: {baseline}. Narration at move-out: \"{notes[:600]}\". {_WEAR_GUIDE} "
        "Compare carefully and classify every difference. Pre-existing defects must be labeled pre_existing, never new_damage. "
        "When unsure, lower the confidence rather than guessing. Boxes are normalized [x0,y0,x1,y1]. "
        'Return JSON {"summary":"one sentence","findings":[{"item":"","category":"new_damage|pre_existing|wear_and_tear|cleaning|missing|resolved",'
        '"description":"","severity":"minor|moderate|major","after_frame":<A index or null>,"after_box":[...],'
        '"before_frame":<B index or null>,"before_box":[...],"confidence":0-1,"reason":"why this category"}]}'
    )}]
    for j, fr in enumerate(before[:4]):
        content += [{"type": "text", "text": f"B{j} (move-in)"}, _image_part(fr["path"])]
    for j, fr in enumerate(after[:4]):
        content += [{"type": "text", "text": f"A{j} (move-out)"}, _image_part(fr["path"])]
    data = _chat_json("You are a fair, evidence-driven move-out inspector. Protect both landlord and tenant from unfair "
                      "claims. Never identify people.", content, model=_model(), max_tokens=2200)
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("description"):
            continue
        cat = str(f.get("category") or "").lower()
        if cat not in FINDING_CATEGORIES:
            continue

        def idx(v: Any, n: int) -> int | None:
            return int(v) if isinstance(v, (int, float)) and 0 <= int(v) < n else None

        findings.append({
            "item": str(f.get("item") or "")[:60], "category": cat, "description": str(f.get("description"))[:300],
            "severity": _sev(f.get("severity")), "after_frame": idx(f.get("after_frame"), len(after)),
            "after_box": _box(f.get("after_box")), "before_frame": idx(f.get("before_frame"), len(before)),
            "before_box": _box(f.get("before_box")), "confidence": _conf(f.get("confidence")),
            "reason": str(f.get("reason") or "")[:240],
        })
    return {"summary": str(data.get("summary") or "")[:300], "findings": findings}


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

def summarize(ctx: dict[str, Any]) -> str:
    """Spoken summary (60-110 words) grounded on the report numbers."""
    if mode() == "offline" or not ctx:
        return _template_summary(ctx)
    data = _chat_json(
        "You write short, warm, factual spoken summaries of property inspection reports. No markdown, no lists.",
        "Write a 60-110 word spoken summary of this report for the landlord and tenant. Mention the property, the "
        "number of new issues, the most important ones, the estimated chargeable range, and that the landlord reviews "
        "everything before anything is final. Return JSON {\"summary\":\"...\"}. Report: " + json.dumps(ctx)[:6000],
        model=_model("text"), max_tokens=400,
    )
    return str(data.get("summary") or "").strip() or _template_summary(ctx)


def _template_summary(ctx: dict[str, Any]) -> str:
    name = ctx.get("property") or "the property"
    if ctx.get("kind") == "move_in":
        return (f"Move-in report for {name} is ready. I documented {ctx.get('rooms', 0)} rooms and "
                f"{ctx.get('defects', 0)} existing issues, so nobody gets charged for them later.")
    low, high = ctx.get("chargeable_low", 0), ctx.get("chargeable_high", 0)
    return (f"Move-out report for {name} is ready. I found {ctx.get('new_issues', 0)} new issues across "
            f"{ctx.get('rooms', 0)} rooms, with an estimated ${low:,.0f} to ${high:,.0f} in chargeable repairs. "
            "Wear and tear and anything documented at move-in are not counted. The landlord reviews every item before it's final.")


def translate(text: str, language: str) -> str:
    lang = (language or "en").lower()
    if lang.startswith("en") or not text.strip():
        return text
    if mode() == "offline":
        return text
    data = _chat_json("You translate short spoken messages naturally.",
                      f"Translate into language code '{lang}'. Keep numbers and currency. Return JSON {{\"text\":\"...\"}}. Text: {text}",
                      model=_model("text"), max_tokens=600)
    return str(data.get("text") or text)


_URGENT = re.compile(r"\b(leak|leaking|flood|flooding|gas|smoke|fire|no heat|no hot water|sparks?|burst|mold|break-?in|locked out|sewage)\b", re.I)


def classify_message(text: str, *, has_media: bool, role: str) -> dict[str, Any]:
    """-> {kind, urgent, summary, due, room, needs_clarification, addressed_to_mechus}"""
    t = (text or "").strip()
    addressed = bool(re.search(rf"(^|\s)@?{re.escape(WAKE_WORD)}\b", t, re.I))
    urgent = bool(_URGENT.search(t))
    if mode() == "offline" or not t:
        kind = "evidence" if has_media and not t else "chitchat"
        if urgent or re.search(r"\b(broken|not working|fix|repair|clog|stuck|crack)\b", t, re.I):
            kind = "maintenance_request"
        elif re.search(r"\b(rent|paid|payment|deposit|\$\d)", t, re.I):
            kind = "payment"
        elif re.search(r"\b(i'll|i will|we'll|by (mon|tues|wednes|thurs|fri|satur|sun)day|tomorrow|next week)\b", t, re.I):
            kind = "agreement"
        elif re.search(r"\b(schedule|come by|available|time works|appointment)\b", t, re.I):
            kind = "scheduling"
        if addressed and "?" in t:
            kind = "question"
        return {"kind": kind, "urgent": urgent, "summary": t[:140] or ("Photo" if has_media else ""), "due": None,
                "room": "", "needs_clarification": False, "addressed_to_mechus": addressed}
    data = _chat_json(
        "You file messages from a landlord/tenant/contractor group chat into a property log.",
        f"Sender role: {role}. Has photo/video: {has_media}. Message: \"{t[:1500]}\". "
        'Return JSON {"kind":"maintenance_request|evidence|agreement|payment|scheduling|question|chitchat",'
        '"urgent":true|false (safety or habitability: leaks, gas, no heat, fire, break-in),'
        '"summary":"<=14 words","due":"ISO date or null","room":"room name or empty",'
        '"needs_clarification":true|false}',
        model=_model("text"), max_tokens=250,
    )
    kind = str(data.get("kind") or "chitchat")
    return {"kind": kind if kind in ("maintenance_request", "evidence", "agreement", "payment", "scheduling", "question", "chitchat") else "chitchat",
            "urgent": bool(data.get("urgent")) or urgent, "summary": str(data.get("summary") or t[:140])[:160],
            "due": data.get("due") if isinstance(data.get("due"), str) else None, "room": str(data.get("room") or "")[:40],
            "needs_clarification": bool(data.get("needs_clarification")), "addressed_to_mechus": addressed}


def answer_question(question: str, events: list[dict[str, Any]], property_name: str) -> str:
    log_lines = [f"[{time.strftime('%b %d', time.localtime(e.get('ts', 0)))}] {e.get('actor') or e.get('role') or 'someone'}: "
                 f"{e.get('text') or e.get('summary') or ''}" for e in events[-120:] if (e.get('text') or e.get('summary'))]
    if mode() == "offline":
        hits = [l for l in log_lines if any(w in l.lower() for w in re.findall(r"[a-z]{4,}", question.lower()))]
        return ("Here's what I have on that: " + " / ".join(hits[-3:])) if hits else "I don't have anything logged about that yet."
    data = _chat_json(
        "You answer questions about a property's message log. Only use the log. Be brief and cite dates.",
        f"Property: {property_name}. Question: {question}\nLog:\n" + "\n".join(log_lines)[-9000:] + '\nReturn JSON {"answer":"..."}',
        model=_model("text"), max_tokens=300,
    )
    return str(data.get("answer") or "I couldn't find that in the log.")[:600]
