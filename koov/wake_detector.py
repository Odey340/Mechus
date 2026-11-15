"""
Text-level wake-phrase detection, normalization, and stripping.

This module operates purely on transcribed text — it does NOT do audio processing.
Audio-level detection (Vosk, openWakeWord) lives in koov/wake.py.

Pipeline:
    raw_transcript -> normalize_for_wake() -> detect_wake() -> strip_wake_phrase()
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

from koov.brand import MECHUS_STT_ALIASES, join_split_wake_word

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_for_wake(text: str) -> str:
    """
    Lowercase, remove punctuation, collapse whitespace.

    "Computer, can you hear me?" → "computer can you hear me"
    """
    if not text:
        return ""
    # Normalize unicode (smart quotes, em-dashes etc.)
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    # Collapse common contractions before stripping punctuation — otherwise "what's" becomes "what s"
    text = re.sub(r"([a-z])['\u2019]([a-z])", r"\1\2", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    text = expand_stt_koov_address_mishears(text)
    text = join_split_wake_word(text)  # "mech us" -> "mechus"
    return text


# ---------------------------------------------------------------------------
# Fuzzy / Levenshtein helpers
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[lb]


# Levenshtein neighbors of wake tokens that are common English words — matching them
# caused transcript false wakes on e.g. "join too" ("too" is 1 edit from "koo", and
# distance 2 from "koov"/"coop" with default max_dist).
_FUZZY_FALSE_FRIENDS: dict[str, frozenset[str]] = {
    "koo": frozenset({"too", "cool", "know", "move", "room"}),
    "koov": frozenset({"too", "cool", "know", "move", "room", "zoom"}),
    "coop": frozenset({"too", "cool", "know", "loop"}),
}

# Zoom/Whisper often emit these instead of "koov". "kuv" is *two* edits from "koov"
# (3↔4 letters) so KOOV_WAKE_TRANSCRIPT_MAX_DIST=1 fuzzy match misses it — alias instead.
_KOOV_STT_ALIASES: frozenset[str] = frozenset(
    {
        "kuv",
        "hoov",
        "coov",
        "coop",
        "koop",
        "koob",
        "koof",
        "koo",
        "couv",
        "cuv",
        "goov",
        "kooth",
        "cub",
    }
)

# STT often writes "koov" as "cool" when addressing the agent ("hey cool, tell me…").
_COOL_ADDRESS_LEADERS: frozenset[str] = frozenset(
    {"hey", "hi", "ok", "okay", "yeah", "yo", "alright", "right", "all"}
)
_COOL_COMMAND_NEXT: frozenset[str] = frozenset(
    {
        "tell", "can", "could", "would", "will", "what", "who", "how", "when", "where",
        "which", "summarize", "recap", "stop", "hold", "wait", "hang", "give", "explain",
        "describe", "list", "name", "hear", "help", "do", "did", "does", "is", "are",
    }
)
_COOL_PRAISE_CONTEXT: frozenset[str] = frozenset(
    {"is", "was", "are", "s", "thats", "that", "its", "it", "so", "really", "pretty", "very", "super"}
)


def expand_stt_koov_address_mishears(normalized: str) -> str:
    """
    Rewrite contextual STT mishears so wake detection sees ``koov``.

    Only maps ``cool`` → ``koov`` when it reads as an address ("hey cool, tell me…"),
    not praise ("that's cool", "so cool").
    """
    if not normalized:
        return ""
    words = normalized.split()
    if not words:
        return normalized
    out: list[str] = []
    for i, w in enumerate(words):
        if w != "cool":
            out.append(w)
            continue
        prev = words[i - 1] if i > 0 else ""
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if prev in _COOL_PRAISE_CONTEXT or nxt in _COOL_PRAISE_CONTEXT:
            out.append(w)
            continue
        if prev in ("that", "thats", "its", "so"):
            out.append(w)
            continue
        if prev in _COOL_ADDRESS_LEADERS or nxt in _COOL_COMMAND_NEXT:
            out.append("koov")
            continue
        if i == len(words) - 1 and prev in _COOL_ADDRESS_LEADERS:
            out.append("koov")
            continue
        out.append(w)
    return " ".join(out)


def line_has_explicit_wake_token(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> bool:
    """True when the line itself contains a wake token (not only via VAD merge with prior chunk)."""
    if not normalized_text or not wake_phrases:
        return False
    words = normalized_text.split()
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        if _phrase_in_words(words, phrase_words, max_dist) >= 0:
            return True
    return False


def _word_matches_phrase_word(word: str, phrase_word: str, max_dist: int = 2) -> bool:
    """True if `word` is within `max_dist` edits of `phrase_word`."""
    if word == phrase_word:
        return True
    if phrase_word == "koov" and word in _KOOV_STT_ALIASES:
        return True
    if phrase_word == "mechus" and word in MECHUS_STT_ALIASES:
        return True
    # Short words are too ambiguous for fuzzy matching — require exact match.
    # This prevents "no" (len 2) from matching "koo" (dist=2).
    if len(word) < 3 or len(phrase_word) < 3:
        return False
    # 3-letter wake tokens (e.g. "koo") with max_dist=2 falsely match common words
    # like "know" (also dist=2), which makes transcript-wake fire on "you know how…".
    # Keep fuzzy STT tolerance for 1 edit (koo/coo) but not 2.
    eff = max_dist
    if len(phrase_word) <= 3:
        eff = min(1, max_dist)
    # 4-letter single-word wakes (e.g. "koov") with max_dist=2 match "know" (dist=2).
    # Same-length 4↔4: allow only one edit so STT slop ("coov") still matches.
    if len(phrase_word) == 4 and len(word) == 4:
        eff = min(1, max_dist)
    # Skip if lengths differ too much (fast path)
    if abs(len(word) - len(phrase_word)) > eff:
        return False
    if phrase_word in _FUZZY_FALSE_FRIENDS and word in _FUZZY_FALSE_FRIENDS[phrase_word]:
        return False
    return _levenshtein(word, phrase_word) <= eff


def _phrase_in_words(
    words: list[str],
    phrase_words: list[str],
    max_dist: int = 2,
) -> int:
    """
    Return the start index (in `words`) where `phrase_words` matches with fuzzy
    Levenshtein ≤ max_dist per word, or -1 if not found.
    """
    pw_len = len(phrase_words)
    if pw_len == 0:
        return 0
    for i in range(len(words) - pw_len + 1):
        if all(
            _word_matches_phrase_word(words[i + j], phrase_words[j], max_dist)
            for j in range(pw_len)
        ):
            return i
    return -1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_wake(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> bool:
    """
    Return True if any wake phrase is found in the normalized transcript
    using fuzzy Levenshtein matching (≤ max_dist edits per word).

    normalized_text must already be lowercase with punctuation removed.

    Examples:
        detect_wake("computer can you hear me", ("computer", "koov"))  → True
        detect_wake("kuv summarize meeting", ("koov", "computer"))      → True  (levenshtein=1)
        detect_wake("coop summarize meeting", ("koov", "computer"))     → True  (levenshtein=2)
    """
    if not normalized_text or not wake_phrases:
        return False
    words = normalized_text.split()
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        if _phrase_in_words(words, phrase_words, max_dist) >= 0:
            return True
    return False


def _last_wake_span(
    words: list[str],
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int,
) -> tuple[int, int, str | None]:
    """Return (start_word_index, phrase_word_len, matched_phrase) for the last wake in ``words``."""
    last_idx = -1
    last_len = 0
    last_phrase: str | None = None
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        pw_len = len(phrase_words)
        if pw_len == 0 or len(words) < pw_len:
            continue
        for start in range(len(words) - pw_len, -1, -1):
            if _phrase_in_words(words[start:], phrase_words, max_dist) == 0:
                if start > last_idx:
                    last_idx = start
                    last_len = pw_len
                    last_phrase = phrase
                break
    return last_idx, last_len, last_phrase


def strip_wake_phrase(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> str:
    """
    Remove the first occurrence of any wake phrase from the normalized text
    and return the remainder (the command).

    "computer can you hear me" → "can you hear me"
    "kuv summarize meeting"    → "summarize meeting"
    """
    if not normalized_text or not wake_phrases:
        return normalized_text
    words = normalized_text.split()
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        idx = _phrase_in_words(words, phrase_words, max_dist)
        if idx >= 0:
            remaining = words[:idx] + words[idx + len(phrase_words):]
            return " ".join(remaining).strip()
    return normalized_text


def preamble_before_wake_last(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> str:
    """Return words before the last wake phrase (meeting preamble), or \"\"."""
    if not normalized_text or not wake_phrases:
        return ""
    words = normalized_text.split()
    idx, _pw_len, _ = _last_wake_span(words, wake_phrases, max_dist)
    if idx < 0:
        return ""
    return " ".join(words[:idx]).strip()


