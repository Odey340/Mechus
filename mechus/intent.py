"""
Intent classifier for general voice agent and meeting Q&A.

Maps user input to a query type so retrieval and response can be scoped
deterministically. General-voice intents (greeting, help, stop) are handled
first so the agent responds correctly in any context.
"""

from __future__ import annotations

import re

from mechus.text import extract_requested_owner

# If True, the utterance is asking something substantive — do not treat as a pure greeting
# even when it contains "hello", "hi", "hey", etc. (e.g. "Hello? Computer, what is latency?")
# Note: do not treat bare "?" as substantive — that breaks presence checks ("are you there?")
# and greeting short-circuits; real questions still match via wh-words / "what is" / etc.
_SUBSTANTIVE_QUESTION_RE = re.compile(
    r"("
    r"\b(what\'s|whats|what is|what are|what was|what did|what do|what would|what can|what will)\b|"
    r"\b(how|why|when|where|which)\s+\w|"
    r"\bwho\s+(is|are|was|were|did|said|mentioned|raised|owns?|will)\b|"
    r"\b(explain|define|describe|summarize|recap)\b|"
    r"\btell me\b|"
    r"\bcan you\s+(explain|tell|summarize|recap|describe|define)\b"
    r")",
    re.IGNORECASE,
)


def _has_substantive_question_or_command(t: str) -> bool:
    """True when the user is clearly asking or commanding beyond a standalone hello/hi."""
    if not t or not t.strip():
        return False
    t = t.strip().lower()
    if _SUBSTANTIVE_QUESTION_RE.search(t):
        return True
    return False


# General voice agent intents (no meeting context required)
INTENT_GREETING = "greeting"
INTENT_HELP = "help_capabilities"
INTENT_STOP = "stop_dismiss"
INTENT_CONFIRM = "confirm_ack"
INTENT_CLARIFICATION = "clarification"

# Meeting / content intents (rules-first; match spec)
INTENT_ACTION_ITEMS = "action_item_query"
INTENT_DECISION = "decision_query"
INTENT_RISK = "risk_query"
INTENT_TIMELINE = "timeline_query"
INTENT_OPEN_QUESTIONS = "open_question_query"
INTENT_COMPETITOR = "competitor_query"
INTENT_RECAP = "recap_query"
INTENT_WHO = "speaker_attribution_query"
INTENT_PERSON_ACTION = "person_action_query"
INTENT_RATIONALE = "rationale_query"
INTENT_GENERAL = "general"


