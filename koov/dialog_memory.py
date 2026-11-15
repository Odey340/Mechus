"""
Lightweight dialog anchors and salient-term carryover for retrieval query expansion.

Intent classification stays on the raw user line; expanded queries improve BM25 / memory
retrieval for vague follow-ups ("what about that") without LLM calls.
"""

from __future__ import annotations

import re
from typing import Iterable

# Common English stopwords for vague / keyword-density heuristics (small set, not exhaustive).
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "so", "to", "of", "in", "on", "for",
    "at", "by", "with", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "there", "here", "what", "which",
    "who", "whom", "whose", "how", "when", "where", "why", "can", "could", "would",
    "should", "may", "might", "must", "do", "does", "did", "have", "has", "had",
    "we", "you", "i", "me", "my", "your", "our", "their", "they", "them", "he", "she",
    "his", "her", "just", "like", "about", "from", "into", "over", "after", "before",
    "again", "also", "too", "very", "more", "some", "any", "no", "not", "only", "same",
    "then", "than", "well", "will", "okay", "ok", "yeah", "yes", "no", "um", "uh",
})

_FOLLOWUP_MARKERS: tuple[str, ...] = (
    "what about",
    "how about",
    "what of",
    "same thing",
    "same as",
    "that one",
    "this one",
    "the first point",
    "the first one",
    "the first two",
    "the last one",
    "the last time",
    "earlier",
    "earlier one",
    "before that",
    "you said",
    "you told",
    "you told me",
    "i thought you",
    "didn't you say",
    "didnt you say",
    "your answer",
    "that answer",
    "what you meant",
    "what you mean",
    "we said",
    "mentioned",
    "explain that again",
    "that again",
    "say again",
    "say that again",
    "repeat yourself",
    "repeat that",
    "can you repeat",
    "could you repeat",
    "what did you say",
    "what was that",
)

_PRIOR_EXCHANGE_RE = re.compile(
    r"\b("
    r"i\s+thought\s+you|you\s+told\s+me|you\s+said|didn'?t\s+you\s+say|"
    r"your\s+answer|that\s+answer|what\s+you\s+meant|what\s+you\s+mean|"
    r"wait\s+you|hold\s+on\s+you|but\s+you\s+said|you\s+just\s+said"
    r")\b",
    re.I,
)

# Directed back at Koov while continuing the current thread (often after a wake phrase).
_DIRECTED_CONTINUATION_RE = re.compile(
    r"\b("
    r"can you find|could you find|find me|find some|pull that up|pull it up|"
    r"pull that|look that up|look it up|show me|get me|search for|"
    r"is that possible|would that work|can you do that|could you do that|"
    r"for me\b|what did you just hear|just hear|you just hear"
    r")\b",
    re.I,
)

_REPEAT_REQUEST_RE = re.compile(
    r"\b("
    r"repeat(?:\s+yourself|\s+that|\s+what\s+you\s+said)?|"
    r"(?:can|could)\s+you\s+(?:please\s+)?(?:repeat|say\s+that\s+again)|"
    r"say\s+(?:that\s+)?again|"
    r"run\s+that\s+back|"
    r"what\s+did\s+you\s+(?:just\s+)?say|"
    r"what\s+was\s+that"
    r")\b",
    re.I,
)

_WHAT_DID_MEAN = re.compile(
    r"^\s*what\s+did\s+(he|she|they|it|we|you)\s+mean\b",
    re.I,
)

_PRONOUN_DEM: frozenset[str] = frozenset({
    "that", "this", "it", "its", "those", "them", "there", "here", "same", "again",
})

# Single-token TitleCase / ALLCAPS picks to skip (question words, generic imperatives).
_SALIENT_SKIP_SINGLE_WORDS: frozenset[str] = frozenset({
    "what", "who", "whom", "when", "where", "why", "how", "which",
    "tell", "explain", "describe", "summarize", "list", "give", "show", "say", "ask",
    "can", "could", "would", "should", "might", "must",
    "more", "about", "thing", "things", "stuff", "ones", "one",
    "the", "this", "these", "those", "there", "here", "some", "such",
})

