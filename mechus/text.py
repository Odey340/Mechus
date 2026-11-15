from __future__ import annotations

import re


_WS_RE = re.compile(r"\s+")
_STAGE_WORDS = r"(laugh|laughter|chuckle|smirk|sigh|wink|pause|cough|clears throat)"
_STAGE_RE = re.compile(
    rf"(\*[^*]*{_STAGE_WORDS}[^*]*\*|\[[^\]]*{_STAGE_WORDS}[^\]]*\]|\([^)]*{_STAGE_WORDS}[^)]*\))",
    re.IGNORECASE,
)
_ASTERISK_RE = re.compile(r"\*[^*]*\*")

# ElevenLabs v3 audio tag allowlist. Anything outside this set is stripped before TTS so
# GPT can not hallucinate sound effects, accents, or stage directions that read aloud as
# bracketed words (e.g. "[laughs in Spanish]" → speaks the word "Spanish").
# Grouped by purpose; lowercase keys, exact ElevenLabs spellings.
AUDIO_TAG_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Reactions (audible, breath-like)
        "sighs",
        "chuckles",
        "laughs",
        "laughs softly",
        "exhales",
        "hmm",
        "sniffs",
        "gasps",
        "clears throat",
        # Tone / emotion (delivery hints)
        "warmly",
        "confidently",
        "thoughtfully",
        "quietly",
        "relieved",
        "excited",
        "curious",
        "gently",
        "reassuring",
        "attentive",
        "ready",
        "listening",
        "affirmative",
        "softly",
        "amused",
        "deadpan",
        "dry",
        "playful",
        "pensive",
        "earnest",
        "matter-of-fact",
    }
)

_TAG_CAPTURE_RE = re.compile(r"\[([^\[\]]{1,40})\]")
_TRAILING_ENDINGS = {
    "and",
    "but",
    "or",
    "so",
    "because",
    "including",
    "both",
    "of",
    "to",
    "with",
    "for",
    "at",
    "by",
    "from",
    "that",
    "which",
    "who",
    "whom",
    "as",
    "if",
    # Hard caps used to leave dangling articles / auxiliaries ("in the solar.").
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "in",
    "on",
    "into",
    "onto",
    "about",
    "like",
    "than",
    "then",
    "when",
    "while",
    "where",
    "how",
    "what",
    "my",
    "your",
    "our",
    "their",
    "its",
    "his",
    "her",
}


def normalize_text(text: str) -> str:
    text = text.strip()
    text = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u00a0", " ")
    )
    text = _WS_RE.sub(" ", text)
    return text


def sanitize_audio_tags(text: str, *, max_tags: int = 2) -> str:
    """
    Keep only ElevenLabs v3 audio tags from ``AUDIO_TAG_ALLOWLIST`` and cap to ``max_tags``.

    Non-allowlisted bracket spans (e.g. ``[winks in spanish]``, ``[door slam]``,
    ``[Topic: pricing]``) are stripped so the TTS doesn't read the bracketed words
    aloud or emit unintended sound effects.

    Tags are matched case-insensitively against the allowlist but kept in
    lowercase canonical form. Duplicates are removed; the first ``max_tags``
    survive in original order so reaction tags front-load the response.
    """
    if not text:
        return ""
    seen_kept = 0
    seen_lc: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        nonlocal seen_kept
        inner = match.group(1).strip()
        key = inner.lower()
        if key not in AUDIO_TAG_ALLOWLIST:
            return ""
        if key in seen_lc:
            return ""
        if seen_kept >= max(0, int(max_tags)):
            return ""
        seen_lc.add(key)
        seen_kept += 1
        return f"[{key}]"

    out = _TAG_CAPTURE_RE.sub(_replace, text)
    return normalize_text(out)


# Leading spoken fillers the thinking preroll / wake opener may already have said.
_PREROLL_ECHO_PREFIX_RE = re.compile(
    r"^(?:\[.*?\]\s*)*"
    r"(?:let me check(?: on that)?(?: real quick)?|lemme check|sure,? checking|"
    r"sure,? one moment|sure thing,? one moment|alright,? one moment|"
    r"right,? let me(?: look| check| see)?|one sec(?:ond)?|hang on|"
    # Truncated openers often STT as "okay give me i am" instead of "just a sec".
    r"okay,? give me(?: just)?(?: a)?(?:\s+(?:sec|second|moment|i am))?|"
    r"yeah,? give me(?: just)?(?: a)?(?:\s+(?:sec|second|moment))?|"
    r"give me(?: just)? a (?:sec|second|moment)|quick check(?: on that)?|"
    r"checking(?: on that| that now)?|real quick,? let me check|"
    r"got it[,.]?|sure[,.]?|okay[,.]?|ok[,.]?)\s*"
    r"[,.!\-–—]*\s*",
    re.IGNORECASE,
)


