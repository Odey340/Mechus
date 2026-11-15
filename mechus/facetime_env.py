"""FaceTime / Zoom worker env helpers (join UI, ElevenLabs, production latency)."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from urllib.parse import unquote

from mechus.brand import DEFAULT_WAKE_PHRASE

# Production wake word: brand name only. wake_detector.py aliases common STT mishears
# (kuv, hoov, coov, …) and fuzzy-matches one-edit slop (coov, hoov) at MAX_DIST=1.
# "hey mechus" and bare "mechus" both wake — longer phrase matched first in persona registry.
PRODUCTION_WAKE_PHRASE = DEFAULT_WAKE_PHRASE  # "hey mechus,mechus"

_PRODUCTION_LATENCY_KEYS: tuple[str, ...] = (
    "MECHUS_LOW_LATENCY",
    "MECHUS_WAKE_PHRASE",
    "MECHUS_WAKE_OPENER",
    "MECHUS_WAKE_OPENER_MAX_AUDIO_S",
    "MECHUS_WAKE_OPENER_EXTEND_CAP_S",
    "MECHUS_WAKE_OPENER_FADE_OUT_MS",
    "MECHUS_TTS_FADE_IN_AFTER_OPENER_MS",
    "MECHUS_THINKING_PREROLL",
    "MECHUS_THINKING_PREROLL_TRANSCRIPT_STT",
    "MECHUS_THINKING_PREROLL_TRANSCRIPT_ST_MAX_S",
    "MECHUS_THINKING_PREROLL_MAX_AUDIO_S",
    "MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S",
    "MECHUS_ELEVEN_TTS_STREAM",
    "MECHUS_ELEVEN_TTS_SPEED",
    "MECHUS_ELEVEN_TTS_STYLE",
    "MECHUS_ELEVEN_TTS_STABILITY",
    "MECHUS_ELEVEN_TTS_SIMILARITY_BOOST",
    "MECHUS_TTS_FADE_OUT_MS",
    "MECHUS_TTS_FADE_IN_MS",
)

_MEETING_WAKE_STRICT_KEYS: tuple[str, ...] = (
    "MECHUS_CONVERSATIONAL",
    "MECHUS_TRANSCRIPT_BARGE_IN",
    "MECHUS_TRANSCRIPT_BARGE_IN_STOP_ONLY",
    "MECHUS_INTERRUPT_ON_SPEECH",
    "MECHUS_INTERRUPT_ON_SPEECH_ZOOM",
    "MECHUS_WAKE_TRANSCRIPT_MAX_DIST",
    "MECHUS_WAKE_TRANSCRIPT_MERGE_S",
    "MECHUS_WAKE_TRANSCRIPT_MAX_LEADING_WORDS",
    "MECHUS_FOLLOWUP_WINDOW_S",
    "MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S",
    "MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S",
)


def facetime_worker_runtime_overrides() -> dict[str, str]:
    """Canonical runtime settings shared by the API and FaceTime worker.

    Keep worker-critical values in one place.  Previously ``api_server`` passed
    one set of latency/wake values and ``facetime_worker`` silently replaced
    several of them during boot, so the environment shown in ``docker inspect``
    did not describe the behavior that was actually running.
    """
    return {
        "MECHUS_CONVERSATIONAL": "0",
        "MECHUS_WAKE_OPENER": "1",
        "MECHUS_WAKE_OPENER_EXTEND_CAP_S": "18",
        "MECHUS_THINKING_PREROLL": "1",
        "MECHUS_THINKING_PREROLL_TRANSCRIPT_STT": "0",
        "MECHUS_TRANSCRIPT_BARGE_IN": "1",
        "MECHUS_TRANSCRIPT_BARGE_IN_STOP_ONLY": "1",
        "MECHUS_INTERRUPT_ON_SPEECH": "0",
        "MECHUS_INTERRUPT_ON_SPEECH_ZOOM": "0",
        "MECHUS_INPUT_DEVICE": "pulse",
        "MECHUS_OUTPUT_DEVICE": "pulse",
        "MECHUS_WAKE_TRANSCRIPT_MAX_DIST": "1",
        "MECHUS_WAKE_TRANSCRIPT_MERGE_S": "2.0",
        "MECHUS_WAKE_TRANSCRIPT_MAX_LEADING_WORDS": "8",
        "MECHUS_FOLLOWUP_WINDOW_S": "12",
        "MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S": "0.8",
        "MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S": "1.0",
        "MECHUS_TRANSCRIPT_CHUNK_SECONDS": "1.8",
        "MECHUS_TRANSCRIPT_END_SILENCE_SECONDS": "0.28",
        "MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S": "0",
        "MECHUS_MIN_SILENCE_BEFORE_SPEAKING_INSTANT_S": "0",
        "MECHUS_RESPONSE_TEMPERATURE": "0.35",
        "MECHUS_MAX_RESPONSE_WORDS": "70",
        "MECHUS_RESPONSE_OPENAI_TIMEOUT_S": "12",
        "MECHUS_RESPONSE_PROMPT_MAX_CHARS": "7000",
        "MECHUS_RETRIEVAL_MAX_CHARS": "2800",
    }


def _target_env(env: MutableMapping[str, str] | None) -> MutableMapping[str, str]:
    return os.environ if env is None else env


def apply_eleven_agent_model_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """ElevenLabs stack tuned for live meeting agents (per ElevenLabs model guide).

    - STT: ``scribe_v2_realtime`` (~150ms, WebSocket streaming)
    - TTS: ``eleven_flash_v2_5`` (~75ms, Agents Platform default)
    - ``eleven_v3`` is expressive but higher latency — use only when explicitly set.
    """
    target = _target_env(env)
    if (target.get("MECHUS_ELEVEN_API_KEY") or "").strip():
        target.setdefault("MECHUS_STT_BACKEND", "elevenlabs")
    target.setdefault("MECHUS_ELEVEN_STT_REALTIME", "1")
    target.setdefault("MECHUS_ELEVEN_STT_MODEL", "scribe_v2_realtime")
    model = (target.get("MECHUS_ELEVEN_MODEL_ID") or "").strip()
    if not model:
        target.setdefault("MECHUS_ELEVEN_MODEL_ID", "eleven_flash_v2_5")
        model = target["MECHUS_ELEVEN_MODEL_ID"].strip()
    use_v3 = "v3" in model.lower()
    if use_v3:
        target.setdefault("MECHUS_ELEVEN_AUDIO_TAGS", "1")
    else:
        target.setdefault("MECHUS_ELEVEN_AUDIO_TAGS", "0")
        target.setdefault("MECHUS_ELEVEN_TTS_LATENCY_OPT", "3")
    target.setdefault("MECHUS_ELEVEN_TTS_STREAM", "1")


def apply_eleven_tts_expressiveness_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Flash/Turbo TTS: lower stability + higher style = less flat delivery (still low-latency)."""
    target = _target_env(env)
    target.setdefault("MECHUS_ELEVEN_TTS_SPEED", "1.09")
    target.setdefault("MECHUS_ELEVEN_TTS_STYLE", "0.86")
    target.setdefault("MECHUS_ELEVEN_TTS_STABILITY", "0.12")
    target.setdefault("MECHUS_ELEVEN_TTS_SIMILARITY_BOOST", "0.72")
    target.setdefault("MECHUS_TTS_FADE_OUT_MS", "0")
    target.setdefault("MECHUS_TTS_FADE_IN_MS", "10")