def strip_wake_phrase_last(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> str:
    """Remove the last wake phrase — use for long transcript barge-in lines with preamble."""
    if not normalized_text or not wake_phrases:
        return normalized_text
    words = normalized_text.split()
    idx, pw_len, _ = _last_wake_span(words, wake_phrases, max_dist)
    if idx < 0:
        return normalized_text
    return " ".join(words[idx + pw_len :]).strip()


def matched_wake_phrase_last(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> str | None:
    """Wake phrase that matched last in the utterance (for persona + command on barge-in)."""
    if not normalized_text or not wake_phrases:
        return None
    words = normalized_text.split()
    _, _, phrase = _last_wake_span(words, wake_phrases, max_dist)
    return phrase


def matched_wake_phrase(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> str | None:
    """Return the first wake phrase that matches, or None."""
    if not normalized_text or not wake_phrases:
        return None
    words = normalized_text.split()
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        if _phrase_in_words(words, phrase_words, max_dist) >= 0:
            return phrase
    return None


def wake_phrase_first_word_index(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> int:
    """
    Word index (0-based) where the first matching wake phrase starts in ``normalized_text.split()``.

    Uses the same phrase iteration order as ``strip_wake_phrase`` / ``matched_wake_phrase``.
    Returns -1 if no wake phrase matches.
    """
    if not normalized_text or not wake_phrases:
        return -1
    words = normalized_text.split()
    for phrase in wake_phrases:
        phrase_norm = normalize_for_wake(phrase)
        if not phrase_norm:
            continue
        phrase_words = phrase_norm.split()
        idx = _phrase_in_words(words, phrase_words, max_dist)
        if idx >= 0:
            return idx
    return -1


# ---------------------------------------------------------------------------
# Wake phrase + “real” command (shared by Vosk and meeting-transcript STT)
# ---------------------------------------------------------------------------

_NON_COMMAND_PRAISE = (
    "is cool",
    "sounds good",
    "is great",
    "is awesome",
    "is fine",
    "is okay",
)

# Phrases where "computer" / "koov" is referring to a device or topic, not addressing the agent.
# Matched as substrings of the lowercased, normalized utterance; if any hits, suppress the wake.
_WAKE_FALSE_ADDRESS_PHRASES: tuple[str, ...] = (
    "my computer",
    "your computer",
    "his computer",
    "her computer",
    "their computer",
    "our computer",
    "the computer",
    "this computer",
    "that computer",
    "a computer",
    "an computer",
    "any computer",
    "every computer",
    "some computer",
    "old computer",
    "new computer",
    "work computer",
    "home computer",
    "office computer",
    "personal computer",
    "computer science",
    "computer says",
    "computer was",
    "computer is",
    "computer will",
    "computer keeps",
    "computer crashed",
    "computer broke",
    "computer died",
    "on my computer",
    "on her computer",
    "on his computer",
    "on the computer",
    "from the computer",
    "from my computer",
    "fix the computer",
    "fix my computer",
    "use the computer",
    "use my computer",
)


def is_false_address_use(normalized_text: str) -> bool:
    """True when the wake word appears in a phrase that refers to a device, not Koov."""
    if not normalized_text:
        return False
    t = normalized_text.lower()
    for phrase in _WAKE_FALSE_ADDRESS_PHRASES:
        if phrase in t:
            return True
    return False


# Describing Koov to others (sales pitch, feature walkthrough) — not a directed command.
_WAKE_PRODUCT_MENTION_PHRASES: tuple[str, ...] = (
    "for koov to",
    "for koop to",
    "for koo to",
    "for kuv to",
    "allows for koov",
    "allows for koop",
    "allows for koo",
    "allows for kuv",
    "does koov give",
    "does koop give",
    "does koo give",
    "does kuv give",
    "koov give you",
    "koop give you",
    "koov gives you",
    "koop gives you",
    "that koov give",
    "that koov gives",
    "that koov can",
    "that koop gives",
    "that koop can",
    "advantage that koov",
    "invite koov",
    "invite koop",
    "koov serves as",
    "koov really shines",
    "koov can actually",
    "koov ships",
    "what koov gives",
    "what koop gives",
    "using koov to",
    "with koov and",
    "ask koov and",
    "call with koov",
    "koov dashboard",
    "qoo dashboard",
    "that koov does give",
    "koov does give you",
    "koov brings out",
    "koov really brings",
    "products that kuv",
    "products that koov",
    "kind of the advantage that koov",
)

# Team talking about how to address Koov, not speaking to Koov.
_WAKE_META_DISCUSSION_PHRASES: tuple[str, ...] = (
    "dont say what do you think",
    "do not say what do you think",
    "say computer",
    "not koov",
    "not koo",
    "wake word",
    "wake phrase",
    "wake phrase is",
    "thats not a wake word",
    "that is not a wake word",
    # Treat "computer" as a label/topic (not addressing the agent).
    # Examples from demo / setup talk:
    # - "but now it's called computer"
    # - "you can change computer to whatever you want"
    "called computer",
    "call it computer",
    "calling it computer",
    "change computer to",
    "rename computer",
)


def is_product_mention_use(normalized_text: str) -> bool:
    """True when Koov/computer is mentioned about the product, not as addressee."""
    if not normalized_text:
        return False
    t = normalized_text.lower()
    return any(phrase in t for phrase in _WAKE_PRODUCT_MENTION_PHRASES)


def is_meta_wake_discussion(normalized_text: str) -> bool:
    """True when speakers are coaching wake-word usage, not invoking Koov."""
    if not normalized_text:
        return False
    t = normalized_text.lower()
    return any(phrase in t for phrase in _WAKE_META_DISCUSSION_PHRASES)


def should_suppress_transcript_wake(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
) -> bool:
    """
    Suppress transcript wake for pitch/meta/device mentions.

    Device phrases (e.g. "your computer") in preamble before a real
    "Computer, what do you think?" at the end must not block the wake.
    """
    if not normalized_text:
        return False
    if is_meta_wake_discussion(normalized_text):
        return True
    if is_product_mention_use(normalized_text):
        return True
    words = normalized_text.split()
    wake_idx, pw_len, _ = _last_wake_span(words, wake_phrases, max_dist)
    if wake_idx < 0:
        return is_false_address_use(normalized_text)
    before = " ".join(words[:wake_idx])
    after = " ".join(words[wake_idx + pw_len :]).strip()
    if is_product_mention_use(before) and not remainder_qualifies_as_wake_command(after):
        return True
    if is_false_address_use(before) and not remainder_qualifies_as_wake_command(after):
        return True
    if wake_idx == 0:
        return is_false_address_use(normalized_text)
    return False


# Leading disfluencies — skipped so "koov um what did we decide" still qualifies.
_LEADING_FILLERS = frozenset(
    {
        "um",
        "uh",
        "uhm",
        "erm",
        "er",
        "like",
        "so",
        "well",
        "ok",
        "okay",
        "mm",
        "hmm",
        "mhm",
        # Attention words that often prefix the wake phrase ("hey computer",
        # "yo Koov"). They contribute no command meaning, so strip them before
        # we decide whether the remainder is actionable.
        "hey",
        "yo",
        "hi",
        "hello",
    }
)

# First meaningful word after wake must be here, OR the first two words match
# _TWO_WORD_PREFIXES, OR ("about", _ABOUT_BRIDGE_WORDS[1]) for "about the …".
# Not included: narrative-prone openers like "about", "there", "last", "next", "first"
# as standalone first words (they still work via two-word patterns or "about" + bridge).
_FIRST_COMMAND_WORDS = frozenset(
    (
        # questions — bare forms
        "what",
        "how",
        "when",
        "who",
        "why",
        "where",
        "which",
        "whose",
        # contraction-flattened WH forms (normalize_for_wake strips apostrophes:
        # "how's" → "hows", "what's" → "whats", etc.)
        "hows",
        "whats",
        "whos",
        "wheres",
        "whens",
        # meeting / task
        "give",
        "recap",
        "action",
        "decide",
        "decision",
        "decisions",
        "summary",
        "summarize",
        "list",
        "name",
        "names",
        "tell",
        "explains",
        "explain",
        "describes",
        "describe",
        "need",
        "please",
        "can",
        "could",
        "would",
        "should",
        "must",
        "hear",
        "heard",
        "respond",
        "listening",
        "listen",
        "remember",
        "say",
        "ask",
        "want",
        "check",
        "help",
        "get",
        "make",
        "show",
        "find",
        "read",
        "write",
        "repeat",
        "run",
        "start",
        "stop",
        "go",
        "wait",
        "done",
        "continue",
        "share",
        "call",
        "note",
        "remind",
        "draft",
        "follow",
        "outline",
        "quick",
        "meeting",
        "team",
        "minutes",
        "agenda",
        "mute",
        "unmute",
        "screen",
        "link",
        "send",
        "email",
        "schedule",
        "do",
        "did",
        "does",
        "hello",
        "hi",
        "hey",
        "thanks",
        "thank",
        "yes",
        "no",
        # auxiliaries / address
        "are",
        "is",
        "am",
        "will",
        "have",
        "has",
    )
)

# WH / contracted-WH words that are openers ONLY when followed by a substantive word.
# "what is", "how is", "who is" alone (without a subject/noun after) are truncated
# questions — e.g. "computer what is" (user trailed off) should not fire.
_BARE_WH_TRUNCATION_SECOND = frozenset({
    "is", "are", "was", "were", "the", "a", "an",
})

# First-command words that are MEANINGLESS on their own — they require more
# tokens before they can be treated as a real command. Bare "what" / "how" /
# "can" alone is just a question fragment ("what?"). Conversely, single-token
# imperatives like "summarize" / "recap" / "stop" are complete commands and
# allowed alone.
_BARE_FIRST_WORD_NEEDS_MORE = frozenset({
    # WH words (need a subject)
    "what", "how", "when", "who", "why", "where", "which", "whose",
    "hows", "whats", "whos", "wheres", "whens",
    # Modals / openers that can't stand alone
    "can", "could", "would", "should", "must",
    "give", "tell", "name", "show", "find", "make", "get", "share",
    "ask", "want", "need", "please", "say", "remind", "draft",
    "follow", "outline", "go", "run", "call", "note", "check",
    "remember", "read", "write", "explain", "explains",
    "describe", "describes",
})

_TWO_WORD_PREFIXES = frozenset(
    {
        ("can", "you"),
        ("could", "you"),
        ("would", "you"),
        ("could", "we"),
        ("would", "we"),
        ("tell", "me"),
        ("tell", "us"),
        ("give", "me"),
        ("give", "us"),
        ("let", "me"),
        ("let", "us"),
        ("i", "need"),
        ("i", "want"),
        ("i", "would"),
        ("i", "could"),
        ("we", "need"),
        ("we", "want"),
        ("we", "should"),
    }
)

# "koov about the budget" — first word alone must not be generic "about" without a bridge.
_ABOUT_BRIDGE_WORDS = frozenset({"the", "a", "an", "this", "that", "our", "my", "your"})

# Stop/interrupt directives. These must wake even though they are short (1-2
# words) so "Koov, stop" can cut Koov off mid-reply. Includes common STT
# mishearings of "stop".
_STOP_DIRECTIVE_FIRST_WORDS = frozenset({
    "stop", "stahp", "stoppit", "cancel", "quiet", "enough",
    "hush", "shush", "pause", "halt", "abort", "shut",
    "wait", "hold", "hang",
})
_STOP_DIRECTIVE_PHRASES = frozenset({
    "stop", "stop it", "stop now", "stop talking", "stop please",
    "stop speaking", "shut up", "be quiet", "never mind", "nevermind",
    "that's enough", "thats enough", "enough", "pause", "cancel that",
    "hold on", "hang on", "wait", "one sec", "one second",
    "hush", "quiet please", "be quiet please",
    "wrong", "that's wrong", "thats wrong", "not right", "no wait",
    "wait wait", "scratch that", "ignore that",
})


def remainder_is_stop_directive(remainder: str) -> bool:
    """True when the post-wake remainder is a stop/interrupt command.

    Used so "Koov, stop" (wake + 1 word) still registers as a directed wake
    and cuts off Koov's reply, even though it has fewer than the usual 3
    substantive tokens required for a command.
    """
    t = re.sub(r"[^a-z0-9 ]", " ", (remainder or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    if t in _STOP_DIRECTIVE_PHRASES:
        return True
    toks = t.split()
    # First word is a stop verb and the line is short (not "stop the rollout
    # discussion" which is a topic, not an interrupt).
    if toks and toks[0] in _STOP_DIRECTIVE_FIRST_WORDS and len(toks) <= 3:
        return True
    if toks and toks[-1] in _STOP_DIRECTIVE_FIRST_WORDS and len(toks) <= 4:
        return True
    return False


_TOKEN_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)


def _tokens_after_leading_fillers(remainder: str) -> list[str]:
    """Alphanumeric tokens with leading fillers removed."""
    tokens = [m.group(0).lower() for m in _TOKEN_RE.finditer(remainder)]
    i = 0
    while i < len(tokens) and tokens[i] in _LEADING_FILLERS:
        i += 1
    return tokens[i:]


_VAGUE_WAKE_COMMANDS = frozenset({
    "what do we do",
    "what do we do now",
    "what should we do",
    "what should we do now",
    "what do we need to do",
    "what do we need to do now",
})


def remainder_qualifies_as_wake_command(remainder: str) -> bool:
    """
    After removing the wake phrase, require a command that *starts* the request:
    the first meaningful word is an opener (WH / modal / imperative / …), or the
    first two words match a common request prefix (“can you”, “i need”, …).

    Wake-only does not activate. Idle praise (“koov is cool”) is rejected.
    Unlike scanning the whole remainder, this avoids firing when a long transcript
    merely contains words like “how” or “about” far from the wake phrase.
    """
    if not remainder:
        return False
    t = remainder.strip().lower()
    if len(t) < 2:
        return False
    if any(t.startswith(p) or p in t for p in _NON_COMMAND_PRAISE) and len(t) < 25:
        return False
    tokens = _tokens_after_leading_fillers(t)
    if not tokens:
        return False
    if " ".join(tokens) in _VAGUE_WAKE_COMMANDS:
        return False
    w0 = tokens[0]
    if w0 in _FIRST_COMMAND_WORDS:
        # Single-token first words: ONLY allow imperatives that stand alone
        # ("summarize", "recap", "stop", "help", "wait", "continue", etc.).
        # Bare WH words / modals ("what", "how", "can") are truncated questions
        # and must have more after them.
        if len(tokens) == 1:
            return w0 not in _BARE_FIRST_WORD_NEEDS_MORE
        if len(tokens) == 2 and tokens[1] in _BARE_WH_TRUNCATION_SECOND:
            return False  # "what is", "how the", "who are" etc. — nothing after
        return True
    if len(tokens) >= 2:
        w1 = tokens[1]
        if (w0, w1) in _TWO_WORD_PREFIXES:
            # "can you" alone is a fragment — wait for the rest of the question.
            return len(tokens) >= 3
        if w0 == "about" and w1 in _ABOUT_BRIDGE_WORDS:
            return len(tokens) >= 3
    return False


_INCOMPLETE_TRANSCRIPT_COMMANDS = frozenset({
    "can you",
    "could you",
    "would you",
    "will you",
    "tell me",
    "give me",
    "let me",
    "a little bit more",
    "a bit more",
    "little bit more",
    "bit more",
    "some more",
    "more about",
    "do you",
    "talk to me about",
    "tell me about",
    "talk about",
})


def is_incomplete_transcript_command(remainder: str) -> bool:
    """True when STT captured an opener but the user has not finished the question."""
    t = re.sub(r"[^a-z0-9 ]", " ", (remainder or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return True
    if t in _INCOMPLETE_TRANSCRIPT_COMMANDS:
        return True
    for suffix in (
        " a little bit more",
        " a bit more",
        " little bit more",
        " bit more",
        " some more",
    ):
        if t.endswith(suffix):
            return True
    tokens = _tokens_after_leading_fillers(t)
    if len(tokens) == 2 and (tokens[0], tokens[1]) in _TWO_WORD_PREFIXES:
        return True
    # "who won the", "what's the" — STT cut off before the noun.
    if tokens and tokens[-1] in ("the", "a", "an", "that", "this") and len(tokens) <= 4:
        return True
    return False


def qualifies_as_wake_command(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
    *,
    require_wake_at_start: bool = False,
    max_leading_words_before_wake: int | None = None,
    bypass_command_qualifier: bool = False,
) -> bool:
    """
    True if normalized text contains a wake phrase and there is a real request
    (see ``remainder_qualifies_as_wake_command``) aligned with that wake.

    When the wake is at the **start** of the utterance, the command must begin
    the remainder (after stripping the wake and leading fillers) — same strict
    rule as VoskWakeDetector, so long narratives like “koov … later how …” do
    not false-fire on a distant “how”.

    When the wake is **mid-utterance** (e.g. “… and computer can you hear me”),
    the remainder includes preamble before the command; we scan token-aligned
    suffixes so “can you hear me” still qualifies.

    ``bypass_command_qualifier``: when True (e.g. for non-English transcripts),
    skip the English-centric WH/modal qualifier and accept any wake with at
    least two substantive remainder tokens. The user-language plumbing in the
    agent enables this for Spanish/French/German/Hindi/etc. so phrases like
    "computer, qué hora es?" still wake.
    """
    if not normalized_text or not wake_phrases:
        return False
    if not detect_wake(normalized_text, wake_phrases, max_dist):
        return False
    wake_at = wake_phrase_first_word_index(normalized_text, wake_phrases, max_dist)
    _max_leading: int | None = max_leading_words_before_wake
    if require_wake_at_start:
        _max_leading = 0 if _max_leading is None else _max_leading
    if _max_leading is not None and wake_at > _max_leading:
        return False
    remainder = strip_wake_phrase(normalized_text, wake_phrases, max_dist)
    if bypass_command_qualifier:
        tokens = _tokens_after_leading_fillers(remainder.strip().lower())
        substantive = [t for t in tokens if len(t) >= 2]
        return len(substantive) >= 2
    if wake_at <= 0:
        return remainder_qualifies_as_wake_command(remainder)
    # Mid-utterance wake: preamble may precede the real command in ``remainder``.
    tokens = _tokens_after_leading_fillers(remainder.strip().lower())
    if not tokens:
        return False
    for i in range(len(tokens)):
        if remainder_qualifies_as_wake_command(" ".join(tokens[i:])):
            return True
    return False


def qualifies_as_directed_transcript_wake(
    normalized_text: str,
    wake_phrases: tuple[str, ...] | list[str],
    max_dist: int = 2,
    *,
    max_leading_words_before_wake: int | None = None,
    max_command_tokens_after_wake: int = 6,
    bypass_command_qualifier: bool = False,
) -> bool:
    """
    Stricter wake+command check for meeting transcript STT.

    Rejects sales-pitch lines ("allows for Koov to be…", "does Koov give you…")
    and long narratives that only contain a command word far after the wake
    (e.g. "…meeting that you join" matching ``join``).

    ``bypass_command_qualifier``: when True (e.g. STT detected non-English),
    skip the English-only WH/modal qualifier — accept any wake with at least
    two substantive remainder tokens. Lets "computer, qué hora es?" through
    even though "que" is not in our English ``_FIRST_COMMAND_WORDS``.
    """
    if not normalized_text or not wake_phrases:
        return False
    if should_suppress_transcript_wake(normalized_text, wake_phrases, max_dist):
        return False
    if not detect_wake(normalized_text, wake_phrases, max_dist):
        return False
    words = normalized_text.split()
    wake_idx, pw_len, _ = _last_wake_span(words, wake_phrases, max_dist)
    if wake_idx < 0:
        return False
    if (
        max_leading_words_before_wake is not None
        and wake_idx > max_leading_words_before_wake
    ):
        return False
    after_wake = " ".join(words[wake_idx + pw_len :]).strip()
    if not after_wake:
        return False
    from koov.dialog_memory import is_repeat_request

    if is_repeat_request(after_wake):
        return True
    if is_incomplete_transcript_command(after_wake):
        return False
    tokens = _tokens_after_leading_fillers(after_wake.lower())
    if not tokens:
        return False
    # "Koov, stop" / "Koov, shut up" — interrupt directives must wake even
    # though they are short, so the user can cut Koov off mid-reply.
    if remainder_is_stop_directive(" ".join(tokens)):
        return True
    substantive = [t for t in tokens if len(t) >= 2]
    if bypass_command_qualifier:
        # Multilingual mode: relax the English WH/modal gate. Two substantive
        # tokens are enough (e.g. "qué hora", "wie spät", "kya time").
        return len(substantive) >= 2
    if len(substantive) < 3:
        return False
    window = tokens[: max(1, max_command_tokens_after_wake)]
    if remainder_qualifies_as_wake_command(" ".join(window)):
        return True
    for i in range(1, len(window)):
        if remainder_qualifies_as_wake_command(" ".join(window[i:])):
            return True
    return False