# Generic hyphenated meeting filler (not useful as retrieval anchors).
_SALIENT_NOISE_HYPHEN: frozenset[str] = frozenset({
    "follow-ups", "follow-up", "followups", "check-in", "check-ins", "round-up", "round-ups",
    "heads-up", "touch-base", "touch-bases",
})


def _salient_keep(term: str) -> bool:
    """Drop tokens that add noise to BM25 / memory retrieval."""
    s = (term or "").strip()
    if len(s) < 2:
        return False
    key = s.lower()
    if " " not in s and key in _SALIENT_SKIP_SINGLE_WORDS:
        return False
    if "-" in s and key in _SALIENT_NOISE_HYPHEN:
        return False
    return True


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", s.lower())


_TRANSCRIPT_BACKCHANNELS: frozenset[str] = frozenset({
    "mm",
    "mmm",
    "mm-hmm",
    "mm hmm",
    "mhm",
    "uh-huh",
    "uh huh",
    "uh",
    "um",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "right",
    "sure",
    "got it",
    "i see",
})


def is_stt_noise_annotation(text: str) -> bool:
    """Bracketed STT labels like ``[water running]`` — not speech to Koov."""
    t = (text or "").strip()
    return len(t) >= 3 and t.startswith("[") and t.endswith("]")


def is_transcript_backchannel(text: str) -> bool:
    """Short acknowledgments / filler — not directed follow-up questions."""
    t = re.sub(r"[^\w\s'-]", "", (text or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    return t in _TRANSCRIPT_BACKCHANNELS


_TOPIC_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "to", "of", "in", "on", "at", "for",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has",
    "had", "will", "would", "could", "should", "can", "may", "might", "must", "shall",
    "i", "you", "we", "they", "he", "she", "it", "me", "my", "your", "our", "their",
    "what", "whats", "which", "who", "whom", "how", "when", "where", "why",
    "hey", "hi", "hello", "koov", "koo", "kuv", "coop", "cool", "uh", "um", "like",
    "tell", "about", "give", "could", "please", "right", "now", "little", "bit",
    "would", "pick", "choose", "favorite", "favourite", "think", "that", "this",
})


def _topic_tokens(text: str) -> set[str]:
    t = re.sub(r"[^\w\s'-]", " ", (text or "").lower())
    return {w for w in t.split() if len(w) > 2 and w not in _TOPIC_STOPWORDS}


def is_similar_topic_followup(new_text: str, prior_text: str) -> bool:
    """True when the new question revisits the same topic as the prior user turn."""
    if not (new_text or "").strip() or not (prior_text or "").strip():
        return False
    nl = (new_text or "").strip().lower()
    pl = (prior_text or "").strip().lower()
    if nl == pl:
        return True
    fav_new = re.search(r"favou?rite\s+(\w+)", nl)
    fav_prior = re.search(r"favou?rite\s+(\w+)", pl)
    if fav_new and fav_prior:
        return fav_new.group(1) == fav_prior.group(1)
    if re.search(r"\b(pick|choose|would you)\b", nl) and re.search(
        r"\b(favou?rite|think|opinion|your take)\b", pl
    ):
        return True
    a = _topic_tokens(nl)
    b = _topic_tokens(pl)
    if not a or not b:
        return False
    overlap = len(a & b) / float(min(len(a), len(b)))
    if overlap >= 0.55:
        return True
    for topic in (
        "action", "items", "assignment", "assignments", "project", "recap", "summary",
    ):
        if topic in nl and topic in pl:
            return True
    return False


def is_repeat_request(text: str) -> bool:
    """User wants the last Koov spoken line replayed (no new LLM call needed)."""
    t = (text or "").strip()
    if not t:
        return False
    if _REPEAT_REQUEST_RE.search(t):
        return True
    low = t.lower()
    return any(
        p in low
        for p in (
            "repeat yourself",
            "repeat that",
            "say that again",
            "can you repeat",
            "could you repeat",
            "can you say that again",
            "could you say that again",
            "what did you just say",
            "run that back",
        )
    )


