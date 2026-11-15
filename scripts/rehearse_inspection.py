#!/usr/bin/env python3
"""Rehearse a walkthrough without a phone call.

Feeds a video file (or a folder of photos) through the same inspector a FaceTime call
uses, with the real vision model, and writes the landlord report as a PDF. Use it to see
what Mechus says and finds before a live demo.

  python scripts/rehearse_inspection.py walkthrough.mp4
  python scripts/rehearse_inspection.py photos/ --tour "kitchen: counters, floor"
  python scripts/rehearse_inspection.py clip.mp4 --say "8=skip" --say "20=that's it"

Options:
  --every S     seconds of video between analysed frames (default 3, like a live call)
  --rpm N       cap on vision requests per minute (default 12, to stay inside free quotas)
  --say T=TEXT  pretend the tenant says TEXT at T seconds (repeatable)
  --out DIR     where to write the report (default rehearsal-<timestamp>/)

This calls the vision model for real (uses your API quota). Nothing is dialed and nothing
is sent to anyone.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def frames_from(source: Path, every_s: float):
    """Yield (seconds, jpeg bytes). A photo counts as two looks, 'every_s' apart."""
    from PIL import Image

    def to_jpeg(img) -> bytes:
        img = img.convert("RGB")
        img.thumbnail((768, 768))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    if source.is_dir():
        t = 0.0
        for path in sorted(p for p in source.iterdir() if p.suffix.lower() in IMAGE_EXT):
            jpeg = to_jpeg(Image.open(path))
            for _ in range(2):
                yield t, jpeg
                t += every_s
        return
    import cv2

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        sys.exit(f"Could not open {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps * every_s)))
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step == 0:
            yield index / fps, to_jpeg(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        index += 1
    cap.release()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("--mode", choices=("move_out", "move_in"), default="move_out")
    ap.add_argument("--tour", default=None, help='e.g. "kitchen: counters, floor; bathroom: sink" (default: the standard tour)')
    ap.add_argument("--every", type=float, default=3.0)
    ap.add_argument("--rpm", type=float, default=12.0)
    ap.add_argument("--say", action="append", default=[], metavar="T=TEXT")
    ap.add_argument("--unit", default="Rehearsal unit")
    ap.add_argument("--tenant", default="Test tenant")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if not args.source.exists():
        sys.exit(f"{args.source} does not exist")

    out = (args.out or Path(f"rehearsal-{time.strftime('%Y%m%d-%H%M%S')}")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["MECHUS_NOTES_DIR"] = str(out)

    from mechus.envfile import load_env_file

    load_env_file(os.getenv("MECHUS_ENV_FILE", str(REPO / ".env")))
    os.environ["MECHUS_NOTES_DIR"] = str(out)

    from mechus import inspection
    from mechus.estates import checks

    clock = {"t": 0.0}
    inspection._CLOCK = lambda: clock["t"]  # tour timeouts follow the footage, not the wall clock

    said: list[tuple[float, str]] = []
    for item in args.say:
        t, _, text = item.partition("=")
        said.append((float(t), text))
    said.sort()

    current = {"jpeg": b""}
    meeting_id = "rehearsal"
    inspector = inspection.Inspector(
        mode=args.mode, unit="rehearsal", meeting_id=meeting_id, get_frame=lambda: current["jpeg"],
        speak=lambda line: print(f"   MECHUS: {line}"),
        store=inspection.LocalInspectionStore(str(out / "inspections")), interval_s=0, tour=args.tour,
        evidence_dir=str(out / "checks" / meeting_id),
    )
    print(f"Model: {inspector.backend}   Tour: {inspector.tour.label() if inspector.tour else 'free roam'}")
    if inspector.tour is not None:
        print(f"   MECHUS: {inspector.tour.opening()}")

    min_gap = 60.0 / max(1.0, args.rpm)
    last_call = 0.0
    n = 0
    for t, jpeg in frames_from(args.source, args.every):
        clock["t"] = t
        while said and said[0][0] <= t:
            _, text = said.pop(0)
            print(f"   TENANT: {text}")
            inspector.hear(text)
        wait = min_gap - (time.time() - last_call)
        if wait > 0:
            time.sleep(wait)
        last_call = time.time()
        current["jpeg"] = jpeg
        n += 1
        try:
            inspector.step()
        except Exception as exc:  # noqa: BLE001
            print(f"[{t:6.1f}s] model call failed: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        print(f"[{t:6.1f}s] frame {n}: {inspector.card_status()}  issues so far: {len(inspector.announced)}")
        if inspector.tour is not None and inspector.tour.done:
            break

    status = inspector.status()
    tour = status.get("tour") or {}
    check = {
        "meeting_id": meeting_id, "mode": args.mode, "at": time.time(), "error": "",
        "result": (status.get("verdict") or "incomplete").replace("in_progress", "incomplete"),
        "rooms": tour.get("rooms") or [], "not_shown": tour.get("not_shown") or [], "findings": status.get("findings") or [],
    }
    prop = {"name": args.unit, "tenant": {"name": args.tenant, "phone": ""}}
    pdf = out / checks.pdf_filename(prop, check)
    pdf.write_bytes(checks.render_pdf(prop, check))
    print()
    print(checks.summary_text(prop, check))
    print(f"Frames analysed: {n}   Report: {pdf}")


if __name__ == "__main__":
    main()
