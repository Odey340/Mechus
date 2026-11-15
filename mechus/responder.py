from __future__ import annotations

from mechus.openai_compat import chat_completions_url as _mechus_chat_url, supports_json_mode as _mechus_json_ok

from dataclasses import dataclass
import json
import logging
import re
import ssl
import threading
import time
import urllib.request
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mechus.personas import Persona

from mechus.intent import (
    INTENT_CLARIFICATION,
    INTENT_CONFIRM,
    INTENT_GREETING,
    INTENT_HELP,
    INTENT_STOP,
    classify_intent,
)
from mechus.intent import (
    INTENT_ACTION_ITEMS,
    INTENT_DECISION,
    INTENT_GENERAL,
    INTENT_OPEN_QUESTIONS,
    INTENT_PERSON_ACTION,
    INTENT_RECAP,
    INTENT_RISK,
    INTENT_TIMELINE,
)
from mechus.brand import ASSISTANT_NAME
from mechus.json_utils import safe_parse_json
from mechus.text import (
    AUDIO_TAG_ALLOWLIST,
    clean_spoken_text,
    extract_requested_owner,
    limit_words,
    normalize_text,
    owner_matches_item_owner,
    sanitize_audio_tags,
    split_sentences,
    strip_wake_word,
    truncate_chars,
)

log = logging.getLogger(__name__)

# One line for prompts — avoids repeating a long sorted block inside the system string literal.
_AUDIO_TAGS_ONE_LINE = ", ".join(sorted(AUDIO_TAG_ALLOWLIST))


from mechus.language import (
    guess_language_from_text,
    is_english_language,
    language_display_name,
    normalize_language_code,
)

def _apply_wake_spoken_limits(
    text: str,
    *,
    max_words: int,
    max_chars: int,
    audio_tags: bool,
) -> str:
    """Cap wake speech by words, then by chars without dangling fragments (e.g. 'for the.')."""
    text = clean_spoken_text(text, preserve_audio_tags=audio_tags)
    mw = max(1, int(max_words))
    mc = max(1, int(max_chars))
    for n in range(mw, 0, -1):
        candidate = limit_words(text, n)
        if len(candidate) <= mc:
            return candidate
    return truncate_chars(limit_words(text, 1), mc)


def _limit_canned_spoken(text: str, max_words: int) -> str:
    """Word-cap that never truncates a pre-written cue below its full length (handles tiny MECHUS_MAX_RESPONSE_WORDS)."""
    mw = max(1, int(max_words))
    n = len(normalize_text(text).split())
    return limit_words(text, max(mw, n))


# Fallback must never return these fragmentary one-word answers (pipeline garbage).
_FRAGMENTARY_BLOCKLIST = frozenset(
    s.lower().strip().rstrip(".")
    for s in ("Use.", "Got.", "Here's.", "When.", "Fine.", "Sure.", "Okay.", "Yes.", "No.")
)

# Patterns that indicate we accidentally returned raw context instead of an answer.
_RAW_CONTEXT_MARKERS = (
    "meeting context (use only this",
    "use only this to answer",
    "meeting transcript (this session)",
    "[topic:",
    "meeting context:",
)


def _is_fragmentary(text: str) -> bool:
    """True if text is too short or is a known garbage fragment — never return as answer."""
    if not text or not isinstance(text, str):
        return True
    t = text.strip().rstrip(".")
    words = t.split()
    if len(words) < 5:
        return True
    return t.lower() in _FRAGMENTARY_BLOCKLIST


# Self-referential recap openers we don't want every recap to start with. The
# user complaint was: "every time asked for recap or summary it says 'like I
# said in the meeting' — not necessary every time." We strip these deterministic
# prefixes (regardless of LLM behavior) so the recap reads as plain narration.
_RECAP_BACKREFERENCE_PREFIXES: tuple[str, ...] = (
    "as i said in the meeting,",
    "as i said in the meeting",
    "like i said in the meeting,",
    "like i said in the meeting",
    "as i said earlier,",
    "as i said earlier",
    "like i said earlier,",
    "like i said earlier",
    "as i mentioned earlier,",
    "as i mentioned earlier",
    "like i mentioned earlier,",
    "like i mentioned earlier",
    "as i mentioned,",
    "as i mentioned",
    "like i mentioned,",
    "like i mentioned",
    "as we discussed,",
    "as we discussed",
    "as we talked about,",
    "as we talked about",
    "in the meeting we talked about",
    "in the meeting we discussed",
    "in this meeting we talked about",
    "in this meeting we discussed",
    "in the meeting,",
    "to recap what i said,",
    "to recap what i said",
    "to recap what we said,",
    "to recap what we said",
    "to summarize what i said,",
    "to summarize what we said,",
)


def _strip_recap_backreference_prefix(text: str) -> str:
    """Remove "as I said in the meeting" / "like I mentioned earlier" style
    openers when they show up in recap/summary responses. Idempotent: only
    strips matching prefixes once."""
    if not text:
        return text
    t = text.strip()
    low = t.lower()
    for prefix in _RECAP_BACKREFERENCE_PREFIXES:
        if low.startswith(prefix):
            stripped = t[len(prefix):].lstrip(" ,.;:—-")
            # Recapitalize the first letter so the recap reads cleanly.
            if stripped:
                stripped = stripped[0].upper() + stripped[1:]
            return stripped
    return t


def _is_raw_context_echo(text: str) -> bool:
    """True if the response looks like we echoed the raw retrieval/context string back."""
    if not text:
        return False
    tl = text.lower()
    return any(m in tl for m in _RAW_CONTEXT_MARKERS)


def _clean_for_extraction(text: str) -> str:
    """
    Strip retrieval framing and metadata noise from a context/memory string so that
    extraction functions see clean meeting dialogue only.
    """
    if not text:
        return ""
    text = re.sub(
        r"(?i)meeting context\s*\(use only this[^)]*\)\s*:?\s*",
        "",
        text,
    )
    text = re.sub(r"(?i)meeting transcript\s*\(this session\)\s*:?\s*", "", text)
    text = re.sub(r"(?i)meeting context\s*:?\s*", "", text)
    # Remove "[Topic: ...]" tags
    text = re.sub(r"\[Topic:[^\]]*\]", " ", text, flags=re.IGNORECASE)
    # Remove "[Alex]", "[Sarah]" speaker labels that came from chunking (keep "Alex: ...")
    text = re.sub(r"\[([A-Za-z0-9_ ]+)\]\s*(?=\S)", r"\1: ", text)
    # Treat trailing em-dash / hyphen on a line as a sentence ending (incomplete thought).
    # This prevents split_sentences from merging them with the next speaker's sentence.
    text = re.sub(r"[-\u2014]+\s*(\n|(?=[A-Z][a-z]+ *:))", r". ", text)
    # Collapse whitespace (but keep newlines for split_sentences)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


@dataclass(frozen=True)
class ResponseInput:
    wake_phrase: str
    request_text: str
    wake_phrases: tuple[str, ...] | None = None  # All wake words for strip_wake_word; if None, use (wake_phrase,)
    context_text: str | None = None
    memory_text: str | None = None
    context_chunks: list[dict] | None = None
    intent: str | None = None
    # Canonical state from retrieval: {decisions, action_items, open_issues}. Responder renders this; does not re-infer from text.
    state: dict | None = None
    # From retrieve_for_question: used_light_path, engine_snippet_chars, etc.
    retrieval_meta: dict | None = None
    # Selected persona for this turn (one of the bots in mechus/personas/). When the
    # default persona, the responder keeps its built-in Mechus system prompt.
    persona: "Persona | None" = None
    # MECHUS_CONVERSATIONAL: slightly more natural, support-agent pacing in the system prompt.
    conversational_mode: bool = False
    # ISO-639-1/2 language code the user spoke in (e.g. "en", "es", "fr", "hi").
    # When set and not English, the responder instructs the LLM to reply in that
    # language so ElevenLabs v3 (which is multilingual) speaks it back. None /
    # "" / "en" => default English behavior.
    user_language: str | None = None


def rephrase_repeat_spoken(text: str) -> str:
    """Lightweight same-facts rephrase when the LLM rephrase path is unavailable."""
    import random as _random

    s = (text or "").strip()
    if not s:
        return s
    lead = _random.choice(
        (
            "Sure — ",
            "Okay — ",
            "Right — ",
            "So — ",
            "Again — ",
        )
    )
    body = s
    if body.lower().startswith(("yeah", "sure", "okay", "ok", "right")):
        body = re.sub(
            r"^(yeah|sure|okay|ok|right)[,.\s—-]*",
            "",
            body,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
    swaps = (
        (r"\bthat's\b", "so that's"),
        (r"\bthe only\b", "really, the only"),
        (r"\bwe talked\b", "we chatted"),
        (r"\bbriefly\b", "for a bit"),
        (r"\bmentioned\b", "brought up"),
        (r"\bdue tomorrow\b", "due the next day"),
    )
    low = body.lower()
    for pat, repl in swaps:
        if re.search(pat, low, re.I):
            body = re.sub(pat, repl, body, count=1, flags=re.IGNORECASE)
            break
    out = (lead + body).strip()
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def _fetch_openai_rephrase(
    *,
    api_key: str,
    model: str,
    last_spoken: str,
    timeout_s: float = 10.0,
) -> str | None:
    """One-shot rephrase for repeat requests — same facts, fresher wording."""
    last = (last_spoken or "").strip()
    if not last or not api_key.strip():
        return None
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Rephrase the assistant's last spoken reply in fresh words. "
                    "Keep every fact, name, date, and number identical. "
                    "Same approximate length. Spoken English only. "
                    "No preamble, no 'yeah' opener, no questions back."
                ),
            },
            {"role": "user", "content": last},
        ],
        "max_tokens": min(220, max(64, len(last) // 2 + 48)),
        "temperature": 0.55,
    }
    from mechus.http_pool import http_request

    try:
        resp = http_request(
            "POST",
            _mechus_chat_url(),
            body=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key.strip()}",
            },
            timeout_s=max(4.0, float(timeout_s)),
        )
        payload = safe_parse_json(resp.data.decode("utf-8", "replace"))
        choices = payload.get("choices") or []
        if not choices:
            return None
        out = str(choices[0].get("message", {}).get("content", "")).strip()
        return out or None
    except Exception as exc:  # noqa: BLE001
        log.debug("OpenAI rephrase failed: %s", exc)
        return None


class Responder:
    def respond(self, inp: ResponseInput) -> str:
        raise NotImplementedError

    def rephrase_repeat(self, last_spoken: str, *, request_text: str = "") -> str:
        return rephrase_repeat_spoken(last_spoken)


def _structured_recap(context: str) -> str:
    """
    Build a structured recap in the format:
      Decision: <...>. Action items: <...>. Risks: <...>.
    Omits sections that have no content.
    """
    parts: list[str] = []

    decisions = _extract_decisions(context, max_items=2, min_confidence=0.6)
    if decisions:
        d_text = _compress_decision(decisions[-1])  # Most recent / highest-confidence decision
        parts.append(f"Decision: {d_text.rstrip('.')}.")

    actions = _extract_action_items(context, max_items=3)
    if actions:
        # Deduplicate and join concisely
        seen: set[str] = set()
        clean_actions: list[str] = []
        for a in actions:
            k = a.lower().rstrip(".!?")
            if k not in seen:
                seen.add(k)
                clean_actions.append(a.rstrip(".!?"))
        parts.append("Action items: " + "; ".join(clean_actions[:3]) + ".")

    risks = _extract_risks(context, max_items=3)
    if risks:
        seen_r: set[str] = set()
        clean_risks: list[str] = []
        for r in risks:
            k = r.lower().rstrip(".!?")
            if k not in seen_r:
                seen_r.add(k)
                clean_risks.append(r.rstrip(".!?"))
        parts.append("Risks: " + "; ".join(clean_risks[:3]) + ".")

    return " ".join(parts)