def apply_zoom_meeting_interaction_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Zoom: human pacing + natural interruption (not stop-only)."""
    target = _target_env(env)
    target["MECHUS_TRANSCRIPT_BARGE_IN"] = "1"
    # Any directed "Mechus, …" wake may cut a long reply — not just "Mechus, stop".
    target["MECHUS_TRANSCRIPT_BARGE_IN_STOP_ONLY"] = "0"
    target["MECHUS_TTS_INTERRUPT_GRACE_MS"] = "900"
    # Room for a complete spoken thought — 18/110 clipped mid-clause and sounded cut off.
    target["MECHUS_WAKE_SPOKEN_MAX_WORDS"] = "28"
    target["MECHUS_WAKE_SPOKEN_MAX_CHARS"] = "180"
    target["MECHUS_MAX_RESPONSE_WORDS"] = "70"
    # Play optional spoken_continuation so answers aren't just a clipped head sentence.
    target["MECHUS_WAKE_TTS_CONTINUATION"] = "1"
    target["MECHUS_TTS_PHRASE_MAX_CHARS"] = "280"
    target["MECHUS_TTS_PHRASE_PAUSE_SCALE"] = "1.0"


def apply_zoom_tts_clarity_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Zoom virtual mic: natural Flash TTS without uh-uh spam or flat IVR tone.

    Glitchiness on Zoom is mostly pacing/fades (see zoom_sdk_join); these knobs
    avoid the ultra-expressive (0.12/0.86) and ultra-flat (0.55/0.38) extremes.
    """
    target = _target_env(env)
    target["MECHUS_ELEVEN_TTS_SPEED"] = "1.0"
    target["MECHUS_ELEVEN_TTS_STYLE"] = "0.50"
    target["MECHUS_ELEVEN_TTS_STABILITY"] = "0.40"
    target.setdefault("MECHUS_ELEVEN_TTS_SIMILARITY_BOOST", "0.75")
    # Agent fades off — ZoomAudioOutput applies one edge ramp (avoids stacked mush).
    target["MECHUS_TTS_FADE_OUT_MS"] = "0"
    target["MECHUS_TTS_FADE_IN_MS"] = "0"
    target["MECHUS_TTS_FADE_IN_AFTER_OPENER_MS"] = "0"
    # Latency 1: cleaner phonemes than 2/3 (slightly slower first byte).
    target["MECHUS_ELEVEN_TTS_LATENCY_OPT"] = "1"


