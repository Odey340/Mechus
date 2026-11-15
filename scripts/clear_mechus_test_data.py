#!/usr/bin/env python3
"""
Clear all persisted Mechus test data so past meetings do not affect new sessions.

Removes:
- mechus_notes/*.jsonl (session transcripts and notes)
- mechus_notes/worker-status-*.json (worker status files)
- mechus_meetings/*/ (per-meeting chunks and snapshots)
- Optionally .rtms_meetings.json (RTMS meeting link store)

Uses MECHUS_NOTES_DIR and MECHUS_MEETING_STORE_DIR if set; otherwise defaults.
Run from repo root or set PYTHONPATH so mechus is importable if using MeetingStore.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    notes_dir = Path(os.environ.get("MECHUS_NOTES_DIR", "mechus_notes"))
    if not notes_dir.is_absolute():
        notes_dir = repo_root / notes_dir
    meeting_store_dir = Path(os.environ.get("MECHUS_MEETING_STORE_DIR", "mechus_meetings"))
    if not meeting_store_dir.is_absolute():
        meeting_store_dir = repo_root / meeting_store_dir
    rtms_store = repo_root / os.environ.get("MECHUS_RTMS_MEETING_STORE", ".rtms_meetings.json")

    removed: list[str] = []

    # Notes: session *.jsonl and worker-status-*.json
    if notes_dir.exists():
        for p in notes_dir.iterdir():
            if p.is_file() and (p.suffix == ".jsonl" or p.name.startswith("worker-status-") and p.suffix == ".json"):
                p.unlink()
                removed.append(str(p))

    # Meeting store: per-meeting directories
    if meeting_store_dir.exists():
        for d in meeting_store_dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d)
                removed.append(str(d))

    # RTMS store (optional)
    if rtms_store.exists():
        rtms_store.unlink()
        removed.append(str(rtms_store))

    if removed:
        print("Cleared Mechus test data:")
        for r in removed:
            print("  -", r)
    else:
        print("No Mechus test data found to clear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
