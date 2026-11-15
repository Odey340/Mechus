#!/usr/bin/env python3
"""
Set up Koov's Tiger Data schema and (optionally) load a demo meeting.

  1. Create a free Tiger Cloud service (mlh.link/tigerdata-quickstart) and download its connection file.
  2. In .env:  TIGER_DATABASE_URL=postgres://tsdbadmin:<PASSWORD>@<HOST>:<PORT>/tsdb?sslmode=require
  3. python scripts/tiger_init.py            # create hypertables, continuous aggregate, policies
     python scripts/tiger_init.py --demo     # also insert a short sample meeting and print its analytics
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from koov.envfile import load_env_file  # noqa: E402
from koov import tiger_store  # noqa: E402

DEMO = [
    ("alex", "Alex", "Okay let's kick off the HackUTA standup, where are we on the Zoom bot?", "status"),
    ("priya", "Priya", "The bot joins reliably now and transcripts stream into Tiger Data in real time.", "status"),
    ("sam", "Sam", "Latency is down to about a second for wake word answers on the Vultr box.", "performance"),
    ("alex", "Alex", "Nice. Decision: we demo the live analytics dashboard, not the FaceTime path.", "demo"),
    ("priya", "Priya", "Action item for me, finish the talk share chart by 9pm.", "demo"),
    ("sam", "Sam", "I'll own the deploy script and make sure the Vultr server is up before judging.", "deploy"),
    ("alex", "Alex", "Open question, do we show pricing or keep it to the tech story?", "pitch"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="insert a sample meeting and print analytics")
    args = ap.parse_args()

    load_env_file(str(REPO_ROOT / ".env"))
    if not tiger_store.database_url():
        sys.exit("TIGER_DATABASE_URL is not set. Add your Tiger Cloud service URL to .env first.")

    print("Creating schema...")
    print(json.dumps(tiger_store.ensure_schema(), indent=2))
    print(json.dumps(tiger_store.health(), indent=2, default=str))

    if not args.demo:
        return
    sink = tiger_store.get_sink()
    meeting_id = f"demo-{uuid.uuid4().hex[:8]}"
    t0 = time.time() - 600
    for i, (spk, name, text, topic) in enumerate(DEMO):
        ts = t0 + i * 75
        sink.record_chunk(meeting_id, spk, text, ts, chunk_id=i, topic=topic, speaker_display=name)
        sink.record_event(meeting_id, "koov_response", 650 + 90 * i, {"intent": "demo"}, ts=ts + 5)
    sink.record_snapshot(meeting_id, {
        "meeting_id": meeting_id,
        "updated_at": t0 + 500,
        "decisions": [{"id": "d1", "value": "Demo the live analytics dashboard"}],
        "action_items": [
            {"owner": "Priya", "task": "Finish talk share chart", "deadline": "9pm"},
            {"owner": "Sam", "task": "Vultr deploy script", "deadline": "before judging"},
        ],
        "open_questions": ["Show pricing or keep it to the tech story?"],
        "postponed": [],
    })
    sink.flush()
    print(f"\nDemo meeting: {meeting_id}")
    print(json.dumps(tiger_store.meeting_analytics(meeting_id, "5 minutes"), indent=2, default=str))
    print("\nSearch 'Vultr':")
    for r in tiger_store.search("Vultr", meeting_id=meeting_id):
        print(f"  [{r['speaker_display']}] {r['text']}")


if __name__ == "__main__":
    main()
