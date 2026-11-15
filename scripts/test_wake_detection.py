"""
Unit tests for koov/wake_detector.py

Run from repo root:
    python scripts/test_wake_detection.py
"""
import re
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from koov.wake_detector import (
    normalize_for_wake,
    detect_wake,
    strip_wake_phrase,
    qualifies_as_wake_command,
    remainder_qualifies_as_wake_command,
)

WAKE_PHRASES = ("hey computer", "computer", "koov", "koo", "coop")

def run(desc, expr, expected):
    result = expr
    status = "PASS" if result == expected else "FAIL"
    print(f"  [{status}] {desc}")
    if result != expected:
        print(f"         got:      {result!r}")
        print(f"         expected: {expected!r}")
    return status == "PASS"

passed = failed = 0

# ── normalize_for_wake ─────────────────────────────────────────────────────
print("\nnormalize_for_wake")
tests = [
    ("punctuation removed",
     normalize_for_wake("Computer, can you hear me?"),
     "computer can you hear me"),
    ("smart quotes",
     normalize_for_wake("Koov — summarize this."),
     "koov summarize this"),
    ("already clean",
     normalize_for_wake("koov recap"),
     "koov recap"),
    ("empty",
     normalize_for_wake(""),
     ""),
]
for args in tests:
    ok = run(*args)
    if ok: passed += 1
    else: failed += 1

# ── detect_wake ────────────────────────────────────────────────────────────
print("\ndetect_wake (exact)")
exact_tests = [
    ("computer can you hear me", True),
    ("koov summarize meeting", True),
    ("hey computer recap", True),
    ("no wake phrase here", False),
    ("just some speech about nothing", False),
]
for phrase, expected in exact_tests:
    n = normalize_for_wake(phrase)
    ok = run(phrase, detect_wake(n, WAKE_PHRASES), expected)
    if ok: passed += 1
    else: failed += 1

print("\ndetect_wake (fuzzy — STT mishearings)")
fuzzy_tests = [
    # (raw transcript from STT,  expected)
    ("kuv summarize meeting",     True),   # kuv ≈ koov, dist=1
    ("koo can you hear me",       True),   # exact koo
    ("coop action items",         True),   # exact coop
    ("ku summarize",              False),  # ku is only 2 chars; too short for fuzzy match
    ("cave recap",                False),  # cave is 3 edits from koov
    ("computer can you hear me",  True),
    ("coputer can you hear me",   True),   # typo, dist=1
    # "too" was 1-edit from wake alias "koo" → false wakes on "join too" (denylisted)
    ("how about you join too why don t you join too you should join too", False),
]
for raw, expected in fuzzy_tests:
    n = normalize_for_wake(raw)
    ok = run(f"detect_wake({raw!r})", detect_wake(n, WAKE_PHRASES), expected)
    if ok: passed += 1
    else: failed += 1

# ── strip_wake_phrase ──────────────────────────────────────────────────────
print("\nstrip_wake_phrase")
strip_tests = [
    ("computer can you hear me",    "can you hear me"),
    ("koov summarize the meeting",  "summarize the meeting"),
    ("kuv recap",                   "recap"),
    ("hey computer action items",   "action items"),
    ("koo there",                   "there"),
    ("no wake phrase",              "no wake phrase"),   # unchanged
    ("computer",                    ""),                  # wake-only
]
for raw, expected in strip_tests:
    n = normalize_for_wake(raw)
    ok = run(f"strip({raw!r})", strip_wake_phrase(n, WAKE_PHRASES), expected)
    if ok: passed += 1
    else: failed += 1

# ── qualifies_as_wake_command (transcript wake gate) ───────────────────────
print("\nqualifies_as_wake_command (wake + real command; transcript path)")
qual_tests = [
    ("computer can you hear me", True),
    ("Computer, can you hear me?", True),
    ("today i went to the mall computer can you hear me", True),
    ("koov what did we decide", True),
    ("computer tell me about kent state university", True),
    ("koov is cool", False),
    ("computer", False),  # wake-only — must have wake + command token
    ("koov", False),
    ("random speech without wake", False),
    # Substring false positives (e.g. "can" inside "candle") — must not qualify
    ("computer candle", False),
    ("computer scandal", False),
    # Wake + at least one command token (want, check)
    ("computer i want you to check on that", True),
]
for raw, expected in qual_tests:
    n = normalize_for_wake(raw)
    ok = run(
        f"qualifies({raw!r})",
        qualifies_as_wake_command(n, WAKE_PHRASES),
        expected,
    )
    if ok:
        passed += 1
    else:
        failed += 1

# ── strict start-of-remainder (no command token deep in a paragraph) ─────────
print("\nqualifies_as_wake_command (strict start-of-remainder)")
strict_qual_tests = [
    # Command must start right after wake (possibly after um/uh), not elsewhere in the line
    (
        "koov i had an idea about a few things and later we talked about how cliff might join",
        False,
    ),
    ("koov um what did we decide", True),
    ("koov uh please summarize the meeting", True),
    ("koov about the budget", True),
    ("koov about a launch", True),
    ("koov i need a recap", True),
    ("koov tell me about the launch", True),
    # "how" only late in the sentence does not qualify
    (
        "koov we discussed parking and then someone said how about next tuesday",
        False,
    ),
    # Narrative "i was …" without (i, need|want|…) prefix
    ("koov i was wondering if you heard anything", False),
    # Remainder "there" alone is not a request opener
    ("koov there", False),
]
for raw, expected in strict_qual_tests:
    n = normalize_for_wake(raw)
    ok = run(
        f"qualifies({raw!r})",
        qualifies_as_wake_command(n, WAKE_PHRASES),
        expected,
    )
    if ok:
        passed += 1
    else:
        failed += 1

# ── remainder_qualifies_as_wake_command (direct) ───────────────────────────
print("\nremainder_qualifies_as_wake_command")
remainder_tests = [
    ("can you hear me", True),
    ("there", False),
    ("about the timeline", True),
    ("about nothing", False),
    ("i need help", True),
    ("i had an idea", False),
]
for raw, expected in remainder_tests:
    ok = run(f"remainder({raw!r})", remainder_qualifies_as_wake_command(raw), expected)
    if ok:
        passed += 1
    else:
        failed += 1


def _vosk_style_remainder_qualifies(text: str, wake_phrases: tuple[str, ...]) -> bool:
    """Mirror koov.wake.VoskWakeDetector._has_command_or_question (uses same remainder gate)."""
    if not text:
        return False
    t = text.lower().strip()
    for phrase in wake_phrases:
        if t.startswith(phrase + " ") or t == phrase:
            t = t[len(phrase) :].strip()
            break
    for phrase in wake_phrases:
        t = re.sub(rf"\b{re.escape(phrase)}\b", "", t, flags=re.IGNORECASE).strip()
    return remainder_qualifies_as_wake_command(t)


print("\nVosk-style strip + remainder_qualifies (parity with VoskWakeDetector)")
vosk_tests = [
    ("computer can you hear me", True),
    ("koov i had an idea about stuff", False),
    ("koov what did we decide", True),
    ("koov tell me about the launch", True),
]
for raw, expected in vosk_tests:
    ok = run(
        f"vosk_style({raw!r})",
        _vosk_style_remainder_qualifies(raw, WAKE_PHRASES),
        expected,
    )
    if ok:
        passed += 1
    else:
        failed += 1

# ── Summary ────────────────────────────────────────────────────────────────
total = passed + failed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed")
if failed:
    sys.exit(1)