def apply_multilingual_env_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Auto-detect STT language and reply in the speaker's language (production default)."""
    target = _target_env(env)
    target.setdefault("MECHUS_MULTILINGUAL", "1")
    if target.get("MECHUS_MULTILINGUAL", "1").lower() in ("0", "false", "no", "off"):
        target.setdefault("MECHUS_WHISPER_LANGUAGE", "en")
        return
    lang = (target.get("MECHUS_WHISPER_LANGUAGE") or "").strip().lower()
    # Drop English-only defaults from .env; keep explicit non-English overrides (e.g. es).
    if not lang or lang in ("en", "en-us", "en-gb", "auto", "detect", "multilingual"):
        target.pop("MECHUS_WHISPER_LANGUAGE", None)


def apply_production_latency_env_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Fast time-to-first-audio: cached opener ack + thinking preroll during LLM (Zoom + FaceTime)."""
    target = _target_env(env)
    apply_multilingual_env_defaults(target)
    target.setdefault("MECHUS_LOW_LATENCY", "1")
    apply_eleven_agent_model_defaults(target)
    apply_eleven_tts_expressiveness_defaults(target)
    target.setdefault("MECHUS_WAKE_PHRASE", PRODUCTION_WAKE_PHRASE)
    target.setdefault("MECHUS_WAKE_OPENER", "1")
    target.setdefault("MECHUS_WAKE_OPENER_MAX_AUDIO_S", "1.65")
    # Gapless opener+extend bridge fills LLM wait (parallel thinking preroll is too short).
    target.setdefault("MECHUS_WAKE_OPENER_EXTEND_CAP_S", "18")
    # Opener hands off into reply TTS — fade-out on the bridge clip sounds like a sigh.
    target.setdefault("MECHUS_WAKE_OPENER_FADE_OUT_MS", "0")
    target.setdefault("MECHUS_TTS_FADE_IN_AFTER_OPENER_MS", "12")
    target.setdefault("MECHUS_THINKING_PREROLL", "1")
    # Gapless opener+extend bridge fills the LLM wait; parallel transcript preroll stacks fillers.
    target.setdefault("MECHUS_THINKING_PREROLL_TRANSCRIPT_STT", "0")
    target.setdefault("MECHUS_THINKING_PREROLL_TRANSCRIPT_ST_MAX_S", "0.45")
    target.setdefault("MECHUS_THINKING_PREROLL_MAX_AUDIO_S", "0.4")
    target.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S", "0")
    target.setdefault("MECHUS_ELEVEN_TTS_STREAM", "1")


def production_latency_env_dict(env: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Docker / subprocess -e bundle after apply_production_latency_env_defaults()."""
    target = _target_env(env)
    apply_production_latency_env_defaults(target)
    apply_meeting_transcript_wake_strict_env(target)
    keys = _PRODUCTION_LATENCY_KEYS + _MEETING_WAKE_STRICT_KEYS
    return {k: target[k] for k in keys if k in target}