def _extractive_summary(context: str, max_sentences: int = 3) -> str:
    """
    Structured recap first; fall back to most-recent substantive sentences if no structure found.
    """
    structured = _structured_recap(context)
    if structured:
        return structured

    # Fallback: pick recent substantive sentences
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    substantive = []
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        text = _strip_speaker_label(s)
        if len(text) >= 25:
            substantive.append(text)
    if not substantive:
        for s in sents:
            if not _is_usable_sentence(s):
                continue
            text = _strip_speaker_label(s)
            if len(text) >= 12:
                substantive.append(text)
    if not substantive:
        return ""
    chosen = substantive[-max_sentences:]
    return normalize_text(" ".join(chosen))


# High-confidence explicit commitments (confidence 1.0)
_DECISION_HIGH = re.compile(
    r"\b(we decided|we've decided|we have decided|it's decided|"
    r"we agreed|everyone agreed|all agreed|"
    r"lock it in|locked in|"
    r"finalize on|finalise on|confirmed that|"
    r"the (?:\w+ )*(?:launch|release|rollout) date (?:will be|is)|"
    r"(?:launch|release|rollout|beta) date (?:will be|is)|"
    r"going to (?:launch|release|ship|deploy) (?:on|by))\b",
    re.IGNORECASE,
)

# Medium-confidence implicit commitments (confidence 0.8)
_DECISION_MED = re.compile(
    r"\b(going with|we'll go with|let's go with|we're going with|"
    r"let's do|we'll do|we can do|we should do|"
    r"set (?:for|to) \w|"
    r"the (?:date|deadline|launch|release) is |"
    r"(?:date|deadline) (?:is|will be) |"
    r"(?:the\s+)?(?:plan|target) is |"
    r"we're targeting|we are targeting|"
    r"(?:set|moved?|pushed?) (?:to|for) (?:the\s+)?\d|"  # "moved to June", "set for the 15th"
    r"(?:the\s+)?deadline (?:is|will be|moved to)|"
    r"we['']?re (?:launching|releasing|shipping) (?:on|by|in)|"
    r"(?:april|may|june|july|august|september|october|november|december|january|february|march) \d{1,2} (?:is|it is|that is))\b",
    re.IGNORECASE,
)

# Low-confidence soft agreement (confidence 0.6) — only used in absence of higher signals
_DECISION_LOW = re.compile(
    r"\b(sounds good|works for me|works for us|that works|"
    r"ok then|okay then|alright then|"
    r"(?:sounds|looks|seems) (?:good|fine|ok|okay|reasonable|right)|"
    r"(?:i|we|that) (?:think|say|vote) (?:we )?(?:go with|use|pick|choose)|"
    r"(?:yeah|yes|yep|sure),? (?:that|let's|let us|we can|works|sounds|ok|go ahead))\b",
    re.IGNORECASE,
)

_NOT_DECISION_PATTERNS = re.compile(
    r"\b(need to decide|still need to|have to decide|haven't decided|not decided|"
    r"should (?:we )?decide|will (?:we )?decide|going to decide|trying to decide|"
    r"haven't agreed|not agreed|still deciding|still figuring)\b",
    re.IGNORECASE,
)

_ACTION_PATTERNS = re.compile(
    r"\b(i'll|i will|i'm going to|i am going to|i plan to|i intend to|"
    r"will handle|will finish|will coordinate|will prepare|will send|will write|"
    r"will review|will schedule|will set up|will build|will deploy|will share|"
    r"will follow up|will reach out|will check|will look into|will figure out|"
    r"will make sure|will take care|will get|will do|will run|will lead|"
    r"going to handle|going to finish|going to prepare|going to send|going to write|"
    r"going to review|going to set up|going to build|going to reach out|"
    r"responsible for|taking care of|in charge of|owns this|taking this|"
    r"i'll own|i'll take|i'll lead|i'll run|i'll drive|"
    r"let me|i can take|i can handle|i'll handle|"
    r"(?:[A-Z][a-z]+)\s+will\s+\w|"            # Name + will
    r"(?:[A-Z][a-z]+)\s+(?:is\s+)?(?:going to|gonna)\s+\w|"  # Name + going to
    r"(?:[A-Z][a-z]+)\s+(?:owns|handles|leads|runs|drives)\s+\w|"  # Name + owns/handles
    r"(?:[A-Z][a-z]+)\s+(?:has\s+been|was)\s+assigned|"   # Name + assigned (passive)
    r"assigned\s+to\s+(?:[A-Z][a-z]+)|"        # assigned to + Name
    r"(?:[A-Z][a-z]+)\s+(?:has\s+been|was)\s+asked\s+to|"  # Name + asked to
    r"we\s+(?:need|want|asked)\s+(?:[A-Z][a-z]+)\s+to|"    # we need/asked Name to
    r"(?:that'?s|it'?s)\s+on\s+(?:[A-Z][a-z]+)\s+to|"     # that's on Name to
    r"(?:[A-Z][a-z]+)\s+(?:is\s+taking|takes\s+care\s+of))\b",  # Name is taking
    re.IGNORECASE,
)

_RISK_PATTERNS = re.compile(
    r"\b(one risk|a risk|the risk|main risk|potential risk|big risk|another risk|"
    r"one concern|a concern|the concern|main concern|another concern|"
    r"one issue|an issue|the issue|another issue|"
    r"problem is|the problem|a problem|one problem|"
    r"worried about|worry about|worrying that|"
    r"not sure (?:if|whether|about)|"
    r"hasn't been tested|not (?:fully )?tested|not ready|still broken|"
    r"might (?:fail|break|not work)|could (?:fail|break|cause)|"
    r"blocking|blocker|"
    r"unclear (?:if|whether|how|what)|"
    r"uncertain (?:about|if|whether)|"
    r"(?:the\s+)?(?:payment|integration|onboarding|security|performance)\s+(?:is\s+)?(?:not|hasn't|still)|"
    r"flag(?:ged)? (?:as a |as |this as )?(?:risk|concern|issue))\b",
    re.IGNORECASE,
)

_TIMELINE_PATTERNS = re.compile(
    r"\b(\d{1,2}(?:st|nd|rd|th)?(?:\s+of)?\s+(?:january|february|march|april|may|june|"
    r"july|august|september|october|november|december)|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}|"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:next\s+week|this\s+week|end\s+of\s+week|next\s+month)|"
    r"(?:by\s+(?:the\s+)?\d{1,2})|"
    r"(?:q[1-4]\b)|(?:\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)|"
    r"(?:deadline|due date|launch|release|ship|deploy|go live|by when))\b",
    re.IGNORECASE,
)


def _strip_speaker_label(s: str) -> str:
    """Remove 'Speaker: ' prefix from a sentence."""
    s = re.sub(r"^[A-Za-z0-9_ ]+:\s*", "", s.strip())
    return s.strip()


# Filler prefixes that precede the actual commitment in implicit decisions
_DECISION_FILLER_PREFIX = re.compile(
    r"^(?:so\s+)?(?:to\s+be\s+clear[,\s]+|"
    r"just\s+to\s+confirm[,\s]+|"
    r"alright[,\s]+(?:so\s+)?|"
    r"ok(?:ay)?[,\s]+(?:so\s+)?|"
    r"yeah[,\s]+(?:so\s+)?|"
    r"i\s+think[,\s]+(?:we\s+should\s+)?|"
    r"i\s+mean[,\s]+)",
    re.IGNORECASE,
)

# Filler words at the end of a decision ("... now", "... then")
_DECISION_FILLER_SUFFIX = re.compile(r"\s+(?:now|then|right)[.!?]?$", re.IGNORECASE)


def _compress_decision(text: str) -> str:
    """
    Strip filler prefixes/suffixes from an implicit decision sentence so the
    core commitment is clear: "So to be clear the date is June 15 now" → "The date is June 15".
    """
    t = text.strip()
    t = _DECISION_FILLER_PREFIX.sub("", t).strip()
    t = _DECISION_FILLER_SUFFIX.sub("", t).strip()
    if t:
        t = t[0].upper() + t[1:]
    return t or text.strip()


def _normalize_action_item(sentence: str) -> str:
    """
    Convert first-person action statements to third-person using the speaker label.
    'Alex: I'll coordinate the invites' → 'Alex will coordinate the invites'
    'And I'll do the blog post' (no label) → returned as-is after stripping filler.
    """
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]{0,19}):\s*", sentence.strip())
    if m:
        speaker = m.group(1).strip().title()
        rest = sentence[m.end():].strip()
    else:
        speaker = None
        rest = sentence.strip()

    # Strip leading conjunctions ("And I'll" → "I'll")
    rest = re.sub(r"^(?:and|but|also|plus)\s+", "", rest, flags=re.IGNORECASE).strip()

    if speaker:
        # Replace first-person subject with speaker name
        rest = re.sub(r"^I'll\b", f"{speaker} will", rest, flags=re.IGNORECASE)
        rest = re.sub(r"^I will\b", f"{speaker} will", rest, flags=re.IGNORECASE)
        rest = re.sub(r"^I'm going to\b", f"{speaker} will", rest, flags=re.IGNORECASE)
        rest = re.sub(r"^I am going to\b", f"{speaker} will", rest, flags=re.IGNORECASE)
        rest = re.sub(r"^I\b", speaker, rest, flags=re.IGNORECASE)
    return rest.strip()


def _join_sentences(items: list[str]) -> str:
    """Join extracted sentences cleanly, deduplicating and normalising punctuation."""
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.strip().rstrip(".!?").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(item.strip().rstrip(".!?"))
    if not unique:
        return ""
    return ". ".join(unique) + "."


def _is_mechus_question(s: str) -> bool:
    """True if the sentence is directed at Mechus (should not be used as a meeting fact)."""
    sl = s.lower().strip()
    # Strip speaker label before checking
    stripped = re.sub(r"^[A-Za-z0-9_ ]+:\s*", "", sl)
    wake_words = ("mechus", "computer", "hey mechus", "hey computer", ASSISTANT_NAME.lower(), f"hey {ASSISTANT_NAME.lower()}")
    # Check if wake word appears in first 5 words (e.g. "Wait sorry Mechus, ...")
    first_words = " ".join(stripped.split()[:5])
    for w in wake_words:
        if w in first_words:
            return True
    return False


def _is_usable_sentence(s: str) -> bool:
    """True if the sentence is substantive enough to be returned as an answer."""
    text = _strip_speaker_label(s).strip()
    if _is_mechus_question(s):
        return False
    # Reject very short or trailing-off sentences (incomplete thoughts)
    if len(text) < 12:
        return False
    if text.endswith("—") or text.endswith("-") or text.endswith("..."):
        return False
    return True


def _score_decision(s: str) -> float:
    """Return confidence score (0.0–1.0) for a sentence being a decision. 0 = not a decision."""
    if _NOT_DECISION_PATTERNS.search(s):
        return 0.0
    if _DECISION_HIGH.search(s):
        return 1.0
    if _DECISION_MED.search(s):
        return 0.8
    if _DECISION_LOW.search(s):
        return 0.6
    return 0.0


def _extract_decisions(context: str, max_items: int = 3, min_confidence: float = 0.6) -> list[str]:
    """
    Return sentences from cleaned context that contain a decision signal above min_confidence.
    Higher confidence = explicit language (decided, locked in).
    Lower confidence = implicit language (sounds good, the date is X).
    """
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    # Collect (confidence, text) pairs
    candidates: list[tuple[float, str]] = []
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        score = _score_decision(s)
        if score >= min_confidence and len(s.strip()) >= 12:
            text = _strip_speaker_label(s)
            if text:
                candidates.append((score, text))
    # Sort highest confidence first, then take top N (deduplicated)
    candidates.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    found: list[str] = []
    for _, text in candidates:
        key = text.lower().rstrip(".!?")
        if key not in seen:
            seen.add(key)
            found.append(text)
        if len(found) >= max_items:
            break
    return found


