"""
Unit tests for mechus/intent_classifier.py

Run from repo root:
    python scripts/test_intent_classifier.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mechus.intent_classifier import (
    classify_pipeline_intent,
    INTENT_PRESENCE_CHECK,
    INTENT_RECAP,
    INTENT_DECISION,
    INTENT_ACTION_ITEMS,
    INTENT_RISK,
    INTENT_GENERAL,
    INTENT_STOP,
    INTENT_GREETING,
    INTENT_HELP,
)
from mechus.intent import classify_intent as classify_intent_base, INTENT_GREETING as IG, INTENT_GENERAL as IGN

def run(desc, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {desc!r}")
    if got != expected:
        print(f"         got:      {got!r}")
        print(f"         expected: {expected!r}")
    return status == "PASS"

passed = failed = 0

# ── Presence checks ────────────────────────────────────────────────────────
print("\npresence_check intents (must fire FIRST before any other check)")
pc_tests = [
    ("can you hear me",     INTENT_PRESENCE_CHECK),
    ("can you hear us",     INTENT_PRESENCE_CHECK),
    ("do you hear me",      INTENT_PRESENCE_CHECK),
    ("can hear me",         INTENT_PRESENCE_CHECK),   # STT dropped "you"
    ("can hear",            INTENT_PRESENCE_CHECK),   # STT dropped "you me"
    ("hear me",             INTENT_PRESENCE_CHECK),   # STT dropped "can you"
    ("you hear",            INTENT_PRESENCE_CHECK),
    ("are you there",       INTENT_PRESENCE_CHECK),
    ("are you listening",   INTENT_PRESENCE_CHECK),
    ("is this working",     INTENT_PRESENCE_CHECK),
    ("",                    INTENT_PRESENCE_CHECK),   # wake-only = empty command
]
for cmd, expected in pc_tests:
    ok = run(cmd, classify_pipeline_intent(cmd), expected)
    if ok: passed += 1
    else: failed += 1

# ── Stop ──────────────────────────────────────────────────────────────────
print("\nstop intents")
stop_tests = [
    ("stop listening",          INTENT_STOP),
    ("not now",                 INTENT_STOP),
    ("that's all for now",      INTENT_STOP),
]
for cmd, expected in stop_tests:
    ok = run(cmd, classify_pipeline_intent(cmd), expected)
    if ok: passed += 1
    else: failed += 1

# ── Meeting intents ────────────────────────────────────────────────────────
print("\nmeeting intents")
meeting_tests = [
    ("summarize",               INTENT_RECAP),
    ("recap",                   INTENT_RECAP),
    ("what happened",           INTENT_RECAP),
    ("meeting so far",          INTENT_RECAP),
    ("what did we decide",      INTENT_DECISION),
    ("any decisions",           INTENT_DECISION),
    ("action items",            INTENT_ACTION_ITEMS),
    ("next steps",              INTENT_ACTION_ITEMS),
    ("tasks",                   INTENT_ACTION_ITEMS),
    ("risks",                   INTENT_RISK),
    ("any concerns",            INTENT_RISK),
]
for cmd, expected in meeting_tests:
    ok = run(cmd, classify_pipeline_intent(cmd), expected)
    if ok: passed += 1
    else: failed += 1

# ── Compound hello + question (must NOT be greeting) ───────────────────────
print("\ncompound hello + question (→ general / meeting, not greeting)")
compound_tests = [
    ("hello what is latency",           INTENT_GENERAL),
    ("hi what's the difference between x and y", INTENT_GENERAL),
    ("hey how do we deploy this",     INTENT_GENERAL),
    ("hello summarize the meeting",   INTENT_RECAP),
]
for cmd, expected in compound_tests:
    ok = run(cmd, classify_pipeline_intent(cmd), expected)
    if ok: passed += 1
    else: failed += 1

print("\nbase classify_intent: compound vs pure greeting")
base_greeting_tests = [
    ("hello what is latency", IGN),
    ("hi", IG),
    ("hello", IG),
]
for cmd, expected in base_greeting_tests:
    ok = run(cmd, classify_intent_base(cmd), expected)
    if ok: passed += 1
    else: failed += 1

# ── General questions ─────────────────────────────────────────────────────
print("\ngeneral questions (→ general)")
general_tests = [
    ("what is two plus two",        INTENT_GENERAL),
    ("what's the difference between alpha and beta", INTENT_GENERAL),
    ("who are you",                 INTENT_HELP),
    ("what can you do",             INTENT_HELP),
]
for cmd, expected in general_tests:
    ok = run(cmd, classify_pipeline_intent(cmd), expected)
    if ok: passed += 1
    else: failed += 1

# ── Full pipeline (wake-stripped commands from real transcripts) ───────────
print("\nfull pipeline commands (as they arrive from wake_detector.strip_wake_phrase)")
from mechus.wake_detector import normalize_for_wake, strip_wake_phrase

WAKE_PHRASES = ("hey computer", "computer", "mechus", "koo", "coop")

pipeline_tests = [
    # (raw Vosk transcript,           expected intent)
    ("computer can you hear me",      INTENT_PRESENCE_CHECK),
    ("mechus summarize the meeting",    INTENT_RECAP),
    ("mechus what did we decide",       INTENT_DECISION),
    ("mechus action items",             INTENT_ACTION_ITEMS),
    ("computer what is two plus two", INTENT_GENERAL),
    ("kuv recap",                     INTENT_RECAP),
    ("coop action items",             INTENT_ACTION_ITEMS),
    ("mechus are you there",            INTENT_PRESENCE_CHECK),
    ("computer hear me",              INTENT_PRESENCE_CHECK),
]
for raw, expected in pipeline_tests:
    norm = normalize_for_wake(raw)
    cmd = strip_wake_phrase(norm, WAKE_PHRASES)
    intent = classify_pipeline_intent(cmd)
    ok = run(f"{raw!r} → cmd={cmd!r}", intent, expected)
    if ok: passed += 1
    else: failed += 1

# ── Summary ────────────────────────────────────────────────────────────────
total = passed + failed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed")
if failed:
    sys.exit(1)