def display_name_from_join_url(url: str) -> str:
    """Read prefill name from FaceTime join hash param ``l=`` (Apple web join links)."""
    u = (url or "").strip()
    if "#" not in u:
        return ""
    frag = u.split("#", 1)[1]
    for part in frag.split("&"):
        if part.startswith("l="):
            return unquote(part[2:], errors="replace").strip()
    return ""


def page_is_name_lobby(page_text: str) -> bool:
    """True when Chromium is on the pre-join name screen (not in the live call UI)."""
    t = (page_text or "").lower()
    if "enter your name to join" in t:
        return True
    if 'id="name-entry"' in t and "continue-button" in t:
        return True
    if "start-component" in t and "name-input" in t and "continue-button" in t:
        return True
    return False


def apply_facetime_eleven_env_defaults(env: MutableMapping[str, str] | None = None) -> str:
    """Configure ElevenLabs env for FaceTime workers; return resolved model id.

    Audio tags and JSON ``private_thoughts`` only apply to ``eleven_v3``. Forcing v3 +
    tags on flash/turbo models breaks TTS (brackets spoken literally or HTTP 400).
    """
    target = _target_env(env)
    apply_eleven_agent_model_defaults(target)
    model = (target.get("MECHUS_ELEVEN_MODEL_ID") or "eleven_flash_v2_5").strip()
    use_v3 = "v3" in model.lower()
    if use_v3:
        target.setdefault("MECHUS_ELEVEN_AUDIO_TAGS", "1")
        target.setdefault("MECHUS_RESPONSE_JSON_MODE", "1")
    else:
        target["MECHUS_ELEVEN_AUDIO_TAGS"] = "0"
        target.setdefault("MECHUS_RESPONSE_JSON_MODE", "1")
    if not use_v3:
        target.setdefault("MECHUS_ELEVEN_TTS_LATENCY_OPT", "3")
    elif (target.get("MECHUS_ELEVEN_TTS_LATENCY_OPT") or "").strip():
        target.pop("MECHUS_ELEVEN_TTS_LATENCY_OPT", None)
    target.setdefault("MECHUS_ELEVEN_OUTPUT_FORMAT", "pcm_16000")
    target.setdefault("MECHUS_ELEVEN_TTS_STREAM", "1")
    target.setdefault("MECHUS_WAKE_SPOKEN_MAX_WORDS", "20")
    target.setdefault("MECHUS_WAKE_SPOKEN_MAX_CHARS", "80")
    target.setdefault("MECHUS_MAX_RESPONSE_WORDS", "70")
    apply_production_latency_env_defaults(target)
    target.setdefault("MECHUS_WAKE_TTS_CONTINUATION", "1")
    return model