def strip_preroll_echo_from_response(response: str, preroll_phrase: str = "") -> str:
    """Remove leading filler that duplicates a just-played thinking preroll or opener.

    Prevents audible stutter like preroll 'lemme check' + reply 'check the planets…'.
    """
    text = (response or "").strip()
    if not text:
        return text
    stripped = _PREROLL_ECHO_PREFIX_RE.sub("", text).strip()
    if stripped and stripped != text:
        text = stripped
    pre = re.sub(r"\[.*?\]", "", (preroll_phrase or "")).strip()
    pre_words = re.findall(r"[a-z0-9']+", pre.lower())
    if pre_words:
        # Drop a shared boundary word (e.g. preroll ends with 'check', reply starts with 'check').
        m = re.match(r"^([A-Za-z0-9']+)", text)
        if m and m.group(1).lower() == pre_words[-1]:
            text = text[m.end() :].lstrip(" ,.-–—")
        # Drop full preroll prefix if the model repeated it verbatim.
        pre_joined = " ".join(pre_words)
        lead = re.findall(r"[a-z0-9']+", text.lower())
        if len(lead) >= len(pre_words) and lead[: len(pre_words)] == pre_words:
            # Re-slice from original text preserving casing after prefix
            pat = r"^(?:\W*\w+\W*){" + str(len(pre_words)) + r"}"
            text = re.sub(pat, "", text, count=1, flags=re.IGNORECASE).strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text.strip()


_BOT_OPENER_RE = re.compile(
    r"^(?:"
    r"(?:certainly|of course|absolutely|great question|good question)"
    r"[!,.]?\s*"
    r"|(?:i(?:'d| would) be (?:happy|glad) to)\s+"
    r"|(?:thank you for (?:asking|that))\s*[!.,]?\s*"
    r"|(?:as an ai(?: assistant)?)\s*[,]?\s*"
    r"|(?:let me help you(?: with that)?)\s*[,!.]?\s*"
    r"|(?:here(?:'s| is) what (?:i found|you need))\s*[:]\s*"
    r"|(?:based on (?:my knowledge|the information|what i know))\s*[,]?\s*"
    r"|(?:i understand (?:that|your))\s+"
    r")+",
    re.IGNORECASE,
)

_CONTRACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bI am\b"), "I'm"),
    (re.compile(r"\bI will\b"), "I'll"),
    (re.compile(r"\bI have\b"), "I've"),
    (re.compile(r"\bI would\b"), "I'd"),
    (re.compile(r"\byou are\b", re.I), "you're"),
    (re.compile(r"\bwe are\b", re.I), "we're"),
    (re.compile(r"\bthey are\b", re.I), "they're"),
    (re.compile(r"\bit is\b", re.I), "it's"),
    (re.compile(r"\bthat is\b", re.I), "that's"),
    (re.compile(r"\bthere is\b", re.I), "there's"),
    (re.compile(r"\bwhat is\b", re.I), "what's"),
    (re.compile(r"\bwho is\b", re.I), "who's"),
    (re.compile(r"\bdo not\b", re.I), "don't"),
    (re.compile(r"\bdoes not\b", re.I), "doesn't"),
    (re.compile(r"\bdid not\b", re.I), "didn't"),
    (re.compile(r"\bcannot\b", re.I), "can't"),
    (re.compile(r"\bcan not\b", re.I), "can't"),
    (re.compile(r"\bwill not\b", re.I), "won't"),
    (re.compile(r"\bwould not\b", re.I), "wouldn't"),
    (re.compile(r"\bshould not\b", re.I), "shouldn't"),
    (re.compile(r"\bcould not\b", re.I), "couldn't"),
    (re.compile(r"\bis not\b", re.I), "isn't"),
    (re.compile(r"\bare not\b", re.I), "aren't"),
    (re.compile(r"\bhave not\b", re.I), "haven't"),
    (re.compile(r"\bhas not\b", re.I), "hasn't"),
    (re.compile(r"\blet us\b", re.I), "let's"),
)