def _extract_action_items(context: str, max_items: int = 5) -> list[str]:
    """Return action-item sentences normalized to third-person attribution."""
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    found: list[str] = []
    seen: set[str] = set()
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        if _ACTION_PATTERNS.search(s) and len(s.strip()) >= 12:
            # Normalize before dedup so "I'll X" and "Alex will X" don't both appear
            text = _normalize_action_item(s)
            key = text.lower().rstrip(".!?")
            if text and key not in seen:
                seen.add(key)
                found.append(text)
        if len(found) >= max_items:
            break
    return found


def _extract_risks(context: str, max_items: int = 3) -> list[str]:
    """Return sentences that flag a risk or concern (all matches up to max_items)."""
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    found: list[str] = []
    seen: set[str] = set()
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        if _RISK_PATTERNS.search(s) and len(s.strip()) >= 12:
            text = _strip_speaker_label(s)
            key = text.lower().rstrip(".!?")
            if text and key not in seen:
                seen.add(key)
                found.append(text)
        if len(found) >= max_items:
            break
    return found


def _extract_timeline(context: str, max_items: int = 3) -> list[str]:
    """Return sentences that mention dates, deadlines, or timeline signals."""
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    found = []
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        if _TIMELINE_PATTERNS.search(s) and len(s.strip()) >= 10:
            text = _strip_speaker_label(s)
            if text:
                found.append(text)
        if len(found) >= max_items:
            break
    return found


def _find_action_items(context: str, max_items: int = 3) -> str:
    """Build a short response string from extracted action items (legacy helper)."""
    items = _extract_action_items(context, max_items=max_items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "; ".join(items[:3]) + "."


def _find_snippet(context: str, query: str, max_sentences: int = 2) -> str:
    """Keyword-match sentences from cleaned context, skipping Mechus-directed lines."""
    clean = _clean_for_extraction(context)
    sents = split_sentences(clean)
    if not sents:
        return ""
    q = normalize_text(query).lower()
    stopwords = {"mechus", "what", "did", "they", "say", "about", "the", "a", "an", "is", "are", "was"}
    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t and t not in stopwords and len(t) > 2]
    if not tokens:
        return ""
    hits: list[str] = []
    for s in sents:
        if not _is_usable_sentence(s):
            continue
        sl = s.lower()
        if any(t in sl for t in tokens):
            text = _strip_speaker_label(s)
            if text:
                hits.append(text)
        if len(hits) >= max_sentences:
            break
    return normalize_text(" ".join(hits))


def _render_state_answer(intent: str, state: dict, request: str, max_words: int) -> str | None:
    """
    Render an answer from canonical state only. No text scraping, no regex.
    Returns None if intent should not be answered from state (e.g. recap, general) or state has no data.
    """
    decisions = state.get("decisions") or []
    action_items = state.get("action_items") or []
    open_issues = state.get("open_issues") or []

    if intent == "decision_query":
        if not decisions:
            return None
        parts = [f"We decided: {d.get('value', '')}." for d in decisions[:3] if d.get("value")]
        if not parts:
            return None
        out = " ".join(parts)
        w = out.split()
        if len(w) < 5:
            return None
        return out

    if intent in ("action_item_query", "person_action_query"):
        if not action_items:
            return None
        request_l = request.lower()
        subset = action_items
        requested_owner = extract_requested_owner(request)
        if requested_owner:
            filtered = [
                a for a in action_items
                if owner_matches_item_owner(a.get("owner") or "", requested_owner)
            ]
            if not filtered:
                return f"No action items for {requested_owner} yet."
            subset = filtered
        else:
            # Fallback: capitalized names in transcript-style questions
            candidate_names = re.findall(r"\b([A-Z][a-z]{1,20})\b", request)
            known_owners_lower = {
                (a.get("owner") or "").lower().split()[0]: a.get("owner")
                for a in action_items
            }
            _matched_owner: str | None = None
            for cname in candidate_names:
                clow = cname.lower()
                if clow in known_owners_lower:
                    _matched_owner = known_owners_lower[clow]
                    break
            if _matched_owner is None:
                for word in re.findall(r"\b[a-z]{2,20}\b", request_l):
                    if word in known_owners_lower:
                        _matched_owner = known_owners_lower[word]
                        break
            if _matched_owner:
                filtered = [
                    a for a in action_items
                    if owner_matches_item_owner(a.get("owner") or "", _matched_owner)
                ]
                if filtered:
                    subset = filtered
        parts = []
        for a in subset[:8]:
            owner = a.get("owner") or "Someone"
            task = (a.get("task") or "").strip()
            deadline = a.get("deadline")
            source = (a.get("source") or "").strip().lower()
            if not task:
                continue
            line = f"{owner} — {task}"
            if deadline:
                line += f" (by {deadline})"
            # Flag items carried over from prior sessions so the LLM can label them
            if source in ("prior_session", "cross_meeting"):
                line += " [from prior session]"
            parts.append(line)
        if not parts:
            return None
        out = ". ".join(parts) + "."
        w = out.split()
        if len(w) < 5:
            return None
        return out

    if intent == "open_question_query":
        if not open_issues:
            return None
        open_only = [i for i in open_issues if (i.get("status") or "") == "open"]
        postponed = [i for i in open_issues if (i.get("status") or "") == "postponed"]
        parts = []
        if open_only:
            parts.append("Still open: " + "; ".join(i.get("display_summary", "") or i.get("topic_key", "") for i in open_only[:5]))
        if postponed:
            parts.append("Postponed: " + "; ".join(i.get("display_summary", "") or i.get("topic_key", "") for i in postponed[:3]))
        if not parts:
            return None
        out = ". ".join(parts) + "."
        w = out.split()
        if len(w) < 5:
            return None
        return out

    # recap_query, general, or other: do not answer from state (escalate to LLM or refusal)
    return None


def _build_answer_from_structured_chunks(
    chunks: list[dict],
    intent: str,
    request: str,
    max_words: int = 50,
) -> str:
    """
    Build answer from chunks using tags only. No heuristic keyword fallback.
    If the intent has no tag match, return refusal (so caller can escalate to LLM).
    """
    if not chunks:
        return "I don't have enough information yet."
    # Tag-only selection. No scraping.
    if intent == "decision_query":
        selected = [c for c in chunks if "decision" in c.get("tags", [])]
    elif intent in ("action_item_query", "person_action_query"):
        selected = [c for c in chunks if "action_item" in c.get("tags", [])]
        request_l = (request or "").lower()
        for name in ("bob", "alice", "charlie", "dana", "carol"):
            if name in request_l:
                by_name = [c for c in selected if (c.get("speaker") or "").lower() == name]
                if by_name:
                    selected = by_name
                break
    elif intent == "open_question_query":
        selected = [c for c in chunks if "open_question" in c.get("tags", [])]
    else:
        # general / recap: do not build from chunks (would be scraping)
        return "I don't have enough information yet."

    if not selected:
        return "I don't have enough information yet."

    parts = []
    for c in selected[:3]:
        text = (c.get("text") or "").strip()
        if len(text) < 10:
            continue
        speaker = c.get("speaker") or "Someone"
        parts.append(f"{speaker} — {text}")
    if not parts:
        return "I don't have enough information yet."
    out = ". ".join(parts) + "."
    if _is_fragmentary(out):
        return "I don't have enough information yet."
    return out


_AI_OPENER_RE = re.compile(
    r"^\s*(certainly|of course|sure thing|sure(?:[,!.])|absolutely|"
    r"i'?d be happy to|i would be happy to|happy to help|"
    r"great question|good question|that'?s a great question|"
    r"as an ai|as a language model|as your assistant)"
    r"[\s,!\.\-:—]*",
    re.IGNORECASE,
)
_REASONING_LEAK_RE = re.compile(
    r"^\s*(?:let me (?:think|check|see)|i'?ll (?:think|check|look|figure)|"
    r"thinking[,\s]|hmm,? let me|one moment|give me a (?:second|sec|moment))"
    # Stop at the first natural break so we keep the actual answer that follows.
    r"[^,\.\!\?\-—]*[,\.\!\?\-—]+\s*",
    re.IGNORECASE,
)


_TAG_PREFIX_RE = re.compile(r"^\s*(\[[^\[\]]{1,40}\])\s*")


def _strip_self_narration(text: str) -> str:
    """Drop boilerplate AI openers and meta-narration like 'Let me think about that...'.

    Audio tags at the very start are preserved. We re-detect a tag prefix after each
    strip pass so '[sighs] Let me think — yeah, we landed on July.' becomes
    '[sighs] yeah, we landed on July.' and 'Certainly! [warmly] Let me think — ok.'
    becomes '[warmly] ok.'.
    """
    if not text:
        return ""
    s = text.strip()
    tag_prefix = ""
    m = _TAG_PREFIX_RE.match(s)
    if m:
        tag_prefix = m.group(1) + " "
        s = s[m.end():]
    s_after_opener = _AI_OPENER_RE.sub("", s)
    if s_after_opener != s and not tag_prefix:
        # Opener removal exposed a new audio tag — promote it to the prefix.
        m2 = _TAG_PREFIX_RE.match(s_after_opener)
        if m2:
            tag_prefix = m2.group(1) + " "
            s_after_opener = s_after_opener[m2.end():]
    s_after_leak = _REASONING_LEAK_RE.sub("", s_after_opener)
    if not s_after_leak.strip():
        return text  # Don't accidentally empty out the response.
    return (tag_prefix + s_after_leak).strip()


def _safe_response(text: str, max_words: int, fallback: str) -> str:
    """Return text limited to max_words, but return fallback if text looks like raw context echo."""
    if not text or _is_raw_context_echo(text):
        return limit_words(fallback, max_words)
    return limit_words(text, max_words)