def apply_meeting_transcript_wake_strict_env(env: MutableMapping[str, str] | None = None) -> None:
    """Zoom/FaceTime: strict wake + echo-safe barge-in (stop directives only)."""
    target = _target_env(env)
    target.setdefault("MECHUS_CONVERSATIONAL", "0")
    target.setdefault("MECHUS_TRANSCRIPT_BARGE_IN", "1")
    target.setdefault("MECHUS_TRANSCRIPT_BARGE_IN_STOP_ONLY", "1")
    target.setdefault("MECHUS_INTERRUPT_ON_SPEECH", "0")
    target.setdefault("MECHUS_INTERRUPT_ON_SPEECH_ZOOM", "0")
    target.setdefault("MECHUS_WAKE_TRANSCRIPT_MAX_DIST", "2")
    target.setdefault("MECHUS_WAKE_TRANSCRIPT_MERGE_S", "2.0")
    target.setdefault("MECHUS_WAKE_TRANSCRIPT_MAX_LEADING_WORDS", "8")
    target.setdefault("MECHUS_FOLLOWUP_WINDOW_S", "22")
    target.setdefault("MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S", "2.5")
    target.setdefault("MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S", "3.0")


def apply_facetime_worker_env_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """FaceTime worker: gapless opener bridge + thinking preroll (no stacked parallel fillers)."""
    target = _target_env(env)
    apply_production_latency_env_defaults(target)
    apply_meeting_transcript_wake_strict_env(target)
    # apply_facetime_eleven_env_defaults() runs first; force worker-critical flags so a
    # stale host .env or an old override block cannot disable the opener bridge.
    target["MECHUS_WAKE_OPENER"] = "1"
    target["MECHUS_THINKING_PREROLL"] = "1"
    target["MECHUS_THINKING_PREROLL_TRANSCRIPT_STT"] = "0"
    if not (target.get("MECHUS_WAKE_OPENER_EXTEND_CAP_S") or "").strip():
        target["MECHUS_WAKE_OPENER_EXTEND_CAP_S"] = "18"
    target.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S", "0")
    target.setdefault("MECHUS_ELEVEN_TTS_STREAM", "1")
    target["MECHUS_TRANSCRIPT_CHUNK_SECONDS"] = target.get(
        "MECHUS_TRANSCRIPT_CHUNK_SECONDS", "1.8"
    )
    target.setdefault("MECHUS_TRANSCRIPT_END_SILENCE_SECONDS", "0.28")
    target["MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S"] = "0.8"
    target["MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S"] = "1.0"
    target.setdefault("MECHUS_ELEVEN_STT_SEGMENT_ISOLATED", "1")
    target["MECHUS_ELEVEN_STT_REALTIME"] = "0"
    target.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_INSTANT_S", "0")