_TTS_DISFLUENCY_TAIL_RE = re.compile(
    r"(?:\s*[,.\-–—]?\s*(?:uh+h?|um+m?|uhm+|hm+m?|ah+h?|er+r?)\s*)+$",
    re.IGNORECASE,
)
_TTS_DISFLUENCY_LEAD_RE = re.compile(
    r"^(?:uh+h?|um+m?|uhm+|hm+m?|ah+h?|er+r?)\s*[,.\-–—]?\s*",
    re.IGNORECASE,
)


def strip_tts_disfluencies(text: str) -> str:
    """Drop model or STT filler words that ElevenLabs turns into audible uh-uh tails."""
    text = normalize_text(text)
    if not text:
        return text
    for _ in range(3):
        nxt = _TTS_DISFLUENCY_TAIL_RE.sub("", text).strip()
        nxt = _TTS_DISFLUENCY_LEAD_RE.sub("", nxt).strip()
        if nxt == text:
            break
        text = nxt
    return _ensure_terminal_punct(text) if text else text


def humanize_spoken_text(text: str) -> str:
    """Strip assistant-script phrasing and use contractions so TTS sounds like a person."""
    text = normalize_text(text)
    if not text:
        return text
    tag_prefix = ""
    m = re.match(r"^((?:\[[^\]]+\]\s*)+)", text)
    if m:
        tag_prefix = m.group(1)
        text = text[m.end() :].lstrip()
    for _ in range(3):
        stripped = _BOT_OPENER_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    for pat, repl in _CONTRACTION_RULES:
        text = pat.sub(repl, text)
    text = re.sub(r";\s*", ". ", text)
    text = normalize_text(text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return f"{tag_prefix}{text}".strip() if tag_prefix else text


def clean_spoken_text(text: str, preserve_audio_tags: bool = False) -> str:
    """
    Normalize text for speech output.

    When ``preserve_audio_tags=True`` (ElevenLabs v3 / eleven_v3), square-bracket
    tags like ``[sighs]``, ``[excited]``, and ``[laughs softly]`` are intentional
    ElevenLabs audio directives and must be left intact — but only when they
    match the ``AUDIO_TAG_ALLOWLIST`` (validated via ``sanitize_audio_tags``).
    Asterisk stage directions are stripped because GPT sometimes emits them
    alongside tags.

    When ``preserve_audio_tags=False`` (default / Piper / older ElevenLabs),
    all stage-direction brackets and asterisk sequences are removed.
    """
    text = normalize_text(text)
    if not text:
        return ""
    if not preserve_audio_tags:
        text = _STAGE_RE.sub("", text)
        text = _ASTERISK_RE.sub("", text)
    else:
        text = _ASTERISK_RE.sub("", text)
        text = sanitize_audio_tags(text)
    text = normalize_text(text)
    text = humanize_spoken_text(text)
    return strip_tts_disfluencies(text)


def _strip_trailing_endings(words: list[str]) -> list[str]:
    while words:
        tail = words[-1].rstrip(".,;:").lower()
        if tail not in _TRAILING_ENDINGS:
            break
        words.pop()
    return words


def _ensure_terminal_punct(text: str) -> str:
    if not text:
        return ""
    if text.endswith((".", "!", "?")):
        return text
    return text + "."


def truncate_chars(text: str, max_chars: int) -> str:
    """Hard cap on spoken length (wake / low-latency replies)."""
    text = normalize_text(text)
    if not text or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" ,;:")
    return _ensure_terminal_punct(cut)