class RuleResponder(Responder):
    def __init__(self, max_response_words: int, audio_tags_enabled: bool = False):
        self._max_words = int(max_response_words)
        self._audio_tags = bool(audio_tags_enabled)

    def rephrase_repeat(self, last_spoken: str, *, request_text: str = "") -> str:
        return _limit_canned_spoken(rephrase_repeat_spoken(last_spoken), self._max_words)

    def respond(self, inp: ResponseInput) -> str:
        phrases = inp.wake_phrases if inp.wake_phrases else (inp.wake_phrase,)
        request = strip_wake_word(inp.request_text, phrases)
        request_l = request.lower()

        # Use memory_text + context_text (cleaned) as extraction source.
        # context_text has retrieval prefix — always clean before use.
        # We combine both so extraction sees the full picture even when topic-based
        # memory retrieval misses a segment (e.g. decision is in a different topic bubble).
        raw_memory = normalize_text(inp.memory_text or "")
        raw_context = normalize_text(inp.context_text or "")
        clean_ctx = _clean_for_extraction(raw_context)

        # Combine: deduplicate by putting both together; extraction functions handle repeated text fine
        extraction_parts = [p for p in [raw_memory, clean_ctx] if p]
        extraction_src = " ".join(extraction_parts)

        recency_query = any(p in request_l for p in ["just happened", "just now", "right now", "recently"])

        import random as _random

        if not request:
            return ""

        intent = classify_intent(request)
        if intent == INTENT_STOP:
            return ""
        if intent == INTENT_GREETING:
            if self._audio_tags:
                choices = [
                    "[attentive] Yeah, what do you need?",
                    "[ready] I'm here, go ahead.",
                    "[listening] Listening, what's up?",
                    "[ready] Yeah, go ahead.",
                ]
            else:
                choices = [
                    "Yeah, what do you need?",
                    "I'm here, go ahead.",
                    "Listening, what's up?",
                    "Yeah, go ahead.",
                ]
            return _limit_canned_spoken(_random.choice(choices), self._max_words)
        if intent == INTENT_HELP:
            if self._audio_tags:
                choices = [
                    "[thoughtfully] I've been following along. Ask me for a recap, action items, or what we decided.",
                    "I've got the whole meeting so far — decisions, action items, risks, just ask.",
                    "[warmly] Been here the whole time. What do you need?",
                ]
            else:
                choices = [
                    "I've been following along. Ask me for a recap, action items, or what we decided.",
                    "I've got the whole meeting so far — decisions, action items, risks, just ask.",
                    "Been here the whole time. What do you need?",
                ]
            return _limit_canned_spoken(_random.choice(choices), self._max_words)
        if intent == INTENT_CLARIFICATION:
            if self._audio_tags:
                choices = [
                    "[curious] I didn't say anything — were you asking me something?",
                    "Sorry, were you talking to me?",
                    "[gently] Did you need something from me?",
                ]
            else:
                choices = [
                    "I didn't say anything — were you asking me something?",
                    "Sorry, were you talking to me?",
                    "Did you need something from me?",
                ]
            return _limit_canned_spoken(_random.choice(choices), self._max_words)
        if intent == INTENT_CONFIRM:
            if self._audio_tags:
                choices = [
                    "[affirmative] Got it.",
                    "Noted.",
                    "[ready] Yep, on it.",
                    "Got that.",
                ]
            else:
                choices = ["Got it.", "Noted.", "Yep, on it.", "Got that."]
            return _limit_canned_spoken(_random.choice(choices), self._max_words)

        # Note-taking commands: acknowledge and confirm
        if any(k in request_l for k in ["take notes", "taking notes", "keep notes", "start notes", "log this"]):
            if self._audio_tags:
                choices = [
                    "[confident] Got it, I'm on it.",
                    "Already on it.",
                    "[reassuring] Yep, been tracking since the start.",
                    "On it — I've got everything so far.",
                ]
            else:
                choices = [
                    "Got it, I'm on it.",
                    "Already on it.",
                    "Yep, been tracking since the start.",
                    "On it — I've got everything so far.",
                ]
            return _limit_canned_spoken(_random.choice(choices), self._max_words)

        if any(k in request_l for k in ["summarize", "summary", "recap"]):
            if not extraction_src:
                return limit_words("Nothing to recap yet — we're just getting started.", self._max_words)
            summary = _extractive_summary(extraction_src, max_sentences=3 if not recency_query else 2)
            return _safe_response(summary, self._max_words, "Nothing stands out clearly enough to recap yet.")

        if intent == INTENT_ACTION_ITEMS or any(k in request_l for k in ["action items", "next steps", "todo", "to-do"]):
            items = _extract_action_items(extraction_src, max_items=4)
            if not items:
                return limit_words("Nothing yet — I'll keep listening for tasks.", self._max_words)
            out = _join_sentences(items[:4])
            return _safe_response(out, self._max_words, "Nothing yet — I'll keep listening for tasks.")

        if intent == INTENT_DECISION or any(k in request_l for k in ["what did we decide", "decisions", "decided", "what's the decision", "land on"]):
            decisions = _extract_decisions(extraction_src, max_items=3, min_confidence=0.6)
            if decisions:
                out = _join_sentences(decisions[:2])
                return _safe_response(out, self._max_words, "Nothing clearly locked in yet.")
            return limit_words("Nothing clearly locked in yet.", self._max_words)

        if intent == INTENT_TIMELINE or any(k in request_l for k in ["timeline", "when ", "deadline", "due date", "launch date", "by when"]):
            timeline = _extract_timeline(extraction_src, max_items=2)
            if timeline:
                out = _join_sentences(timeline[:2])
                return _safe_response(out, self._max_words, "No dates or deadlines mentioned yet.")
            return limit_words("No dates or deadlines mentioned yet.", self._max_words)

        if intent == INTENT_RISK or any(k in request_l for k in ["risk", "risks", "concern", "concerns", "worried"]):
            risks = _extract_risks(extraction_src, max_items=3)
            if risks:
                out = _join_sentences(risks[:2])
                return _safe_response(out, self._max_words, "No risks or concerns raised yet.")
            return limit_words("No risks or concerns raised yet.", self._max_words)

        if intent == INTENT_OPEN_QUESTIONS or any(k in request_l for k in ["open question", "tbd", "still open", "unresolved"]):
            snippet = _find_snippet(extraction_src, "open tbd unresolved question unclear")
            return _safe_response(snippet, self._max_words, "Nothing flagged as still open.")

        if any(k in request_l for k in ["what did", "what was", "what were"]):
            snippet = _find_snippet(extraction_src, request)
            return _safe_response(snippet, self._max_words, "I don't have that in what I've heard.")

        if "introduce" in request_l or "who are you" in request_l:
            return limit_words(
                f"I'm {ASSISTANT_NAME} — I've been listening the whole time. Ask me for a recap or what we decided.",
                self._max_words,
            )

        if intent == INTENT_GENERAL:
            return limit_words(
                "I can’t answer general questions in offline rule mode—use the cloud assistant, or ask about this meeting.",
                self._max_words,
            )
        return limit_words(
            "Ask me for a recap, decisions, action items, or what was said.",
            self._max_words,
        )


def _format_state_for_llm(state: dict, *, owner_filter: str | None = None) -> str:
    """
    Turn canonical state into a short summary for the LLM. No transcript dump.
    LLM uses this only to phrase the answer — structure is already correct.
    """
    lines = []
    decisions = state.get("decisions") or []
    if decisions:
        lines.append("Decisions: " + "; ".join(f"{d.get('id', '')} — {d.get('value', '')}" for d in decisions[:10]))
    action_items = state.get("action_items") or []
    if owner_filter:
        action_items = [
            a for a in action_items
            if owner_matches_item_owner(a.get("owner") or "", owner_filter)
        ]
    if action_items:
        parts = [
            f"{a.get('owner', '')} — {a.get('task', '')}" + (f" (by {a.get('deadline')})" if a.get("deadline") else "")
            for a in action_items[:15]
        ]
        lines.append("Action items: " + "; ".join(parts))
    open_issues = state.get("open_issues") or []
    if open_issues:
        open_only = [i.get("display_summary", i.get("topic_key", "")) for i in open_issues if (i.get("status") or "") == "open"]
        postponed = [i.get("display_summary", i.get("topic_key", "")) for i in open_issues if (i.get("status") or "") == "postponed"]
        if open_only:
            lines.append("Still open: " + "; ".join(open_only))
        if postponed:
            lines.append("Postponed: " + "; ".join(postponed))
    reversals = state.get("reversals") or []
    if reversals:
        lines.append("Reversals (superseded): " + "; ".join(f"{r.get('decision_id', '')} → {r.get('new_value', '')}" for r in reversals[-5:]))
    participant_timezones = state.get("participant_timezones") or {}
    if participant_timezones:
        tz_parts = [f"{k}: {v}" for k, v in list(participant_timezones.items())[:12]]
        lines.append("Participant timezones: " + "; ".join(tz_parts))
    timezone_conflicts = state.get("timezone_conflicts") or []
    if timezone_conflicts:
        issues = []
        for c in timezone_conflicts[-3:]:
            if not isinstance(c, dict):
                continue
            utt = str(c.get("utterance") or "").strip()
            impacted = c.get("impacted") or []
            if utt:
                issues.append(f"{utt} -> {', '.join([str(x) for x in impacted[:4]])}")
        if issues:
            lines.append("Potential timezone conflicts: " + "; ".join(issues))
    if not lines:
        return "(No meeting state yet.)"
    return "\n".join(lines)


def pack_grounding_prompt_blocks(
    *,
    state: dict,
    snippets_text: str,
    memory_aux: str,
    budget_chars: int,
    owner_filter: str | None = None,
) -> tuple[str, dict]:
    """
    Build the user-message grounding sections with priority truncation:
    CANONICAL_STATE first, then MEETING_SNIPPETS, then MEMORY_AUX.
    Returns (combined_text, stats dict for logging).
    """
    stats: dict = {
        "truncated_snippets": False,
        "truncated_memory": False,
        "dropped_memory": False,
        "budget_chars": budget_chars,
        "final_chars": 0,
    }
    canon_body = _format_state_for_llm(state or {}, owner_filter=owner_filter)
    if canon_body == "(No meeting state yet.)":
        canon_body = (
            "(No structured meeting state extracted yet. "
            "Do not invent owners, deadlines, or decisions—say we have not captured them if asked.)"
        )
    block_canon = (
        "=== CANONICAL_STATE (ground truth for owners, deadlines, decisions) ===\n"
        + canon_body
        + "\n"
    )
    snip = (snippets_text or "").strip()
    mem = (memory_aux or "").strip()
    header_snip = "=== MEETING_SNIPPETS_AND_TEAM_CONTEXT (live transcript plus optional team context) ===\n"
    header_mem = "=== MEMORY_AUX (dialog continuity only; secondary to snippets for meeting facts) ===\n"

    if len(block_canon) >= budget_chars:
        out = block_canon[: budget_chars - 40] + "\n...(CANONICAL_STATE truncated)\n"
        stats["truncated_snippets"] = True
        stats["final_chars"] = len(out)
        return out, stats

    remaining = budget_chars - len(block_canon)
    block_snip = header_snip + snip + "\n" if snip else ""
    block_mem = header_mem + mem + "\n" if mem else ""

    if len(block_snip) + len(block_mem) <= remaining:
        out = block_canon + block_snip + block_mem
        stats["final_chars"] = len(out)
        return out, stats

    # Reserve at least 15% of remaining for MEMORY_AUX if present
    mem_reserve = min(len(block_mem), max(400, int(remaining * 0.15))) if mem else 0
    snip_budget = max(0, remaining - mem_reserve)
    if len(block_snip) > snip_budget:
        if snip_budget > len(header_snip) + 80:
            cut = snip_budget - len(header_snip) - 35
            block_snip = header_snip + snip[:cut] + "\n...(MEETING_SNIPPETS truncated)\n"
        else:
            block_snip = header_snip + "(truncated)\n"
        stats["truncated_snippets"] = True
    remaining2 = budget_chars - len(block_canon) - len(block_snip)
    if mem and remaining2 > len(header_mem):
        if len(block_mem) <= remaining2:
            out = block_canon + block_snip + block_mem
        else:
            room = remaining2 - len(header_mem) - 30
            block_mem = header_mem + mem[: max(0, room)] + "\n...(MEMORY_AUX truncated)\n"
            stats["truncated_memory"] = True
            out = block_canon + block_snip + block_mem
    elif mem:
        stats["dropped_memory"] = True
        out = block_canon + block_snip
    else:
        out = block_canon + block_snip
    stats["final_chars"] = len(out)
    return out, stats


