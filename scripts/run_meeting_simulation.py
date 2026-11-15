#!/usr/bin/env python3
"""
Meeting simulation runner: prints one line at a time so you can speak/paste
into the meeting. Mimics real cadence (pauses, wait for Mechus) without dumping
the whole transcript.

Usage:
  python scripts/run_meeting_simulation.py              # 3s pause between lines, Enter to continue on Mechus lines
  python scripts/run_meeting_simulation.py --pause 5   # 5 second pause
  python scripts/run_meeting_simulation.py --no-auto   # Always press Enter to advance (no auto pause)
  python scripts/run_meeting_simulation.py --chaotic   # Chaotic / stress-test simulation
"""

from __future__ import annotations

import argparse
import sys
import time

# Product Launch Planning — human-style script (one message at a time)
PRODUCT_LAUNCH_SCRIPT: list[tuple[str, str, bool]] = [
    ("Alex", "Alright everyone, thanks for joining. The goal today is to finalize the launch plan for the beta release.", False),
    ("Sarah", "Yeah, engineering is mostly done with the core features, but we still need to decide the rollout date.", False),
    ("Mike", "Mechus, can you start taking notes for this meeting?", True),
    ("Alex", "Okay so first topic is the launch timeline. Originally we said mid-July.", False),
    ("Sarah", "Engineering would actually prefer July 25th. That gives us time for one more QA cycle.", False),
    ("Mike", "From marketing's side July 25th works. That gives us two weeks to prepare the campaign.", False),
    ("Alex", "Alright let's lock it in then. The beta launch date will be July 25th.", False),
    ("Sarah", "Mechus, what did we just decide?", True),
    ("Mike", "I'll handle the launch announcement blog post.", False),
    ("Sarah", "Engineering will finish QA by July 20th.", False),
    ("Alex", "I'll coordinate the beta user invitations.", False),
    ("Alex", "Mechus, can you list the action items so far?", True),
    ("Sarah", "One risk is that the payment integration hasn't been fully tested yet.", False),
    ("Mike", "Another concern is whether the onboarding flow is clear enough for new users.", False),
    ("Mike", "Mechus, have any risks been mentioned so far?", True),
    ("Alex", "Alright quick recap before we end.", False),
    ("Alex", "Launch date July 25th, engineering finishes QA by the 20th, marketing prepares the announcement, and we monitor those two risks.", False),
    ("Sarah", "Mechus, give us a quick recap of the meeting.", True),
]

# Chaotic / stress-test: interruptions, incomplete sentences, general knowledge, corrections
CHAOTIC_SCRIPT: list[tuple[str, str, bool]] = [
    ("Alex", "So we're thinking Q3 for the—", False),
    ("Sarah", "Wait sorry, Mechus, are you there?", True),
    ("Mike", "I thought we said Q2 last week.", False),
    ("Alex", "We did but then engineering said—", False),
    ("Sarah", "Mechus, what did we decide about the timeline?", True),
    ("Mike", "Actually can we backtrack. Mechus, what's the difference between a beta and an alpha release?", True),
    ("Alex", "So to be clear the date is June 15 now.", False),
    ("Sarah", "Yeah June 15. Mechus, can you give us the action items?", True),
    ("Mike", "I'll do the blog post.", False),
    ("Alex", "And I'll do the invites. Mechus, quick one — what is two plus two?", True),
    ("Sarah", "We're getting off track. Mechus, recap what we decided today.", True),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run meeting simulation line-by-line (speak or paste each line into the meeting)."
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=3.0,
        help="Seconds to wait after each line before showing the next (default 3).",
    )
    parser.add_argument(
        "--no-auto",
        action="store_true",
        help="Never auto-advance; always require Enter to show next line.",
    )
    parser.add_argument(
        "--chaotic",
        action="store_true",
        help="Use chaotic / stress-test simulation (overlaps, corrections, general-knowledge questions).",
    )
    args = parser.parse_args()

    script = CHAOTIC_SCRIPT if args.chaotic else PRODUCT_LAUNCH_SCRIPT
    pause = 0.0 if args.no_auto else max(0, args.pause)
    title = "Chaotic meeting (stress test)" if args.chaotic else "Product Launch Planning"

    print(f"\n{'='*60}")
    print(f"  Meeting simulation: {title}")
    print(f"  Speak or paste each line into the meeting. {'Enter to advance.' if args.no_auto else f'Auto-advance in {pause}s (or press Enter now).'}")
    print(f"{'='*60}\n")

    for i, (speaker, line, wait_for_mechus) in enumerate(script, 1):
        print(f"  [{i}/{len(script)}] {speaker}:")
        print(f"      {line}")
        if wait_for_mechus:
            print("      >>> Wait for Mechus to respond, then press Enter.")
            print()
            try:
                input("  [Enter] ").strip()
            except EOFError:
                print("\nDone.")
                break
            except KeyboardInterrupt:
                print("\nStopped.")
                sys.exit(0)
        else:
            if pause > 0:
                print(f"      >>> Next line in {int(pause)}s (Ctrl+C to stop).")
            else:
                print("      >>> Press Enter for next line.")
            print()
            try:
                if pause > 0:
                    time.sleep(pause)
                else:
                    input("  [Enter] ").strip()
            except KeyboardInterrupt:
                print("\nStopped.")
                sys.exit(0)
            except EOFError:
                break

    print("\n  Simulation complete.\n")


if __name__ == "__main__":
    main()