def limit_words(text: str, max_words: int) -> str:
    text = normalize_text(text)
    if not text:
        return ""
    if max_words <= 0:
        words = _strip_trailing_endings(text.split(" "))
        return _ensure_terminal_punct(" ".join(words).rstrip())
    words = text.split(" ")
    if len(words) <= max_words:
        words = _strip_trailing_endings(words)
        return _ensure_terminal_punct(" ".join(words).rstrip())

    sents = split_sentences(text)
    if sents:
        total = 0
        chosen: list[str] = []
        for sent in sents:
            wc = len(sent.split())
            if total == 0 and wc > max_words:
                break
            if total + wc > max_words:
                break
            chosen.append(sent)
            total += wc
        if chosen:
            return _ensure_terminal_punct(normalize_text(" ".join(chosen)))

    # Hard truncate mid-sentence: drop dangling connectors, then back up once more so
    # we don't leave orphan modifiers like "…in the solar." when "system" was cut.
    cut = _strip_trailing_endings(words[:max_words])
    if cut and len(words) > max_words:
        cut = cut[:-1]
        cut = _strip_trailing_endings(cut)
    if not cut:
        cut = _strip_trailing_endings(words[: max(1, max_words // 2)]) or words[:1]
    trimmed = " ".join(cut).rstrip(" ,;:")
    return _ensure_terminal_punct(trimmed)


def strip_wake_word(text: str, wake_phrase: str | list[str] | tuple[str, ...]) -> str:
    """Remove leading wake word(s). wake_phrase can be a single string or multiple (e.g. ['computer', 'mechus'])."""
    t = normalize_text(text).lower()
    phrases: tuple[str, ...] = (wake_phrase,) if isinstance(wake_phrase, str) else tuple(wake_phrase)
    for w in phrases:
        w = normalize_text(w).lower()
        if not w:
            continue
        if t.startswith(w + " "):
            return normalize_text(text)[len(w) + 1 :].strip()
        if t == w:
            return ""
    return normalize_text(text)


def split_sentences(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts


def phrases_for_natural_speech(text: str, *, max_chars: int = 220) -> list[str]:
    """Split spoken reply into TTS chunks at sentence boundaries (complete thoughts)."""
    text = normalize_text(text)
    if not text:
        return []
    out: list[str] = []
    for sent in split_sentences(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            out.append(_ensure_terminal_punct(sent))
            continue
        words = sent.split()
        chunk_words: list[str] = []
        for w in words:
            trial = " ".join(chunk_words + [w])
            if chunk_words and len(trial) > max_chars:
                out.append(_ensure_terminal_punct(" ".join(chunk_words)))
                chunk_words = [w]
            else:
                chunk_words.append(w)
        if chunk_words:
            out.append(_ensure_terminal_punct(" ".join(chunk_words)))
    if len(out) == 1:
        return out
    return [p for p in out if p.strip()]


_OWNER_STOPWORDS = frozenset(
    {
        "action", "actions", "item", "items", "task", "tasks", "todo", "todos",
        "the", "a", "an", "for", "of", "to", "what", "whats", "are", "is",
        "mechus", "computer", "please", "tell", "me", "list", "show", "give",
        "all", "any", "my", "our", "their", "his", "her", "assigned",
        "responsible", "owner", "owners", "next", "steps", "step",
    }
)


def extract_requested_owner(request_text: str) -> str | None:
    """Parse owner name from 'action items for Sahasra', \"Sahasra's tasks\", etc."""
    if not request_text:
        return None
    t = normalize_text(request_text)
    tl = t.lower()

    patterns = (
        r"(?:action\s+items?|tasks?|todos?|to-?dos?|next\s+steps?)\s+for\s+([a-z][a-z0-9_-]{1,30})",
        r"for\s+([a-z][a-z0-9_-]{1,30})(?:\s*['']s)?\s+(?:action\s+items?|tasks?|todos?|to-?dos?|next\s+steps?)",
        r"([a-z][a-z0-9_-]{1,30})['']s\s+(?:action\s+items?|tasks?|todos?|to-?dos?|next\s+steps?)",
        r"what\s+(?:are|is)\s+([a-z][a-z0-9_-]{1,30})(?:\s*['']s)?\s+(?:action\s+items?|tasks?|todos?|to-?dos?|next\s+steps?)",
        r"what\s+(?:action\s+items?|tasks?|todos?|to-?dos?|next\s+steps?)\s+(?:does|did|has|have)\s+([a-z][a-z0-9_-]{1,30})\s+have",
        r"what\s+(?:is|are)\s+([a-z][a-z0-9_-]{1,30})\s+(?:working\s+on|responsible\s+for|handling|assigned\s+to)",
    )
    for pat in patterns:
        m = re.search(pat, tl)
        if not m:
            continue
        name = (m.group(1) or "").strip()
        if not name or name in _OWNER_STOPWORDS:
            continue
        return name.title()

    # "action items for sahasra" with trailing punctuation stripped
    m = re.search(
        r"(?:action\s+items?|tasks?)\s+for\s+([a-z][a-z0-9_-]{1,30})\b",
        tl,
    )
    if m:
        name = (m.group(1) or "").strip()
        if name and name not in _OWNER_STOPWORDS:
            return name.title()
    return None


def owner_matches_item_owner(item_owner: str, requested_owner: str) -> bool:
    """Case-insensitive owner match; first token must match (e.g. Sahasra vs Sahasra J.)."""
    if not item_owner or not requested_owner:
        return False
    item_l = normalize_text(item_owner).lower()
    req_l = normalize_text(requested_owner).lower()
    if item_l == req_l:
        return True
    item_first = item_l.split()[0] if item_l.split() else ""
    req_first = req_l.split()[0] if req_l.split() else ""
    return bool(item_first and req_first and item_first == req_first)
