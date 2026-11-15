"""
Full pipeline intent classifier.

Stage order (highest priority first):
  1. presence_check  — "can you hear me", "are you there", empty wake-only
  2. stop_dismiss    — "not now", "stop"
  3. greeting        — "hello", "hi", wake-only with no command
  4. Meeting intents — recap, decision, action_items, risk, …
  5. general_question — anything else with a question word / length
  6. unknown         — too short, filler only, truly unclassifiable

This module intentionally wraps koov/intent.py so the full pipeline has ONE
canonical entry point (classify_pipeline_intent) and ONE constant namespace
(re-exported below).
"""
from __future__ import annotations

import re

from koov.intent import (
    INTENT_ACTION_ITEMS,
    INTENT_CLARIFICATION,
    INTENT_COMPETITOR,
    INTENT_CONFIRM,
    INTENT_DECISION,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HELP,
    INTENT_OPEN_QUESTIONS,
    INTENT_PERSON_ACTION,
    INTENT_RATIONALE,
    INTENT_RECAP,
    INTENT_RISK,
    INTENT_STOP,
    INTENT_TIMELINE,
    INTENT_WHO,
    classify_intent as _base_classify,
)

# New intent not in koov/intent.py
INTENT_PRESENCE_CHECK = "presence_check"
INTENT_UNKNOWN = "unknown"

# Re-export all existing intent constants so callers only need this module.
__all__ = [
    "INTENT_PRESENCE_CHECK",
    "INTENT_UNKNOWN",
    "INTENT_ACTION_ITEMS",
    "INTENT_CLARIFICATION",
    "INTENT_COMPETITOR",
    "INTENT_CONFIRM",
    "INTENT_DECISION",
    "INTENT_GENERAL",
    "INTENT_GREETING",
    "INTENT_HELP",
    "INTENT_OPEN_QUESTIONS",
    "INTENT_PERSON_ACTION",
    "INTENT_RATIONALE",
    "INTENT_RECAP",
    "INTENT_RISK",
    "INTENT_STOP",
    "INTENT_TIMELINE",
    "INTENT_WHO",
    "classify_pipeline_intent",
]

# ---------------------------------------------------------------------------
# Presence-check patterns (run BEFORE all other intents)
# ---------------------------------------------------------------------------
_PRESENCE_PHRASES = [
    "can you hear me", "can you hear us", "do you hear me",
    "can hear me", "can hear us", "can hear",
    "you hear me", "you hear us", "you hear",
    "hear me", "hear us",
    "are you there", "you there",
    "are you listening", "is anyone there",
    "are you awake", "are you on", "are you working",
    "are you alive", "you alive",
    "is this working", "can you respond",
    "are you hearing",
    # Common STT / human variants (still instant canned replies, no LLM)
    "mic check", "audio check", "sound check",
    "am i audible", "can you hear my voice", "hear my voice",
    "am i coming through", "coming through okay", "coming through ok",
    "do i sound okay", "do i sound ok",
]

# Short words that alone count as a presence check (STT often only captures tail)
_PRESENCE_WORDS = frozenset(["hear", "listening", "there"])


_PRESENCE_NEGATORS = ("stop", "not", "don't", "dont", "no")


def _is_presence_check(command_text: str) -> bool:
    """
    Return True if command_text (already wake-stripped) looks like
    a presence check / can-you-hear-me request.

    Handles:
    - "can you hear me"    (exact)
    - "can hear"           (STT dropped "you")
    - "hear me"            (STT dropped "can you")
    - "me"                 (only the tail was captured)
    - very short (≤ 4 words) phrase containing "hear"
    """
    t = (command_text or "").lower().strip()
    words = t.split()
    if len(words) <= 1:
        # Single-word inputs are only presence-checks for the "me" / "you" cases
        # handled below; everything else (e.g. "wait", "maybe") must fall through
        # so stop-intent / hesitation logic can apply.
        return t in ("me", "you")
    # "stop listening", "not listening", etc. → never a presence check
    if words and words[0] in _PRESENCE_NEGATORS:
        return False
    if any(p in t for p in _PRESENCE_PHRASES):
        return True
    # Participant-to-participant acknowledgments (not directed at Koov).
    if any(
        p in t
        for p in (
            "i hear you",
            "we hear you",
            "yeah i hear",
            "yes i hear",
            "hear you on that",
            "hear you loud and clear",
        )
    ):
        return False
    # Very short phrase: ≤ 4 words, contains "hear" or sole presence word
    if len(words) <= 4:
        if any(w in _PRESENCE_WORDS for w in words):
            return True
        if words == ["me"] or words == ["you"]:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_pipeline_intent(command_text: str) -> str:
    """
    Classify intent for the full voice pipeline.

    `command_text` must already have the wake phrase stripped.
    Returns one of the INTENT_* constants.

    Examples:
        classify_pipeline_intent("can you hear me")  → presence_check
        classify_pipeline_intent("summarize")         → recap_query
        classify_pipeline_intent("what did we decide") → decision_query
        classify_pipeline_intent("what is two plus two") → general
        classify_pipeline_intent("")                  → presence_check (wake-only; router stays silent)
    """
    # Wake-only: no words after the wake phrase — bucket with presence so command_router
    # stays silent (unknown would speak a fallback and feel wrong).
    if not (command_text or "").strip():
        return INTENT_PRESENCE_CHECK

    # Stage 1: presence / hear-me (before any other check)
    if _is_presence_check(command_text):
        return INTENT_PRESENCE_CHECK

    from koov.dialog_memory import is_interrupt_request

    if is_interrupt_request(command_text):
        return INTENT_STOP

    t = (command_text or "").strip().lower()

    # Bare interrupt directives ("stop", "shut up", "be quiet", "enough") —
    # classify as STOP so "Koov, stop" cleanly cuts Koov off. Matched only when
    # the whole command is the directive, so "should we stop the rollout" (a
    # real question) is not swallowed.
    _bare = re.sub(r"[^a-z0-9 ]", " ", t)
    _bare = re.sub(r"\s+", " ", _bare).strip()
    if _bare in (
        "stop", "stop it", "stop now", "stop talking", "stop speaking",
        "stop please", "shut up", "be quiet", "quiet", "hush", "enough",
        "that s enough", "thats enough", "pause", "halt", "cancel",
    ):
        return INTENT_STOP

    # Stage 2: stop / dismiss
    _stop_phrases = (
        "not now", "you're good", "you are good", "that's all", "that is all",
        "never mind", "nevermind", "forget it", "stop listening",
        "we're done", "we are done", "hold on", "hang on", "wait",
        "one sec", "one second", "give me a sec", "give me a second",
        "give me a moment",
    )
    _stop_is_real_question = any(
        m in t
        for m in (
            " does ",
            " do ",
            " did ",
            " is ",
            " are ",
            " can ",
            " would ",
            " what ",
            " how ",
            " who ",
            " why ",
            " have ",
            " opinion",
            "?",
        )
    )
    if (
        any(p in t for p in _stop_phrases)
        and not (
            _stop_is_real_question
            and any(p in t for p in ("wait", "hold on", "hang on"))
        )
    ) or re.match(r"^(okay|ok|got it|thanks|thank you)(\s+for\s+now)?\.?$", t):
        return INTENT_STOP

    # Stage 3 onward: delegate to existing classifier (greeting, meeting, general)
    base = _base_classify(command_text)

    # If base says "greeting" but it's not a pure hear-check, keep greeting
    if base == INTENT_GENERAL and not t:
        return INTENT_UNKNOWN

    return base