# Room chat about Koov's voice/behavior — not a directed question to Koov.
_ROOM_KOOV_COMMENTARY_RE = re.compile(
    r"\b("
    r"sound(?:s)?\s+lazy|sounds?\s+tired|sound(?:s)?\s+off|voice\s+change|"
    r"changed\s+your\s+voice|why\s+does\s+it\s+sound|didn'?t\s+ask\s+you|"
    r"i\s+didn'?t\s+ask|talking\s+too\s+fast|too\s+fast|too\s+slow|"
    r"what\s+the\s+fuck\s+did\s+your"
    r")\b",
    re.I,
)


def is_room_koov_commentary(text: str) -> bool:
    """Humans venting about Koov in the room — must not trigger a wakeless reply."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_ROOM_KOOV_COMMENTARY_RE.search(t))


_INTERRUPT_FOLLOWUP_RE = re.compile(
    r"\b("
    r"shut\s+up|be\s+quiet|stop\s+talking|stop\s+speaking|"
    r"stop\s+it|hold\s+on|hang\s+on|quiet\s+down|enough\s+already|"
    r"no\s+wait|wait\s+wait|that'?s\s+wrong|thats\s+wrong|not\s+right|"
    r"wrong|scratch\s+that|ignore\s+that|never\s+mind\s+that|"
    r"actually\s+no|not\s+what\s+i\s+meant"
    r")\b",
    re.I,
)


def is_interrupt_request(text: str) -> bool:
    """User wants Koov to stop speaking — not a new question."""
    t = (text or "").strip()
    if not t:
        return False
    low = re.sub(r"\s+", " ", t.lower()).strip(" .,!?")
    if low in (
        "stop", "stop it", "hold on", "hang on", "wait", "shut up", "quiet",
        "wrong", "that's wrong", "thats wrong", "no wait", "wait wait",
        "scratch that", "not right", "never mind",
    ):
        return True
    if _INTERRUPT_FOLLOWUP_RE.search(low):
        return True
    if low.startswith("stop ") and not any(
        w in low for w in ("stop the meeting", "stop rollout", "stop by", "bus stop")
    ):
        return True
    toks = low.split()
    if toks and toks[-1] in ("stop", "wait", "quiet", "hush") and len(toks) <= 4:
        return True
    return False


def is_lone_proper_name(text: str) -> bool:
    """Single capitalized name (e.g. transcript label 'Jeremy') — not a Koov follow-up."""
    w = _words(text)
    if len(w) != 1:
        return False
    token = (text or "").strip().split()[0]
    return (
        token[:1].isupper()
        and token.isalpha()
        and w[0].lower() not in _PRONOUN_DEM
        and w[0].lower() not in _STOPWORDS
    )


def is_dialog_continuation(text: str) -> bool:
    """True when the user is continuing the active Koov thread (find/pull/that/for me)."""
    t = (text or "").strip()
    if not t:
        return False
    if _DIRECTED_CONTINUATION_RE.search(t):
        return True
    low = t.lower()
    if " for me" in low or low.endswith(" for me"):
        return True
    w = _words(t)
    if len(w) <= 8 and any(x in _PRONOUN_DEM for x in w):
        if any(
            x in low
            for x in (
                "find",
                "pull",
                "look",
                "show",
                "get",
                "search",
                "possible",
                "monitor",
            )
        ):
            return True
    return False


def is_recent_auditory_query(text: str) -> bool:
    """User wants what was literally said in the last few seconds (not a topic recap)."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if re.search(r"last\s+\d+\s*seconds?", low):
        return True
    return any(
        p in low
        for p in (
            "what did you just hear",
            "what did you hear",
            "what do you hear",
            "just hear",
            "you just hear",
            "in the last few seconds",
        )
    )


def is_prior_exchange_reference(text: str) -> bool:
    """True when the user is referring back to Koov's last reply (not a new topic)."""
    t = (text or "").strip()
    if not t:
        return False
    if _PRIOR_EXCHANGE_RE.search(t):
        return True
    low = t.lower()
    for m in ("you told", "you said", "i thought you", "your answer"):
        if m in low:
            return True
    return False


