"""
Command router: maps a classified intent to a response.

Responsibilities:
  - Return instant fixed replies for presence_check, greeting, stop, confirm
  - Forward meeting/content intents to the LLM responder via the provided callback
  - Forward general questions to the LLM responder
  - Return a fallback for unknown/unclassifiable input

The router does NOT call retrieval, STT, or audio APIs directly.
All heavy lifting is delegated to the `responder_fn` callback supplied by the agent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from mechus.brand import ASSISTANT_NAME

from mechus.intent_classifier import (
    INTENT_PRESENCE_CHECK,
    INTENT_UNKNOWN,
    INTENT_GREETING,
    INTENT_STOP,
    INTENT_CONFIRM,
    INTENT_CLARIFICATION,
    INTENT_HELP,
    INTENT_RECAP,
    INTENT_DECISION,
    INTENT_ACTION_ITEMS,
    INTENT_RISK,
    INTENT_TIMELINE,
    INTENT_OPEN_QUESTIONS,
    INTENT_WHO,
    INTENT_PERSON_ACTION,
    INTENT_RATIONALE,
    INTENT_COMPETITOR,
    INTENT_GENERAL,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instant fixed replies (no LLM, no retrieval)
# ---------------------------------------------------------------------------

_INSTANT_REPLIES: dict[str, str] = {
    INTENT_STOP: "",  # agent may speak stop_ack when enabled
    INTENT_CONFIRM: "",  # deliberate silence
}

_STOP_ACK_REPLIES = [
    "Oh okay — let me know if you need anything.",
    "Got it, I'll be here.",
    f"Sure, just say {ASSISTANT_NAME} when you're ready.",
    "Okay, I'm here when you need me.",
    "No problem, I'm listening.",
]

_PRESENCE_CHECK_REPLIES = [
    "Yeah, I can hear you.",
    "Loud and clear.",
    "Yep, I'm here.",
    "I can hear you fine.",
    "I'm here.",
    "All good, I hear you.",
    "Got you.",
    "Crystal clear.",
    "Yep, loud and clear.",
    "Still here.",
    "Hearing you fine.",
]

# Wake phrase with no follow-up text (common “check if you’re listening”) — must not be silent.
_WAKE_ONLY_REPLIES = [
    "Yeah, I'm here.",
    "Go ahead.",
    "I'm listening.",
    "What's up?",
]

from mechus.language import pick_instant_reply


def pick_stop_ack_reply(*, user_language: str | None = None) -> str:
    """Short acknowledgment after the user interrupts Mechus (stop / hold on / wait)."""
    return pick_instant_reply("stop_ack", _STOP_ACK_REPLIES, user_language=user_language)

_GREETING_REPLIES = [
    "Hey, I'm here — ask me for a recap or what we decided anytime.",
    "Yeah I've been following along — just ask.",
    "I'm in — what do you need?",
    "Here, been listening the whole time.",
    "Hey — I'm listening.",
    "Hi, I'm here.",
    "Hey, what's up?",
    "Morning — I'm here.",
    "Hey there.",
    "Hi — what do you need?",
]

_HELP_REPLIES = [
    "I've been following along. Ask me for a recap, action items, or what we decided.",
    "I've got the whole meeting so far — decisions, action items, risks, just ask.",
    "Been here the whole time. What do you need?",
    "Ask for a recap, decisions, or risks.",
    "Try a recap or what we decided.",
    "I can summarize or pull action items.",
    "Just ask for the recap or open questions.",
]

_MEETING_INTENTS = frozenset([
    INTENT_RECAP,
    INTENT_DECISION,
    INTENT_ACTION_ITEMS,
    INTENT_RISK,
    INTENT_TIMELINE,
    INTENT_OPEN_QUESTIONS,
    INTENT_WHO,
    INTENT_PERSON_ACTION,
    INTENT_RATIONALE,
    INTENT_COMPETITOR,
])


@dataclass
class RouteResult:
    response: str
    intent: str
    is_instant: bool  # True = returned without calling the LLM responder


def route(
    intent: str,
    command_text: str,
    *,
    responder_fn: Callable[[str], str] | None = None,
    meeting_transcript: bool = False,
    user_language: str | None = None,
) -> RouteResult:
    """
    Route an intent to the appropriate response handler.

    Args:
        intent:        One of the INTENT_* constants.
        command_text:  The wake-stripped command text (already normalized).
        responder_fn:  Callback that takes the command text and returns a response
                       string. Called for all non-instant intents. If None, a
                       fallback string is returned.

    Returns:
        RouteResult with response text, intent, and is_instant flag.

    Logging format (structured):
        [ROUTE] intent=presence_check → instant reply
        [ROUTE] intent=recap_query   → responder
    """
    # --- Stage 1: instant / fixed replies ---
    if intent == INTENT_PRESENCE_CHECK:
        # Bare wake ("mechus" / "hey mechus" with nothing after) stays silent — same rule
        # as meeting transcript mode. Hear-me checks need a fast clean ack.
        if not command_text or not command_text.strip():
            log.info("[ROUTE] intent=presence_check, wake-only → deliberate silence")
            return RouteResult(response="", intent=intent, is_instant=True)
        reply = pick_instant_reply("presence", _PRESENCE_CHECK_REPLIES, user_language=user_language)
        log.info("[ROUTE] intent=presence_check → instant reply")
        return RouteResult(response=reply, intent=intent, is_instant=True)

    if intent in _INSTANT_REPLIES:
        reply = _INSTANT_REPLIES[intent]
        if reply:
            log.info("[ROUTE] intent=%s → instant reply: %s", intent, reply)
        else:
            log.info("[ROUTE] intent=%s → deliberate silence", intent)
        return RouteResult(response=reply, intent=intent, is_instant=True)

    if intent == INTENT_UNKNOWN:
        reply = pick_instant_reply(
            "unknown",
            (
                "I'm not sure what you mean. Try asking for a recap, decisions, or action items.",
            ),
            user_language=user_language,
        )
        log.info("[ROUTE] intent=unknown → fallback reply")
        return RouteResult(response=reply, intent=intent, is_instant=True)

    # --- Stage 2: greeting / help (instant but with content) ---
    if intent == INTENT_GREETING:
        reply = pick_instant_reply("greeting", _GREETING_REPLIES, user_language=user_language)
        log.info("[ROUTE] intent=greeting → instant reply")
        return RouteResult(response=reply, intent=intent, is_instant=True)

    if intent == INTENT_HELP:
        reply = pick_instant_reply("help", _HELP_REPLIES, user_language=user_language)
        log.info("[ROUTE] intent=help → instant reply")
        return RouteResult(response=reply, intent=intent, is_instant=True)

    if intent == INTENT_CLARIFICATION:
        reply = pick_instant_reply(
            "unknown",
            (
                "Could you rephrase that? I want to make sure I answer correctly.",
                "Sorry, were you talking to me?",
                "Did you need something specific from me?",
            ),
            user_language=user_language,
        )
        log.info("[ROUTE] intent=clarification → instant reply")
        return RouteResult(response=reply, intent=intent, is_instant=True)

    # --- Stage 3: meeting / general intents → responder ---
    if responder_fn is None:
        fallback = "I can help with that, but I'm not connected to a language model right now."
        log.info("[ROUTE] intent=%s → no responder_fn, using fallback", intent)
        return RouteResult(response=fallback, intent=intent, is_instant=True)

    log.info("[ROUTE] intent=%s → responder", intent)
    response = responder_fn(command_text) or ""
    return RouteResult(response=response, intent=intent, is_instant=False)