def _intent_system_appendix(intent: str, retrieval_meta: dict | None = None) -> str:
    """Extra system instructions per meeting intent (spoken output)."""
    meta = retrieval_meta or {}
    if intent == INTENT_RECAP:
        # recap_scope is set by intent classifier from user's words; meeting_scope
        # is the broader scope flag propagated from the agent retrieval layer.
        scope = (
            meta.get("recap_scope")
            or meta.get("meeting_scope")
            or "full"
        ).strip().lower()
        tw = meta.get("time_window_minutes")
        base = (
            "\nFor this recap:\n"
            "- DEFAULT scope: this current meeting only. Do NOT reference or mix in prior meetings / "
            "TEAM_SHARED_CONTEXT unless the user explicitly asked to include past sessions.\n"
            "- If a time window was specified (e.g. 'last 5 minutes'), ONLY cover what happened in "
            "that exact window. Be strict — do not include anything from before that window.\n"
            "- Cover ALL topics discussed — including casual conversation, debates, informal exchanges. "
            "Nothing is too small if it was discussed.\n"
            "- IGNORE background TV/radio ads, sirens, unrelated media playback, and garbled lines.\n"
            "- Disagreements and back-and-forth are important — summarize what the disagreement was "
            "about and how it resolved.\n"
            "- Go through topics in chronological order.\n"
            "- Be specific, not vague. Name names, mention specifics, dollar amounts, dates.\n"
            "- Use at least 4 complete sentences for a full-meeting recap (up to 8 if there was a lot). "
            "Walk topics in order; do not compress the whole meeting into one line.\n"
            "- End with any decisions or action items if there were any.\n"
            "- Vary your phrasing. Do NOT prefix every recap with 'as I said in the meeting', "
            "'like I mentioned earlier', 'as we discussed', or similar self-referential openers.\n"
            "- Refer to people by name when natural.\n"
        )
        if scope == "current_only":
            base += (
                "- Scope: THIS meeting only. Ignore TEAM_SHARED_CONTEXT entirely.\n"
            )
        elif scope == "prior_only":
            base += (
                "- Scope: the user wants a recap of PREVIOUS sessions only (TEAM_SHARED_CONTEXT). "
                "Focus on what those prior meetings covered — decisions, action items, open threads. "
                "Do NOT summarize today's live transcript unless comparing.\n"
            )
        elif scope == "all_sessions":
            base += (
                "- Scope: the user wants a combined recap spanning all sessions. Summarize the current "
                "meeting first, then note any relevant continuity from prior sessions in TEAM_SHARED_CONTEXT. "
                "Label them clearly ('in this meeting…', 'from last session…').\n"
            )
        if scope == "window" and tw is not None:
            if meta.get("time_window_empty"):
                base += (
                    f"- Scope: the user asked for a recap of the last {tw:g} minute(s), but nothing "
                    "substantive was captured in that window. Say plainly that nothing notable happened "
                    "in that time. Do NOT summarize earlier parts of the meeting to fill the gap.\n"
                )
            else:
                base += (
                    f"- Scope: the user asked for a recap of roughly the last {tw:g} minutes only. "
                    "Summarize only what appears in MEETING_SNIPPETS for that window.\n"
                )
        elif scope == "topic":
            base += (
                "- Scope: the user asked for a topic-focused recap. Summarize only what "
                "MEETING_SNIPPETS support for that topic; if it was not discussed, say so clearly.\n"
            )
        return base
    if intent in (INTENT_ACTION_ITEMS, INTENT_PERSON_ACTION):
        owner = (meta.get("requested_owner") or "").strip()
        owner_note = ""
        if owner:
            owner_note = (
                f" The user asked ONLY for action items owned by {owner}. "
                f"List every task for {owner} from CANONICAL_STATE; do NOT mention tasks "
                f"assigned to anyone else."
            )
        meeting_scope = (meta.get("meeting_scope") or "full").strip().lower()
        scope_note = ""
        if meeting_scope == "current_only":
            scope_note = (
                " Focus on THIS meeting's action items only — ignore TEAM_SHARED_CONTEXT."
            )
        elif meeting_scope == "prior_only":
            scope_note = (
                " The user asked about PRIOR meetings. Surface action items from TEAM_SHARED_CONTEXT "
                "and note which prior session each came from if available."
            )
        elif meeting_scope == "all_sessions":
            scope_note = (
                " Include action items from BOTH this meeting (CANONICAL_STATE / MEETING_SNIPPETS) "
                "AND prior sessions (TEAM_SHARED_CONTEXT). Group them: 'From this meeting: …' then "
                "'From previous sessions: …' if there are prior ones."
            )
        return (
            "\nFor action items: list only what appears in CANONICAL_STATE and MEETING_SNIPPETS. "
            "Include every distinct action item, owner, and deadline — do not collapse to a subset "
            "unless the user asked for a subset. End with a concise spoken checklist of who owns what. "
            "Do not invent owners or deadlines." + owner_note + scope_note + "\n"
        )
    if intent == INTENT_DECISION:
        return (
            "\nFor decisions: use CANONICAL_STATE first, then support from MEETING_SNIPPETS. "
            "List every decision captured — include who made it and why if stated. "
            "Do not claim the group decided something unless it appears in CANONICAL_STATE or MEETING_SNIPPETS. "
            "Format as a spoken checklist: 'first… next… finally…'\n"
        )
    if intent == INTENT_TIMELINE:
        return (
            "\nFor dates and milestones: include every distinct date or deadline in CANONICAL_STATE and MEETING_SNIPPETS "
            "that answers the question — do not omit items to shorten the reply.\n"
        )
    if intent in (INTENT_RISK, INTENT_OPEN_QUESTIONS):
        return (
            "\nGround risks, dates, and open questions in MEETING_SNIPPETS and CANONICAL_STATE only. "
            "When listing multiple items, include all that appear in context for this question. "
            "Be specific — name who raised the concern or what exactly is unresolved.\n"
        )
    if intent == INTENT_GENERAL:
        if meta.get("substantive_general"):
            return (
                "\nGeneral question (substantive): answer from general knowledge in 2–5 complete sentences. "
                "Include the main number or fact first, then one sentence of context (units, average vs "
                "closest, or what it means). Do not stop after a single clipped line. Do not refuse only "
                "because CANONICAL_STATE is empty.\n"
            )
        return (
            "\nGeneral question: treat this like a normal assistant. Answer from general knowledge concisely. "
            "Do not refuse only because CANONICAL_STATE is empty. If the ask "
            "is clearly about this meeting’s commitments, ground in CANONICAL_STATE and snippets first.\n"
        )
    return ""


def _is_openai_placeholder_fallback(text: str) -> bool:
    """
    True when OpenAIResponder returned a generic failure/refusal string.
    HybridResponder should delegate to RuleResponder in that case so extraction still works.
    """
    if not text or not str(text).strip():
        return True
    t = normalize_text(str(text)).strip().lower()
    t = t.replace("\u2014", "-").replace("—", "-")
    if t.rstrip(".") == "i don't have enough information yet":
        return True
    if "couldn't get an answer right now" in t and "try asking again" in t:
        return True
    if t.rstrip(".") == "nothing has been discussed yet that i can summarize":
        return True
    return False


def _fallback_response(inp: ResponseInput, max_words: int) -> str:
    """
    Fallback when LLM fails or returns short/refusal.
    Prefer state-rendering (canonical decisions/action_items/issues). No scraping.
    Recap/general → always refusal (let LLM handle; don't try to answer from state/chunks).
    """
    intent = inp.intent or INTENT_GENERAL
    phrases = inp.wake_phrases if inp.wake_phrases else (inp.wake_phrase,)
    request = strip_wake_word(inp.request_text, phrases).strip()

    if intent == INTENT_RECAP:
        return "Nothing has been discussed yet that I can summarize."
    if intent == INTENT_GENERAL:
        # Wording must not match _is_openai_placeholder_fallback, or Hybrid chains to RuleResponder again.
        return limit_words(
            "Having trouble reaching the assistant—try again in a moment.",
            max_words,
        )

    state = inp.state or {}
    if state:
        rendered = _render_state_answer(intent, state, request, max_words)
        if rendered and not _is_fragmentary(rendered):
            return rendered

    chunks = inp.context_chunks or []
    if chunks and intent in ("decision_query", "action_item_query", "person_action_query", "open_question_query"):
        chunk_ans = _build_answer_from_structured_chunks(chunks, intent, request, max_words)
        if not _is_fragmentary(chunk_ans):
            return chunk_ans

    return "I don't have enough information yet."


def _parse_json_voice_response_full(
    raw_content: str,
) -> tuple[str | None, str | None, str | None]:
    """Parse JSON voice response; return ``(spoken, private_thoughts, continuation)``.

    The model is asked to emit ``private_thoughts`` (its short reasoning, never spoken)
    and ``spoken_response`` (the only text Mechus says out loud). Optional
    ``spoken_continuation`` is a second short sentence played after the head when enabled.
    Legacy ``answer`` / ``closing`` keys are also accepted for back-compat.
    """
    t = (raw_content or "").strip()
    if not t:
        return None, None, None
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    parsed = safe_parse_json(t)
    if parsed.get("_parse_failed"):
        return None, None, None
    private_thoughts = str(parsed.get("private_thoughts") or "").strip() or None
    continuation = str(parsed.get("spoken_continuation") or "").strip() or None
    spoken = str(parsed.get("spoken_response") or "").strip()
    if spoken:
        return normalize_text(spoken), private_thoughts, continuation
    ans = str(parsed.get("answer") or "").strip()
    closing = str(parsed.get("closing") or "").strip()
    if not ans:
        return None, private_thoughts, continuation
    if closing:
        return normalize_text(f"{ans} {closing}"), private_thoughts, continuation
    return normalize_text(ans), private_thoughts, continuation


def _parse_json_voice_response(raw_content: str) -> str | None:
    """Return spoken text only from a JSON voice response (back-compat wrapper)."""
    spoken, _, _ = _parse_json_voice_response_full(raw_content)
    return spoken


def _general_wants_substantive_answer(request: str) -> bool:
    """True when a general question needs more than a one-line wake blip."""
    rl = (request or "").strip().lower()
    if not rl:
        return False
    _word_count = len(rl.split())
    _contraction_what_phrases = ("what's ", "whats ")
    if any(rl.startswith(p) or f" {p}" in rl for p in _contraction_what_phrases) and _word_count >= 4:
        return True
    if any(
        p in rl
        for p in (
            "who is",
            "who was",
            "who are",
            "what is",
            "what are",
            "tell me about",
            "explain",
            "describe",
            "how do",
            "how to",
            "how can",
            "how should",
            "how far",
            "how many",
            "how long",
            "how much",
            "how big",
            "how old",
            "distance between",
            "distance from",
            "best way",
            "why does",
            "why do",
            "why is",
            "steps to",
            "tips for",
            "difference between",
            "compare ",
            "find me",
            "find a",
            "find the",
            "search for",
            "look up",
            "research",
            "look into",
            "recommend",
            "where to buy",
            "where can i buy",
            "most popular",
            "trending",
        )
    ):
        return True
    if any(
        sup in rl
        for sup in (
            " largest ",
            " biggest ",
            " smallest ",
            " tallest ",
            " shortest ",
            " highest ",
            " lowest ",
            " heaviest ",
            " lightest ",
            " fastest ",
            " slowest ",
            " oldest ",
            " youngest ",
            " hottest ",
            " coldest ",
            " brightest ",
            " closest ",
            " farthest ",
            " best ",
            " worst ",
        )
    ):
        return True
    return len(rl.split()) >= 7


# Intents where we prefer lower variance + higher token budget (lists, commitments).
_INTENT_LIST_COMPLETENESS = frozenset(
    {
        INTENT_ACTION_ITEMS,
        INTENT_DECISION,
        INTENT_TIMELINE,
        INTENT_OPEN_QUESTIONS,
        INTENT_RISK,
        INTENT_PERSON_ACTION,
    }
)

# Meeting intents that need more than the low-latency 12-word wake cap (recap, lists).
_INTENT_FULL_SPOKEN = _INTENT_LIST_COMPLETENESS | frozenset({INTENT_RECAP})


def intent_wants_extended_spoken(intent: str, request: str) -> bool:
    """True when the reply should be multi-sentence (not the 12-word wake blip)."""
    i = (intent or "").strip().lower()
    if i in _INTENT_FULL_SPOKEN:
        return True
    return i == INTENT_GENERAL and _general_wants_substantive_answer(request)


