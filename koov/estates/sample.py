"""One-click sample property: a finished move-in + move-out pair (the model report).

Room photos are rendered here (no binary assets in the repo), with the move-out
set showing real, visible damage, and the findings/boxes are hand-checked to sit
exactly on it. Nothing is sent anywhere: the sample has no phone numbers.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from koov.estates import costs, media, report, store
from koov.estates.examples import MODEL_FINDINGS

W, H = 1280, 960
BACK = (320, 180, 960, 620)
SAMPLE_NAME = "Sample · Harbor Lofts 4B"


def _shell(d: ImageDraw.ImageDraw, wall: tuple, side: tuple, floor: tuple, ceiling=(246, 244, 240)) -> None:
    x0, y0, x1, y1 = BACK
    d.polygon([(0, 0), (W, 0), (x1, y0), (x0, y0)], fill=ceiling)
    d.polygon([(0, 0), (x0, y0), (x0, y1), (0, H)], fill=side)
    d.polygon([(W, 0), (x1, y0), (x1, y1), (W, H)], fill=tuple(max(0, c - 10) for c in side))
    d.polygon([(0, H), (W, H), (x1, y1), (x0, y1)], fill=floor)
    d.rectangle(BACK, fill=wall)
    d.line([(x0, y1), (x1, y1)], fill=(200, 194, 184), width=6)  # baseboard
    d.line([(0, H), (x0, y1)], fill=(205, 199, 190), width=4)
    d.line([(W, H), (x1, y1)], fill=(205, 199, 190), width=4)


def _window(d: ImageDraw.ImageDraw, box: tuple, *, blinds: bool) -> None:
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=(196, 222, 240), outline=(250, 250, 248), width=10)
    for i in range(6):
        y = y0 + 10 + i * (y1 - y0 - 20) / 6
        d.line([(x0 + 10, y), (x1 - 10, y + 6)], fill=(222, 236, 246), width=3)
    d.line([((x0 + x1) // 2, y0), ((x0 + x1) // 2, y1)], fill=(250, 250, 248), width=8)
    if blinds:
        for y in range(y0 + 14, y1 - 6, 13):
            d.rectangle([x0 + 8, y, x1 - 8, y + 7], fill=(236, 234, 228))
    else:
        for bx in (x0 + 6, x1 - 34):  # brackets left behind
            d.rectangle([bx, y0 + 4, bx + 28, y0 + 16], fill=(170, 170, 168))


def _finish(img: Image.Image, seed: int) -> Image.Image:
    """Soft lens blur, vignette and sensor noise so it reads as a phone photo."""
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    arr = np.asarray(img).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    vignette = 1 - 0.28 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    arr *= vignette[..., None]
    arr += np.random.default_rng(seed).normal(0, 3.2, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def render_room(room: str, *, after: bool) -> tuple[Image.Image, dict[str, list[float]]]:
    """Returns the image and named damage boxes (normalized)."""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    boxes: dict[str, list[float]] = {}
    norm = lambda x0, y0, x1, y1: [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)]

    if room == "Living room":
        _shell(d, (233, 228, 218), (224, 218, 207), (185, 173, 153))
        d.polygon([(110, 250), (250, 320), (250, 700), (110, 790)], fill=(214, 205, 190), outline=(190, 180, 165))  # door
        d.ellipse([222, 500, 236, 514], fill=(176, 150, 92))
        _window(d, (600, 240, 880, 450), blinds=True)
        d.rounded_rectangle([370, 500, 760, 650], radius=26, fill=(96, 112, 104))  # sofa
        d.rounded_rectangle([390, 470, 740, 540], radius=22, fill=(110, 126, 118))
        d.polygon([(420, 700), (900, 700), (1010, 860), (330, 860)], fill=(168, 150, 128))  # rug
        if after:
            d.polygon([(150, 960), (330, 960), (560, 660), (470, 660)], fill=(196, 186, 168))  # traffic path
            boxes["carpet"] = norm(150, 660, 560, 960)
            d.ellipse([262, 448, 302, 486], fill=(90, 84, 76))  # hole near door
            d.ellipse([256, 442, 308, 492], outline=(244, 240, 232), width=4)
            d.line([(270, 458), (294, 476)], fill=(60, 56, 50), width=3)
            boxes["hole"] = norm(250, 436, 314, 498)
    elif room == "Kitchen":
        _shell(d, (239, 233, 221), (230, 223, 211), (207, 198, 184))
        for x in range(0, W, 80):
            d.line([(x, H), (BACK[0] + (x / W) * (BACK[2] - BACK[0]), BACK[3])], fill=(190, 182, 168), width=2)
        d.rectangle([340, 200, 940, 330], fill=(138, 106, 74))  # upper cabinets
        for x in range(340, 941, 150):
            d.line([(x, 200), (x, 330)], fill=(110, 84, 58), width=3)
        d.rectangle([320, 475, 960, 620], fill=(138, 106, 74))  # lower cabinets
        for x in range(320, 961, 160):
            d.line([(x, 475), (x, 620)], fill=(110, 84, 58), width=3)
        d.rectangle([320, 455, 960, 478], fill=(218, 212, 204))  # countertop
        d.rectangle([600, 478, 760, 620], fill=(43, 43, 43))  # stove
        d.rectangle([620, 515, 740, 600], fill=(28, 28, 30), outline=(90, 90, 90), width=3)
        for cx in (640, 720):
            d.ellipse([cx - 22, 458, cx + 22, 470], fill=(30, 30, 30))
        d.rectangle([380, 380, 520, 452], fill=(200, 205, 210))  # backsplash window-ish
        if after:
            d.ellipse([808, 459, 852, 473], fill=(74, 52, 36))  # burn mark
            d.ellipse([816, 462, 844, 470], fill=(46, 32, 22))
            boxes["burn"] = norm(796, 452, 864, 480)
            for (x, y, r) in ((640, 545, 18), (700, 560, 22), (668, 585, 14), (720, 530, 12)):
                d.ellipse([x - r, y - r // 2, x + r, y + r // 2], fill=(110, 92, 52))
            boxes["oven"] = norm(612, 508, 748, 608)
    elif room == "Bathroom":
        _shell(d, (243, 243, 240), (236, 236, 232), (216, 221, 224))
        for y in range(400, 621, 36):
            d.line([(BACK[0], y), (BACK[2], y)], fill=(222, 222, 218), width=2)
        for x in range(BACK[0], BACK[2] + 1, 36):
            d.line([(x, 400), (x, 620)], fill=(222, 222, 218), width=2)
        d.rounded_rectangle([330, 520, 770, 650], radius=18, fill=(252, 252, 250), outline=(214, 214, 210), width=4)  # tub
        d.rectangle([800, 230, 930, 380], fill=(206, 220, 228), outline=(180, 180, 176), width=6)  # mirror
        d.rounded_rectangle([790, 448, 940, 486], radius=10, fill=(250, 250, 248), outline=(210, 210, 206), width=3)  # sink
        d.polygon([(346, 650), (380, 650), (360, 672)], fill=(150, 152, 150))  # chipped tile (pre-existing)
        boxes["tile"] = norm(332, 636, 396, 684)
    elif room == "Bedroom 1":
        _shell(d, (230, 233, 238), (220, 224, 230), (160, 124, 90))
        for x in range(0, W, 70):
            d.line([(x, H), (BACK[0] + (x / W) * (BACK[2] - BACK[0]), BACK[3])], fill=(140, 106, 76), width=2)
        _window(d, (520, 240, 760, 440), blinds=not after)
        boxes["blinds"] = norm(500, 222, 780, 458)
        d.rounded_rectangle([380, 540, 900, 720], radius=20, fill=(107, 127, 163))  # bed
        d.rounded_rectangle([420, 505, 560, 560], radius=18, fill=(240, 240, 236))
        d.rounded_rectangle([720, 505, 860, 560], radius=18, fill=(240, 240, 236))
    return _finish(img, hash((room, after)) % 10_000), boxes


def _save_jpeg(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=88)


def _build_inspection(pid: str, kind: str, *, after: bool, processed_at: float, compare_to: str = "") -> tuple[dict[str, Any], dict[str, dict]]:
    insp = store.create_inspection(pid, {"kind": kind, "compare_to": compare_to} if compare_to else {"kind": kind})
    iid = insp["id"]
    rooms, frames, media_recs, boxes_by_room = [], [], [], {}
    for room in ("Living room", "Kitchen", "Bathroom", "Bedroom 1"):
        img, boxes = render_room(room, after=after)
        mid, fid = store.new_id("m"), store.new_id("f")
        orig = store.media_dir(iid) / f"{mid}.jpg"
        _save_jpeg(img, orig)
        frame_name = f"{mid}.jpg"
        _save_jpeg(img, store.frames_dir(iid) / frame_name)
        with Image.open(orig) as im:
            ph, sh = media.dhash(im), media.sharpness(im)
        media_recs.append({"id": mid, "kind": "photo", "filename": f"{room.lower().replace(' ', '-')}-{'moveout' if after else 'movein'}.jpg",
                           "path": orig.name, "size": orig.stat().st_size, "sha256": media.sha256_file(orig),
                           "uploaded_at": media.iso_now(), "source": "sample", "width": W, "height": H})
        frames.append({"id": fid, "media_id": mid, "t": None, "path": frame_name, "phash": ph, "sharpness": round(sh, 1), "room": room, "view": "overview"})
        rooms.append({"id": store.new_id("r"), "label": room, "frame_ids": [fid], "summary": "", "condition": "good", "items": []})
        boxes_by_room[room] = {"frame_id": fid, "boxes": boxes, "frame": frame_name}
    store.update_inspection(iid, lambda i: i.update(media=media_recs, frames=frames, rooms=rooms))
    return store.get_inspection(iid), boxes_by_room


def create_sample() -> dict[str, Any]:
    existing = next((p for p in store.list_properties() if p.get("sample")), None)
    if existing:
        return existing
    now = store.now()
    prop = store.create_property({
        "name": SAMPLE_NAME, "address": "12 Ithaca St, Austin TX", "unit": "4B", "kind": "residential",
        "landlord": {"name": "Odie Laertes", "phone": ""}, "tenant": {"name": "Penelope Reyes", "phone": "", "language": "es"},
    })
    pid = prop["id"]
    store.update_property(pid, lambda p: p.update(sample=True))

    # Move-in baseline.
    mi, mi_boxes = _build_inspection(pid, "move_in", after=False, processed_at=now - 400 * 86400)
    tile = mi_boxes["Bathroom"]
    mi_findings = [{
        "id": store.new_id("x"), "room": "Bathroom", "item": "Tub tile", "category": "defect", "type": "chip",
        "description": "Chipped corner on the tile at the base of the tub, left side.", "severity": "minor", "confidence": 0.93,
        "after": {"inspection_id": mi["id"], "frame_id": tile["frame_id"], "box": tile["boxes"]["tile"]}, "before": None,
        "reason": "Documented at move-in", "source": "ai", "timestamp": None, "review": "confirmed",
    }]
    summaries_in = {"Living room": "Clean, walls intact, carpet in good condition.", "Kitchen": "Counters and appliances clean and undamaged.",
                    "Bathroom": "Clean; one chipped tile at the base of the tub.", "Bedroom 1": "Clean; blinds installed and working."}
    _finalize(mi, mi_findings, summaries_in,
              "Move-in report for Harbor Lofts 4B is ready. I documented four rooms and one existing issue, a chipped tile at the "
              "base of the tub, so nobody gets charged for it later.", processed_at=now - 400 * 86400)

    # Move-out, compared against the baseline.
    mo, mo_boxes = _build_inspection(pid, "move_out", after=True, processed_at=now - 3600, compare_to=mi["id"])
    by_item = {f["item"]: f for f in MODEL_FINDINGS}
    plan = [("Living room", "Wall", "hole"), ("Living room", "Carpet", "carpet"), ("Kitchen", "Countertop", "burn"),
            ("Kitchen", "Oven", "oven"), ("Bathroom", "Tub tile", "tile"), ("Bedroom 1", "Blinds", "blinds")]
    mo_findings = []
    for room, item, key in plan:
        model = by_item[item]
        a, b = mo_boxes[room], mi_boxes[room]
        mo_findings.append({
            "id": store.new_id("x"), "room": room, "item": item, "category": model["category"], "type": "",
            "description": model["description"], "severity": model["severity"], "confidence": 0.9,
            "after": {"inspection_id": mo["id"], "frame_id": a["frame_id"], "box": a["boxes"].get(key) or b["boxes"].get(key)},
            "before": {"inspection_id": mi["id"], "frame_id": b["frame_id"], "box": b["boxes"].get(key) or a["boxes"].get(key)},
            "reason": model["reason"], "source": "ai", "timestamp": None,
            "review": "confirmed" if model["category"] in ("new_damage", "pre_existing") else "pending",
        })
    summaries_out = {"Living room": "New hole behind the door; normal carpet wear.", "Kitchen": "Burn on the counter; oven needs cleaning.",
                     "Bathroom": "Same chipped tile as move-in; nothing new.", "Bedroom 1": "Window blinds missing."}
    _finalize(mo, mo_findings, summaries_out, "", processed_at=now - 3600, baseline=mi)

    events = [
        ("system", "internal", "Mechus", "", "Move-in report ready: 1 documented issue (chipped tub tile).", now - 400 * 86400),
        ("message", "in", "Penelope Reyes", "tenant", "The kitchen sink is leaking under the cabinet again", now - 6 * 86400),
        ("message", "in", "Odie Laertes", "landlord", "On it. I'll have the plumber come by Thursday morning.", now - 6 * 86400 + 900),
        ("message", "in", "Penelope Reyes", "tenant", "Paid November rent just now", now - 3 * 86400),
        ("system", "internal", "Mechus", "", "Move-out report ready: 4 chargeable items, est. range in the report.", now - 3600),
    ]
    cls = {1: {"kind": "maintenance_request", "urgent": True, "summary": "Kitchen sink leaking under the cabinet"},
           2: {"kind": "agreement", "urgent": False, "summary": "Plumber visit Thursday morning", "due": None},
           3: {"kind": "payment", "urgent": False, "summary": "November rent paid"}}
    for i, (kind, direction, actor, role, text, ts) in enumerate(events):
        ev = store.append_event(pid, {"kind": kind, "direction": direction, "actor": actor, "role": role, "text": text,
                                      "tapbacks": [], "sample": True, **({"classification": cls[i]} if i in cls else {})})
        store.update_event(pid, ev["id"], lambda e, t=ts: e.update(ts=t))
    return store.get_property(pid) or prop


def _finalize(insp: dict[str, Any], findings: list[dict[str, Any]], room_summaries: dict[str, str], summary: str, *,
              processed_at: float, baseline: dict[str, Any] | None = None) -> None:
    iid = insp["id"]
    for f in findings:
        f["cost"] = costs.estimate(f)
        label = f["category"].replace("_", " ")
        for side in ("after", "before"):
            ref = f.get(side)
            if not ref:
                continue
            src_insp = insp if ref["inspection_id"] == iid else baseline
            fr = next(x for x in src_insp["frames"] if x["id"] == ref["frame_id"])
            name = f"{f['id']}-{side}.jpg"
            report.mark(store.frames_dir(ref["inspection_id"]) / fr["path"], store.evidence_dir(iid) / name, ref.get("box"),
                        f["category"], label=label if side == "after" else "move-in")
            ref["image"] = name
    tot = costs.totals(findings)
    if not summary:
        summary = (f"Move-out report for Harbor Lofts 4B is ready. I found {tot['new_issues']} chargeable items across four rooms: "
                   f"a hole behind the living room door, a burn on the kitchen counter, an oven that needs cleaning, and missing "
                   f"bedroom blinds, for an estimated ${tot['chargeable_low']:,} to ${tot['chargeable_high']:,}. Carpet wear and the chipped "
                   "tub tile from move-in are not counted. The landlord reviews every item before anything is final.")
    stages = [{"key": k, "label": label, "state": "done", "detail": "Sample"} for k, label in store.STAGES]
    stages[1].update(state="skipped", detail="Sample photos have no narration")
    stages[6].update(state="skipped", detail="Press “Voice it” to record with ElevenLabs")

    def apply(i: dict[str, Any]) -> None:
        for r in i["rooms"]:
            r["summary"] = room_summaries.get(r["label"], "")
        i.update(findings=findings, totals=tot, summary_text=summary, status="ready", progress=1.0, stage="done",
                 stages=stages, processed_at=processed_at, created_at=processed_at - 600, sample=True)
    store.update_inspection(iid, apply)
