"""
STT non-speech / scene-label detection and bracket scrubbing (no webrtcvad dependency).

Used by mechus.transcript.clean_transcript_text and mechus.agent noise dropping.
"""

from __future__ import annotations

import re

from mechus.text import normalize_text

_WS_RE = re.compile(r"\s+")

# Bracketed/parenthesized STT scene labels (Whisper-style): strip from transcript text.
_INLINE_NOISE_WORDS = (
    r"(laugh|laughter|laughs|laughing|chuckle|chuckles|smirk|pause|pauses|sigh|sighs|"
    r"cough|coughs|clears throat|wink|silence|typing|clicking|click|clicks|crackling|"
    r"whispering|whispers|sniffs|sniff|drinking|writing|hmm|humming|applause|"
    r"music|instrumental|jingle|outro|intro|noise|keyboard|clacking|background|"
    r"fire|mumbling|inaudible|unintelligible|"
    # Phone / device sounds
    r"ringing|ringtone|chime|chimes|chiming|ding|ping|beep|beeps|beeping|buzzing|buzz|"
    r"vibrate|vibrating|notification|alert|alarm|"
    # Physical / ambient sounds
    r"clinking|clank|clanking|clattering|clatter|tapping|tap|rustling|rustle|"
    r"knocking|knock|banging|bang|thud|thump|creaking|creak|shuffling|shuffle|"
    r"scratching|scratch|jingling|jingle|rattling|rattle|"
    # Environmental
    r"barking|bark|meowing|meow|howling|howl|chirping|chirp|"
    r"honking|honk|thunder|thundering|rain|raining|wind|blowing|"
    r"static|feedback|echo|reverb|ambient|"
    # Objects / misc
    r"phone|doorbell|bell|whistle|swoosh|pop|snap|splash|"
    r"footsteps|footstep|steps|walking|door|opening|closing|slap|"
    # Laughter variants
    r"giggle|giggling|snicker|snickering)"
)
INLINE_NOISE_RE = re.compile(
    rf"(\*[^*]*{_INLINE_NOISE_WORDS}[^*]*\*|\[[^\]]*{_INLINE_NOISE_WORDS}[^\]]*\]|\([^)]*{_INLINE_NOISE_WORDS}[^)]*\))",
    re.IGNORECASE,
)

_NOISE_MAX_WORDS = 6
_NOISE_TOKENS = frozenset({
    "silence",
    "typing",
    "clicking",
    "click",
    "clicks",
    "crackling",
    "whispering",
    "whispers",
    "sniffs",
    "sniff",
    "drinking",
    "writing",
    "hmm",
    "hm",
    "um",
    "umm",
    "uh",
    "uhh",
    "uh-huh",
    "laughter",
    "laughs",
    "laughing",
    "laugh",
    "applause",
    "music",
    "instrumental",
    "jingle",
    "outro",
    "intro",
    "noise",
    "background",
    "keyboard",
    "clacking",
    "fire",
    "mumbling",
    "inaudible",
    "unintelligible",
    "sigh",
    "sighs",
    "cough",
    "coughs",
    "pause",
    "pauses",
    # Phone / device
    "ringing",
    "chime",
    "chimes",
    "chiming",
    "beeping",
    "buzzing",
    "vibrating",
    "ringtone",
    "ding",
    "ping",
    "notification",
    # Physical sounds
    "clinking",
    "clanking",
    "clatter",
    "clattering",
    "tapping",
    "rustling",
    "knocking",
    "banging",
    "thud",
    "thump",
    "creaking",
    "shuffling",
    "scratching",
    "rattling",
    "jingling",
    # Environmental
    "barking",
    "meowing",
    "honking",
    "thunder",
    "static",
    "feedback",
    "echo",
    "ambient",
    # Laughter variants
    "giggling",
    "snickering",
})
_NOISE_PHRASES = frozenset({
    "background noise",
    "instrumental music",
    "clacking keyboard",
    "keyboard clacking",
    "crackling fire",
    "fire crackling",
    # Phone / device
    "phone ringing",
    "phone chimes",
    "phone buzzing",
    "phone vibrating",
    "phone notification",
    "phone beeping",
    "ringtone playing",
    "doorbell ringing",
    "alarm ringing",
    "alarm beeping",
    # Object sounds
    "beads clinking",
    "glasses clinking",
    "keys jingling",
    "keys rattling",
    "paper rustling",
    "door opening",
    "door closing",
    "dog barking",
    "cat meowing",
    "car honking",
    "thunder rumbling",
})
_NOISE_ALLOWLIST = frozenset({
    "yeah",
    "yes",
    "no",
    "ok",
    "okay",
    "right",
    "sure",
    "thanks",
    "thank",
    "hello",
    "hey",
    "hi",
    "cool",
    "nice",
    "well",
    "so",
    "oh",
    "ah",
    "wow",
    "god",
    "please",
    "sorry",
    "bye",
    "goodbye",
})


def _norm_word_token(word: str) -> str:
    w = word.strip()
    if not w:
        return ""
    w = re.sub(r"^[^\w']+|[^\w']+$", "", w, flags=re.UNICODE)
    return w.lower()


def is_noise_only_transcript(
    text: str,
    *,
    extra_noise: frozenset[str] | None = None,
) -> bool:
    """
    True if the segment is only STT non-speech labels (silence, typing, etc.).
    Short real utterances (e.g. yeah, okay) are never classified as noise.
    """
    text = normalize_text(text)
    if not text:
        return False
    collapsed = _WS_RE.sub(" ", text).strip().lower()
    if not collapsed:
        return False

    noise_tokens = set(_NOISE_TOKENS)
    noise_phrases = set(_NOISE_PHRASES)
    if extra_noise:
        for raw in extra_noise:
            e = raw.strip().lower()
            if not e:
                continue
            e = _WS_RE.sub(" ", e).strip()
            if " " in e:
                noise_phrases.add(e)
            else:
                noise_tokens.add(e)

    if collapsed in noise_phrases:
        return True

    words = collapsed.split()
    if len(words) > _NOISE_MAX_WORDS:
        return False

    norm_words = [_norm_word_token(w) for w in words]
    norm_words = [w for w in norm_words if w]
    if not norm_words:
        return False

    for w in norm_words:
        if w in _NOISE_ALLOWLIST:
            return False

    joined = " ".join(norm_words)
    if joined in noise_phrases:
        return True

    for w in norm_words:
        if w not in noise_tokens:
            return False
    return True