def is_vague_followup(text: str) -> bool:
    """Heuristic: short / anaphoric utterances that need dialog anchors for retrieval."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    w = _words(t)
    if not w:
        return True
    if is_lone_proper_name(t):
        return False

    if _WHAT_DID_MEAN.match(t):
        return True

    for m in _FOLLOWUP_MARKERS:
        if m in low:
            return True

    if is_dialog_continuation(t):
        return True

    if len(w) <= 5 or len(t) < 28:
        pron_hits = sum(1 for x in w if x in _PRONOUN_DEM)
        if pron_hits >= 1 and len(w) <= 6:
            return True
        if pron_hits >= 2:
            return True
        non_stop = [x for x in w if x not in _STOPWORDS and len(x) > 1]
        if len(w) >= 3 and len(non_stop) <= max(1, len(w) // 4):
            return True
        if len(w) <= 4 and len(non_stop) <= 2:
            return True

    return False


# Participant reactions in the room — not directed at Koov (no wake phrase).
_MEETING_SIDE_COMMENT_RE = re.compile(
    r"^\s*(?:it'?s|that'?s|this(?:'s| is)|sounds|looks|seems|pretty|really)\s+"
    r"(?:good|bad|fine|great|dead|fast|slow|enough|clear|right|wrong|cool|nice|"
    r"ok|okay|alright|awesome|perfect|fine)\b",
    re.I,
)

# First token after normalize_for_wake — common meeting chat, not assistant follow-ups.
_MEETING_CHAT_AUX_STARTERS: frozenset[str] = frozenset({
    "is", "are", "am", "was", "were", "will", "have", "has", "had",
    "its", "it", "that", "this", "there", "thats", "heres",
})

_WH_FOLLOWUP_STARTERS: frozenset[str] = frozenset({
    "what", "how", "who", "why", "when", "where", "which", "whose",
    "whats", "hows", "whos", "wheres", "whens",
})

_LEADING_FOLLOWUP_NOISE: frozenset[str] = frozenset({
    "hey", "cool", "okay", "yeah", "yep", "so", "um", "uh", "well", "hi",
    "google", "gosh",
})

_WAKE_MISHEAR_TOKENS: frozenset[str] = frozenset({
    "koo", "koob", "koop", "koof", "coop", "couv", "kuv", "hoov", "coov",
})


def _strip_leading_followup_noise(norm: str) -> str:
    words = (norm or "").split()
    while words and words[0] in _LEADING_FOLLOWUP_NOISE:
        words = words[1:]
    while words and words[0] in _WAKE_MISHEAR_TOKENS:
        words = words[1:]
    return " ".join(words)


def _has_anaphoric_reference(norm: str) -> bool:
    """Pronoun/demo reference back to the prior Koov exchange (not a new standalone topic)."""
    return any(x in _PRONOUN_DEM for x in _words(norm))


def is_meeting_side_comment(text: str) -> bool:
    """Short reactions between humans (e.g. \"it's good\") — not Koov follow-ups."""
    t = re.sub(r"[^\w\s'-]", "", (text or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    if _MEETING_SIDE_COMMENT_RE.match(t):
        return True
    w = _words(t)
    if len(w) <= 3 and w:
        if w[0] in ("yeah", "yep", "nope", "nah", "sure", "right", "okay", "ok"):
            if w[-1] in (
                "good", "bad", "fine", "great", "cool", "nice", "dead",
                "enough", "fast", "slow", "clear",
            ):
                return True
    return False


def is_directed_koov_command_without_wake(text: str) -> bool:
    """
    Clear imperative or question directed at Koov right after it spoke — no wake repeat.

    Allows "tell me about the solar system" in the follow-up window after hear-me / a reply.
    Rejects short room banter ("it's cool", "yeah sure").
    """
    t = (text or "").strip()
    if not t or len(t) < 8:
        return False
    if (
        is_stt_noise_annotation(t)
        or is_transcript_backchannel(t)
        or is_meeting_side_comment(t)
    ):
        return False
    from koov.wake_detector import normalize_for_wake, remainder_qualifies_as_wake_command

    norm = _strip_leading_followup_noise(normalize_for_wake(t))
    if not norm:
        return False
    if any(
        p in norm
        for p in (
            " anyone ",
            " our team ",
            " the team ",
            " we should ",
            " we need ",
            " we have to ",
        )
    ):
        return False
    words = norm.split()
    if len(words) < 3:
        return False
    first = words[0].lower()
    if first in _MEETING_CHAT_AUX_STARTERS:
        return False
    if first == "stop" and len(words) > 1:
        return False
    if re.search(r"\b(?:time|date)\b", norm) and first in ("what", "when"):
        return True
    if first in ("summarize", "recap") or "summarize the meeting" in norm or "recap the meeting" in norm:
        return False
    if first in ("tell", "explain", "describe", "list", "name"):
        return True
    if first in ("can", "could", "would", "will") and len(words) >= 3:
        return True
    if first in ("what", "who", "how", "when", "where", "which"):
        if not (_has_anaphoric_reference(norm) and len(words) >= 3):
            return False
        dem = frozenset({"those", "these", "that", "this"})
        if any(w in dem for w in words):
            return is_prior_exchange_reference(t) or is_dialog_continuation(t)
        if len(words) <= 6:
            return True
        return is_prior_exchange_reference(t) or is_dialog_continuation(t)
    return False


def qualifies_as_transcript_followup_without_wake(text: str) -> bool:
    """
    True when a recent Koov dialog exists and this line is clearly directed back
    at the assistant — without repeating the wake phrase.

    Stricter than ``is_vague_followup``: open-meeting banter must not wake Koov.
    """
    t = (text or "").strip()
    if not t or len(t) < 3:
        return False
    if is_repeat_request(t):
        return True
    if is_interrupt_request(t):
        return False
    if is_room_koov_commentary(t):
        return False
    if (
        is_stt_noise_annotation(t)
        or is_transcript_backchannel(t)
        or is_meeting_side_comment(t)
    ):
        return False
    if is_lone_proper_name(t):
        return False
    low = t.lower()
    for m in _FOLLOWUP_MARKERS:
        if m in low:
            return True
    if _WHAT_DID_MEAN.match(t):
        return True
    if is_prior_exchange_reference(t):
        return True
    if is_dialog_continuation(t):
        return True
    if is_recent_auditory_query(t):
        return True
    if is_directed_koov_command_without_wake(t):
        return True
    from koov.wake_detector import normalize_for_wake

    norm = normalize_for_wake(t)
    if not norm:
        return False
    if "summarize the meeting" in norm or "recap the meeting" in norm or norm in (
        "summarize meeting",
        "summarize everything",
        "recap meeting",
    ):
        return False
    norm = _strip_leading_followup_noise(norm)
    if not norm:
        return False
    if norm in ("how come", "how so"):
        return True
    first = norm.split()[0].lower() if norm.split() else ""
    if first in _MEETING_CHAT_AUX_STARTERS and not is_dialog_continuation(t):
        return False
    # "stop being mad at me" is room chat; Koov stop is usually just "stop" / "stop koov".
    if first == "stop" and len(norm.split()) > 2:
        return False
    # WH-questions without a wake only count when they clearly continue the Koov thread
    # (pronoun back-reference), not when humans ask each other new topics in the room.
    if first in _WH_FOLLOWUP_STARTERS:
        words = norm.split()
        if not (_has_anaphoric_reference(norm) and len(words) >= 3):
            return False
        dem = frozenset({"those", "these", "that", "this"})
        if any(w in dem for w in words):
            return is_prior_exchange_reference(t) or is_dialog_continuation(t)
        if len(words) <= 6:
            return True
        return is_prior_exchange_reference(t) or is_dialog_continuation(t)
    if "why" in norm.split() and len(norm.split()) >= 2:
        if is_prior_exchange_reference(t):
            return True
        if re.search(r"\b(you|your|koov)\b", norm):
            return True
        return False
    if norm in ("what are those", "what are these", "what is that", "what is this"):
        return False
    return False


def extract_salient_terms(text: str, *, max_terms: int = 8) -> list[str]:
    """
    Lightweight entity-like tokens: quoted phrases, TitleCase / ALLCAPS tokens,
    and long alphanumeric tokens (e.g. product codes). Deduped, order preserved.
    """
    t = (text or "").strip()
    if not t:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if len(s) < 2:
            return
        key = s.lower()
        if key in seen:
            return
        # Drop lone interrogatives / generic verbs from TitleCase (not useful as entities).
        if " " not in s and key in _SALIENT_SKIP_SINGLE_WORDS:
            return
        seen.add(key)
        out.append(s)

    for m in re.finditer(r'["\']([^"\']{2,64})["\']', t):
        _add(m.group(1).strip())

    for m in re.finditer(r"\b([A-Z][a-z]{2,})\b", t):
        _add(m.group(1))

    for m in re.finditer(r"\b[A-Z][a-z]+(?:[A-Z][a-z]*)+\b", t):
        _add(m.group(0))

    for m in re.finditer(r"\b([A-Z]{2,24})\b", t):
        tok = m.group(1)
        if tok.lower() not in _STOPWORDS and tok.lower() not in _SALIENT_SKIP_SINGLE_WORDS:
            _add(tok)

    for m in re.finditer(r"\b([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\b", t):
        frag = m.group(1)
        if frag.lower() in _SALIENT_NOISE_HYPHEN:
            continue
        _add(frag)

    for m in re.finditer(r"\b[A-Za-z0-9]*\d[A-Za-z0-9\-]{3,}\b", t):
        _add(m.group(0))

    for m in re.finditer(r"\b([A-Za-z][A-Za-z]+(?:\s+\d{1,4})+)\b", t):
        frag = m.group(1).strip()
        if re.search(r"\d", frag):
            _add(frag)

    for m in re.finditer(r"\b([A-Z]{1,4}\d{1,3}[A-Z]?)\b", t):
        tok = m.group(1)
        if len(tok) >= 3 and re.search(r"\d", tok):
            _add(tok)

    out = [x for x in out if _salient_keep(x)]
    return out[:max_terms]


def merge_salient(
    rolling: Iterable[str],
    new_terms: Iterable[str],
    *,
    max_total: int = 12,
) -> list[str]:
    """FIFO-friendly merge: prefer newer terms, then prior rolling, dedupe case-insensitively."""
    seen: set[str] = set()
    out: list[str] = []
    for group in (new_terms, rolling):
        for x in group:
            s = (x or "").strip()
            if not s or not _salient_keep(s):
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
            if len(out) >= max_total:
                return out
    return out


def _terms_not_in_original(original_lower: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t.lower() not in original_lower]


def build_retrieval_query(
    original: str,
    *,
    last_user: str | None,
    last_koov: str | None,
    salient_terms: list[str],
    max_chars: int = 400,
) -> str:
    """
    Build a bounded retrieval string: user line plus optional Context user / Salient lines.
    """
    orig = (original or "").strip()
    if not orig:
        return ""

    ol = orig.lower()
    extra = [t for t in _terms_not_in_original(ol, salient_terms) if _salient_keep(t)]
    vague = is_vague_followup(orig)
    prior = is_prior_exchange_reference(orig)
    cont = is_dialog_continuation(orig)
    need_context = vague or prior or cont or bool(extra)

    if not need_context:
        return orig[:max_chars]

    lines: list[str] = [orig]
    lu = (last_user or "").strip()
    lk = (last_koov or "").strip()
    if lu and (vague or prior or cont):
        lines.append(f"Context user: {lu[:200]}")
    if lk and (prior or vague or cont):
        lines.append(f"Context koov: {lk[:280]}")

    sal_parts: list[str] = list(extra[:12])
    if (vague or prior or cont) and last_koov and not sal_parts:
        lk_terms = extract_salient_terms(last_koov, max_terms=4)
        for x in lk_terms:
            if x.lower() not in ol:
                sal_parts.append(x)
                break

    if sal_parts:
        lines.append("Salient: " + ", ".join(sal_parts[:12]))

    s = "\n".join(lines).strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."
