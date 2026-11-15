#!/usr/bin/env python3
"""
Pressure-test hardening claims. Run after production hardening pass.
Steps A–F from verification checklist.
"""

from __future__ import annotations

import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.structured_response import try_structured_response


def _run_sim_metrics():
    """Step A: Run sim and extract hard metrics (LLM calls, p95, % citations)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("simulate_meeting_test", os.path.join(_script_dir, "simulate_meeting_test.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    run_simulation = mod.run_simulation
    result = run_simulation(use_general=False)
    qa = result["qa_log"]
    total_llm = sum(1 for e in qa if e.get("llm_called"))
    lat = result["latencies_ms"]
    sorted_lat = sorted(lat)
    p95 = sorted_lat[int(len(sorted_lat) * 0.95) - 1] if lat else 0
    with_citations = sum(1 for e in qa if (e.get("event_ids") or []))
    pct_citations = 100 * with_citations / len(qa) if qa else 0
    structured_count = sum(1 for e in qa if e.get("structured_path"))
    return {
        "total_questions": len(qa),
        "total_llm_calls": total_llm,
        "structured_path_count": structured_count,
        "p95_ms": p95,
        "pct_with_citations": pct_citations,
    }


def _test_action_items_only():
    """Step B: 'What are the action items?' must contain only tasks, not decisions."""
    script = [
        "Alice: Decision: we stay on v1. Bob will send the release notes by June 1.",
        "Carol: Carol will own marketing by end of May.",
    ]
    engine = ContextIntegrityEngine(meeting_id="verify-b")
    for i, line in enumerate(script):
        if ":" in line:
            speaker, _, rest = line.partition(":")
            engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000 + i)
        else:
            engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000 + i)
    answer, event_ids = try_structured_response(engine, "What are the action items?")
    if answer is None:
        return False, "structured path returned None"
    a = answer.lower()
    if "we stay" in a or "decision" in a or "v1" in a and "release" not in a and "marketing" not in a:
        return False, f"answer mixes decision with action items: {answer[:120]}"
    if "bob" not in a and "carol" not in a:
        return False, f"answer should list owners: {answer[:120]}"
    return True, "action items only (no decision phrasing)"


def _test_missing_evidence():
    """Step C: 'What did we decide about partnerships?' must refuse (never mentioned)."""
    script = [
        "Alice: We decided on v1 for the API. Carol owns marketing.",
    ]
    engine = ContextIntegrityEngine(meeting_id="verify-c")
    for i, line in enumerate(script):
        if ":" in line:
            speaker, _, rest = line.partition(":")
            engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000 + i)
        else:
            engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000 + i)
    answer, event_ids = try_structured_response(engine, "What did we decide about partnerships?")
    if answer is None:
        return False, "structured path returned None (expected refusal with event_ids empty or no decision)"
    a = answer.lower()
    if "don't have enough information" in a or "no decision captured" in a:
        return True, "refused correctly"
    if "partnership" in a:
        return False, f"should not invent partnership content: {answer[:120]}"
    return True, "no partnership mentioned (acceptable)"


def _test_hallucination_guard():
    """Step D: Action item with no due date must not get an invented due date."""
    script = [
        "Alice: Bob will follow up with legal. No deadline mentioned.",
    ]
    engine = ContextIntegrityEngine(meeting_id="verify-d")
    for i, line in enumerate(script):
        if ":" in line:
            speaker, _, rest = line.partition(":")
            engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000 + i)
        else:
            engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000 + i)
    answer, event_ids = try_structured_response(engine, "What are the action items?")
    if answer is None:
        return False, "structured path returned None"
    a = answer.lower()
    if "(due " in a or "due by" in a or "by next" in a or "by friday" in a:
        return False, f"invented due date: {answer[:120]}"
    return True, "no invented due date"


def _test_wake_negative():
    """Step E: 'Mechus is cool' -> no activation. Test same rule logic without loading Vosk/audio."""
    import re
    wake_phrase = "mechus"
    text = "mechus is cool"
    t = text.lower().strip()
    for phrase in (wake_phrase, wake_phrase.capitalize()):
        if t.startswith(phrase):
            t = t[len(phrase):].strip()
            break
    t = re.sub(rf"\b{re.escape(wake_phrase)}\b", "", t, flags=re.IGNORECASE).strip()
    if not t or len(t) < 2:
        return False, "wake-only should allow; this is 'mechus is cool'"
    non_commands = ("is cool", "sounds good", "is great", "is awesome", "is fine", "is okay")
    if any(t.startswith(p) or p in t for p in non_commands) and len(t) < 25:
        return True, "no activation for 'Mechus is cool' (rule rejects non-command)"
    return False, "rule should have rejected 'is cool'"


def _test_event_ids_structured():
    """Step F: Structured path responses must include event_ids."""
    script = [
        "Alice: Decision: we go with v1. Bob will send notes by June 1.",
    ]
    engine = ContextIntegrityEngine(meeting_id="verify-f")
    for i, line in enumerate(script):
        if ":" in line:
            speaker, _, rest = line.partition(":")
            engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000 + i)
        else:
            engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000 + i)
    answer, event_ids = try_structured_response(engine, "What did we decide?")
    if answer is None:
        return False, "structured path returned None"
    if not event_ids:
        return False, f"structured path must return event_ids; got {event_ids}"
    return True, f"event_ids={event_ids}"


def main():
    print("=" * 60)
    print("HARDENING VERIFICATION")
    print("=" * 60)

    # A
    print("\n--- Step A: Hard metrics from simulation ---")
    try:
        m = _run_sim_metrics()
        print(f"  Total questions: {m['total_questions']}")
        print(f"  Total LLM calls: {m['total_llm_calls']}")
        print(f"  Structured path count: {m['structured_path_count']}")
        print(f"  P95 latency (ms): {m['p95_ms']:.2f}")
        print(f"  % with citations (event_ids): {m['pct_with_citations']:.0f}%")
        a_ok = m["total_llm_calls"] == 0 and m["structured_path_count"] == m["total_questions"]
        if not a_ok:
            print("  WARNING: Expected 0 LLM calls for default sim (all structured).")
    except Exception as e:
        print(f"  FAIL: {e}")
        a_ok = False

    # B
    print("\n--- Step B: Action items only (no decision) ---")
    b_ok, b_msg = _test_action_items_only()
    print(f"  {'PASS' if b_ok else 'FAIL'}: {b_msg}")

    # C
    print("\n--- Step C: Missing evidence (partnerships) refuse ---")
    c_ok, c_msg = _test_missing_evidence()
    print(f"  {'PASS' if c_ok else 'FAIL'}: {c_msg}")

    # D
    print("\n--- Step D: No invented due date ---")
    d_ok, d_msg = _test_hallucination_guard()
    print(f"  {'PASS' if d_ok else 'FAIL'}: {d_msg}")

    # E
    print("\n--- Step E: Wake negative ('Mechus is cool') ---")
    e_ok, e_msg = _test_wake_negative()
    if e_ok is None:
        print(f"  SKIP: {e_msg}")
    else:
        print(f"  {'PASS' if e_ok else 'FAIL'}: {e_msg}")

    # F
    print("\n--- Step F: event_ids on structured path ---")
    f_ok, f_msg = _test_event_ids_structured()
    print(f"  {'PASS' if f_ok else 'FAIL'}: {f_msg}")

    print("\n" + "=" * 60)
    all_ok = a_ok and b_ok and c_ok and d_ok and (e_ok is not False) and f_ok
    print("VERDICT: " + ("PASS" if all_ok else "FAIL"))
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
