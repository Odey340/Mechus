"""
Multi-persona registry for Mechus bots.

Each persona has its own wake phrases, ElevenLabs voice, system prompt,
filler phrases, refusal rules, tools, and style overrides. Personas are
loaded from JSON files in a directory (default ``mechus/personas/``) and
the agent uses ``resolve_by_wake()`` to pick the right one based on
which wake phrase actually fired.

If no JSON files are present, a synthetic ``default`` persona is built
from the existing ``MechusConfig`` so behavior is identical to single-
persona Mechus out of the box.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from typing import Iterable

from mechus.brand import ASSISTANT_NAME

log = logging.getLogger(__name__)


# Marker so callers can tell whether a persona is the synthetic default
# (use the original Mechus system prompt) or a JSON-loaded one (replace it).
DEFAULT_PERSONA_ID = "default"


@dataclass(frozen=True)
class Persona:
    """A single Mechus personality."""

    id: str
    name: str
    wake_phrases: tuple[str, ...]
    voice_id: str | None
    model_id: str | None
    system_prompt: str
    audio_tags: bool
    max_words: int
    temperature: float | None
    tools: tuple[str, ...]
    refusal_rules: tuple[str, ...]
    filler_phrases: tuple[str, ...]
    is_default: bool = False

    def primary_wake_phrase(self) -> str:
        return self.wake_phrases[0] if self.wake_phrases else ""


_DEFAULT_FILLER_PHRASES: tuple[str, ...] = (
    # ── Generic stalls (apply to anything) ──────────────────────────────
    "Yeah, let me pull that up real quick.",
    "Give me just a moment on that.",
    "Sure, let me find that for you.",
    "One moment, looking into it now.",
    "Yeah, give me a second on that.",
    "Okay, give me just a sec.",
    "Sure thing, one moment.",
    "Yeah, give me a moment.",
    "Sure, one sec.",
    "One sec.",
    "Give me a moment.",
    "Hang on.",
    "On it.",
    "Hold on.",
    "Okay, one sec.",
    "Right, hang on.",
    "Yeah give me a moment.",
    "On it, one sec.",
    "Yeah, real quick.",
    "Sure, real quick.",
    # ── Fact-leaning (lookup, retrieval, "let me check") ────────────────
    "Let me check.",
    "Let me check on that real quick.",
    "Sure, checking.",
    "Right, let me look.",
    "Right, let me look that up for you.",
    "Let me pull that up.",
    "Sure, let me check that.",
    "Let's look that up.",
    "Checking that now.",
    "Looking into it.",
    "Checking on that.",
    "Let me find that.",
    "Let me see what I can find on that.",
    "Pulling that up now.",
    "Quick check on that.",
    "Let me grab that for you.",
    "Looking that up now.",
    "Already on it.",
    "Real quick, let me check.",
    "Let me look into that right now.",
    "Looking that up for you.",
    "Let me see what I can pull up.",
    "Give me just a second, pulling that up.",
    "Let me scan that real quick.",
    # ── Opinion-leaning (reflection, weighing in) ───────────────────────
    "Hmm, let me think about that for a second.",
    "Hmm, let me think.",
    "Let me think about that.",
    "Let me actually think about this.",
    "Okay let me actually think this through.",
    "Hm. Yeah, let me think.",
    "Okay let me think for a sec.",
    "Hmm, good question.",
    "Oh, good question.",
    "Oh I like that question.",
    "That's a good one.",
    "That's actually a really good one.",
    "Oh, that's a fun one.",
    "Interesting one.",
    "Wow, that's interesting.",
    "Ooh, interesting.",
    "Ooh that's a good one.",
    "Hmm, yeah.",
    "Good one, give me a sec.",
    "That's worth thinking about.",
    "Okay, that's a tough one.",
    "Hmm, fair question.",
    "Honestly, let me think for a sec.",
    "Hmm, I have thoughts on that.",
    "Oof, interesting.",
    "That's actually interesting to think about.",
    "Hm. Yeah, I have thoughts on that.",
    "Let me sit with that for a moment.",
    "That's worth chewing on.",
    "Good one, let me think.",
    "Hmm. Yeah, fair point.",
    "Oof, that's actually a tough call.",
    "Okay I want to think about this properly.",
    # ── Meeting-leaning (recap / what we said) ──────────────────────────
    "As we talked about it.",
    "Right, from what we said.",
    "Like we stated earlier.",
    "Pulling up the meeting notes now.",
    "Let me scan the transcript real quick.",
    "Going through what was said.",
    "Checking the meeting context.",
    "Walking back through that now.",
    "Let me check the transcript.",
    "Going back through what was said.",
    "Reviewing the notes real quick.",
    "Let me pull up what we covered.",
    # ── Carefulness for tricky / nuanced asks ───────────────────────────
    "Give me a second, I want to make sure I get this right.",
    "Give me a second, I want to get this right.",
    "Let me get this right for you.",
    "One sec, want to be accurate here.",
)

# Short, category-diverse clips pre-synthesized at startup so wake openers match
# intent (fact vs opinion vs meeting) instead of always falling back to "Yeah…".
_THINKING_WARMUP_PHRASES: tuple[str, ...] = (
    "One sec.",
    "Hang on.",
    "Yeah, one sec.",
    "Let me see.",
    "Checking.",
    "On it.",
    "One moment.",
    "Sure, one sec.",
)

THINKING_PHRASES_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "meeting": (
        "Let me see what we said about that.",
        "As we said earlier, let me pull that up.",
        "Let me check what we covered on that.",
        "Walking back through what we discussed.",
        "Let me scan what we said in the meeting.",
        "Right, from what we talked about earlier.",
        "Let me check the meeting notes on that.",
        "Going back through what we said.",
        "Let me pull up what we covered.",
        "Reviewing what we discussed real quick.",
        "Let me check the transcript on that.",
        "As we mentioned earlier, one sec.",
        "Let me see what we decided on that.",
        "Checking what we said about it.",
        "Let me look back at what we covered.",
        "Pulling up what we said earlier.",
        "Let me revisit what we discussed.",
        "From the meeting so far, let me check.",
        "Going through what we talked about.",
    ),
    "opinion": (
        "That's a good question.",
        "Hmm, interesting.",
        "Hmm, let me see.",
        "Oh, good question.",
        "That's a fair question.",
        "Hmm, let me think about that.",
        "Interesting one, give me a sec.",
        "Yeah, that's worth thinking about.",
        "Hmm, I have thoughts on that.",
        "Okay, let me think that through.",
        "That's actually a good one.",
        "Hmm, fair point.",
        "Let me sit with that for a moment.",
        "Oh I like that question.",
        "That's a tough one, let me think.",
        "Hmm, yeah, let me weigh in.",
        "Good question, one sec.",
        "Let me think on that for a second.",
        "Hmm, that's interesting to think about.",
        "Okay, let me actually think this through.",
    ),
    "fact": (
        "One sec.",
        "Yeah, one sec.",
        "Hang on.",
        "Let me see.",
        "One moment.",
        "Checking.",
        "Looking it up.",
        "Sure, one sec.",
        "Got it, one sec.",
        "Right, one sec.",
        "Okay, one sec.",
        "On it.",
        "Yeah, hang on.",
        "Alright, one sec.",
        "Sure, hang on.",
        "Yep, one sec.",
        "Let me check.",
    ),
    "general": (
        "Sure, one moment.",
        "Give me just a sec.",
        "On it.",
        "Hang on.",
        "One sec.",
        "Yeah, give me a moment.",
        "Alright, one moment.",
        "Sure, real quick.",
        "Okay, one sec.",
        "Right, hang on.",
    ),
}


_MEETING_THINKING_MARKERS = (
    "what we said",
    "what was said",
    "we said",
    "we talked",
    "we discussed",
    "meeting notes",
    "transcript",
    "walking back",
    "from what we",
    "as we said",
    "as we mentioned",
    "what we covered",
    "what we decided",
)


def _phrase_matches_thinking_category(phrase: str, category: str) -> bool:
    """Keep persona fillers out of the wrong intent bucket (e.g. meeting lines on weather)."""
    low = (phrase or "").strip().lower()
    if not low:
        return False
    cat = (category or "general").strip().lower()
    if cat == "meeting":
        return any(m in low for m in _MEETING_THINKING_MARKERS) or any(
            k in low for k in ("recap", "summarize", "notes", "transcript")
        )
    if cat == "fact":
        if any(m in low for m in _MEETING_THINKING_MARKERS):
            return False
        return any(
            k in low
            for k in ("look", "check", "pull", "find", "grab", "scan", "verify", "up")
        )
    if cat == "opinion":
        if any(m in low for m in _MEETING_THINKING_MARKERS):
            return False
        return any(
            k in low
            for k in ("think", "question", "interesting", "fair", "tough", "hmm")
        )
    if cat == "general":
        if any(m in low for m in _MEETING_THINKING_MARKERS):
            return False
        return True
    return True


def thinking_phrase_pool_for_category(
    category: str,
    *,
    persona_fillers: tuple[str, ...] | list[str],
    audio_tags: bool = False,
) -> list[str]:
    """Category-specific thinking clips; persona fillers only when they match the bucket."""
    cat = (category or "general").strip().lower()
    base = list(THINKING_PHRASES_BY_CATEGORY.get(cat) or THINKING_PHRASES_BY_CATEGORY["general"])
    seen = {p.strip().lower() for p in base}
    for p in persona_fillers or ():
        norm = (p or "").strip()
        if not norm:
            continue
        if not _phrase_matches_thinking_category(norm, cat):
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        base.append(norm)
    if audio_tags:
        for p in _AUDIO_TAGGED_FILLER_PHRASES:
            key = p.strip().lower()
            if key not in seen:
                seen.add(key)
                base.append(p)
    return base


# Extra filler phrases that use ElevenLabs v3 audio tags for richer delivery.
# These are appended to the filler pool only when audio_tags=True on the persona,
# so a non-v3 TTS engine never receives bracketed tag text it might read aloud.
_AUDIO_TAGGED_FILLER_PHRASES: tuple[str, ...] = (
    # ── Generic ─────────────────────────────────────────────────────────
    "[hmm] one moment.",
    "[hmm] give me a sec.",
    "[hmm] hang on.",
    "[quietly] give me a second.",
    # ── Fact-leaning ────────────────────────────────────────────────────
    "[curious] let me look that up.",
    "[attentive] checking on that.",
    "[attentive] let me find that.",
    "[curious] checking that now.",
    "[attentive] on it, one moment.",
    # ── Opinion-leaning ─────────────────────────────────────────────────
    "[thoughtfully] let me think about that.",
    "[thoughtfully] give me a sec.",
    "[pensive] that's a tough one.",
    "[pensive] hmm, good question.",
    "[hmm] let me think for a sec.",
    "[chuckles] that's a fun one, let me think.",
    "[excited] oh good question.",
    "[amused] ooh that's a good one.",
    # ── Meeting-leaning ─────────────────────────────────────────────────
    "[attentive] let me check what we said.",
    "[attentive] checking the meeting context.",
    "[attentive] let me scan the transcript.",
)


def _normalize_phrase(p: str) -> str:
    return (p or "").strip().lower()


def _coerce_phrase_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items: Iterable[str] = (value,)
    elif isinstance(value, (list, tuple)):
        items = (str(x) for x in value)
    else:
        items = ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        norm = _normalize_phrase(str(raw))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return tuple(out)


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items: Iterable[str] = (value,)
    elif isinstance(value, (list, tuple)):
        items = (str(x) for x in value)
    else:
        items = ()
    return tuple(s.strip() for s in items if str(s).strip())


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is subclass of int — guard
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    try:
        return bool(value)
    except Exception:
        return default


def _make_default_persona(config) -> Persona:
    """Synthesize the default persona from MechusConfig when no JSON exists.

    The system prompt is left empty — OpenAIResponder keeps using its
    built-in Mechus persona block when ``persona.is_default`` is True so
    behavior is byte-identical to single-persona Mechus.
    """
    wake_phrases = tuple(config.wake_phrases) if config.wake_phrases else (config.wake_phrase,)
    _audio_tags = bool(config.eleven_audio_tags)
    _filler = (
        _DEFAULT_FILLER_PHRASES + _AUDIO_TAGGED_FILLER_PHRASES
        if _audio_tags
        else _DEFAULT_FILLER_PHRASES
    )
    return Persona(
        id=DEFAULT_PERSONA_ID,
        name=ASSISTANT_NAME,
        wake_phrases=wake_phrases,
        voice_id=config.eleven_voice_id,
        model_id=config.eleven_model_id,
        system_prompt="",
        audio_tags=_audio_tags,
        max_words=int(config.max_response_words),
        temperature=config.response_temperature,
        tools=("realtime",),
        refusal_rules=(),
        filler_phrases=_filler,
        is_default=True,
    )


def _persona_from_dict(data: dict, config, *, source: str) -> Persona | None:
    pid = str(data.get("id") or "").strip().lower()
    if not pid:
        log.warning("Persona file %s missing 'id'; skipping", source)
        return None
    name = str(data.get("name") or pid).strip()
    wake_phrases = _coerce_phrase_tuple(data.get("wake_phrases"))
    if not wake_phrases:
        log.warning("Persona '%s' (%s) has no wake_phrases; skipping", pid, source)
        return None

    voice_id = data.get("voice_id")
    if voice_id is not None:
        voice_id = str(voice_id).strip() or None
        # Allow ${VAR} env-substitution so users can keep voice IDs in .env.
        if voice_id and voice_id.startswith("${") and voice_id.endswith("}"):
            env_key = voice_id[2:-1]
            voice_id = (os.getenv(env_key) or "").strip() or None
    if not voice_id:
        voice_id = config.eleven_voice_id

    model_id = data.get("model_id")
    if model_id is not None:
        model_id = str(model_id).strip() or None
    if not model_id:
        model_id = config.eleven_model_id

    is_default = pid == DEFAULT_PERSONA_ID
    system_prompt = str(data.get("system_prompt") or "").strip()
    if not system_prompt and not is_default:
        log.warning("Persona '%s' (%s) has no system_prompt; using default Mechus prompt", pid, source)

    audio_tags = _coerce_bool(data.get("audio_tags"), bool(config.eleven_audio_tags))
    max_words = _coerce_int(data.get("max_words"), int(config.max_response_words))
    temperature = _coerce_optional_float(data.get("temperature"))

    tools_raw = data.get("tools")
    tools = _coerce_str_tuple(tools_raw)
    if tools_raw is None:
        tools = ("realtime",)

    refusal_rules = _coerce_str_tuple(data.get("refusal_rules"))
    filler = _coerce_str_tuple(data.get("filler_phrases"))
    if not filler:
        # Append audio-tagged variants when this persona has audio tags enabled.
        filler = (
            _DEFAULT_FILLER_PHRASES + _AUDIO_TAGGED_FILLER_PHRASES
            if audio_tags
            else _DEFAULT_FILLER_PHRASES
        )

    return Persona(
        id=pid,
        name=name,
        wake_phrases=wake_phrases,
        voice_id=voice_id,
        model_id=model_id,
        system_prompt=system_prompt,
        audio_tags=audio_tags,
        max_words=max_words,
        temperature=temperature,
        tools=tools,
        refusal_rules=refusal_rules,
        filler_phrases=filler,
        is_default=is_default,
    )


class PersonaRegistry:
    """Loaded set of personas with wake-phrase resolution."""

    def __init__(self, personas: list[Persona], default: Persona):
        # Stable, default-first ordering; longest wake phrases first within the
        # rest so multi-word wakes win over single-word wakes ("hey penny" >
        # "penny") in resolve_by_wake().
        rest = [p for p in personas if p.id != default.id]
        rest.sort(key=lambda p: -max((len(w) for w in p.wake_phrases), default=0))
        self._personas: tuple[Persona, ...] = (default,) + tuple(rest)
        self._default: Persona = default
        self._by_id: dict[str, Persona] = {p.id: p for p in self._personas}
        self._by_phrase: dict[str, Persona] = {}
        # default phrases lose to JSON ones if both claim the same phrase
        for p in tuple(rest) + (default,):
            for phrase in p.wake_phrases:
                self._by_phrase.setdefault(phrase, p)

    @classmethod
    def load(
        cls,
        config,
        *,
        personas_dir: str | None = None,
        enabled_ids: Iterable[str] | None = None,
        locked_persona_id: str | None = None,
    ) -> "PersonaRegistry":
        """Load personas from JSON files. Falls back to the synthetic default.

        When ``locked_persona_id`` is set (or ``config.bot_persona_id`` / ``MECHUS_BOT_PERSONA_ID``),
        the registry contains only that persona — used so each invited bot process has one
        personality and wake set.
        """
        default = _make_default_persona(config)
        personas: list[Persona] = []

        locked = (locked_persona_id or "").strip().lower() or None
        if locked is None:
            locked = (getattr(config, "bot_persona_id", None) or "").strip().lower() or None

        d = personas_dir or os.environ.get("MECHUS_PERSONAS_DIR") or os.path.join("mechus", "personas")
        loaded_default_from_json = False
        if d and os.path.isdir(d):
            for entry in sorted(os.listdir(d)):
                if not entry.lower().endswith(".json"):
                    continue
                full = os.path.join(d, entry)
                try:
                    with open(full, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to load persona file %s: %s", full, exc)
                    continue
                if not isinstance(data, dict):
                    log.warning("Persona file %s does not contain a JSON object; skipping", full)
                    continue
                persona = _persona_from_dict(data, config, source=full)
                if persona is None:
                    continue
                if persona.id == DEFAULT_PERSONA_ID:
                    # JSON default REPLACES synthetic default (so users can fully customize Mechus).
                    default = persona
                    loaded_default_from_json = True
                    continue
                personas.append(persona)
        else:
            if d:
                log.info("MECHUS_PERSONAS_DIR not found (%s); using single default persona", d)

        if locked:
            resolved: Persona | None = None
            if default.id == locked:
                resolved = default
            else:
                resolved = next((p for p in personas if p.id == locked), None)
            if resolved is None:
                raise ValueError(
                    f"Unknown locked persona id {locked!r} (MECHUS_BOT_PERSONA_ID / locked_persona_id). "
                    "Use a persona id from JSON files in MECHUS_PERSONAS_DIR or 'default'."
                )
            registry = cls([resolved], resolved)
            log.info(
                "Personas locked to single id=%s (wake phrases: %s)",
                resolved.id,
                ", ".join(resolved.wake_phrases),
            )
            return registry

        # enabled filter (CSV list); always keeps default
        if enabled_ids is None:
            env_csv = os.environ.get("MECHUS_PERSONAS_ENABLED", "").strip()
            enabled_ids = [s.strip().lower() for s in env_csv.split(",") if s.strip()] if env_csv else None
        if enabled_ids:
            allow = {s.strip().lower() for s in enabled_ids if str(s).strip()}
            allow.add(DEFAULT_PERSONA_ID)
            personas = [p for p in personas if p.id in allow]

        registry = cls(personas, default)
        log.info(
            "Personas loaded: %s (default=%s, json=%s)",
            ", ".join(p.id for p in registry.personas()),
            default.id,
            loaded_default_from_json,
        )
        return registry

    def personas(self) -> tuple[Persona, ...]:
        return self._personas

    def default(self) -> Persona:
        return self._default

    def get(self, persona_id: str) -> Persona | None:
        return self._by_id.get((persona_id or "").strip().lower())

    def all_wake_phrases(self) -> tuple[str, ...]:
        """Flat phrase list for wake detection — longer phrases first so 'hey penny' wins over 'penny'."""
        seen: set[str] = set()
        out: list[str] = []
        # Sort all (persona, phrase) pairs by phrase length desc so multi-word wakes are tried first.
        pairs: list[tuple[int, str]] = []
        for p in self._personas:
            for phrase in p.wake_phrases:
                pairs.append((len(phrase), phrase))
        pairs.sort(key=lambda t: -t[0])
        for _, phrase in pairs:
            if phrase in seen:
                continue
            seen.add(phrase)
            out.append(phrase)
        return tuple(out)

    def resolve_by_wake(self, matched_phrase: str) -> Persona:
        """Pick the persona that owns ``matched_phrase``; fallback to default."""
        norm = _normalize_phrase(matched_phrase)
        if not norm:
            return self._default
        if norm in self._by_phrase:
            return self._by_phrase[norm]
        # Fuzzy: find any persona whose phrase appears as a token-aligned prefix.
        for p in self._personas:
            for phrase in p.wake_phrases:
                if phrase == norm:
                    return p
        # Last resort: scan substrings (e.g. STT slop produced "hey  penny").
        for phrase, persona in self._by_phrase.items():
            if phrase and phrase in norm:
                return persona
        return self._default

    def resolve_from_text(
        self,
        text: str,
        *,
        max_dist: int = 2,
    ) -> Persona:
        """Pick a persona by scanning text with fuzzy wake matching (Vosk path)."""
        from mechus.wake_detector import matched_wake_phrase, normalize_for_wake

        if not text:
            return self._default
        norm = normalize_for_wake(text)
        match = matched_wake_phrase(norm, self.all_wake_phrases(), max_dist=max_dist)
        if not match:
            return self._default
        return self.resolve_by_wake(match)