def classify_intent(request_text: str) -> str:
    """
    Classify user intent. Use before retrieval and response.
    General-voice intents first, then meeting-specific.
    """
    if not request_text or not request_text.strip():
        return INTENT_GENERAL
    t = request_text.strip().lower()

    # Stop / dismiss (high priority so we don't answer with content)
    if any(p in t for p in [
        "not now", "you're good", "you are good", "that's all", "that is all",
        "never mind", "nevermind", "forget it", "stop", "we're done", "we are done",
    ]) or re.match(r"^(okay|ok|got it|thanks|thank you)(\s+for\s+now)?\.?$", t):
        return INTENT_STOP

    # Greeting / presence check — not when a real question follows ("hello, what is …")
    if not _has_substantive_question_or_command(t):
        if any(p in t for p in [
            "hello", "hi ", "hi,", "hey ", "hey,", "yo ", "good morning", "good afternoon",
            "good evening", "are you there", "you there", "can you hear me", "can you hear us",
            "are you listening", "are you awake", "anyone there", "hear me clearly",
        ]) or t in ("hi", "hey", "yo", "hello"):
            return INTENT_GREETING

    # Help / capabilities / identity — not when a meeting-style question is in the same line
    # (barge-in often includes "what are you …" plus "what would happen if I …").
    _help_markers = (
        "what can you do", "what do you do", "how can you help", "who are you",
        "introduce yourself", "what are you", "your capabilities", "help me",
    )
    _question_in_same_utterance = any(
        m in t
        for m in (
            "what would", "what happens", "what will", "would i", "would we",
            "if i ", "if we ", "how would", "can i ", "should i ",
        )
    )
    if any(p in t for p in _help_markers):
        if _question_in_same_utterance:
            pass
        elif not _has_substantive_question_or_command(t) or re.match(
            r"^(what can you do|what do you do|how can you help|who are you|"
            r"introduce yourself|what are you|your capabilities|help me)\.?$",
            t.strip(),
        ):
            return INTENT_HELP

    # Clarification (user didn't understand or wants repeat)
    if any(p in t for p in [
        "what did you say", "say that again", "repeat", "can you repeat",
        "i didn't catch", "what was that", "huh", "what?",
    ]) or re.match(r"^what\s*\?$", t):
        return INTENT_CLARIFICATION

    # Confirm / acknowledgment (short "got it" without dismiss)
    if re.match(r"^(got it|ok|okay|understood|sounds good|perfect|great)(\s+[.!?])?$", t):
        return INTENT_CONFIRM

    # --- Meeting / content intents ---

    # Time-based recap — must be before general recap so it routes correctly
    # Digits or spoken numbers (STT: "last five minutes")
    _spoken_n = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)"
    if re.search(r"last\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)", t) or \
       re.search(rf"last\s+{_spoken_n}\s*(?:minutes?|mins?|hours?|hrs?)", t) or \
       "last half hour" in t or "past half hour" in t or \
       re.search(r"past\s+\d+\s*(?:minutes?|mins?)", t) or \
       re.search(rf"past\s+{_spoken_n}\s*(?:minutes?|mins?)", t):
        return INTENT_RECAP

    # Recap / summary
    if any(p in t for p in [
        "recap", "summarize", "summary", "overall", "whole meeting",
        "entire meeting", "full recap", "quick recap",
        "meeting summar", "summarize the meeting", "summarize this meeting",
        "meeting so far", "what happened", "what's been", "what has been",
        "bring me up to speed", "catch me up", "what did we cover",
        "what have we covered", "overview",
    ]):
        return INTENT_RECAP

    # Action items / next steps / who owns — broad matching
    # Person-scoped action items (must be before generic action-item patterns).
    # Use extract_requested_owner so "what are the action items" is not misread as owner "the".
    if extract_requested_owner(request_text or ""):
        return INTENT_PERSON_ACTION

    if any(p in t for p in [
        "action item", "action items", "next steps", "next step",
        "who owns", "who will", "who's responsible", "who is responsible",
        "todo", "to-do", "tasks", "follow up", "follow-up",
        "all the action", "list the action", "what are the tasks",
        "what needs to be done", "who is doing", "who's doing",
        "who's handling", "who is handling", "who's taking care",
        "assigned to", "who was assigned",
    ]):
        return INTENT_ACTION_ITEMS

    # Person-specific action — capitalized person name (not sentence-start question words) + action verb
    _sentence_cap = re.search(r"\b([A-Z][a-z]+)\b", request_text)
    _not_question_word = (
        _sentence_cap is not None
        and _sentence_cap.group(1)
        not in {
            "What", "Who", "Where", "When", "Why", "How", "Which", "Can", "Could",
            "Would", "Should", "Did", "Does", "Do", "Is", "Are", "Was", "Were",
            "Will", "Has", "Have", "Had", "Tell", "Give", "Show", "This", "That",
            "There", "Here", "Please", "Okay", "Ok", "We", "You", "They",
        }
    )
    if _not_question_word and any(p in t for p in [
        "working on", "responsible for", "handling", "owns", "assigned",
        "doing", "taking care", "in charge", "what is", "what's",
    ]):
        return INTENT_PERSON_ACTION

    # Who raised / who said (speaker attribution)
    if any(p in t for p in [
        "who raised", "who said", "who mentioned", "who brought up",
        "who raised the concern", "who raised the risk", "who suggested",
        "who proposed", "who flagged", "who's in the meeting", 
        "who is in the meeting", "who's on the call", "who is on the call", 
        "who's here", "who joined", "participants", "attendees", "who do we have", "who's attending",
    ]):
        return INTENT_WHO

    # Open questions / TBD
    if any(p in t for p in [
        "open question", "open questions", "still open", "questions still",
        "tbd", "to be determined", "unresolved", "still need to",
        "what questions", "what's still open", "what is still open",
        "not resolved", "not decided yet", "still figuring",
    ]):
        return INTENT_OPEN_QUESTIONS

    # Risk / concern
    if any(p in t for p in [
        "risk", "risks", "concern", "concerns", "worried", "worries",
        "blocker", "blockers", "issue", "issues", "problem", "problems",
        "what could go wrong", "what are the risks",
    ]):
        return INTENT_RISK

    # Timeline / dates
    if any(p in t for p in [
        "timeline", "when", "launch date", "deadline", "due date",
        "implementation", "go live", "go-live", "weeks to", "by when",
        "what date", "what's the date", "when are we", "when do we",
        "when is", "by when", "what's the timeline", "schedule",
        "milestones", "key dates", "critical dates",
    ]):
        return INTENT_TIMELINE

    # Decision
    if any(p in t for p in [
        "decide", "decision", "decided", "what did we decide",
        "what did we just decide", "final decision", "what's the decision",
        "what was decided", "what have we decided", "locked in",
        "agreed on", "what did we agree", "confirmed",
        "are we still doing", "are we going with",
    ]):
        return INTENT_DECISION

    # Why / rationale
    if "why" in t and any(p in t for p in [
        "decide", "decision", "choose", "go with", "pick", "selected",
        "went with", "doing", "changed",
    ]):
        return INTENT_RATIONALE

    # Competitor / comparison
    if any(p in t for p in [
        "competitor", "competitors", "compare", "comparison", "vs ", " versus ",
    ]):
        return INTENT_COMPETITOR

    # Meeting recap — "tell me about the solar system" is general knowledge, not recap.
    if "tell me about" in t and any(
        c in t
        for c in (
            "what we",
            "we discussed",
            "we talked",
            "we said",
            "the meeting",
            "this meeting",
            "today's meeting",
            "our call",
            "this call",
            "from the meeting",
            "from today",
        )
    ):
        return INTENT_RECAP

    # Status / general meeting question — catch common patterns before falling to GENERAL
    if any(p in t for p in [
        "what did we say", "what was said", "what did we talk",
        "what did we discuss", "what was discussed",
        "what about", "status of", "update on", "where are we on",
        "what's the status", "what is the status",
    ]):
        return INTENT_RECAP

    return INTENT_GENERAL


