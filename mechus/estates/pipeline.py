"""Inspection processing: footage -> rooms -> findings -> costs -> evidence -> audio.

Runs in a background thread; every stage persists state + progress so the
dashboard can poll. A failure keeps everything produced so far.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from mechus.estates import ai, costs, media, report, scribe, store, voice

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="estates")
_running: set[str] = set()
_running_lock = threading.Lock()


def start(iid: str) -> bool:
    with _running_lock:
        if iid in _running:
            return False
        _running.add(iid)

    def job() -> None:
        try:
            run(iid)
        finally:
            with _running_lock:
                _running.discard(iid)

    store.update_inspection(iid, _reset)
    _executor.submit(job)
    return True


def is_running(iid: str) -> bool:
    with _running_lock:
        return iid in _running


def _reset(insp: dict[str, Any]) -> None:
    insp.update(status="processing", stage="ingest", progress=0.02, error="", warnings=[])
    for s in insp["stages"]:
        s.update(state="pending", detail="")


def _stage(iid: str, key: str, state: str, detail: str = "") -> None:
    keys = [k for k, _ in store.STAGES]

    def apply(insp: dict[str, Any]) -> None:
        for s in insp["stages"]:
            if s["key"] == key:
                s["state"], s["detail"] = state, detail[:240]
        insp["stage"] = key
        done = sum(1 for s in insp["stages"] if s["state"] in ("done", "skipped"))
        insp["progress"] = round(min(0.99, max(insp.get("progress", 0), (done + (0.5 if state == "running" else 0)) / len(keys))), 3)

    store.update_inspection(iid, apply)


def _norm_room(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def _room_type(label: str) -> str:
    return re.sub(r"\s*\d+$", "", _norm_room(label))


def pick_frames(frames: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Sharpest first, skipping near-duplicates, returned in time order."""
    chosen: list[dict[str, Any]] = []
    for fr in sorted(frames, key=lambda f: -float(f.get("sharpness") or 0)):
        if all(media.hamming(int(fr.get("phash") or 0), int(c.get("phash") or 0)) >= 10 for c in chosen):
            chosen.append(fr)
        if len(chosen) >= n:
            break
    for fr in frames:  # top up with anything left if dedupe was too strict
        if len(chosen) >= n:
            break
        if fr not in chosen:
            chosen.append(fr)
    order = {id(f): i for i, f in enumerate(frames)}
    return sorted(chosen, key=lambda f: order.get(id(f), 0))


def run(iid: str) -> None:
    try:
        _run(iid)
    except Exception as exc:  # noqa: BLE001 - surface to the dashboard
        log.error("inspection %s failed: %s\n%s", iid, exc, traceback.format_exc())
        stage = (store.get_inspection(iid) or {}).get("stage", "")
        _stage(iid, stage or "ingest", "failed", str(exc))
        store.update_inspection(iid, lambda i: i.update(status="failed", error=str(exc)[:400]))