def _fetch_openai_private_thoughts(
    *,
    api_key: str,
    model: str,
    request: str,
    intent: str,
    context_excerpt: str,
    timeout_s: float,
) -> str | None:
    """Fast parallel thinking call (MECHUS_OPENAI_THINKING_MODEL). Never spoken aloud."""
    system = (
        f"You are {ASSISTANT_NAME}'s internal reasoning layer during a live meeting. "
        "Output ONE JSON object only: {\"private_thoughts\": \"<one short sentence>\"}. "
        "Summarize what the user wants and which facts matter — never write the spoken answer."
    )
    user = (
        f"Intent: {intent or 'general'}\n"
        f"Question: {request.strip()}\n"
        f"Context excerpt:\n{(context_excerpt or '').strip()[:2500]}"
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 80,
        "temperature": 0.2,
        **({"response_format": {"type": "json_object"}} if _mechus_json_ok() else {}),
    }
    from mechus.http_pool import http_request

    try:
        resp = http_request(
            "POST",
            _mechus_chat_url(),
            body=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout_s=max(3.0, min(timeout_s, 8.0)),
        )
        payload = safe_parse_json(resp.data.decode("utf-8", "replace"))
        choices = payload.get("choices") or []
        if not choices:
            return None
        raw = str(choices[0].get("message", {}).get("content", ""))
        parsed = safe_parse_json(raw)
        if isinstance(parsed, dict):
            pt = str(parsed.get("private_thoughts") or "").strip()
            return pt or None
    except Exception as exc:  # noqa: BLE001
        log.debug("Thinking model call failed: %s", exc)
    return None


