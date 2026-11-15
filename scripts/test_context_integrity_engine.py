#!/usr/bin/env python3
"""
Test script for the Context Integrity Engine.

Runs sample meeting transcript chunks through the engine, prints [MEMORY UPDATE]
per chunk, then triggers END OF MEETING and prints the full report.
If confidence is low, suggests fixes.
"""

from __future__ import annotations

import os
import sys

# Add project root for imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from koov.context_integrity_engine import (
    ContextIntegrityEngine,
    format_final_report,
    format_memory_update,
)


SAMPLE_CHUNKS = [
    ("Alice", "Let's kick off. We need to decide on the Q2 launch date. I propose we go with June 15."),
    ("Bob", "I agree with June 15. Action item: Alice will finalize the release notes by June 1. Due date: June 1."),
    ("Carol", "Open question: who owns the marketing rollout? We still need to figure out the budget."),
    ("Alice", "Decision: we have decided to use the existing API. Let's go with the v2 endpoint."),
    ("Bob", "Risk: we're worried about the dependency on the legacy system. Potential concern: latency could spike."),
    ("Carol", "Assuming we get sign-off from legal by next Friday. Our assumption: no change to the timeline."),
    ("Alice", "Actually we should not use the v2 endpoint. Let's revert to v1 for now."),
    ("Bob", "So the final decision is we stay on v1. Carol will own the marketing rollout. Due by end of May."),
    ("Carol", "Metric: target 10% conversion. Goal is 50K users by launch."),
    ("Alice", "END OF MEETING — GENERATE OUTPUT"),
]


def suggest_fixes_for_low_confidence(score: int, report) -> list[str]:
    suggestions = []
    if score < 70:
        suggestions.append("Confidence is below 70%. Consider:")
        if not report.final_decisions and not report.action_items_table:
            suggestions.append("  - Add explicit phrases: 'Decision: ...', 'We decided...', 'Action item: X will Y by Z'.")
        if report.contradictions_identified:
            suggestions.append("  - Reduce contradictory statements or add explicit 'We are reversing...' for clarity.")
        if len(report.unresolved_questions) > 5:
            suggestions.append("  - Resolve or explicitly defer open questions so they are tracked as decisions.")
        suggestions.append("  - Use speaker labels (e.g. 'Alice:', 'Bob:') so owner extraction is more accurate.")
        suggestions.append("  - Include explicit deadlines (e.g. 'due June 1', 'by next Friday') for action items.")
    return suggestions


def main() -> int:
    engine = ContextIntegrityEngine(meeting_id="test-meeting-1")
    print("=== Context Integrity Engine test ===\n")

    for i, (speaker, text) in enumerate(SAMPLE_CHUNKS, start=1):
        print(f"--- Chunk {i} ({speaker}) ---")
        print(f"  Input: {text[:80]}{'...' if len(text) > 80 else ''}\n")
        mu, is_end, _tagged = engine.ingest_chunk(text, speaker_id=speaker, timestamp=1000.0 + i)
        print(format_memory_update(mu))
        print()
        if is_end:
            break

    reversed_count = sum(1 for d in engine._decisions if d.reversed_by_chunk_id is not None)
    if reversed_count == 0 and len(SAMPLE_CHUNKS) >= 8:
        print("[CHECK] No decision reversals recorded; chunk 7 should reverse v2 decision.\n")
    else:
        print(f"[CHECK] Decision reversals recorded: {reversed_count}\n")

    report = engine.generate_final_report()
    print("=== FINAL REPORT ===\n")
    print(format_final_report(report))

    score = report.confidence_score
    print("\n--- Confidence check ---")
    if score < 70:
        print(f"LOW CONFIDENCE: {score}%")
        for line in suggest_fixes_for_low_confidence(score, report):
            print(line)
    else:
        print(f"Confidence OK: {score}%")

    return 0 if score >= 50 else 1


if __name__ == "__main__":
    exit(main())