def _run(iid: str) -> None:
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    prop = store.get_property(insp["property_id"]) or {}
    fdir = store.frames_dir(iid)

    # 1. Ingest ---------------------------------------------------------------
    _stage(iid, "ingest", "running")
    if not insp["media"]:
        raise RuntimeError("Add at least one video or photo first")
    frames: list[dict[str, Any]] = []
    audio_files: list[tuple[str, Path]] = []
    for m in insp["media"]:
        src = store.media_dir(iid) / m["path"]
        if m["kind"] == "video":
            pr = media.probe(src)
            for kf in media.extract_keyframes(src, fdir, duration_s=pr.duration_s):
                frames.append({"id": store.new_id("f"), "media_id": m["id"], "t": kf.t, "path": kf.path.name,
                               "phash": kf.phash, "sharpness": round(kf.sharpness, 1), "room": ""})
            if pr.has_audio:
                wav = store.media_dir(iid) / f"{m['id']}.wav"
                if media.extract_audio(src, wav):
                    audio_files.append((m["id"], wav))
        elif m["kind"] == "photo":
            dest = fdir / f"{m['id']}.jpg"
            media.normalize_photo(src, dest)
            from PIL import Image

            with Image.open(dest) as img:
                ph, sh = media.dhash(img), media.sharpness(img)
            frames.append({"id": store.new_id("f"), "media_id": m["id"], "t": None, "path": dest.name,
                           "phash": ph, "sharpness": round(sh, 1), "room": ""})
        elif m["kind"] == "audio":
            wav = store.media_dir(iid) / f"{m['id']}.wav"
            if media.extract_audio(src, wav):
                audio_files.append((m["id"], wav))
    if not frames:
        raise RuntimeError("No usable images found in the uploads (too dark, blurry, or unsupported)")
    store.update_inspection(iid, lambda i: i.update(frames=frames))
    _stage(iid, "ingest", "done", f"{len(frames)} keyframes from {len(insp['media'])} file(s)")

    # 2. Transcribe (ElevenLabs Scribe) ---------------------------------------
    transcript: dict[str, Any] = {"text": "", "language": "", "by_media": {}}
    if not audio_files:
        _stage(iid, "transcribe", "skipped", "No narration in the footage")
    elif not scribe.configured():
        _stage(iid, "transcribe", "skipped", "Add MECHUS_ELEVEN_API_KEY to transcribe narration")
    else:
        _stage(iid, "transcribe", "running")
        texts = []
        for mid, wav in audio_files:
            try:
                res = scribe.transcribe(wav)
            except RuntimeError as exc:
                store.update_inspection(iid, lambda i, e=exc: i["warnings"].append(f"Narration not transcribed: {e}"))
                continue
            transcript["by_media"][mid] = res["segments"]
            transcript["language"] = transcript["language"] or res["language"]
            texts.append(res["text"])
        transcript["text"] = "\n".join(t for t in texts if t)
        store.update_inspection(iid, lambda i: i.update(transcript=transcript))
        _stage(iid, "transcribe", "done", f"{len(transcript['text'].split())} words" if transcript["text"] else "Silent footage")

    # 3. Rooms -------------------------------------------------------------------
    _stage(iid, "rooms", "running")
    labels = ai.label_rooms(
        [{"path": str(fdir / f["path"]), "hint": scribe.narration_near(transcript, f["media_id"], f["t"])} for f in frames],
        property_kind=prop.get("kind", "residential"),
    )
    rooms: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}
    kept = []
    for fr, lab in zip(frames, labels):
        if lab.get("quality") == "unusable":
            continue
        fr["room"] = lab["room"]
        fr["view"] = lab.get("view", "")
        kept.append(fr)
        key = _norm_room(lab["room"])
        if key not in by_label:
            by_label[key] = {"id": store.new_id("r"), "label": lab["room"], "frame_ids": [], "summary": "", "condition": "", "items": []}
            rooms.append(by_label[key])
        by_label[key]["frame_ids"].append(fr["id"])
    frames = kept or frames
    store.update_inspection(iid, lambda i: i.update(frames=frames, rooms=rooms))
    _stage(iid, "rooms", "done", ", ".join(r["label"] for r in rooms))

    # 4. Inspect / compare ------------------------------------------------------
    _stage(iid, "inspect", "running")
    frame_by_id = {f["id"]: f for f in frames}
    baseline = store.get_inspection(insp["compare_to"]) if insp.get("compare_to") else None
    if baseline and baseline.get("status") != "ready":
        baseline = None
    findings: list[dict[str, Any]] = []
    warnings: list[str] = []

    def notes_for(room_frames: list[dict[str, Any]]) -> str:
        return " ".join(filter(None, (scribe.narration_near(transcript, f["media_id"], f["t"]) for f in room_frames)))[:800]

    def inventory(room: dict[str, Any]) -> list[dict[str, Any]]:
        rframes = pick_frames([frame_by_id[x] for x in room["frame_ids"]], 6)
        res = ai.inventory_room(room["label"], [{"path": str(fdir / f["path"])} for f in rframes], notes=notes_for(rframes))
        room.update(summary=res["summary"], condition=res["condition"], items=res["items"])
        return [{
            "id": store.new_id("x"), "room": room["label"], "item": d["item"], "category": "defect",
            "type": d["type"], "description": d["description"], "severity": d["severity"], "confidence": d["confidence"],
            "after": {"inspection_id": iid, "frame_id": rframes[d["frame"]]["id"], "box": d["box"]} if rframes else None,
            "before": None, "reason": "Documented at move-in", "source": "ai",
        } for d in res["defects"]]

    if insp["kind"] == "move_out" and baseline:
        b_frames = {f["id"]: f for f in baseline.get("frames", [])}
        b_rooms = {_norm_room(r["label"]): r for r in baseline.get("rooms", [])}
        b_types: dict[str, dict[str, Any]] = {}
        for r in baseline.get("rooms", []):
            b_types.setdefault(_room_type(r["label"]), r)
        matched: set[str] = set()

        def compare(room: dict[str, Any]) -> list[dict[str, Any]]:
            b_room = b_rooms.get(_norm_room(room["label"])) or b_types.get(_room_type(room["label"]))
            if not b_room:
                warnings.append(f"{room['label']} wasn't filmed at move-in, so its issues are listed but not charged.")
                return inventory(room)
            matched.add(b_room["id"])
            after = pick_frames([frame_by_id[x] for x in room["frame_ids"]], 4)
            before = pick_frames([b_frames[x] for x in b_room["frame_ids"] if x in b_frames], 4)
            base_defects = [
                {"item": f["item"], "description": f["description"], "severity": f["severity"],
                 "frame": next((k for k, bf in enumerate(before) if (f.get("after") or {}).get("frame_id") == bf["id"]), 0),
                 "box": (f.get("after") or {}).get("box")}
                for f in baseline.get("findings", []) if f.get("room") == b_room["label"] and f.get("review") != "dismissed"
            ]
            res = ai.compare_room(
                room["label"],
                [{"path": str(store.frames_dir(baseline["id"]) / f["path"])} for f in before],
                [{"path": str(fdir / f["path"])} for f in after],
                baseline_defects=base_defects, notes=notes_for(after),
            )
            room.update(summary=res["summary"], condition="", items=[])
            out = []
            for f in res["findings"]:
                out.append({
                    "id": store.new_id("x"), "room": room["label"], "item": f["item"], "category": f["category"],
                    "type": "", "description": f["description"], "severity": f["severity"], "confidence": f["confidence"],
                    "after": {"inspection_id": iid, "frame_id": after[f["after_frame"]]["id"], "box": f["after_box"]}
                    if f["after_frame"] is not None and after else None,
                    "before": {"inspection_id": baseline["id"], "frame_id": before[f["before_frame"]]["id"], "box": f["before_box"]}
                    if f["before_frame"] is not None and before else None,
                    "reason": f["reason"], "source": "ai",
                })
            return out

        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(compare, rooms):
                findings.extend(result)
        for r in baseline.get("rooms", []):
            if r["id"] not in matched:
                warnings.append(f"{r['label']} was filmed at move-in but not at move-out.")
    else:
        if insp["kind"] == "move_out":
            warnings.append("No finished move-in report to compare against, so this is a condition inventory.")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(inventory, rooms):
                findings.extend(result)

    for f in findings:  # timestamp back into the source video
        fr = frame_by_id.get((f.get("after") or {}).get("frame_id", ""))
        f["timestamp"] = {"media_id": fr["media_id"], "t": fr["t"]} if fr and fr.get("t") is not None else None
        f["review"] = "pending"
    store.update_inspection(iid, lambda i: (i.update(rooms=rooms, findings=findings), i["warnings"].extend(warnings)))
    _stage(iid, "inspect", "done", f"{len(findings)} finding(s) in {len(rooms)} room(s)")

    # 5. Costs ---------------------------------------------------------------------
    _stage(iid, "costs", "running")
    for f in findings:
        f["cost"] = costs.estimate(f)
    tot = costs.totals(findings)
    store.update_inspection(iid, lambda i: i.update(findings=findings, totals=tot))
    _stage(iid, "costs", "done", f"${tot['chargeable_low']:,}–${tot['chargeable_high']:,} chargeable")

    # 6. Evidence ---------------------------------------------------------------------
    _stage(iid, "evidence", "running")
    edir = store.evidence_dir(iid)
    for f in findings:
        label = f["category"].replace("_", " ")
        for side in ("after", "before"):
            ref = f.get(side)
            if not ref:
                continue
            src_insp = insp if ref["inspection_id"] == iid else baseline
            fr = next((x for x in (frames if src_insp is insp else (src_insp or {}).get("frames", [])) if x["id"] == ref["frame_id"]), None)
            if not fr:
                continue
            src = store.frames_dir(ref["inspection_id"]) / fr["path"]
            name = f"{f['id']}-{side}.jpg"
            report.mark(src, edir / name, ref.get("box"), f["category"], label=label if side == "after" else "move-in")
            ref["image"] = name
    store.update_inspection(iid, lambda i: i.update(findings=findings))
    _stage(iid, "evidence", "done", f"{sum(1 for f in findings if f.get('after'))} marked photo(s)")

    # 7. Summary + ElevenLabs audio --------------------------------------------------
    _stage(iid, "summary", "running")
    ctx = {"property": prop.get("name"), "kind": insp["kind"], "rooms": len(rooms),
           "defects": sum(1 for f in findings if f["category"] == "defect"), **tot,
           "top": [f"{f['room']}: {f['description']}" for f in findings if (f.get("cost") or {}).get("chargeable")][:6]}
    try:
        summary = ai.summarize(ctx)
    except RuntimeError:
        summary = ai._template_summary(ctx)
    store.update_inspection(iid, lambda i: i.update(summary_text=summary))
    if voice.configured():
        try:
            info = voice.synthesize_wav(summary, store.audio_dir() / f"{iid}-en.wav")
            store.update_inspection(iid, lambda i: i.update(audio={"en": {"file": f"{iid}-en.wav", "duration_s": info["duration_s"], "text": summary}}))
            _stage(iid, "summary", "done", f"{info['duration_s']}s voice summary")
        except Exception as exc:  # noqa: BLE001 - audio is optional
            _stage(iid, "summary", "done", f"Summary written; audio failed: {exc}")
    else:
        _stage(iid, "summary", "skipped", "Summary written; add ElevenLabs keys for audio")

    store.update_inspection(iid, lambda i: i.update(status="ready", progress=1.0, processed_at=store.now(), stage="done"))
    store.append_event(prop.get("id") or insp["property_id"], {
        "kind": "system", "direction": "internal", "actor": "Mechus",
        "text": f"{insp['kind'].replace('_', '-').capitalize()} report ready: {tot['findings']} finding(s), "
                f"${tot['chargeable_low']:,}–${tot['chargeable_high']:,} chargeable.",
        "inspection_id": iid,
    })


def regenerate_audio(iid: str, language: str) -> dict[str, Any]:
    insp = store.get_inspection(iid)
    if insp is None:
        raise KeyError(iid)
    lang = re.sub(r"[^a-z-]", "", (language or "en").lower())[:8] or "en"
    text = ai.translate(insp.get("summary_text") or "", lang)
    info = voice.synthesize_wav(text, store.audio_dir() / f"{iid}-{lang}.wav", language=lang)
    entry = {"file": f"{iid}-{lang}.wav", "duration_s": info["duration_s"], "text": text}
    store.update_inspection(iid, lambda i: i.setdefault("audio", {}).__setitem__(lang, entry))
    return entry