class OpenAIResponder(Responder):
    """Use OpenAI Chat Completions for response generation. Set MECHUS_LLM_BACKEND=openai and MECHUS_OPENAI_API_KEY."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        max_response_words: int = 80,
        *,
        thinking_model: str | None = None,
        openai_max_tokens: int = 280,
        openai_max_tokens_list: int | None = None,
        timeout_s: float = 22.0,
        prompt_max_chars: int = 12000,
        json_mode: bool = False,
        stream_enabled: bool = False,
        temperature_override: float | None = None,
        temp_meeting: float = 0.28,
        audio_tags_enabled: bool = False,
    ):
        self._api_key = api_key.strip()
        self._model = (model or "gpt-4o-mini").strip()
        _think = (thinking_model or self._model).strip()
        self._thinking_model = _think
        self._use_dual_model = _think != self._model
        self._max_words = max(1, int(max_response_words))
        self._openai_max_tokens = max(64, int(openai_max_tokens))
        _tl = openai_max_tokens_list if openai_max_tokens_list is not None else self._openai_max_tokens
        self._openai_max_tokens_list = max(64, int(_tl))
        self._timeout_s = max(5.0, float(timeout_s))
        self._prompt_max_chars = max(2000, int(prompt_max_chars))
        self._json_mode = bool(json_mode)
        self._stream_enabled = bool(stream_enabled)
        self._temperature_override = temperature_override
        self._temp_meeting = max(0.0, min(2.0, float(temp_meeting)))
        self._audio_tags = bool(audio_tags_enabled)
        self._question_count: dict[str, int] = {}
        self._last_private_thoughts: str | None = None
        self._last_spoken_continuation: str | None = None

    def rephrase_repeat(self, last_spoken: str, *, request_text: str = "") -> str:
        out = _fetch_openai_rephrase(
            api_key=self._api_key,
            model=self._model,
            last_spoken=last_spoken,
            timeout_s=min(12.0, self._timeout_s),
        )
        if out:
            return _limit_canned_spoken(_strip_self_narration(out), self._max_words)
        return rephrase_repeat_spoken(last_spoken)

    def respond(self, inp: ResponseInput) -> str:
        phrases = inp.wake_phrases if inp.wake_phrases else (inp.wake_phrase,)
        request = strip_wake_word(inp.request_text, phrases).strip()

        # Persona-aware overrides. When inp.persona is None or is_default, all values
        # match the OpenAIResponder ctor defaults (single-persona Mechus behavior).
        persona = inp.persona
        if persona is not None:
            _audio_tags = bool(persona.audio_tags)
            _max_words = max(1, int(persona.max_words))
            _temperature_override = (
                persona.temperature if persona.temperature is not None else self._temperature_override
            )
            _persona_tools = set(persona.tools or ())
        else:
            _audio_tags = self._audio_tags
            _max_words = self._max_words
            _temperature_override = self._temperature_override
            _persona_tools = {"realtime"}
        _has_custom_persona = (
            persona is not None
            and not getattr(persona, "is_default", False)
            and bool(getattr(persona, "system_prompt", "").strip())
        )

        # Utterance intent for skip-LLM paths — match RuleResponder (classify request text only).
        # Do not use bundle intent here: a mismatched inp.intent would skip greeting and call the API
        # (short replies like "Hey." then bypass Hybrid placeholder detection).
        route_intent = classify_intent(request)
        intent_early = inp.intent or route_intent
        is_general = route_intent == INTENT_GENERAL
        _substantive_general = (
            intent_early == INTENT_GENERAL and _general_wants_substantive_answer(request)
        )
        self._last_private_thoughts = None
        self._last_spoken_continuation = None
        if not request:
            return ""
        if route_intent == INTENT_STOP:
            return ""
        if route_intent == INTENT_GREETING:
            import random as _random
            if _audio_tags:
                _g_choices = [
                    "[attentive] Yeah, what do you need?",
                    "[ready] I'm here, go ahead.",
                    "[listening] Listening, what's up?",
                    "[ready] Yeah, go ahead.",
                ]
            else:
                _g_choices = [
                    "Yeah, what do you need?",
                    "I'm here, go ahead.",
                    "Listening, what's up?",
                    "Yeah, go ahead.",
                ]
            return _limit_canned_spoken(_random.choice(_g_choices), _max_words)
        if route_intent == INTENT_HELP:
            _help = (
                "[thoughtfully] I've been following along. Ask me for a recap, action items, or what we decided."
                if _audio_tags
                else "I've been following along. Ask me for a recap, action items, or what we decided."
            )
            return _limit_canned_spoken(_help, _max_words)
        if route_intent == INTENT_CLARIFICATION:
            _clar = (
                "[curious] I didn't say anything — were you asking me something?"
                if _audio_tags
                else "I didn't say anything — were you asking me something?"
            )
            return _limit_canned_spoken(_clar, _max_words)
        if route_intent == INTENT_CONFIRM:
            _conf = "[affirmative] Got it." if _audio_tags else "Got it."
            return _limit_canned_spoken(_conf, _max_words)

        if self._stream_enabled:
            log.debug("MECHUS_RESPONSE_STREAM is on; streaming TTS not wired — using blocking completion.")

        # Track how many times this question has been asked
        # Normalize key to catch slight variations of same question
        import re as _re
        _cmd_key = _re.sub(r'\b(the|a|an|in|of|is|what|whats|please|tell me)\b', '', request.strip().lower())
        _cmd_key = _re.sub(r'\s+', ' ', _cmd_key).strip()
        self._question_count[_cmd_key] = self._question_count.get(_cmd_key, 0) + 1
        _ask_count = self._question_count[_cmd_key]

        state = inp.state or {}
        _wake_brevity = bool(state.get("wake_brevity"))
        _needs_full_spoken = intent_early in _INTENT_FULL_SPOKEN or _substantive_general
        # Low-latency FaceTime caps quick factual replies; recap/lists/explanatory Qs need room.
        _use_brevity = _wake_brevity and not _needs_full_spoken
        _wake_max_words = int(state.get("wake_spoken_max_words") or 0) or (
            12 if _use_brevity else 0
        )
        _wake_max_chars = int(state.get("wake_spoken_max_chars") or 0) or (
            40 if _use_brevity else 0
        )
        context = (inp.context_text or "").strip()
        memory = (inp.memory_text or "").strip()

        # ── Parallel realtime fetch ──────────────────────────────────────────
        # Start the weather/search fetch NOW — before context packing — so the
        # network round-trip overlaps with local CPU work instead of blocking after it.
        import threading as _threading
        _rt_result: list[str] = []
        _rt_thread: _threading.Thread | None = None
        _jarvis_lookup = False
        _practical_kw = ["walk or drive", "should i walk", "should i drive", "do we walk", "do we drive", "how do i get", "should i go", "should we go"]
        _is_practical = any(p in request.lower() for p in _practical_kw)
        # Only run the realtime fetch when the persona has the "realtime" tool enabled.
        if not _is_practical and ("realtime" in _persona_tools):
            try:
                from mechus.dialog_memory import is_dialog_continuation
                from mechus.realtime import (
                    build_search_query,
                    fetch_realtime as _fetch_rt_fn,
                    needs_realtime,
                    needs_web_lookup,
                )
                _rl = request.lower()
                _who_lookup = _substantive_general and any(
                    p in _rl for p in ("who is", "who was", "who are", "tell me about")
                )
                _fact_lookup = _substantive_general and any(
                    p in _rl
                    for p in (
                        "how far",
                        "how many",
                        "how long",
                        "how much",
                        "distance",
                        "uranus",
                        "neptune",
                        "planet",
                    )
                )
                _web_lookup = needs_web_lookup(request) and (
                    _substantive_general or is_dialog_continuation(request)
                )
                _jarvis_lookup = bool(_web_lookup)
                if needs_realtime(request) or _who_lookup or _fact_lookup or _web_lookup:
                    _dlg_user = (state.get("dialog_last_user") or "").strip() or None
                    _dlg_mechus = (state.get("dialog_last_mechus") or "").strip() or None
                    _search_q = build_search_query(
                        request,
                        last_user=_dlg_user,
                        last_mechus=_dlg_mechus,
                    )

                    # ── Fast deterministic path (time/timezone) ──────────────
                    # _time_search uses only the server clock — no I/O, no
                    # network. Run it SYNCHRONOUSLY here so we can short-circuit
                    # before spawning a thread or packing context, eliminating
                    # the 4.5-second join wait that was the main latency source.
                    try:
                        from mechus.realtime import _time_search as _ts_fn
                        _ctx_loc = (state.get("last_place_context") or "").strip()
                        _fb_tz = (state.get("fallback_timezone") or "").strip()
                        _sync_time = _ts_fn(
                            request,
                            context_location=_ctx_loc,
                            fallback_timezone=_fb_tz,
                        )
                        if _sync_time:
                            _rt_result.append(_sync_time)
                    except Exception:
                        pass

                    def _rt_worker() -> None:
                        if _rt_result:
                            # Deterministic time result already available; skip
                            # network fetch to avoid redundant/slower results.
                            return
                        try:
                            r = _fetch_rt_fn(
                                request,
                                last_user=_dlg_user,
                                last_mechus=_dlg_mechus,
                                context_location=(state.get("last_place_context") or "").strip(),
                                fallback_timezone=(state.get("fallback_timezone") or "").strip(),
                            )
                            if r:
                                _rt_result.append(r)
                        except Exception as _e:
                            log.debug("Realtime fetch failed: %s", _e)

                    _rt_thread = _threading.Thread(target=_rt_worker, daemon=True)
                    _rt_thread.start()
                    if _web_lookup and _search_q and _search_q.lower() != request.lower():
                        log.info(
                            "Jarvis web lookup enriched: %r -> %r",
                            request[:80],
                            _search_q[:120],
                        )
                    elif _web_lookup or needs_realtime(request):
                        log.info("Jarvis lookup started for: %s", (_search_q or request)[:80])
            except Exception:
                pass

        # Extract speaker names from context to help GPT reference them naturally
        speaker_names: list[str] = []
        raw_ctx_for_names = context + " " + memory
        import re as _re
        # Cap scan — context+memory can be tens of kB; names almost always appear early.
        _name_scan = raw_ctx_for_names[:7000]
        for match in _re.finditer(r"^([A-Za-z][A-Za-z0-9_]{1,20}):\s+\S", _name_scan, _re.MULTILINE):
            name = match.group(1).strip()
            if name not in ("Speaker", "Mechus", "User", "Meta") and name not in speaker_names:
                speaker_names.append(name)


        meta = dict(inp.retrieval_meta or {})
        _requested_owner = extract_requested_owner(request)
        if _requested_owner:
            meta["requested_owner"] = _requested_owner

        meeting_context, pack_stats = pack_grounding_prompt_blocks(
            state=state,
            snippets_text=context,
            memory_aux=memory,
            budget_chars=self._prompt_max_chars,
            owner_filter=_requested_owner,
        )

        if speaker_names:
            meeting_context = f"People in this meeting: {', '.join(speaker_names[:6])}\n\n" + meeting_context
        if _substantive_general:
            meta["substantive_general"] = True
        if pack_stats.get("truncated_snippets") or pack_stats.get("truncated_memory") or pack_stats.get("dropped_memory"):
            log.info(
                "OpenAI prompt packing: intent=%s light_path=%s engine_snip_chars=%s trunc_snip=%s trunc_mem=%s drop_mem=%s final_chars=%s",
                intent_early,
                meta.get("used_light_path"),
                meta.get("engine_snippet_chars"),
                pack_stats.get("truncated_snippets"),
                pack_stats.get("truncated_memory"),
                pack_stats.get("dropped_memory"),
                pack_stats.get("final_chars"),
            )

        if _has_custom_persona:
            # Persona JSON replaces the built-in Mechus identity block, but the meeting
            # awareness rules below (grounding, priority, audio tags, intent appendix,
            # JSON mode) are still appended so retrieval and anti-leak logic still apply.
            system = persona.system_prompt.strip() + "\n\n"
            if persona.refusal_rules:
                system += "Boundaries:\n"
                for rule in persona.refusal_rules:
                    rule = rule.strip()
                    if not rule:
                        continue
                    system += f"- {rule}\n"
                system += "\n"
            system += (
                "Grounding: CANONICAL_STATE is truth for decisions, owners, deadlines — never contradict it for this meeting. "
                "MEETING_SNIPPETS = what was said in THIS meeting. "
                "TEAM_SHARED_CONTEXT = data from PRIOR meetings (not today's live session) — only use when user explicitly asks about past sessions or for historical context. "
                "For plain recaps/summaries, focus on MEETING_SNIPPETS and CANONICAL_STATE only (current meeting). "
                "MEMORY_AUX = dialog continuity only. Missing meeting facts → say you don't have them, don't guess. "
                "General knowledge → answer directly; never refuse those. "
                "Interpret likely speech-recognition errors charitably from context.\n"
            )
        else:
            system = (
                f"You are {ASSISTANT_NAME} — live teammate in this meeting: warm, direct, and natural. "
                "You've been listening. Sound like a real person talking (contractions, real names, 'we' / 'us' / the team). "
                "No AI openers ('Certainly', 'Great question', 'Of course'). No self-narration — answer.\n\n"
                "Grounding: CANONICAL_STATE is truth for decisions, owners, deadlines — never contradict it for this meeting. "
                "MEETING_SNIPPETS = what was said in THIS meeting. "
                "TEAM_SHARED_CONTEXT = data from PRIOR meetings (not today's live session) — only use when user explicitly asks about past sessions or for historical context. "
                "For plain recaps/summaries, focus on MEETING_SNIPPETS and CANONICAL_STATE only (current meeting). "
                "MEMORY_AUX = dialog continuity only. Missing meeting facts → say you don't have them, don't guess. "
                "General knowledge → answer directly; never refuse those. "
                "Interpret likely speech-recognition errors charitably from context.\n\n"
                "Style:\n"
                "- Be SHORT: 1–2 sentences max; 3 only for a full recap. For recaps or action-item asks you may use "
                "a few short sentences plus a tight spoken list at the end ('first… next…').\n"
                "- Spoken voice only — no markdown bullets or section headers; say 'first', 'second', 'next' out loud.\n"
                "- When you know who said something, use their name ('Alex locked in July 25') not passive corporate voice.\n"
                "- Vary how you open; light filler is ok ('yeah', 'so', 'right') when it fits.\n"
                "- Meeting recaps: skip stock phrases about technical difficulties or empty-meeting templates; "
                "if content is thin, say so in one fresh sentence without repeating prior wording.\n"
                "- Opinion asks ('what do you think', 'favorite', 'what would you pick'): answer like a calm colleague — "
                "one clear take in fresh words; if you can't pick, say so briefly then offer one useful angle. "
                "Never reuse the same opening sentence you used on a similar question earlier in this call.\n"
                "- Practical / logistics: think what they actually need to accomplish, not only the literal words. "
                "Common sense: to USE something at a destination they usually must BRING it (car wash → car; "
                "dead battery → can't drive, tow/other; left phone at work → you can't call it from that phone). "
                "If an answer would be nonsensical in the real world, fix it.\n"
                "- Real-time block in the user message: use ONLY that data — never 'check a news site' or 'I don't have access'. "
                "Always start with the source: 'According to <source name>, …'.\n"
                "- Time asks: one sentence, one timezone unless they asked for multiple. "
                "When a real-time block with CURRENT_TIME is present, use ONLY that — never compute from UTC offset. "
                "If no real-time block is present, use SERVER_UTC_NOW below to compute the correct time. "
                "Never invent a time — UTC offset is NOT the time.\n"
                "- Jokes: actually land a punchline; keep it clean.\n"
            )
        # ── Server UTC anchor ─────────────────────────────────────────────────
        # Inject the actual current UTC time so the LLM can always compute a
        # correct local time for any timezone — even if the deterministic realtime
        # lookup failed to produce a CURRENT_TIME block.  This prevents the model
        # from hallucinating times like "5:30 AM IST" (UTC offset confused with clock).
        try:
            from datetime import datetime as _dt, timezone as _tz
            _utc_now = _dt.now(_tz.utc)
            _utc_str = _utc_now.strftime("%Y-%m-%d %H:%M UTC")
            system += f"\nSERVER_UTC_NOW: {_utc_str}. Use this to derive any local time if CURRENT_TIME is absent.\n"
        except Exception:
            pass
        if _substantive_general:
            system += (
                "\nFor this question only: ignore the global one–two sentence cap — "
                "give 2–5 complete spoken sentences with the fact plus brief context.\n"
            )
        # Multilingual: when the speaker used a non-English language, instruct the
        # LLM to reply in that language so ElevenLabs Flash / v3 TTS speaks it back.
        _user_lang = (inp.user_language or "").strip().lower()
        if _user_lang and not is_english_language(_user_lang):
            _lang_name = language_display_name(_user_lang)
            system += (
                f"\nLanguage: the speaker is using {_lang_name} ({normalize_language_code(_user_lang)}). "
                f"Respond in {_lang_name}. Keep proper nouns, acronyms, numbers, "
                f"and any ElevenLabs [audio_tags] in their original form.\n"
            )
        if (inp.state or {}).get("vary_from_last_reply"):
            system += (
                "\nThe user just asked something very similar to their previous question. "
                "Use different wording and structure than your last reply in Recent conversation. "
                "Do NOT repeat template lines like 'I don't really have favorites like people do'.\n"
            )
        if (inp.state or {}).get("meeting_transcript_voice"):
            system += (
                "\nMeeting voice (live call):\n"
                "- Sound like a real teammate on the line — engaged, relaxed, not a narrator or IVR.\n"
                "- Use natural contractions ('we're', 'that's', 'got it') and how people actually talk.\n"
                "- Finish every thought completely — never trail off mid-clause or sound cut off.\n"
                "- Prefer one clean finished sentence over a clipped half-thought plus a dangling 'and…'.\n"
            )
            if _needs_full_spoken:
                if _substantive_general:
                    system += (
                        "- Factual or how-to questions: 2–5 clear sentences with concrete detail.\n"
                    )
                else:
                    system += (
                        "- Recaps and lists: take the space you need — several clear sentences, "
                        "spoken in order ('first…', 'next…'), not a single compressed line.\n"
                    )
            else:
                system += (
                    "- One or two short complete sentences unless they asked for a list.\n"
                )
            system += (
                "- No corporate jargon or assistant-script openers ('Certainly', 'Great question').\n"
                "- Answer only what they asked; skip 'ask me anytime' filler.\n"
            )
            if (inp.state or {}).get("accent_adapt_speech"):
                if not is_english_language(inp.user_language):
                    system += (
                        "\nAccent & clarity (important):\n"
                        "- The speaker may have a strong accent or be code-switching.\n"
                        "- Use short sentences (about 8–12 words), common words, and clear structure.\n"
                        "- Reply in the same language they used — keep it simple and easy to follow.\n"
                        "- State the key fact first, then one short supporting sentence.\n"
                    )
                else:
                    system += (
                        "\nAccent & clarity (important):\n"
                        "- The speaker may have a strong regional accent, be non-native English, "
                        "or be code-switching languages.\n"
                        "- Use short sentences (about 8–12 words), common words, and clear structure.\n"
                        "- Natural contractions are fine — still sound like a person, not a textbook.\n"
                        "- If they asked in English, reply in clear simple English — not louder, just easier to follow.\n"
                        "- State the key fact first, then one short supporting sentence.\n"
                    )
            if not _audio_tags:
                system += "- Never use ElevenLabs [tags] or stage directions.\n"
        elif inp.conversational_mode:
            system += (
                "\nVoice-agent mode (like a live support call):\n"
                "- Sound human and unhurried but concise — one breath, not a paragraph.\n"
                "- If interrupted mid-answer, stop cleanly; the next turn may be a new question.\n"
                "- Acknowledge the person naturally when it fits ('got it', 'sure') without filler spam.\n"
                "- Never sound like a script reader or IVR tree.\n"
            )
        system += (
            "\nPriority:\n"
            "- The ONLY thing you are answering right now is the line that starts with 'Someone just asked:'.\n"
            "- Do NOT answer earlier questions that appear inside MEETING_SNIPPETS (they are context, not the current ask).\n"
            "- If the current ask is ambiguous, ask one short clarifying question instead of guessing.\n"
        )
        if _audio_tags:
            system += (
                "\nElevenLabs v3: at most ONE optional [tag] at the very start when tone fits. "
                "Allowed only: "
                + _AUDIO_TAGS_ONE_LINE
                + ". Nothing else in brackets. Examples: [sighs] Still open on that. "
                "[warmly] Good catch. [hmm] Trickier than it sounds.\n"
            )
        if is_general:
            if _substantive_general:
                system += (
                    "\nThis is a substantive general question — answer from your own knowledge in 2–5 sentences. "
                    "Give useful specifics (role, context, or practical steps). "
                    "Don't say 'I don't have information' unless it is genuinely unknowable. "
                    "Don't qualify with 'this wasn't discussed in the meeting' unless they asked what was said.\n"
                )
            else:
                system += (
                    "\nThis is a general question — answer from your own knowledge, concisely and directly.\n"
                    "Don't qualify with 'this wasn't discussed in the meeting' unless they explicitly asked what was said.\n"
                )

        system += _intent_system_appendix(intent_early, meta)
        _dual_json = self._json_mode and self._use_dual_model
        if self._json_mode:
            if _use_brevity:
                system += (
                    "\nOutput ONE JSON object only, no markdown. Keys: "
                    + (
                        ""
                        if _dual_json
                        else "private_thoughts (one short sentence, never spoken), "
                    )
                    + f"spoken_response (exact words aloud: ONE complete sentence that finishes the thought, "
                    f"max {_wake_max_words} words / {_wake_max_chars} characters — never trail off), "
                    "spoken_continuation (optional second complete sentence with extra detail; omit if not needed). "
                    "No reasoning inside spoken_response.\n"
                )
            else:
                _spoken_hint = (
                    "2–5 sentences for factual/how-to questions; 1–2 sentences for quick asks; "
                    "3–4+ for list/recap"
                )
                system += (
                    "\nOutput ONE JSON object only, no markdown. Keys: "
                    + (
                        ""
                        if _dual_json
                        else "private_thoughts (one short sentence, never spoken), "
                    )
                    + f"spoken_response (exact words aloud; {_spoken_hint}; sound conversational, not formal; tags only if enabled), "
                    "spoken_continuation (optional second sentence with extra detail for factual asks; "
                    "omit if not needed). "
                    "No reasoning inside spoken_response.\n"
                )

        recent_memory = (inp.memory_text or "").strip()
        recent_mechus = ""
        if recent_memory:
            lines = recent_memory.split("\n")
            turn_lines = [ln for ln in lines if ln.startswith("User:") or ln.startswith("Mechus:")]
            tail = turn_lines[-4:]
            if tail:
                recent_mechus = (
                    "\nRecent conversation (use this to understand follow-up questions like 'how close', "
                    "'why', 'what about that' — these refer to the previous topic. Do NOT repeat previous "
                    "answers verbatim, just build on them naturally):\n"
                    + "\n".join(tail)
                    + "\n"
                )

        # Collect parallel realtime fetch result (started before context packing above)
        _realtime_context = ""
        _realtime_attempted = _rt_thread is not None
        _realtime_failed = False
        if _rt_thread is not None:
            _rt_thread.join(timeout=2.5)
            if _rt_result:
                _rt_text = _rt_result[0].strip()
                if _rt_text.startswith(("CURRENT_WEATHER:", "CURRENT_TIME:")):
                    answer = _rt_text.split(":", 1)[1].strip()
                    self._last_private_thoughts = "Answered directly from deterministic real-time data."
                    return limit_words(answer, _max_words)
                if _jarvis_lookup:
                    _realtime_context = (
                        "\nWeb lookup (ground your answer in this; name 1–2 specific options "
                        "when they asked you to find something; MUST cite source):\n"
                        f"{_rt_text}\n"
                    )
                else:
                    _realtime_context = f"\nReal-time data (MUST cite source in response):\n{_rt_text}\n"
                log.info("Realtime data ready for: %s", request[:60])
            else:
                # The query needed real-time data (weather/time/news/score) but
                # we got nothing back (no API key, location not found, network
                # failure). The LLM has no fresh data and will hallucinate a
                # number if we don't stop it. Mark this so the system prompt
                # below tells the model to refuse instead of inventing one.
                _realtime_failed = True
                log.info(
                    "Realtime fetch returned no data; LLM will be told to refuse for: %s",
                    request[:60],
                )

        if _realtime_failed:
            _realtime_context = (
                "\nReal-time data: UNAVAILABLE for this query. "
                "Do NOT invent specific numbers — temperatures, exact times, "
                "scores, prices, dates, or 'currently happening' details. "
                "Briefly tell the user you don't have current data for that "
                "and offer to help differently. Do not pretend to know.\n"
            )

        if _is_practical:
            user = (
                f"{meeting_context}{recent_mechus}\n"
                f"Someone just asked: {request or '(nothing clear)'}\n\n"
                "Think: what does this person physically need to accomplish? Give the answer that actually helps them do it.\n\n"
            )
        else:
            user = (
                f"{meeting_context}{recent_mechus}{_realtime_context}\n"
                f"Someone just asked: {request or '(nothing clear)'}\n\n"
            )
        if self._json_mode:
            if _use_brevity:
                user += (
                    "Return JSON: "
                    + (
                        "{\"spoken_response\": \"<one short sentence, max 12 words>\", "
                        "\"spoken_continuation\": \"<optional one sentence or empty>\"}."
                        if _dual_json
                        else "{\"private_thoughts\": \"...\", "
                        "\"spoken_response\": \"<one short sentence, max 12 words>\", "
                        "\"spoken_continuation\": \"<optional one sentence or empty>\"}."
                    )
                )
            else:
                user += (
                    "Return JSON: "
                    + (
                        "{\"spoken_response\": \"<the exact words Mechus says aloud, with at most one audio tag>\"}."
                        if _dual_json
                        else "{\"private_thoughts\": \"<your one-sentence reasoning, never spoken>\", "
                        "\"spoken_response\": \"<the exact words Mechus says aloud, with at most one audio tag>\"}."
                    )
                )
        else:
            user += "Reply as you would speak in the room — short, human, in character."
        if is_general:
            user += (
                "\n\nIf this is a general knowledge question (places, schools, definitions, simple facts), "
                "answer it like a helpful colleague—do not say the meeting did not cover it unless they "
                "are explicitly asking what people said in the room."
            )

        if is_general:
            temp = 0.45
        elif intent_early == INTENT_RECAP:
            temp = 0.35
        elif _temperature_override is not None:
            temp = float(_temperature_override)
        elif intent_early in _INTENT_LIST_COMPLETENESS:
            temp = self._temp_meeting
        else:
            temp = 0.62

        max_tok = self._openai_max_tokens
        if intent_early in _INTENT_LIST_COMPLETENESS:
            max_tok = max(max_tok, self._openai_max_tokens_list)
        # Tie max_tokens to MECHUS_MAX_RESPONSE_WORDS (or persona.max_words) so the model
        # can't ramble; saves latency and cost.
        _long_form = _needs_full_spoken
        _wmult = 6 if intent_early == INTENT_RECAP else (5 if _long_form else 3)
        _json_extra = 48 if self._json_mode else 0
        if _use_brevity:
            _budget = max(64, _wake_max_words * 4 + _json_extra + 24)
        else:
            _budget = max(96, int(_max_words * _wmult + _json_extra))
        max_tok = min(max_tok, _budget)

        _think_result: list[str | None] = [None]
        _think_thread: threading.Thread | None = None
        if self._use_dual_model:
            _ctx_excerpt = f"{meeting_context}\n{recent_mechus}".strip()

            def _think_worker() -> None:
                _think_result[0] = _fetch_openai_private_thoughts(
                    api_key=self._api_key,
                    model=self._thinking_model,
                    request=request,
                    intent=intent_early,
                    context_excerpt=_ctx_excerpt,
                    timeout_s=self._timeout_s,
                )

            _think_thread = threading.Thread(target=_think_worker, daemon=True, name="mechus-thinking-llm")
            _think_thread.start()

        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tok,
            "temperature": temp,
        }
        if self._json_mode:
            if _mechus_json_ok():
                body["response_format"] = {"type": "json_object"}

        from mechus.http_pool import http_request

        url = _mechus_chat_url()
        body_bytes = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            last_exc: Exception | None = None
            raw = ""
            for attempt in range(2):
                try:
                    resp = http_request(
                        "POST",
                        url,
                        body=body_bytes,
                        headers=headers,
                        timeout_s=self._timeout_s,
                    )
                    raw = resp.data.decode("utf-8", "replace")
                    break
                except (OSError, TimeoutError) as exc:
                    last_exc = exc
                    if attempt == 0:
                        log.info("OpenAI Chat retry after %s", exc)
                        time.sleep(0.35)
                    else:
                        raise
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt == 0:
                        log.info("OpenAI Chat retry after %s", exc)
                        time.sleep(0.35)
                    else:
                        raise
            else:
                if last_exc is not None:
                    raise last_exc
        except Exception as exc:
            log.warning("OpenAI Chat request failed: %s", exc)
            if _think_thread is not None:
                _think_thread.join(timeout=0.1)
            return _fallback_response(inp, _max_words)

        if _think_thread is not None:
            _think_thread.join(timeout=min(5.0, self._timeout_s * 0.35))
            if _think_result[0]:
                self._last_private_thoughts = _think_result[0]

        payload = safe_parse_json(raw)
        if payload.get("_parse_failed"):
            log.warning("OpenAI response parse failed: %s", payload.get("_parse_error"))
            return _fallback_response(inp, _max_words)
        choices = payload.get("choices") or []
        if not choices:
            return _fallback_response(inp, _max_words)
        raw_content = str(choices[0].get("message", {}).get("content", ""))
        if self._json_mode:
            text, _pt, _cont = _parse_json_voice_response_full(raw_content)
            if _pt and not self._last_private_thoughts:
                self._last_private_thoughts = _pt
            if _cont and _use_brevity:
                self._last_spoken_continuation = _apply_wake_spoken_limits(
                    _cont,
                    max_words=_wake_max_words,
                    max_chars=_wake_max_chars,
                    audio_tags=_audio_tags,
                )
            elif _cont and _substantive_general:
                self._last_spoken_continuation = limit_words(
                    clean_spoken_text(_cont, preserve_audio_tags=_audio_tags),
                    max(40, _max_words * 2),
                )
            if not text:
                return _fallback_response(inp, _max_words)
        else:
            text = normalize_text(raw_content)
            if not text:
                return _fallback_response(inp, _max_words)
        text = clean_spoken_text(text, preserve_audio_tags=_audio_tags)
        text = _strip_self_narration(text)
        if intent_early == INTENT_RECAP:
            # User asked us to stop opening every recap with "as I said in the
            # meeting" / "like I mentioned earlier". Strip those prefixes after
            # the LLM generates them as a deterministic backstop.
            text = _strip_recap_backreference_prefix(text)
            if self._last_spoken_continuation:
                self._last_spoken_continuation = _strip_recap_backreference_prefix(
                    self._last_spoken_continuation
                )
        if _use_brevity:
            text = _apply_wake_spoken_limits(
                text,
                max_words=_wake_max_words,
                max_chars=_wake_max_chars,
                audio_tags=_audio_tags,
            )
        else:
            if intent_early == INTENT_RECAP:
                _word_cap = max(100, _max_words * 6)
            elif _long_form:
                _word_cap = max(72, _max_words * 4)
            else:
                _word_cap = _max_words * 2
            text = limit_words(text, _word_cap)
        if is_general:
            if not text or not str(text).strip():
                return _fallback_response(inp, _max_words)
            # Allow short factual answers; only reject empty or garbage fragments.
            if _is_fragmentary(text) and len(text.split()) < 4:
                return _fallback_response(inp, _max_words)
            return text
        text_l = text.lower()
        is_refusal = (
            "don't have enough information" in text_l
            or "not in the context" in text_l
            or "no information" in text_l
            or "doesn't contain" in text_l
            or "does not contain" in text_l
        )
        if len(text.split()) < 4 or is_refusal:
            return _fallback_response(inp, _max_words)

        # Vary response if same question asked multiple times
        if _ask_count > 1:
            import random as _random
            if _ask_count == 2:
                _varied_openers = [
                    "Same as before —",
                    "Like I mentioned —",
                    "Still the same —",
                    "As I said earlier —",
                    "To recap what I said —",
                ]
                text = _random.choice(_varied_openers) + " " + text
            else:
                _short = text[:100] + ("..." if len(text) > 100 else "")
                _short_acks = [
                    f"Still the same answer — {_short}",
                    f"Hasn't changed — {_short}",
                    f"We've covered this a few times — {_short}",
                ]
                text = _random.choice(_short_acks)

        return text


class HybridResponder(Responder):
    def __init__(self, primary: Responder, fallback: Responder):
        self._primary = primary
        self._fallback = fallback
        self._last_private_thoughts: str | None = None
        self._last_spoken_continuation: str | None = None

    def rephrase_repeat(self, last_spoken: str, *, request_text: str = "") -> str:
        fn = getattr(self._primary, "rephrase_repeat", None)
        if callable(fn):
            try:
                out = fn(last_spoken, request_text=request_text)
                if out and out.strip():
                    return out.strip()
            except Exception:
                pass
        fn = getattr(self._fallback, "rephrase_repeat", None)
        if callable(fn):
            try:
                return fn(last_spoken, request_text=request_text)
            except Exception:
                pass
        return rephrase_repeat_spoken(last_spoken)

    def respond(self, inp: ResponseInput) -> str:
        self._last_private_thoughts = None
        self._last_spoken_continuation = None
        text = normalize_text(self._primary.respond(inp))
        self._last_private_thoughts = getattr(self._primary, "_last_private_thoughts", None)
        self._last_spoken_continuation = getattr(self._primary, "_last_spoken_continuation", None)
        if text and not _is_openai_placeholder_fallback(text):
            return text
        text = normalize_text(self._fallback.respond(inp))
        if not self._last_private_thoughts:
            self._last_private_thoughts = getattr(self._fallback, "_last_private_thoughts", None)
        if not self._last_spoken_continuation:
            self._last_spoken_continuation = getattr(self._fallback, "_last_spoken_continuation", None)
        return text