# Meeting scope for recap / action-item queries.
#   "current_only" — this meeting only, ignore cross-meeting memory
#   "prior_only"   — prior sessions/meetings only, not today's transcript
#   "all_sessions" — explicitly include everything
#   "full"         — default smart context (current meeting; cross-meeting only if relevant)

_SCOPE_CURRENT_ONLY = re.compile(
    r"\b("
    r"just\s+this\s+meeting|this\s+meeting\s+only|today'?s?\s+meeting|"
    r"current\s+meeting\s+only|only\s+this\s+meeting|"
    r"from\s+this\s+session|from\s+today|just\s+today|"
    r"not\s+(the\s+)?past|not\s+(the\s+)?previous|"
    r"this\s+session\s+only|just\s+(the\s+)?current"
    r")\b",
    re.IGNORECASE,
)

_SCOPE_PRIOR_ONLY = re.compile(
    r"\b("
    r"last\s+time|previous\s+meeting|prior\s+meeting|last\s+session|"
    r"last\s+meeting|from\s+(our\s+)?last|from\s+previous|"
    r"from\s+(our\s+)?prior|from\s+(an?\s+)?earlier\s+meeting|"
    r"previous\s+session|what\s+happened\s+last\s+time|"
    r"what\s+did\s+we\s+(do|decide|cover|discuss)\s+last\s+time|"
    r"last\s+week'?s?\s+meeting|the\s+other\s+(day|week)'?s?\s+meeting"
    r")\b",
    re.IGNORECASE,
)

_SCOPE_ALL_SESSIONS = re.compile(
    r"\b("
    r"all\s+(our\s+)?meetings|across\s+all|all\s+sessions|"
    r"all\s+past\s+and\s+present|every\s+meeting|all\s+time|"
    r"including\s+(all\s+)?past|include\s+(all\s+)?previous|"
    r"combine\s+all|from\s+all\s+(our\s+)?sessions"
    r")\b",
    re.IGNORECASE,
)


def detect_meeting_scope(request_text: str) -> str:
    """Detect which meetings the user wants to pull context from.

    Returns one of:
      "current_only" — this meeting only
      "prior_only"   — prior sessions only
      "all_sessions" — all meetings explicitly
      "full"         — default (current meeting; prior context only when relevant)
    """
    if not request_text or not request_text.strip():
        return "full"
    t = request_text.strip()
    if _SCOPE_ALL_SESSIONS.search(t):
        return "all_sessions"
    if _SCOPE_PRIOR_ONLY.search(t):
        return "prior_only"
    if _SCOPE_CURRENT_ONLY.search(t):
        return "current_only"
    return "full"