def apply_zoom_worker_env_defaults(env: MutableMapping[str, str] | None = None) -> None:
    """Production latency + Eleven agent models for Zoom SDK workers (virtual mic)."""
    target = _target_env(env)
    apply_production_latency_env_defaults(target)
    apply_meeting_transcript_wake_strict_env(target)
    apply_eleven_agent_model_defaults(target)
    target.setdefault("MECHUS_ELEVEN_OUTPUT_FORMAT", "pcm_32000")
    target.setdefault("MECHUS_ELEVEN_STT_SEGMENT_ISOLATED", "1")
    # Realtime WebSocket commits stall on Zoom; batch Scribe is faster and reliable.
    target["MECHUS_ELEVEN_STT_REALTIME"] = "0"
    target["MECHUS_ELEVEN_STT_MODEL"] = "scribe_v2"
    apply_zoom_tts_clarity_defaults(target)
    apply_zoom_meeting_interaction_defaults(target)
    target["MECHUS_ELEVEN_TTS_STREAM"] = "1"
    target.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S", "0")
    target.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_INSTANT_S", "0")
    target["MECHUS_WAKE_OPENER_MAX_AUDIO_S"] = "2.0"
    target["MECHUS_WAKE_OPENER"] = "1"
    target["MECHUS_THINKING_PREROLL"] = "1"
    # Wake opener bridge owns the wait — parallel transcript preroll stacks duplicate fillers.
    target["MECHUS_THINKING_PREROLL_TRANSCRIPT_STT"] = "0"
    target["MECHUS_THINKING_PREROLL_TRANSCRIPT_ST_MAX_S"] = "0"
    target["MECHUS_THINKING_PREROLL_MAX_AUDIO_S"] = "1.4"
    target["MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S"] = "0.8"
    target["MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S"] = "1.0"
    target.setdefault("MECHUS_TRANSCRIPT_BARGE_CHUNK_SECONDS", "0.95")
    target.setdefault("MECHUS_TRANSCRIPT_BARGE_END_SILENCE_SECONDS", "0.14")
    target["MECHUS_TRANSCRIPT_CHUNK_SECONDS"] = target.get(
        "MECHUS_TRANSCRIPT_CHUNK_SECONDS", "1.4"
    )
    target.setdefault("MECHUS_TRANSCRIPT_END_SILENCE_SECONDS", "0.22")


def facetime_worker_eleven_overrides() -> dict[str, str]:
    """Docker ``-e`` overrides for Eleven on FaceTime (respect host ``MECHUS_ELEVEN_MODEL_ID``)."""
    # Build subprocess settings in a copy. Request handling must never rewrite the
    # API server's own process environment (or leak worker presets across meetings).
    target = dict(os.environ)
    model = (target.get("MECHUS_ELEVEN_MODEL_ID") or "eleven_flash_v2_5").strip()
    use_v3 = "v3" in model.lower()
    out: dict[str, str] = {
        "MECHUS_ELEVEN_MODEL_ID": model,
        "MECHUS_ELEVEN_OUTPUT_FORMAT": target.get("MECHUS_ELEVEN_OUTPUT_FORMAT", "pcm_16000"),
        "MECHUS_ELEVEN_AUDIO_TAGS": "1" if use_v3 else "0",
        "MECHUS_RESPONSE_JSON_MODE": "1",
    }
    if not use_v3:
        out["MECHUS_ELEVEN_TTS_LATENCY_OPT"] = target.get("MECHUS_ELEVEN_TTS_LATENCY_OPT", "3")
    out["MECHUS_ELEVEN_TTS_STREAM"] = target.get("MECHUS_ELEVEN_TTS_STREAM", "1")
    apply_eleven_tts_expressiveness_defaults(target)
    for _k in (
        "MECHUS_ELEVEN_TTS_SPEED",
        "MECHUS_ELEVEN_TTS_STYLE",
        "MECHUS_ELEVEN_TTS_STABILITY",
        "MECHUS_ELEVEN_TTS_SIMILARITY_BOOST",
        "MECHUS_TTS_FADE_OUT_MS",
        "MECHUS_TTS_FADE_IN_MS",
    ):
        if _k in target:
            out[_k] = target[_k]
    out["MECHUS_WAKE_SPOKEN_MAX_WORDS"] = target.get("MECHUS_WAKE_SPOKEN_MAX_WORDS", "20")
    out["MECHUS_WAKE_SPOKEN_MAX_CHARS"] = target.get("MECHUS_WAKE_SPOKEN_MAX_CHARS", "80")
    out["MECHUS_MAX_RESPONSE_WORDS"] = target.get("MECHUS_MAX_RESPONSE_WORDS", "70")
    apply_production_latency_env_defaults(target)
    out.update(production_latency_env_dict(target))
    out["MECHUS_WAKE_TTS_CONTINUATION"] = target.get("MECHUS_WAKE_TTS_CONTINUATION", "0")
    return out
