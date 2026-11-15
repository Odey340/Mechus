from __future__ import annotations

from collections import deque
import json
import logging
import os
import queue
import signal
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import webrtcvad

from mechus.audio import (
    AudioInput,
    AudioOutput,
    RollingPCMBuffer,
    SilenceMonitor,
    UtteranceRecorder,
    apply_f32_linear_fade_ends,
    append_f32_tail_silence,
    truncate_f32_to_max_duration,
    pcm16_to_float32,
    float32_to_pcm16,
    trim_f32_silence_edges,
    trim_silence_pcm16,
)
from mechus.brand import ASSISTANT_NAME as _ASSISTANT_NAME
from mechus.config import MechusConfig
from mechus.dsp import pcm16_resample_linear, resample_f32_linear
from mechus.diarization import PyannoteCloudDiarizer, PyannoteDiarizer, SpeakerSegment
from mechus.diarization_async import AsyncCloudDiarizer
from mechus.logging import setup_logging
from mechus.memory import ConversationMemory, NoteStore
from mechus.thoughts import ThoughtStream
from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.intent import (
    INTENT_ACTION_ITEMS,
    INTENT_OPEN_QUESTIONS,
    classify_intent,
)
from mechus.meeting_store import MeetingStore
from mechus.retrieval import retrieve_for_question
from mechus.intent import detect_meeting_scope
from mechus.dialog_memory import (
    build_retrieval_query,
    extract_salient_terms,
    is_dialog_continuation,
    is_interrupt_request,
    is_lone_proper_name,
    is_repeat_request,
    is_similar_topic_followup,
    is_transcript_backchannel,
    is_vague_followup,
    merge_salient,
    qualifies_as_transcript_followup_without_wake,
)
from mechus.structured_response import topic_from_request as _topic_from_request_impl


from mechus.command_validator import CommandValidator
from mechus.response_cache import ResponseCache
from mechus.parallel_processor import ParallelProcessor
from mechus.personas import Persona, PersonaRegistry, _THINKING_WARMUP_PHRASES
from mechus.responder import HybridResponder, OpenAIResponder, ResponseInput, RuleResponder, rephrase_repeat_spoken
from mechus.wake_detector import (
    normalize_for_wake,
    detect_wake,
    is_false_address_use,
    is_product_mention_use,
    should_suppress_transcript_wake,
    line_has_explicit_wake_token,
    matched_wake_phrase,
    is_incomplete_transcript_command,
    preamble_before_wake_last,
    qualifies_as_wake_command,
    qualifies_as_directed_transcript_wake,
    remainder_is_stop_directive,
    remainder_qualifies_as_wake_command,
    matched_wake_phrase_last as wd_matched_wake_last,
    strip_wake_phrase as wd_strip_wake,
    strip_wake_phrase_last as wd_strip_wake_last,
)
from mechus.intent_classifier import (
    classify_pipeline_intent,
    _is_presence_check,
    INTENT_CLARIFICATION,
    INTENT_CONFIRM,
    INTENT_GREETING,
    INTENT_HELP,
    INTENT_PRESENCE_CHECK,
    INTENT_RECAP,
    INTENT_STOP,
    INTENT_UNKNOWN,
)

# Meeting transcript wake: block promo/chitchat, not directed hear-me after wake.
_MEETING_NON_ACTION_INTENTS = frozenset(
    {
        INTENT_GREETING,
        INTENT_HELP,
        INTENT_UNKNOWN,
        INTENT_CLARIFICATION,
        INTENT_CONFIRM,
    }
)

from mechus.command_router import route as route_command

# Substrings common in Mechus's own instant-reply / TTS lines — ignore as transcript wakes (echo).
_MECHUS_SELF_ECHO_FRAGMENTS: tuple[str, ...] = (
    "ask me for a recap",
    "ask for a recap",
    "what we decided",
    "action items",
    "i've been following along",
    "been listening the whole time",
    "just ask",
    "i'm in — what do you need",
    "i'm here — ask",
    "try asking for a recap",
    "loud and clear",
    "go ahead",
    "i'm listening",
)
from mechus.stt import (
    AssemblyAITranscriber,
    DeepgramTranscriber,
    ElevenLabsTranscriber,
    ElevenLabsRealtimeTranscriber,
    FasterWhisperTranscriber,
    OpenAIWhisperAPITranscriber,
    Transcriber,
    Transcript,
    VoskTranscriber,
)
from mechus.text import clean_spoken_text, phrases_for_natural_speech, strip_preroll_echo_from_response, strip_wake_word
import re
from mechus.tts import ElevenLabsTTSEngine, PiperTTSEngine
from mechus.transcript import (
    normalize_stt_command_text,
    TranscriptChunk,
    TranscriptChunker,
    TranscriptRefiner,
    clean_transcript_text,
    extract_tags,
)
from mechus.transcript_noise import is_noise_only_transcript
from mechus.wake import (
    FallbackWakeDetector,
    NoOpWakeDetector,
    OpenWakeWordDetector,
    VoskWakeDetector,
    WakeDetector,
)

log = logging.getLogger(__name__)


def _notes_dir_abspath() -> str:
    from mechus.notes_paths import resolve_notes_dir

    return resolve_notes_dir()


def _safe_note_session_id(meeting_id: str) -> str:
    s = (meeting_id or "session").strip()
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s[:200] if s else "session"


_LOCATION_TO_TZ = {
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "madrid": "Europe/Madrid",
    "rome": "Europe/Rome",
    "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "singapore": "Asia/Singapore",
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "auckland": "Pacific/Auckland",
    "hong kong": "Asia/Hong_Kong",
}

_TZ_ABBR_TO_IANA = {
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "gmt": "Etc/GMT",
    "utc": "UTC",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "ist": "Asia/Kolkata",
    "jst": "Asia/Tokyo",
}


def _normalize_timezone_value(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower()
    if "/" in v:
        try:
            ZoneInfo(v)
            return v
        except Exception:
            return ""
    if low in _TZ_ABBR_TO_IANA:
        return _TZ_ABBR_TO_IANA[low]
    if low in _LOCATION_TO_TZ:
        return _LOCATION_TO_TZ[low]
    return ""


def _extract_timezone_from_utterance(text: str) -> tuple[str, str]:
    """Return (iana_tz, evidence) from utterance text, else ('','')."""
    t = (text or "").strip()
    if not t:
        return "", ""
    low = t.lower()
    m = re.search(r"\b(?:i(?:'m| am)|we(?:'re| are)|calling|joining)\s+(?:in|from)\s+([a-z][a-z\s,.-]{1,40})\b", low)
    if m:
        loc = " ".join(m.group(1).replace(".", " ").split()).strip(" ,.-")
        if loc:
            tz = _normalize_timezone_value(loc)
            if tz:
                return tz, loc
            first = loc.split(",", 1)[0].strip()
            tz2 = _normalize_timezone_value(first)
            if tz2:
                return tz2, first
    m2 = re.search(r"\b([A-Z]{2,4})\s*(?:time|timezone)\b", t)
    if m2:
        abbr = m2.group(1).lower()
        tz = _normalize_timezone_value(abbr)
        if tz:
            return tz, abbr.upper()
    return "", ""


@dataclass(frozen=True)
class WakeCycleResult:
    request_text: str
    response_text: str
    is_instant: bool = False
    persona: Persona | None = None


class MechusAgent:
    def __init__(
        self,
        config: MechusConfig,
        audio_in: "AudioInput | None" = None,
        audio_out: "AudioOutput | None" = None,
    ):
        setup_logging(config.log_level)
        self._config = config
        self._stop = threading.Event()
        self._session_id = time.strftime("session-%Y%m%d-%H%M%S")

        if audio_in is not None:
            self._audio_in = audio_in
        else:
            self._audio_in = AudioInput(config.input_device, config.sample_rate_hz, config.frame_ms)
        if audio_out is not None:
            self._audio_out = audio_out
        else:
            self._audio_out = AudioOutput(config.output_device)
        self._wake_sr = 16000

        _join_backend = (os.getenv("MECHUS_JOIN_BACKEND", "") or "").strip().lower()
        _audio_out_name = type(self._audio_out).__name__.lower()
        _is_zoom = _join_backend.startswith("zoom") or ("zoom" in _audio_out_name)
        _is_facetime = _join_backend == "facetime_web"
        # Chert FaceTime API: audio arrives over a LiveKit room, not PulseAudio, so it
        # shares FaceTime's transcript-wake behavior but none of the Pulse re-routing.
        _is_chert = _join_backend == "facetime_chert"
        self._is_facetime_web: bool = _is_facetime
        self._transcript_wake_only: bool = _is_zoom or _is_facetime or _is_chert
        self._force_transcript_wake: bool = self._transcript_wake_only

        self._silence = SilenceMonitor(config.sample_rate_hz, config.frame_ms, vad_mode=2)

        max_roll = max(config.context_seconds, 2.0)
        self._rolling = RollingPCMBuffer(max_roll, config.sample_rate_hz, config.frame_ms) if max_roll > 0 else None
        # Session-scoped memory: one ConversationMemory and optional ContextIntegrityEngine per meeting_id (no cross-bleed)
        self._memories: dict[str, ConversationMemory] = {}
        self._context_engines: dict[str, ContextIntegrityEngine] = {}
        self._current_meeting_id = self._session_id
        self._note_store = NoteStore(root_dir=_notes_dir_abspath(), session_id=self._session_id)
        self._thought_stream = ThoughtStream()

        # Persona registry — built before wake detector so all personas' wake phrases can fire.
        # Falls back to a synthetic default persona when mechus/personas/ is empty, preserving
        # single-persona behavior for users who haven't customized.
        self._personas: PersonaRegistry = PersonaRegistry.load(
            config,
            personas_dir=getattr(config, "personas_dir", None),
            enabled_ids=getattr(config, "personas_enabled", None) or None,
            locked_persona_id=getattr(config, "bot_persona_id", None),
        )
        self._all_wake_phrases: tuple[str, ...] = self._personas.all_wake_phrases()
        self._wake: WakeDetector = self._build_wake_detector(
            config, transcript_only=self._transcript_wake_only
        )
        self._interrupt_vad = webrtcvad.Vad(2)
        self._interrupt_min_frames = max(1, int(config.interrupt_min_speech_ms / config.frame_ms))
        self._recorder = UtteranceRecorder(
            sample_rate_hz=config.sample_rate_hz,
            frame_ms=config.frame_ms,
            min_seconds=config.min_request_seconds,
            max_seconds=config.max_request_seconds,
            end_silence_seconds=config.end_silence_seconds,
            pre_roll_seconds=config.pre_roll_seconds,
            vad_mode=2,
        )
        self._stt = self._build_transcriber(config)
        self._transcript_chunker = None
        self._transcript_refiner = None
        self._transcript_chunk_id = 0
        self._diarizer = None
        self._async_diarizer: AsyncCloudDiarizer | None = None
        self._speaker_map: dict[str, str] = {}
        self._speaker_transcript_chunkers: dict[str, TranscriptChunker] = {}
        self._speaker_last_frame_ts: dict[str, float] = {}
        self._speaker_display_names: dict[str, str] = {}
        self._call_participants: set[str] = set()
        # Active-speaker timeline for mixed-audio platforms (FaceTime/Meet): the
        # browser worker periodically reports which participant tile the platform
        # shows as "currently speaking", timestamped with time.monotonic() (same
        # clock as audio frames / TranscriptChunk.start_ts). We correlate each
        # transcript chunk's [start_ts, end_ts] window against this timeline to
        # attribute the line to a real name — the only reliable voice→name anchor
        # when there's no per-speaker audio stream. Entries: (monotonic_ts, name).
        self._active_speaker_timeline: deque[tuple[float, str]] = deque(maxlen=512)
        self._active_speaker_lock = threading.Lock()
        self._speaker_count = 0
        self._stream_chunkers: dict[str, TranscriptChunker] = {}
        if config.transcript_enabled:
            self._transcript_chunker = self._make_transcript_chunker(default_speaker_id=None)
            self._transcript_chunk_queue: queue.Queue | None = None
            self._transcript_chunk_thread: threading.Thread | None = None
            self._deferred_transcript_chunks: list[
                tuple[TranscriptChunk, str | None, str | None]
            ] = []
            self._start_transcript_chunk_worker()
            # Only rewrite transcripts when explicitly enabled to avoid extra latency/cost and
            # prevent LLM cleanup from mutating otherwise accurate raw STT output.
            if config.transcript_rewrite and config.openai_api_key:
                model = config.transcript_model or config.openai_chat_model or "gpt-4o-mini"
                self._transcript_refiner = TranscriptRefiner(
                    api_key=config.openai_api_key,
                    model=model,
                    max_words=config.transcript_max_words,
                )
        if config.zoom_stream_attribution and config.diarization_enabled:
            log.info(
                "MECHUS_ZOOM_STREAM_ATTRIBUTION is on: diarization disabled (one speaker per meeting audio stream)"
            )
        if config.diarization_enabled and not config.zoom_stream_attribution:
            try:
                backend = (config.diarization_backend or "pyannote-local").lower()
                if backend in {"pyannote-cloud", "pyannoteai", "pyannote-api"}:
                    if not config.diarization_api_key:
                        log.warning("Diarization disabled (missing MECHUS_DIARIZATION_API_KEY)")
                    else:
                        model = config.diarization_model or "precision-2"
                        self._diarizer = PyannoteCloudDiarizer(
                            api_key=config.diarization_api_key,
                            model=model,
                            api_base_url=config.diarization_api_base_url,
                            exclusive=config.diarization_exclusive,
                            min_speakers=config.diarization_min_speakers,
                            max_speakers=config.diarization_max_speakers,
                            timeout_s=config.diarization_timeout_s,
                            poll_interval_s=config.diarization_poll_interval_s,
                        )
                        if config.diarization_async:
                            self._async_diarizer = AsyncCloudDiarizer(self._diarizer)
                            self._async_diarizer.start()
                            log.info(
                                "Async cloud diarization enabled (non-blocking, FaceTime mode)"
                            )
                elif config.diarization_model:
                    self._diarizer = PyannoteDiarizer(
                        model=config.diarization_model,
                        device=config.diarization_device,
                        hf_token=config.diarization_hf_token,
                        min_speakers=config.diarization_min_speakers,
                        max_speakers=config.diarization_max_speakers,
                    )
                else:
                    log.warning("Diarization disabled (no MECHUS_DIARIZATION_MODEL)")
            except RuntimeError as exc:
                log.warning("Diarization disabled (%s)", exc)
        self._responder = self._build_responder(config)
        self._tts = self._build_tts(config)
        
        # Pre-warm TTS and pre-cache thinking sounds for instant playback.
        # Keyed by (persona_id, phrase) so each persona's filler plays in its own voice.
        self._thinking_audio_cache: dict[tuple[str, str], object] = {}
        try:
            import threading as _warmup_threading
            def _warmup_phrase_order(fillers: tuple[str, ...]) -> list[str]:
                """Diverse category clips first so openers match intent, not only ack-yeah."""
                filler_set = {str(p).strip().lower() for p in fillers if str(p).strip()}
                ordered: list[str] = []
                seen: set[str] = set()
                for p in _THINKING_WARMUP_PHRASES:
                    key = p.strip().lower()
                    if key and key in filler_set and key not in seen:
                        seen.add(key)
                        ordered.append(p.strip())
                for p in fillers:
                    key = str(p).strip().lower()
                    if key and key not in seen:
                        seen.add(key)
                        ordered.append(str(p).strip())
                return ordered

            def _warmup():
                # Personas often share one ElevenLabs engine (same voice_id). Memoize by (engine, phrase)
                # so we do one HTTP call per distinct clip, then alias into each persona's cache key.
                max_phrases = max(4, int(os.getenv("MECHUS_THINKING_WARMUP_MAX", "12")))
                memo: dict[tuple[int, str], object | None] = {}
                for persona in self._personas.personas():
                    engine = self._tts_for(persona)
                    if engine is None:
                        continue
                    for phrase in _warmup_phrase_order(persona.filler_phrases)[:max_phrases]:
                        p = str(phrase).strip()
                        if not p:
                            continue
                        ek = (id(engine), p)
                        if ek not in memo:
                            try:
                                memo[ek] = engine.synthesize(p)
                            except Exception:
                                memo[ek] = None
                        audio = memo[ek]
                        if audio is not None:
                            self._thinking_audio_cache[(persona.id, p)] = audio
            _warmup_threading.Thread(target=_warmup, daemon=True).start()
        except Exception:
            pass
        
        # Command validator to ensure actionable commands; recognizes every persona's wake.
        _validator_phrases = self._all_wake_phrases or tuple(config.wake_phrases)
        self._command_validator = CommandValidator(
            wake_phrase=_validator_phrases[0] if _validator_phrases else config.wake_phrase,
            wake_phrases=_validator_phrases,
        )
        self._last_latency: dict[str, float] | None = None
        self._last_audio_frame_at: float = 0.0  # When we last received an audio frame (for "can Mechus hear me?" check)
        self._last_transcript_write: tuple[str, str, float] | None = None
        # Align transcript-STT wake with Vosk cooldown so we do not double-respond.
        self._last_wake_fire_monotonic_s: float = -1e9
        self._last_dialog_wake_monotonic_s: float = -1e9
        self._mechus_display_name_lc = (
            (os.getenv("MECHUS_DISPLAY_NAME") or _ASSISTANT_NAME).strip().lower() or _ASSISTANT_NAME.lower()
        )
        self._tts_playing: bool = False
        self._thinking_preroll_thread: threading.Thread | None = None
        # Unified preroll→response audio pipeline: one persistent play_streaming session
        # that receives the preroll clip as its first chunk(s), then receives the real
        # response TTS as subsequent chunks — no stream re-open between preroll and reply.
        import queue as _q_mod
        self._preroll_pipeline_q: _q_mod.Queue | None = None
        self._preroll_pipeline_sr: int = 0
        self._preroll_pipeline_thread: threading.Thread | None = None
        # Main reply ElevenLabs/Piper HTTP synthesize started before wake cycle returns so it
        # overlaps note_store / logging and the post-cycle silence + thinking-preroll joins.
        self._main_tts_presynth_thread: threading.Thread | None = None
        self._main_tts_presynth_box: list = []
        self._wake_opener_played: bool = False
        self._bridge_finale_preroll_played: bool = False
        self._last_thinking_phrase_spoken: str = ""
        # Set while the wake-opener thread is bridging LLM latency (see _run_wake_opener).
        self._wake_opener_thread: threading.Thread | None = None
        self._wake_opener_bridge_active: bool = False
        self._pending_transcript_wake: tuple[str, bytes, str] | None = None
        self._tts_cancel_requested: bool = False
        self._spoken_executive_recap: str = ""
        self._wake_pipeline_thread: threading.Thread | None = None
        self._wake_pipeline_lock = threading.Lock()
        self._tts_frame_loop_active: bool = False
        self._barge_stream_chunkers: dict[str, TranscriptChunker] = {}
        self._barge_speaker_chunkers: dict[str, TranscriptChunker] = {}
        # Saved playback position when TTS is cut off so a false barge-in can resume.
        self._tts_resume_state: dict[str, Any] | None = None
        if _is_zoom:
            log.info(
                "Wake mode: transcript-only (Zoom) — audio wake disabled; wake triggers come from transcript STT"
            )
        elif _is_facetime or _is_chert:
            log.info(
                "Wake mode: transcript-only (FaceTime) — audio wake disabled; wake triggers come from transcript STT"
            )
        # Merge short consecutive STT fragments before transcript-wake (split across VAD chunks).
        self._transcript_wake_merge_prev: tuple[str, float] | None = None
        # Recent human lines (seconds apart) prepended when wake+command arrives in a later chunk.
        # (text, monotonic_ts, speaker_key)
        self._transcript_preamble_buf: deque[tuple[str, float, str]] = deque(maxlen=48)
        self._transcript_wake_preamble_ctx: str = ""
        self._transcript_wake_echo_mute_until: float = 0.0
        self._team_id: str | None = None
        self._team_context_text: str = ""
        # Most recent ISO-639-1/2 language code detected by STT (or None when
        # the backend doesn't report it). Propagated to the responder so
        # ElevenLabs v3 (multilingual) speaks the reply in the user's language.
        self._last_user_language: str | None = None
        # When True, Mechus uses shorter/slower/clearer spoken replies (thick accent,
        # non-native English, or non-English language detected by STT).
        self._accent_adapt_speech: bool = False
        # Auto-loaded summaries from prior meetings (most recent N), formatted
        # for the responder's TEAM_SHARED_CONTEXT slot. Set by set_meeting().
        # Disable by setting MECHUS_CROSS_MEETING_MEMORY=0.
        self._cross_meeting_memory_text: str = ""
        # When non-None, cross-meeting memory is pinned to these specific IDs
        # (set via API / join params) rather than auto-selected by recency.
        self._pinned_memory_ids: list[str] | None = None
        
        # Response cache for common questions (no accuracy loss, just faster)
        self._response_cache = ResponseCache(
            ttl_seconds=self._config.response_cache_ttl_seconds,
            max_size=self._config.response_cache_max_size,
        )
        self._dialog_last_user: str | None = None
        self._dialog_last_mechus: str | None = None
        self._dialog_last_mechus_spoken: str = ""
        self._dialog_salient_terms: list[str] = []

        # Parallel processor for overlapping operations
        self._parallel_processor = ParallelProcessor(max_workers=2)
        self._recent_thinking_phrases: list[str] = []  # track last 3 to avoid repeats
        self._participant_timezones: dict[str, str] = {}
        self._timezone_conflicts: list[dict[str, object]] = []
        self._last_place_context: str = ""

    def emit_thought(
        self,
        kind: str,
        text: str,
        *,
        planned_speech: str | None = None,
        connections: list[str] | None = None,
    ) -> None:
        """Append a reasoning entry to the live thought stream and persist to NoteStore."""
        try:
            self._thought_stream.append(
                kind,
                text,
                planned_speech=planned_speech,
                connections=connections,
            )
        except Exception:
            pass
        try:
            _meta: dict = {"thought_kind": kind}
            if planned_speech:
                _meta["planned_speech"] = planned_speech
            if connections:
                _meta["connections"] = connections
            self._note_store.add(kind="thought", text=text, meta=_meta)
        except Exception:
            pass

    def get_thoughts_for_status(self) -> list[dict]:
        """Return a snapshot of agent reasoning for GET /api/status."""
        try:
            return self._thought_stream.snapshot()
        except Exception:
            return []

    def set_meeting(self, meeting_id: str) -> None:
        """Switch to a meeting. Every call starts this meeting fresh: clear persisted and in-memory state."""
        self._current_meeting_id = meeting_id or self._session_id
        log.info("Meeting scope: %s", self._current_meeting_id)
        # Persisted: clear so we never load chunks/snapshots from a previous session (same link → same id).
        MeetingStore.clear_meeting(self._current_meeting_id)
        # In-memory: force fresh memory, context engine, and meeting store for this meeting (no cross-meeting bleed).
        if self._current_meeting_id in self._memories:
            del self._memories[self._current_meeting_id]
        if self._current_meeting_id in self._context_engines:
            del self._context_engines[self._current_meeting_id]
        _stores = getattr(self, "_meeting_stores", None)
        if _stores is not None and self._current_meeting_id in _stores:
            del _stores[self._current_meeting_id]
        # Response cache is global; clear it so we never return answers from a different meeting.
        self._response_cache.clear()
        self._dialog_last_user = None
        self._dialog_last_mechus = None
        self._dialog_last_mechus_spoken = ""
        self._dialog_salient_terms = []
        self._stream_chunkers.clear()
        self._speaker_transcript_chunkers.clear()
        self._speaker_last_frame_ts.clear()
        self._speaker_display_names.clear()
        self._speaker_map.clear()
        self._call_participants.clear()
        with self._active_speaker_lock:
            self._active_speaker_timeline.clear()
        self._speaker_count = 0
        self._transcript_wake_merge_prev = None
        self._transcript_preamble_buf.clear()
        self._transcript_wake_preamble_ctx = ""
        self._transcript_wake_echo_mute_until = 0.0
        self._team_id = None
        self._team_context_text = ""
        self._cross_meeting_memory_text = ""
        self._last_user_language = None
        self._accent_adapt_speech = False
        self._teardown_preroll_pipeline(wait=False)
        self._participant_timezones.clear()
        self._timezone_conflicts.clear()
        # Transcript JSONL is keyed by meeting_id once set_meeting() runs (see NoteStore path).
        note_id = _safe_note_session_id(self._current_meeting_id)
        self._note_store = NoteStore(root_dir=_notes_dir_abspath(), session_id=note_id)
        log.info("NoteStore session file: %s", self._note_store.path)
        # Auto-load prior meeting summaries so Mechus has continuity across calls
        # ("hey Mechus, what did we decide last time?"). Cleared above; re-populated
        # here, AFTER current-meeting state has been wiped so we never confuse
        # prior meeting facts with this one.
        if self._pinned_memory_ids is not None:
            self.set_cross_meeting_memory_from_ids(self._pinned_memory_ids)
        else:
            self._reload_cross_meeting_memory()

    def set_team_context(self, team_id: str, *, max_chars: int = 6000) -> None:
        """Attach opt-in team shared context for this live meeting."""
        from mechus.team_context import TeamContextStore

        self._team_id = (team_id or "").strip() or None
        self._team_context_text = ""
        if not self._team_id:
            return
        try:
            self._team_context_text = TeamContextStore(self._team_id).format_for_prompt(max_chars=max_chars)
            log.info(
                "Team context loaded: team_id=%s chars=%s",
                self._team_id,
                len(self._team_context_text),
            )
        except Exception as exc:
            log.warning("Could not load team context for team_id=%s: %s", self._team_id, exc)
            self._team_context_text = ""

    def _reload_cross_meeting_memory(
        self,
        *,
        max_meetings: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        """Auto-load summaries from the N most recent prior meetings.

        Reads ``final_summary.json`` files from ``MECHUS_MEETING_STORE_DIR``
        (default ``mechus_meetings``), excludes the current meeting, and stores
        a compact prompt snippet on ``self._cross_meeting_memory_text`` that's
        merged with team context when retrieval runs.

        Disabled by ``MECHUS_CROSS_MEETING_MEMORY=0`` (defaults ON). Soft-fails
        on any IO/parse error so a corrupt summary file can never block live
        meetings.
        """
        flag = (os.environ.get("MECHUS_CROSS_MEETING_MEMORY") or "1").strip().lower()
        if flag in ("0", "false", "off", "no"):
            self._cross_meeting_memory_text = ""
            return
        try:
            n = int(max_meetings or os.environ.get("MECHUS_CROSS_MEETING_MEMORY_N", "3"))
        except Exception:
            n = 3
        try:
            cap = int(max_chars or os.environ.get("MECHUS_CROSS_MEETING_MEMORY_MAX_CHARS", "2400"))
        except Exception:
            cap = 2400
        n = max(1, min(n, 10))
        cap = max(400, min(cap, 8000))
        try:
            store_dir = os.environ.get("MECHUS_MEETING_STORE_DIR") or "mechus_meetings"
            base = os.path.abspath(store_dir)
            if not os.path.isdir(base):
                self._cross_meeting_memory_text = ""
                return
            # Link key for the CURRENT meeting: prior sessions of the same link are
            # the "this is the recurring meeting, remember last time" memory and get
            # priority over unrelated recent meetings.
            from mechus.meeting_summary import meeting_link_key

            cur_link = (os.environ.get("MECHUS_MEETING_LINK") or "").strip()
            cur_key = meeting_link_key(cur_link) if cur_link else ""

            entries: list[tuple[float, str, dict]] = []
            current = self._current_meeting_id or ""
            for name in os.listdir(base):
                if name == current:
                    continue
                path = os.path.join(base, name, "final_summary.json")
                if not os.path.isfile(path):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                    with open(path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception as exc:
                    log.debug("Skipping unreadable summary %s: %s", path, exc)
                    continue
                if not isinstance(data, dict):
                    continue
                entries.append((mtime, name, data))
            if not entries:
                self._cross_meeting_memory_text = ""
                return
            entries.sort(key=lambda t: t[0], reverse=True)

            # Same-link sessions first (most recent), then fill remaining slots with
            # other recent meetings for general context.
            same_link = (
                [e for e in entries if str(e[2].get("link_key") or "") == cur_key]
                if cur_key
                else []
            )
            same_ids = {e[1] for e in same_link}
            others = [e for e in entries if e[1] not in same_ids]
            picked = (same_link + others)[:n]
            text = self._format_prior_meetings_for_prompt(
                picked, max_chars=cap, same_link_ids=same_ids
            )
            self._cross_meeting_memory_text = text
            log.info(
                "Cross-meeting memory loaded: meetings=%d (same_link=%d) chars=%d",
                len(picked),
                len(same_link),
                len(text),
            )
        except Exception as exc:
            log.warning("Cross-meeting memory load failed (non-fatal): %s", exc)
            self._cross_meeting_memory_text = ""

    def set_cross_meeting_memory_from_ids(
        self,
        meeting_ids: list[str],
        *,
        max_chars: int | None = None,
    ) -> dict:
        """Manually pin specific past meetings as context for this session.

        Called when the user picks meetings from the dashboard 'Contextual memory'
        picker, overriding the automatic recency-based selection.

        Returns a status dict: {"loaded": int, "chars": int, "meeting_ids": list}
        so the API can confirm what was loaded.  Clears any previous memory and
        replaces it with exactly the requested meetings (in the order given).
        Soft-fails on missing / corrupt files.
        """
        try:
            cap = int(max_chars or os.environ.get("MECHUS_CROSS_MEETING_MEMORY_MAX_CHARS", "2400"))
        except Exception:
            cap = 2400
        cap = max(400, min(cap, 12000))

        store_dir = os.environ.get("MECHUS_MEETING_STORE_DIR") or "mechus_meetings"
        base = os.path.abspath(store_dir)
        from mechus.meeting_summary import meeting_link_key

        cur_link = (os.environ.get("MECHUS_MEETING_LINK") or "").strip()
        cur_key = meeting_link_key(cur_link) if cur_link else ""

        entries: list[tuple[float, str, dict]] = []
        same_link_ids: set[str] = set()
        for mid in meeting_ids:
            if not mid or mid == (self._current_meeting_id or ""):
                continue
            path = os.path.join(base, mid, "final_summary.json")
            if not os.path.isfile(path):
                log.debug("set_cross_meeting_memory_from_ids: no summary for %s", mid)
                continue
            try:
                mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                log.debug("set_cross_meeting_memory_from_ids: skipping %s: %s", mid, exc)
                continue
            if not isinstance(data, dict):
                continue
            entries.append((mtime, mid, data))
            if cur_key and str(data.get("link_key") or "") == cur_key:
                same_link_ids.add(mid)

        if entries:
            text = self._format_prior_meetings_for_prompt(
                entries, max_chars=cap, same_link_ids=same_link_ids
            )
        else:
            text = ""

        self._cross_meeting_memory_text = text
        loaded_ids = [e[1] for e in entries]
        log.info(
            "Cross-meeting memory pinned: meetings=%d same_link=%d chars=%d ids=%s",
            len(entries),
            len(same_link_ids),
            len(text),
            loaded_ids,
        )
        return {"loaded": len(entries), "chars": len(text), "meeting_ids": loaded_ids}

    @staticmethod
    def list_past_meetings(
        *,
        exclude_meeting_id: str | None = None,
        max_results: int = 50,
    ) -> list[dict]:
        """Return metadata for all past meetings that have a final_summary.json.

        Used by the dashboard to populate the 'Contextual memory' meeting picker.
        Each entry: {meeting_id, generated_at, executive_summary_preview,
                     link_key, has_action_items, has_decisions, char_count}.
        Sorted by most-recent first.
        """
        store_dir = os.environ.get("MECHUS_MEETING_STORE_DIR") or "mechus_meetings"
        base = os.path.abspath(store_dir)
        if not os.path.isdir(base):
            return []
        results: list[tuple[float, dict]] = []
        for name in os.listdir(base):
            if name == (exclude_meeting_id or ""):
                continue
            path = os.path.join(base, name, "final_summary.json")
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            exec_sum = (data.get("executive_summary") or "").strip()
            preview = (exec_sum[:180] + "…") if len(exec_sum) > 180 else exec_sum
            entry = {
                "meeting_id": name,
                "title": (data.get("title") or "").strip() or None,
                "generated_at": data.get("generated_at") or data.get("generated_at_unix") or "",
                "meeting_link": data.get("meeting_link") or "",
                "link_key": data.get("link_key") or "",
                "executive_summary_preview": preview,
                "topics": data.get("topics") or [],
                "has_action_items": bool(data.get("action_items")),
                "has_decisions": bool(data.get("decisions")),
                "action_item_count": len(data.get("action_items") or []),
                "decision_count": len(data.get("decisions") or []),
                "char_count": len(json.dumps(data)),
            }
            results.append((mtime, entry))
        results.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in results[:max_results]]

    @staticmethod
    def _format_prior_meetings_for_prompt(
        entries: list[tuple[float, str, dict]],
        *,
        max_chars: int,
        same_link_ids: set[str] | None = None,
    ) -> str:
        """Render a few prior meetings into a compact, prompt-friendly block.

        Format intentionally mirrors ``team_context.format_for_prompt`` so the
        responder treats both equivalently as TEAM_SHARED_CONTEXT. Sessions from
        the SAME meeting link are labeled so the model treats them as earlier
        installments of THIS recurring meeting.
        """
        same_link_ids = same_link_ids or set()
        lines: list[str] = []
        if same_link_ids:
            lines.append(
                "NOTE: 'earlier session of THIS meeting' entries are prior times this "
                "same meeting link was used. Treat them as continuous history — recall "
                "decisions, action items, and open threads from them and speak as if you "
                "remember being here before."
            )
            lines.append("")
        for _mtime, mid, data in entries:
            exec_summary = (data.get("executive_summary") or "").strip()
            decisions = data.get("decisions") or []
            actions = data.get("action_items") or []
            risks = data.get("risks") or []
            label = mid[-12:] if len(mid) > 12 else mid
            if mid in same_link_ids:
                lines.append(f"--- earlier session of THIS meeting: {label} ---")
            else:
                lines.append(f"--- prior meeting: {label} ---")
            if exec_summary:
                lines.append(f"summary: {exec_summary}")
            if isinstance(decisions, list) and decisions:
                top: list[str] = []
                for d in decisions[:3]:
                    if isinstance(d, dict):
                        # final_summary.json stores decisions as {"id", "value"}.
                        val = (d.get("value") or d.get("id") or "").strip()
                        if val:
                            top.append(val)
                    elif isinstance(d, str) and d.strip():
                        top.append(d.strip())
                if top:
                    lines.append("decisions: " + " | ".join(top))
            if isinstance(actions, list) and actions:
                a_strs: list[str] = []
                for a in actions[:5]:
                    if isinstance(a, dict):
                        owner = (a.get("owner") or "").strip()
                        task = (a.get("task") or a.get("text") or "").strip()
                        if not task:
                            continue
                        a_strs.append(f"{owner+': ' if owner else ''}{task}")
                    elif isinstance(a, str) and a.strip():
                        a_strs.append(a.strip())
                if a_strs:
                    lines.append("action items: " + " | ".join(a_strs))
            if isinstance(risks, list) and risks:
                top = [str(r).strip() for r in risks[:2] if str(r).strip()]
                if top:
                    lines.append("risks: " + " | ".join(top))
            lines.append("")
        text = "\n".join(lines).strip()
        if len(text) > max_chars:
            text = text[: max_chars - 3].rstrip() + "..."
        return text

    def _combined_shared_context_text(self) -> str:
        """Concatenate team context + auto-loaded prior meeting memory."""
        parts: list[str] = []
        if self._cross_meeting_memory_text:
            parts.append(self._cross_meeting_memory_text)
        if self._team_context_text:
            parts.append(self._team_context_text)
        return "\n\n".join(p for p in parts if p)

    def merge_speaker_aliases(self, mapping: dict[str, str]) -> None:
        """Merge manual speaker labels (e.g. diarization id or Zoom stream name → display string)."""
        for k, v in mapping.items():
            if k is not None and v is not None:
                self._speaker_map[str(k).strip()] = str(v).strip()

    def merge_participant_timezones(self, mapping: dict[str, str]) -> None:
        """Merge participant timezone hints from dashboard metadata (name/id -> tz or location)."""
        for who, raw_tz in (mapping or {}).items():
            key = str(who or "").strip()
            if not key:
                continue
            tz = _normalize_timezone_value(str(raw_tz or ""))
            if not tz:
                continue
            self._participant_timezones[key] = tz

    def register_call_participants(self, names: list[str] | tuple[str, ...] | set[str]) -> int:
        """Register platform roster names for current-meeting context.

        FaceTime/Meet often expose a roster in the browser DOM but only provide
        mixed audio. This makes Mechus aware of who is on the call even before
        diarization can label voices. Returns number of newly-added names.
        """
        added_names: list[str] = []
        existing = {p.lower() for p in self._call_participants}
        for raw in names or []:
            name = str(raw or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in existing:
                continue
            existing.add(key)
            self._call_participants.add(name)
            added_names.append(name)
        if not added_names:
            return 0

        for name in added_names:
            self._get_memory().add_segment(f"Participant in this meeting: {name}")

        eng = self._get_context_engine()
        if eng is not None:
            try:
                eng.register_participants(added_names)
            except Exception as exc:  # noqa: BLE001
                log.debug("Participant registration in context engine failed: %s", exc)
        log.info("Registered call participant(s): %s", ", ".join(added_names))
        return len(added_names)

    def note_active_speaker(self, name: str | None) -> None:
        """Record who the platform shows as currently speaking (mixed-audio mode).

        Called from the browser worker's poll loop. Timestamped with
        time.monotonic() so it aligns with audio-frame / TranscriptChunk clocks
        in the same process. Used by _resolve_active_speaker_for_window() to put
        a real name on each spoken chunk. No-op for blank/unknown names.
        """
        nm = str(name or "").strip()
        if not nm or nm.lower() == getattr(self, "_mechus_display_name_lc", "mechus"):
            return
        # Register the name as a participant too, so "who's on the call" works
        # even if the roster scrape missed it.
        try:
            if nm.lower() not in {p.lower() for p in self._call_participants}:
                self.register_call_participants([nm])
        except Exception:  # noqa: BLE001
            pass
        with self._active_speaker_lock:
            self._active_speaker_timeline.append((time.monotonic(), nm))

    def _resolve_active_speaker_for_window(
        self, start_ts: float, end_ts: float
    ) -> str | None:
        """Best name for a chunk's time window from the active-speaker timeline.

        start_ts/end_ts are time.monotonic() values (TranscriptChunk clock).
        We tally active-speaker observations that fall within the window (with a
        small slack on each side, since the speaking indicator lags the audio)
        and return the most frequent name. Returns None when we have no signal.
        """
        slack = float(getattr(self._config, "active_speaker_slack_s", 1.5) or 1.5)
        lo = start_ts - slack
        hi = end_ts + slack
        with self._active_speaker_lock:
            snapshot = list(self._active_speaker_timeline)
        if not snapshot:
            return None
        votes: dict[str, int] = {}
        for ts, name in snapshot:
            if lo <= ts <= hi:
                votes[name] = votes.get(name, 0) + 1
        if not votes:
            # No observation inside the window — fall back to the nearest prior
            # observation (someone was speaking just before the chunk closed).
            prior = [(ts, n) for ts, n in snapshot if ts <= hi]
            if not prior:
                return None
            return prior[-1][1]
        return max(votes, key=lambda k: votes[k])

    def get_timezone_context(self) -> dict[str, object]:
        return {
            "participant_timezones": dict(self._participant_timezones),
            "timezone_conflicts": list(self._timezone_conflicts[-5:]),
        }

    def _track_timezone_conflicts(self, utterance: str, speaker_label: str | None) -> None:
        """Heuristic conflict detector for scheduling mentions across participant timezones."""
        if len(self._participant_timezones) < 2:
            return
        low = (utterance or "").lower()
        if " at " not in low and not re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", low):
            return
        if not any(k in low for k in ("schedule", "meeting", "call", "tomorrow", "today", "let's", "lets", "sync")):
            return
        mt = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
        if not mt:
            return
        hr = int(mt.group(1))
        minute = int(mt.group(2) or 0)
        ampm = mt.group(3).lower()
        if hr < 1 or hr > 12:
            return
        if ampm == "pm" and hr != 12:
            hr += 12
        if ampm == "am" and hr == 12:
            hr = 0

        ref_tz = ""
        if speaker_label:
            ref_tz = self._participant_timezones.get(speaker_label, "")
        if not ref_tz and self._participant_timezones:
            ref_tz = next(iter(self._participant_timezones.values()))
        if not ref_tz:
            return
        now_ref = datetime.now(ZoneInfo(ref_tz))
        proposed_ref = now_ref.replace(hour=hr, minute=minute, second=0, microsecond=0)
        if proposed_ref < now_ref:
            proposed_ref = proposed_ref + timedelta(days=1)

        impacted: list[str] = []
        for who, tz in self._participant_timezones.items():
            try:
                local = proposed_ref.astimezone(ZoneInfo(tz))
            except Exception:
                continue
            if local.hour < 8 or local.hour >= 20:
                impacted.append(f"{who} ({local.strftime('%I:%M %p %Z')})")
        if not impacted:
            return

        issue = {
            "utterance": utterance[:180],
            "reference_timezone": ref_tz,
            "impacted": impacted[:6],
            "timestamp": time.time(),
        }
        if self._timezone_conflicts and self._timezone_conflicts[-1].get("utterance") == issue["utterance"]:
            return
        self._timezone_conflicts.append(issue)
        self._get_memory().add_segment(
            "Potential timezone conflict: " + ", ".join(impacted[:3]) + f" may be outside working hours for '{utterance[:60]}'."
        )

    def _maybe_learn_timezone_from_utterance(self, cleaned: str, speaker_label: str | None) -> None:
        if not cleaned or not speaker_label:
            return
        tz, evidence = _extract_timezone_from_utterance(cleaned)
        if not tz:
            return
        prev = self._participant_timezones.get(speaker_label)
        self._participant_timezones[speaker_label] = tz
        if prev != tz:
            self._get_memory().add_segment(f"Timezone noted: {speaker_label} is in {tz} (from '{evidence}').")

    def _make_transcript_chunker(
        self, default_speaker_id: str | None, *, barge: bool = False
    ) -> TranscriptChunker:
        if barge:
            max_s = float(os.environ.get("MECHUS_TRANSCRIPT_BARGE_CHUNK_SECONDS", "0.95"))
            end_s = float(os.environ.get("MECHUS_TRANSCRIPT_BARGE_END_SILENCE_SECONDS", "0.14"))
            min_s = min(float(self._config.transcript_min_seconds), 0.35)
        else:
            max_s = self._config.transcript_chunk_seconds
            end_s = self._config.transcript_end_silence_seconds
            min_s = self._config.transcript_min_seconds
        return TranscriptChunker(
            sample_rate_hz=self._config.sample_rate_hz,
            frame_ms=self._config.frame_ms,
            min_seconds=min_s,
            max_seconds=max_s,
            end_silence_seconds=end_s,
            vad_mode=2,
            default_speaker_id=default_speaker_id,
        )

    def _use_barge_transcript_chunkers(self) -> bool:
        return bool(
            self._tts_playing
            and self._transcript_wake_only
            and self._config.transcript_barge_in_enabled
        )

    def _clear_barge_transcript_chunkers(self) -> None:
        self._barge_stream_chunkers.clear()
        self._barge_speaker_chunkers.clear()

    def _get_memory(self) -> ConversationMemory:
        """Current meeting's conversation memory (session-scoped, no cross-bleed)."""
        if self._current_meeting_id not in self._memories:
            self._memories[self._current_meeting_id] = ConversationMemory(
                max_chars=self._config.memory_max_chars,
                recent_chars=self._config.memory_recent_chars,
            )
        return self._memories[self._current_meeting_id]

    def _get_context_engine(self) -> ContextIntegrityEngine | None:
        """Current meeting's context integrity engine (only when transcript enabled)."""
        if not self._config.transcript_enabled:
            return None
        if self._current_meeting_id not in self._context_engines:
            self._context_engines[self._current_meeting_id] = ContextIntegrityEngine(
                meeting_id=self._current_meeting_id,
            )
        return self._context_engines[self._current_meeting_id]

    def _get_meeting_store(self) -> MeetingStore | None:
        """Per-meeting store for persistence (chunks + snapshot). Uses MECHUS_MEETING_STORE_DIR if set."""
        if not getattr(self._config, "persist_meeting", True):
            return None
        if self._current_meeting_id not in getattr(self, "_meeting_stores", {}):
            if not hasattr(self, "_meeting_stores"):
                self._meeting_stores = {}
            self._meeting_stores[self._current_meeting_id] = MeetingStore(self._current_meeting_id)
        return self._meeting_stores.get(self._current_meeting_id)

    def _maybe_roll_chapter_after_ingest(self, eng: ContextIntegrityEngine) -> None:
        """Append a chapter rollup when tagged chunk count hits MECHUS_CHAPTER_EVERY_N_CHUNKS.

        Runs on a background thread: the chapter map step makes a blocking LLM
        call (up to ~35s), which must not stall the live audio/wake loop.
        """
        if self._get_meeting_store() is None:
            return
        n = getattr(self._config, "chapter_every_n_chunks", 0) or 0
        if n <= 0:
            return
        # Only roll when we just crossed a multiple of N (maybe_roll_chapter also
        # guards this, but checking here avoids spawning needless threads).
        if len(eng._tagged_chunks) % n != 0:  # noqa: SLF001
            return
        # Serialize chapter rolls so two threads don't summarize the same window.
        if getattr(self, "_chapter_roll_active", False):
            return
        self._chapter_roll_active = True
        meeting_id = self._current_meeting_id
        base = os.environ.get("MECHUS_MEETING_STORE_DIR", "mechus_meetings")
        api_key = self._config.openai_api_key
        chat_model = self._config.openai_thinking_model or "gpt-4o-mini"
        max_tokens = self._config.chapter_llm_max_tokens
        # Pin the boundary now so the background roll summarizes this exact window
        # even though the live engine keeps ingesting while it runs.
        chunk_count = len(eng._tagged_chunks)  # noqa: SLF001

        def _worker() -> None:
            try:
                from mechus.meeting_chapters import maybe_roll_chapter

                maybe_roll_chapter(
                    meeting_id,
                    eng,
                    every_n_chunks=n,
                    base_dir=base,
                    api_key=api_key,
                    chat_model=chat_model,
                    chapter_llm_max_tokens=max_tokens,
                    chunk_count=chunk_count,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("Chapter roll failed: %s", exc)
            finally:
                self._chapter_roll_active = False

        threading.Thread(
            target=_worker, daemon=True, name="mechus-chapter-roll"
        ).start()

    def get_memory(self, meeting_id: str | None = None) -> ConversationMemory:
        """Return conversation memory for a meeting (for API/summary). If meeting_id is None, use current meeting."""
        mid = meeting_id or self._current_meeting_id
        if mid not in self._memories:
            self._memories[mid] = ConversationMemory(
                max_chars=self._config.memory_max_chars,
                recent_chars=self._config.memory_recent_chars,
            )
        return self._memories[mid]

    def get_meeting_data_for_summary(self) -> tuple[str, dict]:
        """
        Gather full transcript and canonical state for end-of-meeting summary.
        Returns (transcript_text, state) for use by meeting_summary.generate_final_summary.
        """
        from mechus.meeting_summary import build_summary_evidence_text

        transcript_text = "(No transcript captured.)"
        state: dict = {"decisions": [], "action_items": [], "open_issues": []}
        _max_evidence = getattr(self._config, "summary_max_transcript_chars", 28000)

        store = self._get_meeting_store()
        _ch_base = os.environ.get("MECHUS_MEETING_STORE_DIR", "mechus_meetings")
        if store is not None:
            enriched = store.load_chunks_enriched()
            from mechus.meeting_summary import _collect_participant_names

            state["participant_names"] = _collect_participant_names(enriched)
            transcript_text = build_summary_evidence_text(
                enriched,
                max_chars=_max_evidence,
                meeting_id=self._current_meeting_id,
                chapters_base_dir=_ch_base,
            )
            snap = store.load_snapshot()
            if snap:
                state["decisions"] = [{"id": d.get("id"), "value": d.get("value")} for d in (snap.get("decisions") or [])]
                state["action_items"] = [{"owner": a.get("owner"), "task": a.get("task"), "deadline": a.get("deadline")} for a in (snap.get("action_items") or [])]
                open_q = snap.get("open_questions") or []
                state["open_issues"] = [{"status": "open", "display_summary": q, "topic_key": q} for q in open_q]

        engine = self._get_context_engine()
        if engine is not None:
            state = engine.get_canonical_state_for_qa()
            if store is not None:
                transcript_text = build_summary_evidence_text(
                    store.load_chunks_enriched(),
                    max_chars=_max_evidence,
                    meeting_id=self._current_meeting_id,
                    chapters_base_dir=_ch_base,
                )

        spoken = self.get_spoken_executive_recap()
        if spoken:
            state["spoken_executive_recap"] = spoken

        return transcript_text, state

    def get_spoken_executive_recap(self) -> str:
        """Last full-meeting recap Mechus spoke aloud (used for dashboard summary)."""
        return (getattr(self, "_spoken_executive_recap", None) or "").strip()

    def get_live_recap_state_light(self) -> tuple[str, dict]:
        """Lightweight state for live dashboard recap (no LLM, no heavy evidence build)."""
        state: dict = {}
        engine = self._get_context_engine()
        if engine is not None:
            state = engine.get_canonical_state_for_qa()
        spoken = self.get_spoken_executive_recap()
        if spoken:
            state["spoken_executive_recap"] = spoken
        transcript_text = ""
        try:
            mid = self._current_meeting_id
            memory = self.get_memory(mid) if mid else None
            if memory:
                summary_text = memory.retrieve("summary of everything discussed so far")
                if summary_text:
                    transcript_text = summary_text[:4000]
        except Exception:
            pass
        return transcript_text, state

    def get_live_recap_for_dashboard(self) -> dict:
        """Rolling recap payload for worker-status + live transcript API."""
        from mechus.meeting_summary import build_live_partial_summary

        transcript_text, state = self.get_live_recap_state_light()
        return build_live_partial_summary(transcript_text, state)

    def set_visual_query_handler(self, handler) -> None:
        """Register ``handler(question, timeout_s=...) -> (answer|None, error_code)``.

        Only the FaceTime worker sets this (when visual questions are enabled).
        When unset, visual-sounding commands follow the normal response path.
        """
        self._visual_query_handler = handler if callable(handler) else None

    def get_runtime_activity(self) -> dict[str, object]:
        """Small read-only activity snapshot for non-audio presentation layers."""
        thinking_thread = getattr(self, "_thinking_preroll_thread", None)
        return {
            "tts_playing": bool(getattr(self, "_tts_playing", False)),
            "wake_cycle_active": bool(
                getattr(self, "_wake_opener_bridge_active", False)
                or (thinking_thread is not None and thinking_thread.is_alive())
            ),
            "stopping": bool(getattr(self, "_stop", None) and self._stop.is_set()),
        }

    def get_mechus_interactions(self) -> list[dict]:
        """
        Return all Mechus Q&A interactions (user query + Mechus response) from this session
        for the dashboard. Each item is {"user_query": str, "mechus_response": str, "ts": float}.
        """
        out = []
        try:
            for note in self._note_store.read_notes(kind="wake_cycle"):
                req = note.get("request") or note.get("request_raw") or ""
                resp = note.get("response") or ""
                if req or resp:
                    out.append({
                        "user_query": (req or "").strip(),
                        "mechus_response": (resp or "").strip(),
                        "ts": note.get("ts"),
                    })
        except Exception:
            pass
        return out

    def _build_wake_detector(
        self, config: MechusConfig, *, transcript_only: bool = False
    ) -> WakeDetector:
        # Vosk wake matches against the union of all persona wake phrases (one bot,
        # many names). Falls back to config.wake_phrases when no persona JSONs exist.
        wake_phrases = self._all_wake_phrases or tuple(config.wake_phrases)
        primary_phrase = wake_phrases[0] if wake_phrases else config.wake_phrase
        if transcript_only:
            return NoOpWakeDetector()
        if config.wake_backend == "openwakeword":
            if not config.openwakeword_model_path:
                raise ValueError("MECHUS_OPENWAKEWORD_MODEL_PATH is required for openwakeword backend")
            try:
                return OpenWakeWordDetector(
                    model_path=config.openwakeword_model_path,
                    sample_rate_hz=self._wake_sr,
                    threshold=config.wake_threshold,
                    cooldown_s=config.wake_cooldown_s,
                )
            except RuntimeError as exc:
                log.warning("openWakeWord unavailable (%s); falling back to Vosk", exc)
                return VoskWakeDetector(
                    wake_phrase=primary_phrase,
                    sample_rate_hz=self._wake_sr,
                    model_path=config.vosk_model_path,
                    cooldown_s=config.wake_cooldown_s,
                    wake_phrases=wake_phrases,
                )
        if config.wake_backend in {"openwakeword+vosk", "hybrid"}:
            if not config.openwakeword_model_path:
                raise ValueError("MECHUS_OPENWAKEWORD_MODEL_PATH is required for openwakeword backend")
            fallback = VoskWakeDetector(
                wake_phrase=primary_phrase,
                sample_rate_hz=self._wake_sr,
                model_path=config.vosk_model_path,
                cooldown_s=config.wake_cooldown_s,
                wake_phrases=wake_phrases,
            )
            try:
                primary = OpenWakeWordDetector(
                    model_path=config.openwakeword_model_path,
                    sample_rate_hz=self._wake_sr,
                    threshold=config.wake_threshold,
                    cooldown_s=config.wake_cooldown_s,
                )
            except RuntimeError as exc:
                log.warning("openWakeWord unavailable (%s); using Vosk only", exc)
                return fallback
            return FallbackWakeDetector(primary=primary, fallback=fallback)
        if config.wake_backend == "vosk":
            return VoskWakeDetector(
                wake_phrase=primary_phrase,
                sample_rate_hz=self._wake_sr,
                model_path=config.vosk_model_path,
                cooldown_s=config.wake_cooldown_s,
                wake_phrases=wake_phrases,
            )
        raise ValueError(f"Unknown MECHUS_WAKE_BACKEND: {config.wake_backend}")

    def _build_responder(self, config: MechusConfig):
        rules = RuleResponder(
            max_response_words=config.max_response_words,
            audio_tags_enabled=config.eleven_audio_tags,
        )
        if config.llm_backend == "openai" and config.openai_api_key:
            openai_resp = OpenAIResponder(
                api_key=config.openai_api_key,
                model=config.openai_response_model or config.openai_chat_model or "gpt-4o-mini",
                thinking_model=config.openai_thinking_model,
                max_response_words=config.max_response_words,
                openai_max_tokens=config.response_openai_max_tokens,
                openai_max_tokens_list=config.response_openai_max_tokens_list,
                timeout_s=config.response_openai_timeout_s,
                prompt_max_chars=config.response_prompt_max_chars,
                json_mode=config.response_json_mode,
                stream_enabled=config.response_stream_enabled,
                temperature_override=config.response_temperature,
                temp_meeting=config.response_temp_meeting,
                audio_tags_enabled=config.eleven_audio_tags,
            )
            return HybridResponder(primary=openai_resp, fallback=rules)
        return rules

    def _build_transcriber(self, config: MechusConfig) -> Transcriber:
        """Build the appropriate transcriber based on config."""
        backend = config.stt_backend.lower()
        
        if backend == "openai" or backend == "openai-whisper":
            if not config.openai_api_key:
                raise ValueError("MECHUS_OPENAI_API_KEY is required for OpenAI Whisper API backend")
            return OpenAIWhisperAPITranscriber(
                api_key=config.openai_api_key,
                model=config.openai_whisper_model or "whisper-1",
                language=config.whisper_language,
            )
        
        if backend == "deepgram":
            if not config.deepgram_api_key:
                raise ValueError("MECHUS_DEEPGRAM_API_KEY is required for Deepgram backend")
            return DeepgramTranscriber(
                api_key=config.deepgram_api_key,
                model=config.deepgram_model or "nova-2",
                language=config.whisper_language,
            )
        
        if backend == "assemblyai":
            if not config.assemblyai_api_key:
                raise ValueError("MECHUS_ASSEMBLYAI_API_KEY is required for AssemblyAI backend")
            return AssemblyAITranscriber(
                api_key=config.assemblyai_api_key,
                language_code=config.whisper_language,
            )
        
        if backend == "elevenlabs" or backend == "elevenlabs-realtime":
            if not config.eleven_api_key:
                raise ValueError("MECHUS_ELEVEN_API_KEY is required for ElevenLabs STT (same key as TTS)")
            model = config.eleven_stt_model or "scribe_v2"
            use_realtime = backend == "elevenlabs-realtime" or (
                backend == "elevenlabs" and config.eleven_stt_realtime
            )
            # Model id ``scribe_v2_realtime`` must not force WebSocket mode when realtime=0.
            if not use_realtime and "realtime" in model.lower():
                model = "scribe_v2"
            # The realtime WebSocket rejects batch model ids (e.g. ``scribe_v2`` from .env).
            if use_realtime and not any(t in model.lower() for t in ("realtime", "live")):
                model = "scribe_v2_realtime"
            if use_realtime:
                log.info("STT: ElevenLabs Scribe v2 Realtime (WebSocket, model=%s)", model)
                return ElevenLabsRealtimeTranscriber(
                    api_key=config.eleven_api_key,
                    model_id=model,
                    language=config.whisper_language,
                    base_url=config.eleven_base_url or "https://api.elevenlabs.io",
                )
            return ElevenLabsTranscriber(
                api_key=config.eleven_api_key,
                model_id=model,
                language=config.whisper_language,
                base_url=config.eleven_base_url or "https://api.elevenlabs.io",
            )
        
        if backend == "vosk":
            model_path = config.vosk_model_path or "models/vosk-model-small-en-us-0.15"
            return VoskTranscriber(model_path=model_path, sample_rate_hz=self._wake_sr)

        # Default to faster-whisper (local)
        if backend in {"faster-whisper", "whisper", "local"}:
            return FasterWhisperTranscriber(
                model=config.whisper_model,
                compute_type=config.whisper_compute_type,
                language=config.whisper_language,
            )

        raise ValueError(
            f"Unknown MECHUS_STT_BACKEND: {backend}. "
            "Use 'vosk', 'faster-whisper', 'openai', 'deepgram', 'assemblyai', "
            "'elevenlabs', or 'elevenlabs-realtime'"
        )

    def _stt_streams_realtime(self) -> bool:
        return hasattr(self._stt, "feed_pcm16") and hasattr(self._stt, "commit_segment")

    def _feed_realtime_stt(self, pcm16: bytes) -> None:
        if not getattr(self._stt, "streams_continuously", True):
            return
        feed = getattr(self._stt, "feed_pcm16", None)
        if not callable(feed):
            return
        try:
            feed(pcm16, self._config.sample_rate_hz)
        except Exception as exc:  # noqa: BLE001
            log.debug("Realtime STT feed failed (non-fatal): %s", exc)

    def _transcribe_stt_chunk(self, pcm16_16k: bytes, *, stream_commit: bool = True) -> Transcript:
        if self._stt_streams_realtime():
            if stream_commit:
                if pcm16_16k:
                    return self._stt.commit_segment(pcm16_16k, self._wake_sr)
                return self._stt.commit_segment()
            return self._stt.transcribe_pcm16(pcm16_16k, self._wake_sr)
        return self._stt.transcribe_pcm16(pcm16_16k, self._wake_sr)

    def _build_tts(self, config: MechusConfig):
        if config.tts_backend == "piper":
            if not config.piper_model_path:
                raise ValueError("MECHUS_PIPER_MODEL_PATH is required for piper TTS backend")
            return PiperTTSEngine(config.piper_model_path)
        if config.tts_backend == "elevenlabs":
            if not config.eleven_api_key or not config.eleven_voice_id:
                raise ValueError("MECHUS_ELEVEN_API_KEY and MECHUS_ELEVEN_VOICE_ID are required for elevenlabs TTS backend")
            engine = ElevenLabsTTSEngine(
                api_key=config.eleven_api_key,
                voice_id=config.eleven_voice_id,
                model_id=config.eleven_model_id,
                base_url=config.eleven_base_url or "https://api.elevenlabs.io",
                output_format=config.eleven_output_format,
            )
            # Cache by voice_id so per-persona engines reuse the default voice transparently.
            self._tts_by_voice: dict[str, "ElevenLabsTTSEngine | PiperTTSEngine"] = {
                config.eleven_voice_id: engine
            }
            return engine
        raise ValueError(f"Unknown MECHUS_TTS_BACKEND: {config.tts_backend}")

    def _tts_for(self, persona: "Persona | None"):
        """Pick the TTS engine for a persona.

        By default every persona uses ``MECHUS_ELEVEN_VOICE_ID`` (one voice). Set
        ``MECHUS_PER_PERSONA_ELEVEN_VOICES=1`` only when each invited bot should speak
        with its own ElevenLabs voice (multi-bot deploy).
        """
        if persona is None or self._config.tts_backend != "elevenlabs":
            return self._tts
        if not self._config.per_persona_eleven_voices:
            return self._tts
        voice_id = (persona.voice_id or self._config.eleven_voice_id or "").strip()
        if not voice_id:
            return self._tts
        cache = getattr(self, "_tts_by_voice", None)
        if cache is None:
            return self._tts
        existing = cache.get(voice_id)
        if existing is not None:
            return existing
        # Lazily build a per-persona engine. Reuses API key/base URL from config.
        try:
            engine = ElevenLabsTTSEngine(
                api_key=self._config.eleven_api_key,
                voice_id=voice_id,
                output_format=self._config.eleven_output_format,
                model_id=persona.model_id or self._config.eleven_model_id,
                base_url=self._config.eleven_base_url or "https://api.elevenlabs.io",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not build TTS engine for persona=%s voice_id=%s: %s", persona.id, voice_id, exc)
            return self._tts
        cache[voice_id] = engine
        return engine

    def stop(self) -> None:
        self._stop.set()
        close_stt = getattr(self._stt, "close", None)
        if callable(close_stt):
            try:
                close_stt()
            except Exception:  # noqa: BLE001
                pass
        if self._async_diarizer is not None:
            try:
                self._async_diarizer.stop(timeout=3.0)
            except Exception:
                pass

    def _install_signal_handlers(self) -> None:
        import threading as _threading
        if _threading.current_thread() is not _threading.main_thread():
            return  # signal handlers can only be set in the main thread

        def handler(signum, frame):  # noqa: ARG001
            log.info("Stopping (signal %s)", signum)
            self.stop()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _read_frame(self, timeout_s: float = 0.5):
        frame = self._audio_in.read(timeout_s=timeout_s)
        if frame is None:
            return None
        self._silence.update(frame.pcm16, frame.timestamp_s)
        if self._rolling is not None:
            self._rolling.push(frame.pcm16)
        return frame

    def _flush_stale_speaker_chunkers(self, now_s: float) -> None:
        if not self._speaker_last_frame_ts:
            return
        stale_after_s = max(
            self._config.transcript_end_silence_seconds,
            self._config.frame_ms / 1000.0,
        )
        stale_speakers = [
            speaker_id
            for speaker_id, last_ts in self._speaker_last_frame_ts.items()
            if (now_s - last_ts) >= stale_after_s
        ]
        for speaker_id in stale_speakers:
            chunker = self._speaker_transcript_chunkers.get(speaker_id)
            if chunker is not None:
                chunk = chunker.flush()
                if chunk is not None:
                    self._enqueue_transcript_chunk(
                        chunk,
                        speaker_id=speaker_id,
                        speaker_label=self._speaker_display_names.get(speaker_id, speaker_id),
                    )
            self._speaker_last_frame_ts.pop(speaker_id, None)

    def _start_transcript_chunk_worker(self) -> None:
        """Background STT so VAD commits never block the live audio listen loop."""
        if self._transcript_chunk_thread is not None and self._transcript_chunk_thread.is_alive():
            return
        self._transcript_chunk_queue = queue.Queue(maxsize=96)

        def _worker() -> None:
            while not self._stop.is_set():
                try:
                    item = self._transcript_chunk_queue.get(timeout=0.4)
                except queue.Empty:
                    continue
                chunk, speaker_id, speaker_label = item
                try:
                    self._handle_transcript_chunk(
                        chunk,
                        speaker_id=speaker_id,
                        speaker_label=speaker_label,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Transcript STT worker error: %s", exc)

        self._transcript_chunk_thread = threading.Thread(
            target=_worker,
            daemon=True,
            name="mechus-transcript-stt",
        )
        self._transcript_chunk_thread.start()

    def _enqueue_transcript_chunk(
        self,
        chunk: TranscriptChunk,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Queue VAD audio for STT on a worker thread (listen loop stays real-time)."""
        if self._transcript_chunk_queue is None:
            self._handle_transcript_chunk(
                chunk, speaker_id=speaker_id, speaker_label=speaker_label
            )
            return
        if (
            not force
            and self._tts_playing
            and self._transcript_wake_only
            and getattr(self._config, "transcript_barge_in_stop_only", False)
        ):
            # Do not defer stop detection until TTS ends — still run STT for barge-in,
            # but _handle_transcript_chunk skips heavy ingest while Mechus is speaking.
            pass
        try:
            self._transcript_chunk_queue.put_nowait((chunk, speaker_id, speaker_label))
        except queue.Full:
            now = time.monotonic()
            last = float(getattr(self, "_transcript_queue_full_log_ts", 0.0) or 0.0)
            if now - last >= 5.0:
                log.warning("Transcript STT queue full — dropping oldest chunk")
                self._transcript_queue_full_log_ts = now
            try:
                self._transcript_chunk_queue.get_nowait()
            except queue.Empty:
                pass
            self._transcript_chunk_queue.put_nowait((chunk, speaker_id, speaker_label))

    def _flush_deferred_transcript_chunks(self) -> None:
        pending = getattr(self, "_deferred_transcript_chunks", None) or []
        if not pending:
            return
        self._deferred_transcript_chunks = []
        merged: list[tuple[TranscriptChunk, str | None, str | None]] = []
        for chunk, speaker_id, speaker_label in pending:
            if (
                merged
                and speaker_id is not None
                and merged[-1][1] == speaker_id
            ):
                prev, prev_sid, prev_label = merged[-1]
                merged[-1] = (
                    TranscriptChunk(
                        start_ts=prev.start_ts,
                        end_ts=chunk.end_ts,
                        pcm16=prev.pcm16 + chunk.pcm16,
                        speaker_id=prev_sid,
                    ),
                    prev_sid,
                    prev_label or speaker_label,
                )
            else:
                merged.append((chunk, speaker_id, speaker_label))
        for chunk, speaker_id, speaker_label in merged:
            self._enqueue_transcript_chunk(
                chunk,
                speaker_id=speaker_id,
                speaker_label=speaker_label,
                force=True,
            )

    def _hard_interrupt_tts(self) -> None:
        """Immediately cut Mechus speech (stop / barge-in)."""
        self._tts_cancel_requested = True
        for _resp in (self._responder, getattr(self._responder, "_primary", None), getattr(self._responder, "_fallback", None)):
            if _resp is not None and hasattr(_resp, "_last_spoken_continuation"):
                _resp._last_spoken_continuation = None
        try:
            self._audio_out.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._teardown_preroll_pipeline(wait=False)
        except Exception:  # noqa: BLE001
            pass

    def _reset_spoken_aloud_buffer(self) -> None:
        self._dialog_last_mechus_spoken = ""

    def _note_phrase_spoken_aloud(self, phrase: str) -> None:
        p = (phrase or "").strip()
        if not p:
            return
        prev = (self._dialog_last_mechus_spoken or "").strip()
        self._dialog_last_mechus_spoken = f"{prev} {p}".strip() if prev else p

    def _last_mechus_reply_for_repeat(self) -> str:
        spoken = (self._dialog_last_mechus_spoken or "").strip()
        if spoken:
            return spoken
        return (self._dialog_last_mechus or "").strip()

    def _mark_tts_not_playing(self) -> None:
        self._tts_playing = False
        self._tts_cancel_requested = False
        self._tts_frame_loop_active = False
        self._clear_barge_transcript_chunkers()
        self._flush_deferred_transcript_chunks()

    def _handle_live_frame(self, frame) -> None:
        if self._transcript_chunker is None:
            return
        self._feed_realtime_stt(frame.pcm16)
        sid = getattr(frame, "speaker_id", None)
        speaker_name = getattr(frame, "speaker_name", None)
        use_barge = self._use_barge_transcript_chunkers()

        if self._config.zoom_stream_attribution and sid:
            if speaker_name and speaker_name not in self._speaker_display_names.get(sid, ""):
                self._speaker_display_names[sid] = speaker_name
                # Add participant to memory so Mechus knows who's in the meeting
                self._get_memory().add_segment(f"Participant in this meeting: {speaker_name}")
                eng = self._get_context_engine()
                if eng is not None:
                    eng.ingest_chunk(f"{speaker_name} is in this meeting.", speaker_id="system", timestamp=__import__('time').time())
            chunker_map = self._barge_stream_chunkers if use_barge else self._stream_chunkers
            ch = chunker_map.get(sid)
            if ch is None:
                ch = self._make_transcript_chunker(default_speaker_id=sid, barge=use_barge)
                chunker_map[sid] = ch
            chunk = ch.push(frame.pcm16, frame.timestamp_s)
            if chunk is not None:
                label = self._speaker_map.get(sid, self._speaker_display_names.get(sid, sid))
                self._enqueue_transcript_chunk(
                    chunk,
                    speaker_id=sid,
                    speaker_label=label,
                )
            return

        if sid:
            self._flush_stale_speaker_chunkers(frame.timestamp_s)
            chunker_map = self._barge_speaker_chunkers if use_barge else self._speaker_transcript_chunkers
            chunker = chunker_map.get(sid)
            if chunker is None:
                chunker = self._make_transcript_chunker(default_speaker_id=None, barge=use_barge)
                chunker_map[sid] = chunker
            if speaker_name:
                self._speaker_display_names[sid] = speaker_name
            self._speaker_last_frame_ts[sid] = frame.timestamp_s
            chunk = chunker.push(frame.pcm16, frame.timestamp_s)
            if chunk is not None:
                self._enqueue_transcript_chunk(
                    chunk,
                    speaker_id=sid,
                    speaker_label=self._speaker_display_names.get(sid, speaker_name or sid),
                )
            return

        chunk = self._transcript_chunker.push(frame.pcm16, frame.timestamp_s)
        if chunk is not None:
            self._enqueue_transcript_chunk(chunk)

    def _flush_transcript_chunkers(self) -> None:
        """Flush VAD state into transcript chunks (main + per-stream) before wake handling."""
        if self._transcript_chunker is None:
            return
        leftover = self._transcript_chunker.flush()
        if leftover is not None:
            self._enqueue_transcript_chunk(leftover, force=True)
        for sid, ch in list(self._stream_chunkers.items()):
            lo = ch.flush()
            if lo is not None:
                label = self._speaker_map.get(sid, self._speaker_display_names.get(sid, sid))
                self._enqueue_transcript_chunk(lo, speaker_id=sid, speaker_label=label, force=True)
        for sid, ch in list(self._speaker_transcript_chunkers.items()):
            lo = ch.flush()
            if lo is not None:
                self._enqueue_transcript_chunk(
                    lo,
                    speaker_id=sid,
                    speaker_label=self._speaker_display_names.get(sid, sid),
                    force=True,
                )

    def _frame_stream(self):
        while not self._stop.is_set():
            frame = self._read_frame(timeout_s=0.5)
            if frame is None:
                self._flush_stale_speaker_chunkers(time.monotonic())
                continue
            self._handle_live_frame(frame)
            yield frame

    def _can_interrupt_tts(self) -> bool:
        if not self._config.interrupt_on_speech:
            return False
        out = self._audio_out
        mod = getattr(type(out), "__module__", "")
        name = type(out).__name__
        # Zoom: incoming meeting audio often contains Mechus's own TTS (speakers / delay); WebRTC VAD
        # then treats it as barge-in and cuts playback. Default: no interrupt (full reply).
        # Set MECHUS_INTERRUPT_ON_SPEECH_ZOOM=1 to re-enable (uses MECHUS_TTS_INTERRUPT_GRACE_MS after play).
        if mod == "mechus.zoom_sdk_join" and name in ("ZoomAudioOutput", "OnDemandMuteZoomOutput"):
            if not self._config.interrupt_on_speech_zoom:
                log.debug(
                    "Interrupt disabled: Zoom output (MECHUS_INTERRUPT_ON_SPEECH_ZOOM unset). "
                    "Meeting capture often echoes Mechus's TTS."
                )
                return False
            return True
        in_dev = self._config.input_device
        out_dev = self._config.output_device
        if in_dev is None or out_dev is None:
            log.debug("Interrupt disabled: configure explicit input/output devices for barge-in.")
            return False
        if str(in_dev).lower() == str(out_dev).lower():
            log.debug("Interrupt disabled: input/output devices match; avoid TTS echo.")
            return False
        return True

    def _barge_in_enabled(self) -> bool:
        """Directed barge-in: a new 'Mechus …' command (or 'Mechus, stop') cuts the reply.

        Echo-safe because it goes through the transcript wake detector, which
        filters Mechus's own TTS. Enabled by default for transcript-wake mode
        (FaceTime/Zoom); see MECHUS_TRANSCRIPT_BARGE_IN.
        """
        return bool(
            self._transcript_wake_only and self._config.transcript_barge_in_enabled
        )

    def _clear_tts_resume_state(self) -> None:
        self._tts_resume_state = None

    def _collect_barge_in_command_after_stop(
        self,
        *,
        max_wait_s: float = 1.2,
    ) -> tuple[str, bytes, str] | None:
        """After TTS stops for a barge-in, wait for STT to finish the line.

        Without this, Mechus often fires on a partial "Computer, what..." and
        ignores the rest of the user's interrupt. Returns None when the stop
        was accidental noise / incomplete speech (caller should resume TTS).
        """
        if not self._barge_in_enabled():
            return None
        deadline = time.monotonic() + max(0.35, max_wait_s)
        best: tuple[str, bytes, str] | None = None
        wake_phrases = self._all_wake_phrases or self._config.wake_phrases
        max_dist = self._transcript_wake_max_dist()
        max_leading = self._transcript_wake_max_leading_words()
        while time.monotonic() < deadline and not self._stop.is_set():
            pending = self._pending_transcript_wake
            if pending is not None:
                best = pending
                trig = pending[0]
                if self._transcript_line_qualifies_as_wake(
                    trig,
                    wake_phrases,
                    max_dist=max_dist,
                    max_leading=max_leading,
                ):
                    # One short beat so VAD merge can attach trailing words.
                    time.sleep(0.12)
                    if self._pending_transcript_wake is not None:
                        best = self._pending_transcript_wake
                    break
            time.sleep(0.08)
        if best is None:
            return None
        trig, seed, src = best
        self._pending_transcript_wake = None
        action_norm = normalize_for_wake(trig)
        rem = wd_strip_wake_last(action_norm, wake_phrases, max_dist=max_dist)
        if remainder_is_stop_directive(rem):
            return (trig, seed, src)
        if not self._transcript_line_qualifies_as_wake(
            trig, wake_phrases, max_dist=max_dist, max_leading=max_leading
        ):
            return None
        if self._transcript_wake_only and not self._meeting_action_wake_allowed(
            action_norm, wake_phrases, max_dist=max_dist
        ):
            return None
        return (trig, seed, src)

    def _resume_interrupted_tts(self) -> bool:
        """Resume Mechus's reply after a false barge-in (noise / incomplete speech)."""
        ctx = self._tts_resume_state
        if not ctx:
            return False
        kind = ctx.get("kind")
        if kind == "frame":
            audio_f32 = ctx.get("audio_f32")
            play_sr = int(ctx.get("play_sr") or 0)
            play_start = float(ctx.get("play_start_mono") or time.monotonic())
            if audio_f32 is None or play_sr <= 0:
                self._clear_tts_resume_state()
                return False
            elapsed = max(0.0, time.monotonic() - play_start)
            offset = min(int(elapsed * play_sr), int(getattr(audio_f32, "shape", [0])[0]))
            remaining = np.asarray(audio_f32, dtype=np.float32).reshape(-1)[offset:]
            if remaining.size == 0:
                self._clear_tts_resume_state()
                return False
            log.info(
                "Resuming TTS after false barge-in (%.2fs remaining of %.2fs reply)",
                remaining.shape[0] / float(play_sr),
                audio_f32.shape[0] / float(play_sr),
            )
            self._tts_playing = True
            try:
                self._audio_out.play(
                    remaining,
                    sample_rate_hz=play_sr,
                    gain=self._config.tts_output_gain,
                )
                wait_fn = getattr(self._audio_out, "wait", None)
                if callable(wait_fn):
                    wait_fn()
            finally:
                self._mark_tts_not_playing()
            self._clear_tts_resume_state()
            return True
        if kind == "streaming":
            tts_engine = ctx.get("tts_engine")
            phrases = ctx.get("phrases") or []
            if tts_engine is None or not phrases:
                self._clear_tts_resume_state()
                return False
            log.info("Resuming streaming TTS after false barge-in (%d phrase(s))", len(phrases))
            self._tts_playing = True
            try:
                self._play_spoken_phrases(
                    tts_engine,
                    phrases,
                    presynth_audio=ctx.get("presynth_audio"),
                    tts_source=str(ctx.get("source") or ""),
                    tts_phase="reply",
                    stream_prefetch=ctx.get("stream_prefetch") or {},
                )
            finally:
                self._mark_tts_not_playing()
            self._clear_tts_resume_state()
            return True
        self._clear_tts_resume_state()
        return False

    def _start_tts_barge_in_watcher(self, *, allow_vad: bool):
        """Spawn a thread that watches for a barge-in WHILE streaming TTS plays.

        The main pipeline thread is blocked inside ``play_streaming`` during a
        streaming reply, so it cannot read input frames to notice an interrupt.
        This watcher is the sole frame reader for that window: it feeds frames
        to the transcript chunker (which schedules a directed wake / stop) and,
        when output is echo-safe, also honors raw VAD speech. On detection it
        calls ``audio_out.stop()`` so the streaming loop breaks immediately.

        Returns an opaque state tuple consumed by ``_stop_tts_barge_in_watcher``.
        """
        done = threading.Event()
        detected = threading.Event()
        reason: dict[str, str | None] = {"value": None}
        start = time.monotonic()
        # Directed commands are echo-safe, so react fast. VAD needs the full
        # grace window to avoid Mechus clipping itself on echoey outputs.
        directed_grace_s = 0.3
        vad_grace_s = max(0.0, self._config.tts_interrupt_grace_ms / 1000.0)

        def _watch() -> None:
            speech_frames = 0
            while not done.is_set() and not self._stop.is_set():
                frame = self._read_frame(timeout_s=0.15)
                if frame is None:
                    continue
                try:
                    self._handle_live_frame(frame)
                except Exception as exc:  # noqa: BLE001
                    log.debug("Barge-in watcher frame handling failed: %s", exc)
                now = time.monotonic()
                if (
                    self._barge_in_enabled()
                    and (now - start) >= directed_grace_s
                    and self._pending_transcript_wake_is_barge_in_eligible()
                ):
                    reason["value"] = "directed"
                    detected.set()
                    try:
                        self._audio_out.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                if allow_vad and (now - start) >= vad_grace_s:
                    try:
                        is_speech = self._interrupt_vad.is_speech(
                            frame.pcm16, self._config.sample_rate_hz
                        )
                    except Exception:  # noqa: BLE001
                        is_speech = False
                    if is_speech:
                        speech_frames += 1
                        if speech_frames >= self._interrupt_min_frames:
                            reason["value"] = "vad"
                            detected.set()
                            try:
                                self._audio_out.stop()
                            except Exception:  # noqa: BLE001
                                pass
                            return
                    else:
                        speech_frames = 0

        th = threading.Thread(target=_watch, daemon=True, name="mechus-tts-bargein")
        th.start()
        return (th, done, detected, reason)

    def _stop_tts_barge_in_watcher(self, state) -> tuple[bool, str | None]:
        """Tear down the watcher; return (detected, reason). reason is directed|vad."""
        if state is None:
            return False, None
        th, done, detected, reason = state
        done.set()
        try:
            th.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        if not detected.is_set():
            return False, None
        return True, reason.get("value")

    _WAKE_OPENER_ACK_HINTS: tuple[str, ...] = (
        "moment",
        "sec",
        "second",
        "yeah",
        "sure",
        "checking",
        "look",
        "hang",
        "on it",
        "pull",
        "talked",
        "said",
        "stated",
    )

    _THINKING_FACT_HINTS: tuple[str, ...] = (
        "weather",
        "temperature",
        "time in",
        "what time",
        "current",
        "latest",
        "what is",
        "what's",
        "whats",
        "who is",
        "who's",
        "how many",
        "how much",
        "how far",
        "how long",
        "how big",
        "how old",
        "largest",
        "smallest",
        "fastest",
        "tallest",
        "distance",
        "capital",
        "population",
        "tell me about",
        "explain",
        "describe",
    )
    _THINKING_OPINION_HINTS: tuple[str, ...] = (
        "do you think",
        "what do you think",
        "your opinion",
        "your take",
        "your favorite",
        "favourite",
        "what would you",
        "would you",
        "do you believe",
        "do you like",
        "do you prefer",
        "what's your",
        "what is your",
        "should i",
        "should we",
        "which is better",
        "which would",
        "which do you",
    )
    _THINKING_MEETING_HINTS: tuple[str, ...] = (
        "what did we",
        "what have we",
        "did we",
        "did anyone",
        "did someone",
        "we talked",
        "we discussed",
        "we said",
        "we agreed",
        "as we said",
        "like we stated",
        "meeting",
        "recap",
        "summary",
        "summarize",
        "action items",
        "action item",
        "decisions",
        "decision",
        "who said",
        "mentioned",
        "earlier",
    )

    def _thinking_category(self, request_text: str) -> str:
        req = (request_text or "").lower()
        if any(h in req for h in self._THINKING_MEETING_HINTS):
            return "meeting"
        if any(h in req for h in self._THINKING_OPINION_HINTS):
            return "opinion"
        if any(h in req for h in self._THINKING_FACT_HINTS):
            return "fact"
        return "general"

    def _thinking_preroll_cap_for_category(self, request_text: str) -> float | None:
        """Longer earcon while the LLM runs — category-specific duration."""
        if not self._config.thinking_preroll_enabled:
            return None
        caps = {
            "fact": 1.35,
            "opinion": 1.75,
            "meeting": 1.5,
            "general": 1.25,
        }
        return caps.get(self._thinking_category(request_text or ""), 1.6)

    def _pick_thinking_phrase(
        self,
        phrase_pool: list[str],
        *,
        request_text: str = "",
        prefer_short_fillers: bool = False,
        prefer_acknowledgment: bool = False,
        for_wake_opener: bool = False,
    ) -> str:
        import random as _random

        pool = list(phrase_pool)
        category = self._thinking_category(request_text)
        # When the pool is already category-scoped (from thinking_phrase_pool_for_category),
        # skip keyword narrowing — it was shrinking variety down to one repeated line.
        _pre_scoped = bool(
            phrase_pool
            and category in ("meeting", "opinion", "fact", "general")
            and len(phrase_pool) >= 8
        )
        if not _pre_scoped:
            # Per-category "fits this category" detector. Returns True for phrases
            # that read naturally for the given intent class.
            opinion_keywords = (
                "think", "good question", "fair question", "interesting",
                "tough one", "fun one", "hmm", "honestly", "thoughts",
                "i like", "like that question",
            )
            fact_keywords = (
                "check", "look", "pull", "find", "checking",
                "grab", "look up", "looking up", "looking that", "scan",
            )
            meeting_keywords = (
                "talked", "said", "stated", "meeting", "transcript",
                "notes", "what was said", "context", "walking back",
            )
            category_keywords: tuple[str, ...] = ()
            category_excludes: tuple[str, ...] = ()
            if category == "opinion":
                category_keywords = opinion_keywords
                category_excludes = (
                    "checking", "pulling", "transcript", "meeting notes",
                    "what we said", "what was said", "scan",
                )
            elif category == "fact":
                category_keywords = fact_keywords
                category_excludes = (
                    "think", "thoughts", "fun one", "tough one",
                    "what we said", "what was said", "transcript",
                    "meeting notes", "walking back",
                )
            elif category == "meeting":
                category_keywords = meeting_keywords
                category_excludes = (
                    "good question", "fun one", "interesting one",
                    "favorite", "favourite",
                )
            if category_keywords:
                targeted = [
                    f for f in pool
                    if any(k in f.lower() for k in category_keywords)
                    and not any(x in f.lower() for x in category_excludes)
                ]
                if targeted:
                    pool = targeted
                elif category_excludes:
                    cleaned = [
                        f for f in pool
                        if not any(x in f.lower() for x in category_excludes)
                    ]
                    if cleaned:
                        pool = cleaned
        if for_wake_opener:
            # Opener is hard-capped (~1.35s). NEVER fall back to long phrases — truncating
            # "Okay, give me just a sec." mid-word becomes audible garbage ("Okay, give me I am").
            opener_fit = [
                f for f in pool
                if len(f.strip()) <= 22
                and len(f.split()) <= 3
                and "!" not in f
                and not any(
                    x in f.lower()
                    for x in (
                        " for you",
                        " i'll ",
                        " i can ",
                        " let me pull",
                        " let me find",
                        "give me",
                        "looking",
                        "checking",
                        "pulling",
                        "think about",
                    )
                )
            ]
            if not opener_fit:
                opener_fit = [
                    f for f in phrase_pool
                    if len(f.strip()) <= 22
                    and len(f.split()) <= 3
                    and "give me" not in f.lower()
                ]
            if not opener_fit:
                opener_fit = ["One sec.", "Hang on.", "On it.", "Hold on.", "Okay, one sec."]
            pool = opener_fit
            non_yeah = [f for f in pool if not f.strip().lower().startswith("yeah")]
            if non_yeah:
                pool = non_yeah
        if prefer_short_fillers:
            short = [f for f in pool if len(f.strip()) <= 26 and len(f.split()) <= 4]
            if short:
                pool = short
            # Zoom/meeting: never prefer a "give me just a…" line that will get chopped.
            no_give = [f for f in pool if "give me" not in f.lower()]
            if no_give:
                pool = no_give
        if prefer_acknowledgment:
            hints = MechusAgent._WAKE_OPENER_ACK_HINTS
            scored = sorted(
                pool,
                key=lambda f: sum(1 for h in hints if h in f.lower()),
                reverse=True,
            )
            top = [f for f in scored if sum(1 for h in hints if h in f.lower()) > 0]
            pool = top[: min(6, len(top))] if top else pool
        recent = getattr(self, "_recent_thinking_phrases", None)
        if recent is None:
            recent = []
            self._recent_thinking_phrases = recent
        if len(pool) > 1:
            pool = [p for p in pool if p not in recent] or pool
        pick = _random.choice(pool) if pool else ""
        if pick:
            recent.append(pick)
            # Track a wider window so opinion/fact/meeting variants don't
            # repeat back-to-back across a multi-turn conversation.
            if len(recent) > 6:
                recent.pop(0)
        return pick

    def _resolve_cached_thinking_audio(
        self,
        persona,
        phrase: str,
        phrase_pool: list[str],
        *,
        request_text: str = "",
    ):
        """Return cached TTS for ``phrase``, or the best category-matched cached clip."""
        cache = getattr(self, "_thinking_audio_cache", None)
        if not isinstance(cache, dict):
            return None
        pid = getattr(persona, "id", None)
        key = (pid, phrase)
        if key in cache:
            return cache[key]
        category = self._thinking_category(request_text)
        opinion_kw = ("think", "good question", "interesting", "tough", "fun one", "hmm")
        fact_kw = ("check", "look", "pull", "find", "checking", "grab", "scan")
        meeting_kw = ("talked", "said", "meeting", "transcript", "notes", "walking")
        cat_kw = {
            "opinion": opinion_kw,
            "fact": fact_kw,
            "meeting": meeting_kw,
            "general": ("moment", "sec", "hang", "on it", "sure", "okay", "right"),
        }.get(category, ())
        meeting_markers = (
            "what we said", "what was said", "we said", "meeting notes", "transcript",
        )
        scored: list[tuple[int, str]] = []
        for p in phrase_pool:
            p = str(p).strip()
            if not p:
                continue
            ck = (pid, p)
            if ck not in cache:
                continue
            low = p.lower()
            score = sum(1 for k in cat_kw if k in low)
            if category in ("fact", "opinion", "general") and any(m in low for m in meeting_markers):
                score -= 5
            if phrase and p.lower() == phrase.lower():
                score += 10
            scored.append((score, p))
        if scored:
            scored.sort(key=lambda x: (-x[0], x[1]))
            return cache[(pid, scored[0][1])]
        for p in _THINKING_WARMUP_PHRASES:
            ck = (pid, p)
            if ck in cache:
                return cache[ck]
        _default_id = self._personas.default().id
        for p in self._personas.default().filler_phrases:
            p = str(p).strip()
            if p and (_default_id, p) in cache:
                return cache[(_default_id, p)]
        return None

    def _prepare_bridge_chunk_f32(
        self,
        persona,
        *,
        request_text: str = "",
        max_audio_cap_s: float | None = None,
        prefer_short_fillers: bool = False,
        prefer_acknowledgment: bool = False,
        bridge_segment: str = "full",
        phrase_pin: list[str] | None = None,
        for_wake_opener: bool = False,
    ) -> tuple[np.ndarray, int] | None:
        """Build one cached filler clip for wake-bridge playback (no I/O)."""
        _persona_fillers = list(persona.filler_phrases) or list(self._personas.default().filler_phrases)
        if not _persona_fillers:
            return None
        from mechus.personas import thinking_phrase_pool_for_category

        _phrase_pool = thinking_phrase_pool_for_category(
            self._thinking_category(request_text or ""),
            persona_fillers=_persona_fillers,
            audio_tags=bool(getattr(persona, "audio_tags", False)),
        )
        if phrase_pin and phrase_pin[0]:
            _thinking_phrase = phrase_pin[0]
        else:
            _thinking_phrase = self._pick_thinking_phrase(
                _phrase_pool,
                request_text=request_text,
                prefer_short_fillers=prefer_short_fillers,
                prefer_acknowledgment=prefer_acknowledgment,
                for_wake_opener=for_wake_opener or (
                    (bridge_segment or "").strip().lower() == "opener"
                    and prefer_acknowledgment is False
                ),
            )
            if phrase_pin is not None and _thinking_phrase:
                phrase_pin[:] = [_thinking_phrase]
        if not _thinking_phrase:
            return None
        self._last_thinking_phrase_spoken = _thinking_phrase
        thinking_audio = self._resolve_cached_thinking_audio(
            persona,
            _thinking_phrase,
            _phrase_pool,
            request_text=request_text,
        )
        if thinking_audio is None:
            return None

        _is_wake_opener = (
            ((bridge_segment or "").strip().lower() == "opener" or for_wake_opener)
            and max_audio_cap_s is not None
        )
        _seg = (bridge_segment or "full").strip().lower()
        if _is_wake_opener:
            thinking_f32, play_sr = self._opener_audio_for_playback(
                thinking_audio.audio_f32,
                thinking_audio.sample_rate_hz,
                max_cap_s=float(max_audio_cap_s),
                bridge_segment=_seg,
            )
        elif _seg == "extend":
            thinking_f32, play_sr = self._resample_audio_for_playback(
                thinking_audio.audio_f32,
                thinking_audio.sample_rate_hz,
            )
            _cap_s = float(max_audio_cap_s) if max_audio_cap_s else 0.55
            if _cap_s > 0.0:
                thinking_f32 = truncate_f32_to_max_duration(thinking_f32, play_sr, _cap_s)
        else:
            thinking_f32, play_sr = self._tts_audio_for_playback(
                thinking_audio.audio_f32,
                thinking_audio.sample_rate_hz,
                segment="full",
            )
            _gcap = float(self._config.thinking_preroll_max_audio_s)
            _tcap = float(max_audio_cap_s) if max_audio_cap_s is not None else 0.0
            # When a thread passes an explicit cap (category-based LLM wait), use it
            # instead of min()'ing with the short global default (~0.45s).
            if _tcap > 0.0:
                _cap_s = _tcap
            elif _gcap > 0.0:
                _cap_s = _gcap
            else:
                _cap_s = 0.0
            if _seg != "extend" and _cap_s > 0.0:
                thinking_f32 = truncate_f32_to_max_duration(thinking_f32, play_sr, _cap_s)
            _fi = float(self._config.thinking_preroll_fade_in_ms)
            _fo = float(self._config.thinking_preroll_fade_out_ms)
            if _seg == "finale":
                _fi = 0.0
                _fo = 0.0
            if _fi > 0.0 or _fo > 0.0:
                thinking_f32 = apply_f32_linear_fade_ends(
                    thinking_f32,
                    play_sr,
                    fade_in_ms=_fi,
                    fade_out_ms=_fo,
                )
        arr = np.asarray(thinking_f32, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return None
        return arr, int(play_sr)

    def _thinking_cache_has_audio(self, persona) -> bool:
        """True when at least one cached filler exists for this persona (or default)."""
        cache = getattr(self, "_thinking_audio_cache", None)
        if not isinstance(cache, dict) or not cache:
            return False
        pid = getattr(persona, "id", None)
        if pid:
            for phrase in getattr(persona, "filler_phrases", ()) or ():
                p = str(phrase).strip()
                if p and (pid, p) in cache:
                    return True
        personas = getattr(self, "_personas", None)
        if personas is None:
            return True
        default = personas.default()
        for phrase in default.filler_phrases:
            p = str(phrase).strip()
            if p and (default.id, p) in cache:
                return True
        return False

    def _wait_thinking_cache_min(self, persona, *, timeout_s: float = 4.0) -> bool:
        """Block briefly so the first wake opener can use pre-warmed fillers."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if self._thinking_cache_has_audio(persona):
                return True
            time.sleep(0.05)
        return self._thinking_cache_has_audio(persona)

    def _play_cached_thinking_preroll(
        self,
        persona,
        *,
        request_text: str = "",
        max_audio_cap_s: float | None = None,
        prefer_short_fillers: bool = False,
        prefer_acknowledgment: bool = False,
        bridge_segment: str = "full",
        phrase_pin: list[str] | None = None,
        for_wake_opener: bool = False,
    ) -> bool:
        """One short cached persona filler — no live synthesize (avoids overlapping clips)."""
        try:
            prepared = self._prepare_bridge_chunk_f32(
                persona,
                request_text=request_text,
                max_audio_cap_s=max_audio_cap_s,
                prefer_short_fillers=prefer_short_fillers,
                prefer_acknowledgment=prefer_acknowledgment,
                bridge_segment=bridge_segment,
                phrase_pin=phrase_pin,
                for_wake_opener=for_wake_opener or (
                    (bridge_segment or "").strip().lower() == "opener"
                    and not prefer_acknowledgment
                ),
            )
            if prepared is None:
                return False
            thinking_f32, play_sr = prepared
            _seg = (bridge_segment or "full").strip().lower()
            _is_wake_opener = _seg == "opener" and max_audio_cap_s is not None
            _ramp_in = True
            _ramp_out = True
            if _is_wake_opener:
                _ramp_in = _seg == "opener"
                _ramp_out = False
            elif _seg == "extend":
                _ramp_in = False
                _ramp_out = False
            elif _seg == "finale":
                _ramp_in = False
                _ramp_out = False
            _pb = self._audio_out.play_blocking
            import inspect as _inspect

            try:
                _pb_sig = _inspect.signature(_pb)
                _pb_supports_ramp = (
                    "ramp_in" in _pb_sig.parameters
                    or "ramp_out" in _pb_sig.parameters
                    or any(
                        p.kind == _inspect.Parameter.VAR_KEYWORD
                        for p in _pb_sig.parameters.values()
                    )
                )
            except Exception:
                _pb_supports_ramp = False
            _pb_kwargs: dict = {
                "sample_rate_hz": play_sr,
                "gain": self._config.tts_output_gain,
            }
            if _pb_supports_ramp:
                _pb_kwargs["ramp_in"] = _ramp_in
                _pb_kwargs["ramp_out"] = _ramp_out
            if _is_wake_opener:
                # Wake opener owns the bridge stream; use blocking playback.
                _pb(thinking_f32, **_pb_kwargs)
            elif self._zoom_virtual_mic_output():
                # Zoom's play_streaming holds _play_lock synchronously; launching a
                # pipeline there causes a deadlock (pipeline holds the lock while
                # waiting for response chunks, main thread can't acquire it).
                # Use play_blocking instead — Zoom's virtual mic has negligible
                # stream-reopen overhead so there is no audible glitch.
                _pb(thinking_f32, **_pb_kwargs)
            else:
                # Non-opener thinking preroll on PulseAudio / PortAudio: launch the
                # unified pipeline so the response TTS continues into the same stream
                # without a re-open glitch.
                self._launch_preroll_pipeline(play_sr)
                _pipe_q = self._preroll_pipeline_q
                if _pipe_q is not None:
                    # Apply a short fade-out so the preroll clip ends smoothly at
                    # zero; without this the last sample value can be non-zero,
                    # causing a click when the pipeline transitions to silence fill.
                    try:
                        from mechus.audio import apply_f32_linear_fade_ends as _fade
                        thinking_f32 = _fade(
                            thinking_f32, play_sr, fade_out_ms=30.0
                        )
                    except Exception:
                        pass
                    _pipe_q.put(thinking_f32)
                else:
                    _pb(thinking_f32, **_pb_kwargs)
            return True
        except Exception:
            return False

    def _join_thinking_preroll_thread(self, timeout_s: float = 8.0) -> None:
        """Wait for in-LLM cached filler so main TTS never overlaps it."""
        th = self._thinking_preroll_thread
        if th is None:
            return
        th.join(timeout=max(0.0, timeout_s))
        if th.is_alive():
            log.debug(
                "Thinking preroll thread still running after %.1fs; waiting for clip to finish",
                timeout_s,
            )
            th.join(timeout=4.0)
        self._thinking_preroll_thread = None

    # ── Unified preroll pipeline ─────────────────────────────────────────────
    # When thinking preroll is enabled WITHOUT the wake opener, the preroll clip
    # was previously played via play_blocking (stream A closes) followed by
    # play_streaming for the response (stream B opens) — that device re-open
    # produced an audible click.  The pipeline keeps ONE OutputStream open:
    # the preroll clip is pushed as the first chunk(s); the response TTS chunks
    # are pushed next; a sentinel closes the stream when the reply ends.

    def _launch_preroll_pipeline(self, sample_rate_hz: int) -> None:
        """Open a persistent play_streaming session that bridges preroll → response."""
        self._teardown_preroll_pipeline(wait=False)
        import queue as _q
        pipeline_q: _q.Queue = _q.Queue(maxsize=512)
        self._preroll_pipeline_q = pipeline_q
        self._preroll_pipeline_sr = sample_rate_hz
        _gain = float(self._config.tts_output_gain)

        def _run() -> None:
            # 10ms silence frame — yielded during gaps to prevent the sounddevice
            # OutputStream buffer from draining (underflow → click) while we wait
            # for the first TTS chunk between preroll end and response start.
            _silence_10ms = np.zeros(
                max(1, int(sample_rate_hz * 0.010)), dtype=np.float32
            )

            def _chunks():
                while True:
                    try:
                        item = pipeline_q.get(timeout=0.010)
                    except Exception:
                        # Timeout — check whether the audio device was stopped;
                        # otherwise yield silence to keep the stream alive and
                        # prevent buffer underflow during the preroll→TTS gap.
                        try:
                            if self._audio_out._stop_event.is_set():
                                return
                        except Exception:
                            pass
                        yield _silence_10ms
                        continue
                    if item is None:
                        return
                    yield item

            try:
                self._audio_out.play_streaming(
                    _chunks(),
                    sample_rate_hz=sample_rate_hz,
                    gain=_gain,
                )
            except Exception as exc:
                log.debug("Preroll pipeline stream error: %s", exc)
            finally:
                self._preroll_pipeline_q = None
                self._preroll_pipeline_thread = None

        th = threading.Thread(target=_run, daemon=True, name="mechus-preroll-pipe")
        self._preroll_pipeline_thread = th
        th.start()

    def _teardown_preroll_pipeline(self, *, wait: bool = True) -> None:
        """Send sentinel to close the pipeline stream and optionally wait."""
        q = getattr(self, "_preroll_pipeline_q", None)
        if hasattr(self, "_preroll_pipeline_q"):
            self._preroll_pipeline_q = None
        if q is not None:
            try:
                q.put(None, timeout=0.5)
            except Exception:
                pass
        th = getattr(self, "_preroll_pipeline_thread", None)
        if wait and th is not None:
            th.join(timeout=5.0)
    # ── end unified preroll pipeline ─────────────────────────────────────────

    def _thinking_preroll_bridge_cap_s(
        self, wake_source: str = "", request_text: str = ""
    ) -> float | None:
        """Full-length thinking earcon for the bridge finale (category-aware)."""
        _ = wake_source
        cat = self._thinking_preroll_cap_for_category(request_text)
        if cat and cat > 0.0:
            return cat
        cap = float(self._config.thinking_preroll_max_audio_s)
        return cap if cap > 0.0 else None

    def _bridge_finale_preroll_enabled(self, source: str) -> bool:
        if not self._config.thinking_preroll_enabled:
            return False
        if source in ("transcript_stt", "transcript_followup"):
            return bool(self._config.thinking_preroll_transcript_stt)
        return True

    def _wait_audio_output_idle(self) -> None:
        """Ensure the virtual mic / device finished the previous clip before main TTS."""
        wait = getattr(self._audio_out, "wait", None)
        if callable(wait):
            try:
                wait()
            except Exception:  # noqa: BLE001
                pass

    def _wait_output_ready_for_tts(self, timeout_s: float = 10.0) -> bool:
        """Zoom virtual mic: block until onMicStartSend so TTS is not silently dropped."""
        ready_fn = getattr(self._audio_out, "is_ready_to_send", None)
        if not callable(ready_fn):
            return True
        if ready_fn():
            return True
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        while time.monotonic() < deadline and not self._stop.is_set():
            if ready_fn():
                log.info("Zoom virtual mic ready for TTS")
                return True
            time.sleep(0.08)
        log.warning("Audio output not ready to send after %.1fs — TTS may be silent", timeout_s)
        return False

    def _release_wake_opener_bridge(
        self,
        stop_evt: "threading.Event | None",
        opener_th: "threading.Thread | None",
        *,
        timeout_s: float = 12.0,
        keep_mic_for_handoff: bool = False,
    ) -> None:
        """End the wake opener extend loop and wait for the bridge clip to finish."""
        if stop_evt is not None:
            stop_evt.set()
        if opener_th is not None:
            opener_th.join(timeout=max(0.0, timeout_s))
            if opener_th.is_alive():
                log.warning("Wake opener thread did not finish within %.1fs", timeout_s)
        self._wake_opener_thread = None
        if not keep_mic_for_handoff:
            self._wait_audio_output_idle()

    def _wake_opener_bridging_audio(self) -> bool:
        """True when the wake opener thread owns bridge audio (do not start thinking preroll)."""
        if getattr(self, "_wake_opener_bridge_active", False):
            return True
        th = self._wake_opener_thread
        return th is not None and th.is_alive()

    def _wake_opener_owns_bridge(self, wake_source: str) -> bool:
        """True when this wake path uses the opener extend loop instead of parallel preroll."""
        if not self._config.wake_opener_enabled:
            return False
        return wake_source in ("transcript_stt", "transcript_followup", "vosk")

    def _normalize_bridge_stream_chunk(
        self,
        arr: np.ndarray,
        chunk_sr: int,
        stream_sr_ref: list[int | None],
    ) -> np.ndarray | None:
        """Resample bridge fillers to one stream rate (required for gapless play_streaming)."""
        out = np.asarray(arr, dtype=np.float32).reshape(-1)
        if out.size == 0:
            return None
        cs = int(chunk_sr)
        if stream_sr_ref[0] is None:
            stream_sr_ref[0] = cs
        elif cs != int(stream_sr_ref[0]):
            out = resample_f32_linear(out, src_sr=cs, dst_sr=int(stream_sr_ref[0]))
        return out

    def _play_wake_bridge_streaming(
        self,
        persona,
        *,
        cmd: str,
        source: str,
        stop_evt: "threading.Event",
    ) -> int:
        """Gapless opener + extend on one output stream (Pulse or Zoom virtual mic)."""
        play_sr = self._playback_output_sample_rate()
        stream_sr_ref: list[int | None] = [None]
        extend_cap_s = float(self._config.wake_opener_extend_cap_s)
        opener_cap_s = float(self._config.wake_opener_max_audio_s)
        _cat_cap = self._thinking_preroll_cap_for_category(cmd) or opener_cap_s
        opener_cap_s = max(opener_cap_s, float(_cat_cap or opener_cap_s))
        extend_clips = 0
        opener_logged = False
        _xf_ms = 4.0
        _tail = np.zeros((0,), dtype=np.float32)

        def _emit_chunk(arr: np.ndarray, chunk_sr: int):
            nonlocal _tail
            norm = self._normalize_bridge_stream_chunk(arr, chunk_sr, stream_sr_ref)
            if norm is None:
                return
            sr = int(stream_sr_ref[0] or chunk_sr)
            xf_n = max(1, int(round(sr * _xf_ms / 1000.0)))
            if _tail.size and norm.size > xf_n:
                head = norm[:xf_n]
                n = min(_tail.size, head.size)
                ramp_out = np.linspace(1.0, 0.0, num=n, dtype=np.float32)
                ramp_in = np.linspace(0.0, 1.0, num=n, dtype=np.float32)
                blended = _tail[:n] * ramp_out + head[:n] * ramp_in
                body = norm[xf_n:-xf_n] if norm.size > 2 * xf_n else np.zeros((0,), dtype=np.float32)
                merged = np.concatenate([blended, body]) if body.size else blended
                if merged.size:
                    yield merged
                _tail = norm[-xf_n:].astype(np.float32, copy=True)
            elif norm.size > xf_n:
                yield norm[:-xf_n]
                _tail = norm[-xf_n:].astype(np.float32, copy=True)
            else:
                yield norm
                _tail = np.zeros((0,), dtype=np.float32)

        def _bridge_chunks():
            nonlocal extend_clips, opener_logged, _tail
            t_opener_wait = time.monotonic()
            while True:
                opener = self._prepare_bridge_chunk_f32(
                    persona,
                    request_text=cmd,
                    max_audio_cap_s=opener_cap_s,
                    prefer_acknowledgment=False,
                    bridge_segment="opener",
                    for_wake_opener=False,
                )
                if opener is not None:
                    arr, chunk_sr = opener
                    if arr.size:
                        if not opener_logged:
                            self._wake_opener_played = True
                            self._log_time_to_first_audio(source, phase="opener")
                            opener_logged = True
                        yield from _emit_chunk(arr, int(chunk_sr))
                        break
                if stop_evt.is_set():
                    break
                if (
                    self._barge_in_enabled()
                    and self._pending_transcript_wake_is_barge_in_eligible()
                ):
                    log.info("Wake bridge interrupted (stop directive)")
                    stop_evt.set()
                    try:
                        self._audio_out.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if (time.monotonic() - t_opener_wait) >= 4.0:
                    break
                time.sleep(0.08)

            # Hold the open stream until main TTS — true silence (not a low hum of the opener tail).
            t_extend_start = time.monotonic()
            hold_sr = int(stream_sr_ref[0] or play_sr)
            hold_chunk_ms = 32.0
            hold_n = max(1, int(round(hold_sr * hold_chunk_ms / 1000.0)))
            sustain = np.zeros((hold_n,), dtype=np.float32)
            hold_chunk_s = hold_n / float(hold_sr)
            if opener_logged and extend_cap_s > 0.0:
                log.debug(
                    "Wake bridge: opener done; sustaining stream until reply (cap=%.1fs)",
                    extend_cap_s,
                )
            while not stop_evt.is_set():
                if (
                    self._barge_in_enabled()
                    and self._pending_transcript_wake_is_barge_in_eligible()
                ):
                    log.info("Wake bridge sustain interrupted (stop directive)")
                    stop_evt.set()
                    try:
                        self._audio_out.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if (time.monotonic() - t_extend_start) >= extend_cap_s:
                    log.debug(
                        "Wake opener extend cap reached after %.2fs; releasing",
                        extend_cap_s,
                    )
                    break
                yield sustain
                time.sleep(max(0.0, hold_chunk_s - 0.003))
            # Reply TTS owns the next edge; do not flush an orphan crossfade tail here.

        import inspect as _inspect

        _stream_sr = int(play_sr)
        _probe = self._prepare_bridge_chunk_f32(
            persona,
            request_text=cmd,
            max_audio_cap_s=opener_cap_s,
            prefer_acknowledgment=False,
            bridge_segment="opener",
            for_wake_opener=True,
        )
        if _probe is not None and np.asarray(_probe[0]).size:
            _stream_sr = int(_probe[1])
        _kwargs: dict = {
            "sample_rate_hz": _stream_sr,
            "gain": self._config.tts_output_gain,
        }
        _ps = self._audio_out.play_streaming
        try:
            _sig = _inspect.signature(_ps)
            if "handoff_from_bridge" in _sig.parameters or any(
                p.kind == _inspect.Parameter.VAR_KEYWORD
                for p in _sig.parameters.values()
            ):
                _kwargs["handoff_from_bridge"] = False
            if "keep_mic_live" in _sig.parameters or any(
                p.kind == _inspect.Parameter.VAR_KEYWORD
                for p in _sig.parameters.values()
            ):
                _kwargs["keep_mic_live"] = True
        except Exception:
            pass
        self._reassert_facetime_tts_routing()
        _ps(_bridge_chunks(), **_kwargs)
        return extend_clips

    def _discard_main_response_presynth(self) -> None:
        """Join and drop any in-flight main-response synthesize (wake-cycle errors / pipeline abort)."""
        th = self._main_tts_presynth_thread
        self._main_tts_presynth_thread = None
        self._main_tts_presynth_box = []
        if th is not None and th.is_alive():
            th.join(timeout=180.0)

    def _start_main_response_presynth_early(self, persona, response_text: str) -> None:
        """Begin non-streaming main reply synthesize before wake cycle finishes downstream I/O."""
        txt = (response_text or "").strip()
        if not txt:
            return
        self._discard_main_response_presynth()
        tts_engine = self._tts_for(persona)
        if hasattr(tts_engine, "synthesize_streaming"):
            return
        box: list = []

        def _worker() -> None:
            try:
                box.append(tts_engine.synthesize(txt))
            except Exception as exc:  # noqa: BLE001
                box.append(exc)

        th = threading.Thread(target=_worker, daemon=True, name="mechus-main-tts-presynth")
        self._main_tts_presynth_box = box
        self._main_tts_presynth_thread = th
        th.start()

    def _maybe_start_thinking_preroll_thread(
        self,
        persona,
        *,
        request_text: str = "",
        max_audio_cap_s: float | None = None,
        prefer_short_fillers: bool = False,
        wake_source: str = "",
    ) -> None:
        """Start cached persona earcon in parallel (no-op if disabled or thread already running)."""
        if not self._config.thinking_preroll_enabled:
            return
        # Gapless opener owns the LLM wait on meeting paths — parallel preroll stacks fillers.
        if (
            self._wake_opener_owns_bridge(wake_source)
            or self._wake_opener_bridging_audio()
            or getattr(self, "_wake_opener_played", False)
        ):
            return
        th = self._thinking_preroll_thread
        if th is not None and th.is_alive():
            return
        _fill_persona = persona
        _cap = max_audio_cap_s
        _short = prefer_short_fillers

        def _thinking_preroll_worker() -> None:
            try:
                # Play thinking sound once — looping caused wake blocking
                self._play_cached_thinking_preroll(
                    _fill_persona,
                    request_text=request_text or "",
                    max_audio_cap_s=_cap,
                    prefer_short_fillers=_short,
                )
            except Exception:
                pass

        _th = threading.Thread(target=_thinking_preroll_worker, daemon=True)
        self._thinking_preroll_thread = _th
        _th.start()

    def _reassert_facetime_tts_routing(self) -> None:
        """FaceTime: TTS must land on mechus_sink → mechus_mic_source for remote participants."""
        if not getattr(self, "_is_facetime_web", False):
            jb = (os.environ.get("MECHUS_JOIN_BACKEND", "") or "").strip().lower()
            if jb != "facetime_web":
                return
        sink = (os.environ.get("MECHUS_FT_DEFAULT_SINK") or "mechus_sink").strip()
        try:
            import subprocess as _sp

            _sp.run(
                ["pactl", "set-default-sink", sink],
                check=False,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            r = _sp.run(
                ["pactl", "list", "sink-inputs", "short"],
                check=False,
                stdout=_sp.PIPE,
                stderr=_sp.DEVNULL,
                text=True,
            )
            for line in (r.stdout or "").splitlines():
                low = line.lower()
                if not any(k in low for k in ("python", "portaudio", "alsa plug-in")):
                    continue
                parts = line.split("\t")
                if not parts or not parts[0].strip().isdigit():
                    continue
                idx = parts[0].strip()
                cur_sink = parts[1].strip() if len(parts) > 1 else ""
                if sink in cur_sink or cur_sink.endswith(sink):
                    continue
                _sp.run(
                    ["pactl", "move-sink-input", idx, sink],
                    check=False,
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
        except Exception as exc:
            log.debug("FaceTime TTS pulse re-route skipped: %s", exc)

    def _zoom_virtual_mic_output(self) -> bool:
        """True when TTS goes through ``ZoomAudioOutput`` (32 kHz virtual mic; internal resample)."""
        out = self._audio_out
        inner = getattr(out, "_inner", out)
        mod = getattr(type(inner), "__module__", "")
        return mod == "mechus.zoom_sdk_join" and type(inner).__name__ == "ZoomAudioOutput"

    def _playback_output_sample_rate(self) -> int:
        """Device rate for TTS resample (FaceTime Pulse sinks are usually 44.1 kHz)."""
        if self._zoom_virtual_mic_output():
            return int(self._config.sample_rate_hz)
        if getattr(self, "_is_facetime_web", False):
            try:
                import sounddevice as sd

                inner = getattr(self._audio_out, "_inner", self._audio_out)
                resolve = getattr(inner, "_resolve_device", None) or getattr(
                    self._audio_out, "_resolve_device", None
                )
                if resolve is not None:
                    dev = resolve()
                    if dev is not None:
                        info = sd.query_devices(dev, "output")
                        if isinstance(info, dict):
                            return int(info.get("default_samplerate") or 44100)
            except Exception:
                pass
            return 44100
        return int(self._config.sample_rate_hz)

    def _resample_audio_for_playback(self, audio_f32, tts_sr: int) -> tuple[np.ndarray, int]:
        if self._zoom_virtual_mic_output():
            return audio_f32, int(tts_sr)
        out_sr = self._playback_output_sample_rate()
        if int(tts_sr) == out_sr:
            return audio_f32, out_sr
        return resample_f32_linear(audio_f32, src_sr=tts_sr, dst_sr=out_sr), out_sr

    def _opener_audio_for_playback(
        self,
        audio_f32,
        tts_sr: int,
        *,
        max_cap_s: float,
        bridge_segment: str = "opener",
    ) -> tuple[np.ndarray, int]:
        """Wake opener only: trim + gentle fades. Prefer full short clips over mid-word chops."""
        out_audio, out_sr = self._resample_audio_for_playback(audio_f32, tts_sr)
        _pad_ms = 18.0 if self._zoom_virtual_mic_output() else 12.0
        trimmed = trim_f32_silence_edges(out_audio, out_sr, pad_ms=_pad_ms)
        dur_s = float(trimmed.shape[0]) / float(max(1, out_sr))
        # Only truncate when the clip is clearly longer than the cap. Truncating a
        # ~1.4s "give me just a sec" at 1.35s is what produced "Okay, give me I am".
        if max_cap_s > 0.0 and dur_s > (float(max_cap_s) + 0.25):
            capped = truncate_f32_to_max_duration(trimmed, out_sr, max_cap_s)
        else:
            capped = trimmed
        fi = float(self._config.wake_opener_fade_in_ms)
        fo = float(self._config.wake_opener_fade_out_ms)
        if (bridge_segment or "").strip().lower() in ("opener", "extend"):
            fo = 0.0
        if fi > 0.0 or fo > 0.0:
            capped = apply_f32_linear_fade_ends(
                capped, out_sr, fade_in_ms=fi, fade_out_ms=fo
            )
        return capped, out_sr

    def _phrase_tts_segment(self, phrase_index: int, phrase_count: int) -> str:
        """Per-phrase playback segment — no fade-out until the full reply ends."""
        if phrase_count <= 1:
            return "full"
        if phrase_index >= phrase_count - 1:
            return "phrase_last"
        return "phrase_mid"

    def _tts_audio_for_playback(
        self,
        audio_f32,
        tts_sr: int,
        *,
        segment: str = "full",
    ):
        """
        Sample rate for ``play_blocking`` / ``play``. On Zoom, skip resampling to
        ``MECHUS_SAMPLE_RATE_HZ`` so we do not chain 16 kHz TTS → 48 kHz → 32 kHz Zoom
        (harsh, glitchy, and slower than a single step inside ``ZoomAudioOutput``).

        ``segment`` controls fades so streaming chunks do not dip between every chunk:
        stream_first / stream_mid / stream_last / full / reply_after_opener.
        """
        out_audio, out_sr = self._resample_audio_for_playback(audio_f32, tts_sr)
        _zoom = self._zoom_virtual_mic_output()
        # Zoom virtual-mic buffering eats tight tails — keep more edge pad so last
        # syllables don't sound chopped.
        _pad_ms = 48.0 if _zoom else 28.0
        if segment in ("stream_mid", "stream_last"):
            _pad_ms = 16.0 if _zoom else 12.0
        elif segment in ("phrase_mid", "phrase_last", "full", "reply_after_opener"):
            if segment == "phrase_mid":
                _pad_ms = 52.0 if _zoom else 44.0
            else:
                _pad_ms = 80.0 if _zoom else 58.0
        trimmed = trim_f32_silence_edges(out_audio, out_sr, pad_ms=_pad_ms)
        fi = float(self._config.tts_fade_in_ms)
        fo = float(self._config.tts_fade_out_ms)
        if segment == "stream_first":
            fo = 0.0
            if self._wake_opener_played or getattr(self, "_bridge_finale_preroll_played", False):
                fi = 0.0
            else:
                _after = float(self._config.tts_fade_in_after_opener_ms)
                if _after > 0.0:
                    fi = _after
        elif segment == "stream_mid":
            fi = 0.0
            fo = 0.0
        elif segment == "stream_last":
            fi = 0.0
            fo = 0.0
        elif segment == "phrase_mid":
            fi = min(fi, 12.0)
            if self._zoom_virtual_mic_output():
                # Zoom play() owns the single edge ramp — agent mid fades stack and glitch.
                fi = 0.0
                fo = 0.0
            else:
                fo = 0.0
        elif segment == "phrase_last":
            fi = 0.0
            fo = 0.0
        elif segment == "full":
            # Zoom: leave fades to ZoomAudioOutput (one ~12 ms ramp). Agent+Zoom double-fade
            # swallows consonants and sounds robotic.
            if _zoom:
                fi = 0.0
                fo = 0.0
            else:
                fi = min(fi, 14.0)
                fo = 0.0
        elif segment == "reply_after_opener":
            if _zoom:
                fi = 0.0
                fo = 0.0
            else:
                _after = float(self._config.tts_fade_in_after_opener_ms)
                fi = _after if _after > 0.0 else fi
                fo = 0.0
        if fi > 0.0 or fo > 0.0:
            trimmed = apply_f32_linear_fade_ends(
                trimmed,
                out_sr,
                fade_in_ms=fi,
                fade_out_ms=fo,
            )
        if segment in ("phrase_last", "full", "reply_after_opener"):
            trimmed = append_f32_tail_silence(
                trimmed, out_sr, tail_ms=110.0 if _zoom else 62.0
            )
        elif segment == "phrase_mid":
            trimmed = append_f32_tail_silence(
                trimmed, out_sr, tail_ms=48.0 if _zoom else 38.0
            )
        return trimmed, out_sr

    def _persona_from_trigger(self, trigger_text: str):
        """Best-effort persona for opener audio before full wake cycle completes."""
        phrases = getattr(self, "_all_wake_phrases", None) or tuple(self._config.wake_phrases)
        norm = normalize_for_wake((trigger_text or "").strip())
        matched = wd_matched_wake_last(
            norm,
            phrases,
            max_dist=self._config.wake_transcript_max_dist,
        )
        if matched:
            return self._personas.resolve_by_wake(matched)
        return self._personas.default()

    _WAKE_OPENER_SKIP_INTENTS: frozenset[str] = frozenset(
        {
            INTENT_GREETING,
            INTENT_HELP,
            INTENT_STOP,
            INTENT_CONFIRM,
            INTENT_CLARIFICATION,
            INTENT_UNKNOWN,
            INTENT_PRESENCE_CHECK,
        }
    )

    def _wake_opener_command_text(self, trigger_text: str, *, source: str) -> str:
        raw = normalize_for_wake((trigger_text or "").strip())
        if not raw:
            return ""
        if source == "transcript_followup":
            return normalize_stt_command_text(raw)
        phrases = self._all_wake_phrases or tuple(self._config.wake_phrases)
        if source == "transcript_stt":
            cmd = wd_strip_wake_last(raw, phrases, max_dist=self._config.wake_transcript_max_dist)
        else:
            cmd = wd_strip_wake(raw, phrases, max_dist=2)
        return normalize_stt_command_text(cmd)

    def _should_play_wake_opener(self, trigger_text: str, *, source: str) -> bool:
        if not self._config.wake_opener_enabled:
            return False
        if source not in ("transcript_stt", "transcript_followup", "vosk"):
            return False
        if source == "transcript_stt":
            phrases = self._all_wake_phrases or tuple(self._config.wake_phrases)
            norm = normalize_for_wake((trigger_text or "").strip())
            if norm and not detect_wake(
                norm, phrases, max_dist=self._transcript_wake_max_dist()
            ):
                return False
        cmd = self._wake_opener_command_text(trigger_text, source=source)
        if not (cmd or "").strip():
            return False
        if is_repeat_request(cmd):
            return False
        try:
            from mechus.realtime import query_is_fast_deterministic_realtime

            if query_is_fast_deterministic_realtime(cmd):
                return False
        except Exception:
            pass
        intent = classify_pipeline_intent(cmd)
        if intent in self._WAKE_OPENER_SKIP_INTENTS:
            return False
        return True

    def _log_time_to_first_audio(self, source: str, *, phase: str) -> None:
        t0 = getattr(self, "_wake_pipeline_t0", None)
        if t0 is None:
            return
        ms = (time.perf_counter() - t0) * 1000
        log.info("Time to first audio: %.0fms (source=%s phase=%s)", ms, source, phase)
        if self._last_latency is not None:
            self._last_latency["time_to_first_audio_ms"] = round(ms, 1)

    def _run_wake_opener(
        self,
        persona,
        *,
        source: str,
        trigger_text: str = "",
        stop_evt: "threading.Event | None" = None,
    ) -> None:
        """Cached acknowledgment while the LLM + TTS prefetch run.

        When ``stop_evt`` is provided, keeps playing short fillers (capped by
        ``wake_opener_extend_cap_s``) so the user never hears dead air between
        the initial cue and the main reply. The main thread sets ``stop_evt``
        once the reply audio is queued and ready to play.
        """
        if not self._tts_playing:
            self._tts_playing = True
        self._wake_opener_bridge_active = True
        try:
            cmd = self._wake_opener_command_text(trigger_text, source=source)
            extend_cap_s = float(self._config.wake_opener_extend_cap_s)
            extend_clips = 0
            played = False
            if (
                getattr(self, "_personas", None) is not None
                and isinstance(getattr(self, "_thinking_audio_cache", None), dict)
                and not self._thinking_cache_has_audio(persona)
            ):
                self._wait_thinking_cache_min(persona, timeout_s=4.0)
            _audio_out = getattr(self, "_audio_out", None)
            _use_gapless_stream = (
                extend_cap_s > 0.0
                and stop_evt is not None
                and _audio_out is not None
                and hasattr(_audio_out, "play_streaming")
            )
            if _use_gapless_stream:
                log.info(
                    "Wake bridge: gapless extend active (cap=%.1fs)",
                    extend_cap_s,
                )
                try:
                    extend_clips = self._play_wake_bridge_streaming(
                        persona,
                        cmd=cmd,
                        source=source,
                        stop_evt=stop_evt,
                    )
                    played = self._wake_opener_played
                    if not played and extend_clips == 0:
                        log.debug(
                            "Gapless wake bridge produced no audio; falling back to per-clip playback"
                        )
                        _use_gapless_stream = False
                except Exception:  # noqa: BLE001
                    log.debug("Gapless wake bridge failed; falling back to per-clip playback")
                    _use_gapless_stream = False

            if not _use_gapless_stream:
                self._reassert_facetime_tts_routing()
                played = self._play_cached_thinking_preroll(
                    persona,
                    request_text=cmd,
                    max_audio_cap_s=float(self._config.wake_opener_max_audio_s),
                    prefer_acknowledgment=False,
                    bridge_segment="opener",
                    for_wake_opener=True,
                )
                if played:
                    self._wake_opener_played = True
                    self._log_time_to_first_audio(source, phase="opener")

                if extend_cap_s > 0.0 and stop_evt is not None and not stop_evt.is_set():
                    _audio_out = getattr(self, "_audio_out", None)
                    if _audio_out is not None and hasattr(_audio_out, "play_streaming"):
                        play_sr = self._playback_output_sample_rate()
                        hold_n = max(1, int(round(play_sr * 0.032)))
                        sustain = np.zeros((hold_n,), dtype=np.float32)
                        hold_chunk_s = hold_n / float(play_sr)

                        def _sustain_until_reply():
                            t_extend_start = time.monotonic()
                            while not stop_evt.is_set():
                                if (time.monotonic() - t_extend_start) >= extend_cap_s:
                                    break
                                yield sustain
                                time.sleep(max(0.0, hold_chunk_s - 0.003))

                        import inspect as _inspect

                        _kwargs: dict = {
                            "sample_rate_hz": play_sr,
                            "gain": self._config.tts_output_gain,
                        }
                        _ps = _audio_out.play_streaming
                        try:
                            _sig = _inspect.signature(_ps)
                            if "keep_mic_live" in _sig.parameters or any(
                                p.kind == _inspect.Parameter.VAR_KEYWORD
                                for p in _sig.parameters.values()
                            ):
                                _kwargs["keep_mic_live"] = True
                        except Exception:
                            pass
                        self._reassert_facetime_tts_routing()
                        _ps(_sustain_until_reply(), **_kwargs)
                        extend_clips = 1
                    else:
                        t_extend_start = time.monotonic()
                        while not stop_evt.is_set():
                            if (time.monotonic() - t_extend_start) >= extend_cap_s:
                                log.debug(
                                    "Wake opener extend cap reached after %.2fs; releasing",
                                    extend_cap_s,
                                )
                                break
                            time.sleep(0.04)

            # Seamless handoff: skip a second "finale" earcon when extend already bridged
            # the wait (or opener alone is enough). A finale replay sounds like a cut/restart.
            _need_finale = (
                extend_clips == 0
                and extend_cap_s <= 0.0
                and stop_evt is not None
                and stop_evt.is_set()
                and self._bridge_finale_preroll_enabled(source)
            )
            if _need_finale:
                try:
                    if self._play_cached_thinking_preroll(
                        persona,
                        request_text=cmd,
                        max_audio_cap_s=self._thinking_preroll_bridge_cap_s(source, request_text=cmd),
                        prefer_short_fillers=False,
                        bridge_segment="finale",
                    ):
                        self._bridge_finale_preroll_played = True
                        log.debug("Bridge finale thinking preroll completed")
                except Exception:  # noqa: BLE001
                    pass
            elif played or extend_clips > 0:
                self._bridge_finale_preroll_played = True
        finally:
            self._wake_opener_bridge_active = False

    def _skip_post_llm_silence_wait(self, source: str) -> bool:
        """Meeting transcript chunks already include end-of-utterance; don't wait again."""
        if not self._config.low_latency:
            return False
        return source in ("transcript_stt", "transcript_followup")

    def _rephrase_last_response(self, last_spoken: str, request_text: str = "") -> str:
        """Replay the prior answer — verbatim for repeat requests."""
        last = (last_spoken or "").strip()
        if not last:
            return last
        if is_repeat_request(request_text):
            _use_tags = bool(self._config.eleven_audio_tags)
            return clean_spoken_text(last, preserve_audio_tags=_use_tags)
        fn = getattr(self._responder, "rephrase_repeat", None)
        if callable(fn):
            try:
                out = fn(last, request_text=request_text)
                if out and out.strip():
                    _use_tags = bool(self._config.eleven_audio_tags)
                    return clean_spoken_text(out.strip(), preserve_audio_tags=_use_tags)
            except Exception as exc:  # noqa: BLE001
                log.debug("rephrase_repeat failed: %s", exc)
        return rephrase_repeat_spoken(last)

    def _remember_place_from_request(self, request: str) -> None:
        """Remember the last city/region the user asked about (time/weather follow-ups)."""
        from mechus.realtime import _extract_time_location, _extract_weather_location, _lookup_timezone

        _noise = frozenset({"time", "whats", "what", "now", "like", "weather", "the", "right"})
        for fn in (_extract_time_location, _extract_weather_location):
            try:
                loc = (fn(request) or "").strip()
            except Exception:
                loc = ""
            if not loc or loc.lower() in _noise:
                continue
            if _lookup_timezone(loc) or ("," in loc or len(loc.split()) >= 2):
                self._last_place_context = loc
                return

    def _enrich_wake_response_state(
        self, base: dict | None, *, request_text: str = ""
    ) -> dict:
        st = dict(base or {})
        st.update(self.get_timezone_context())
        if self._transcript_wake_only:
            st["meeting_transcript_voice"] = True
        if self._accent_adapt_speech or self._is_non_english_user_language():
            st["accent_adapt_speech"] = True
        if self._config.low_latency:
            st["wake_brevity"] = True
            st["wake_spoken_max_words"] = self._config.wake_spoken_max_words
            st["wake_spoken_max_chars"] = self._config.wake_spoken_max_chars
        if (self._dialog_last_user or "").strip():
            st["dialog_last_user"] = (self._dialog_last_user or "").strip()[:300]
        if (self._dialog_last_mechus or "").strip():
            st["dialog_last_mechus"] = (self._dialog_last_mechus or "").strip()[:400]
        if is_similar_topic_followup(request_text, self._dialog_last_user or ""):
            st["vary_from_last_reply"] = True
        if (self._last_place_context or "").strip():
            st["last_place_context"] = (self._last_place_context or "").strip()[:120]
        pt = st.get("participant_timezones") or {}
        if isinstance(pt, dict) and pt:
            st["fallback_timezone"] = next(iter(pt.values()))
        return st

    def _maybe_warm_wake_cache(self, persona_id: str, bundle) -> None:
        """Pre-seed short answers for common meeting asks (low-latency path)."""
        if not self._config.low_latency:
            return
        from mechus.text import limit_words

        st = getattr(bundle, "state", None) or {}
        mw = self._config.wake_spoken_max_words
        seeds: list[tuple[str, str]] = []
        decisions = st.get("decisions") or []
        if decisions:
            d0 = decisions[0]
            val = (d0.get("value") if isinstance(d0, dict) else str(d0)) or ""
            val = limit_words(str(val).strip(), mw)
            if val:
                for q in (
                    "what did we decide",
                    "what did we decide about the launch date",
                    "launch date",
                ):
                    seeds.append((q, val))
        actions = st.get("action_items") or []
        if actions:
            a0 = actions[0]
            task = (a0.get("task") if isinstance(a0, dict) else str(a0)) or ""
            owner = (a0.get("owner") if isinstance(a0, dict) else "") or ""
            line = limit_words(f"{owner}: {task}".strip(": "), mw) if task else ""
            if line:
                seeds.append(("action items", line))
                seeds.append(("who owns this", line))
        if not seeds:
            return
        for q, ans in seeds:
            self._response_cache.put(f"{persona_id}::{q}", ans)

    def _play_streaming_phrase_smooth(
        self,
        tts_engine,
        phrase: str,
        *,
        tts_source: str = "",
        tts_phase: str = "reply",
        prefetched: list[tuple] | None = None,
        opener_stop_evt: "threading.Event | None" = None,
        opener_thread: "threading.Thread | None" = None,
        speech_segment: str = "full",
    ) -> None:
        """Stream TTS chunks straight to the audio output as they arrive.

        Eliminates the "wait for full synthesis then play" latency: a producer
        thread calls ``tts_engine.synthesize_streaming`` and pushes chunks onto a
        queue; the consumer hands chunks to ``audio_out.play_streaming`` which
        keeps a single output stream open across all chunks (gapless playback).

        Falls back to the legacy buffered playback path when:
          - ``prefetched`` chunks are supplied (already-buffered reply), or
          - the audio output does not implement ``play_streaming``.
        """
        play_sr = self._playback_output_sample_rate()
        _instant_reply = (tts_phase or "").strip().lower() == "instant"
        _after_opener = bool(
            not _instant_reply
            and (
                self._wake_opener_played
                or getattr(self, "_bridge_finale_preroll_played", False)
            )
        )
        _handoff_from_bridge = _after_opener
        can_live_stream = (
            prefetched is None
            and hasattr(self._audio_out, "play_streaming")
            and hasattr(tts_engine, "synthesize_streaming")
        )

        if can_live_stream:
            import queue as _queue

            q: "_queue.Queue[tuple[np.ndarray, int] | None]" = _queue.Queue(maxsize=64)
            sentinel: tuple[np.ndarray, int] | None = None
            producer_exc: list[BaseException] = []

            def _producer() -> None:
                try:
                    def _on(a, sr: int) -> None:
                        q.put(
                            (np.asarray(a, dtype=np.float32).reshape(-1).copy(), int(sr))
                        )

                    tts_engine.synthesize_streaming(phrase, _on)
                except BaseException as exc:  # noqa: BLE001
                    producer_exc.append(exc)
                finally:
                    q.put(sentinel)

            prod_th = threading.Thread(
                target=_producer,
                daemon=True,
                name="mechus-tts-live-prod",
            )
            prod_th.start()

            # Block on the first chunk so callers can log time-to-first-audio. The wake
            # opener extend loop keeps playing until this chunk arrives (gapless bridge).
            first = q.get()
            if opener_stop_evt is not None or opener_thread is not None:
                self._release_wake_opener_bridge(
                    opener_stop_evt,
                    opener_thread,
                    timeout_s=4.0,
                    keep_mic_for_handoff=True,
                )
            first_resampled = np.zeros((0,), dtype=np.float32)
            _stream_ok = False
            if first is not None:
                first_audio, first_sr = first
                first_resampled, play_sr = self._resample_audio_for_playback(
                    first_audio, int(first_sr)
                )
                first_resampled = np.asarray(first_resampled, dtype=np.float32).reshape(-1)
                if first_resampled.size:
                    first_resampled, play_sr = self._tts_audio_for_playback(
                        first_resampled, play_sr, segment="stream_first"
                    )
                    _stream_ok = first_resampled.size > 0

            if _stream_ok:

                def _consumer_iter():
                    # Tiny crossfade between consecutive chunks to mask the
                    # discontinuities introduced by per-chunk linear resampling
                    # (np.interp resets phase at each chunk boundary, which sounds
                    # like a click/buzz on long replies). 4ms at the playback rate
                    # is inaudible on speech and reliably smooths the seam.
                    xf_ms = 4.0
                    xf_n = max(1, int(round(play_sr * xf_ms / 1000.0)))
                    tail = np.zeros((0,), dtype=np.float32)
                    if first_resampled.size:
                        if first_resampled.size > xf_n:
                            tail = first_resampled[-xf_n:].astype(np.float32, copy=True)
                            yield first_resampled[:-xf_n]
                        else:
                            yield first_resampled
                    while True:
                        if self._tts_playback_aborted():
                            return
                        item = q.get()
                        if item is None:
                            if tail.size:
                                yield tail
                            return
                        audio_f32, sr = item
                        if audio_f32.size == 0:
                            continue
                        resampled, _sr_out = self._resample_audio_for_playback(audio_f32, int(sr))
                        arr = np.asarray(resampled, dtype=np.float32).reshape(-1)
                        if not arr.size:
                            continue
                        if tail.size and arr.size > xf_n:
                            head = arr[:xf_n]
                            n = min(tail.size, head.size)
                            ramp_out = np.linspace(1.0, 0.0, num=n, dtype=np.float32)
                            ramp_in = np.linspace(0.0, 1.0, num=n, dtype=np.float32)
                            blended = tail[:n] * ramp_out + head[:n] * ramp_in
                            emit_body = (
                                arr[xf_n:-xf_n]
                                if arr.size > 2 * xf_n
                                else np.zeros((0,), dtype=np.float32)
                            )
                            yield np.concatenate([blended, emit_body])
                            tail = arr[-xf_n:].astype(np.float32, copy=True)
                        else:
                            if arr.size > xf_n:
                                yield arr[:-xf_n]
                                tail = arr[-xf_n:].astype(np.float32, copy=True)
                            else:
                                yield arr
                                tail = np.zeros((0,), dtype=np.float32)

                self._log_time_to_first_audio(tts_source or "wake", phase=tts_phase)
                self._wake_opener_played = False
                try:
                    import inspect as _inspect

                    _play_streaming = self._audio_out.play_streaming
                    try:
                        _sig = _inspect.signature(_play_streaming)
                        _supports_skip = (
                            "skip_edge_ramp_in" in _sig.parameters
                            or any(
                                p.kind == _inspect.Parameter.VAR_KEYWORD
                                for p in _sig.parameters.values()
                            )
                        )
                        _supports_handoff = (
                            "handoff_from_bridge" in _sig.parameters
                            or any(
                                p.kind == _inspect.Parameter.VAR_KEYWORD
                                for p in _sig.parameters.values()
                            )
                        )
                    except Exception:
                        _supports_skip = True
                        _supports_handoff = True
                    _stream_kwargs: dict = {
                        "sample_rate_hz": play_sr,
                        "gain": self._config.tts_output_gain,
                    }
                    if _supports_skip:
                        _stream_kwargs["skip_edge_ramp_in"] = _after_opener or _instant_reply
                    if _supports_handoff:
                        _stream_kwargs["handoff_from_bridge"] = _handoff_from_bridge
                    _pipe_q = getattr(self, "_preroll_pipeline_q", None)
                    _pipe_th = getattr(self, "_preroll_pipeline_thread", None)
                    _pipe_sr = getattr(self, "_preroll_pipeline_sr", 0)
                    _pipe_active = (
                        not self._zoom_virtual_mic_output()
                        and _pipe_q is not None
                        and _pipe_th is not None
                        and _pipe_th.is_alive()
                        and _pipe_sr == play_sr
                    )
                    if _pipe_active:
                        _self_pipe_q = _pipe_q
                        try:
                            for _chunk in _consumer_iter():
                                _self_pipe_q.put(_chunk)
                        finally:
                            _self_pipe_q.put(None)
                            self._preroll_pipeline_q = None
                            if _pipe_th is not None:
                                _pipe_th.join(timeout=5.0)
                            self._preroll_pipeline_thread = None
                    else:
                        self._teardown_preroll_pipeline(wait=False)
                        _play_streaming(_consumer_iter(), **_stream_kwargs)
                    self._play_phrase_end_tail(speech_segment)
                finally:
                    prod_th.join(timeout=2.0)
                    self._wake_opener_played = False
                    self._bridge_finale_preroll_played = False
                if producer_exc:
                    log.debug("TTS live stream producer raised after start: %s", producer_exc[0])
                return

            prod_th.join(timeout=2.0)
            if producer_exc:
                log.warning(
                    "TTS live stream producer failed: %s; using blocking fallback",
                    producer_exc[0],
                )
            else:
                log.warning(
                    "TTS streaming produced no audio for %d-char phrase; using blocking fallback",
                    len(phrase or ""),
                )

        # Fallback (prefetched buffer, streaming failure, or output without streaming):
        # collect everything then play as one buffer (legacy behavior).
        if prefetched is not None:
            chunks = [
                (np.asarray(a, dtype=np.float32).reshape(-1).copy(), int(sr))
                for a, sr in prefetched
            ]
        else:
            chunks: list[tuple[np.ndarray, int]] = []

            def _collect(a, sr: int) -> None:
                chunks.append((np.asarray(a, dtype=np.float32).reshape(-1).copy(), int(sr)))

            tts_engine.synthesize_streaming(phrase, _collect)

        if not chunks:
            if opener_stop_evt is not None or opener_thread is not None:
                self._release_wake_opener_bridge(
                    opener_stop_evt,
                    opener_thread,
                    timeout_s=4.0,
                )
            try:
                syn = tts_engine.synthesize(phrase)
                if syn is not None and np.asarray(syn.audio_f32).size:
                    _fb_seg = speech_segment
                    if _after_opener and _fb_seg == "full":
                        _fb_seg = "reply_after_opener"
                    audio_f32, play_sr = self._tts_audio_for_playback(
                        syn.audio_f32,
                        syn.sample_rate_hz,
                        segment=_fb_seg,
                    )
                    if audio_f32.size:
                        self._log_time_to_first_audio(tts_source or "wake", phase=tts_phase)
                        self._wake_opener_played = False
                        self._audio_out.play_blocking(
                            audio_f32,
                            sample_rate_hz=play_sr,
                            gain=self._config.tts_output_gain,
                        )
                        return
            except Exception as exc:  # noqa: BLE001
                log.warning("TTS blocking fallback synthesize failed: %s", exc)
            log.error(
                "TTS produced no audio for %d-char phrase (engine=%s)",
                len(phrase or ""),
                type(tts_engine).__name__,
            )
            return

        if opener_stop_evt is not None or opener_thread is not None:
            self._release_wake_opener_bridge(
                opener_stop_evt,
                opener_thread,
                timeout_s=4.0,
                keep_mic_for_handoff=True,
            )

        pieces: list[np.ndarray] = []
        for audio_f32, sr in chunks:
            if audio_f32.size == 0:
                continue
            resampled, play_sr = self._resample_audio_for_playback(audio_f32, int(sr))
            pieces.append(np.asarray(resampled, dtype=np.float32).reshape(-1))

        if not pieces:
            return

        combined = np.concatenate(pieces)
        _seg = speech_segment
        if _after_opener and _seg == "full":
            _seg = "reply_after_opener"
        combined, play_sr = self._tts_audio_for_playback(
            combined,
            play_sr,
            segment=_seg,
        )
        if combined.size == 0:
            return
        self._log_time_to_first_audio(tts_source or "wake", phase=tts_phase)
        self._wake_opener_played = False
        self._audio_out.play_blocking(
            combined,
            sample_rate_hz=play_sr,
            gain=self._config.tts_output_gain,
        )

    def _tts_playback_aborted(self) -> bool:
        """True when barge-in / stop cut the current reply — skip remaining TTS chunks."""
        if getattr(self, "_tts_cancel_requested", False):
            return True
        stop_evt = getattr(self._audio_out, "_stop_event", None)
        if stop_evt is not None and stop_evt.is_set():
            return True
        return self._pending_transcript_wake_is_barge_in_eligible()

    def _play_phrase_end_tail(self, speech_segment: str) -> None:
        """Let the last phoneme finish naturally after streaming (no fade-out)."""
        if speech_segment not in ("phrase_last", "full", "reply_after_opener"):
            return
        if self._tts_playback_aborted():
            return
        sr = self._playback_output_sample_rate()
        ms = 58.0 if speech_segment != "phrase_mid" else 36.0
        n = max(1, int(sr * ms / 1000.0))
        self._audio_out.play_blocking(
            np.zeros(n, dtype=np.float32),
            sample_rate_hz=sr,
            gain=self._config.tts_output_gain,
        )

    def _phrase_pause_s(self, phrase: str) -> float:
        """Brief gap between TTS chunks so commas and periods breathe."""
        p = (phrase or "").rstrip()
        if not p:
            return 0.0
        if self._zoom_virtual_mic_output():
            if p[-1] in ".!?":
                base = 0.68
            elif p[-1] in ",;:":
                base = 0.36
            else:
                base = 0.20
        else:
            if p[-1] in ".!?":
                base = 0.52
            elif p[-1] in ",;:":
                base = 0.28
            else:
                base = 0.14
        # Slight rhythm variation so delivery is not metronomic.
        jitter = 0.88 + (hash(p) % 25) / 100.0
        scale = float(getattr(self._config, "tts_phrase_pause_scale", 1.0) or 1.0)
        return base * jitter * scale

    def _play_inter_phrase_pause(self, phrase: str) -> None:
        if self._tts_playback_aborted():
            return
        pause_s = self._phrase_pause_s(phrase)
        if pause_s <= 0:
            return
        sr = self._playback_output_sample_rate()
        n = max(1, int(sr * pause_s))
        silence = np.zeros(n, dtype=np.float32)
        self._audio_out.play_blocking(
            silence,
            sample_rate_hz=sr,
            gain=self._config.tts_output_gain,
        )

    def _play_spoken_phrases(
        self,
        tts_engine,
        phrases: list[str],
        *,
        presynth_audio=None,
        tts_source: str = "",
        tts_phase: str = "reply",
        stream_prefetch: dict[str, list[tuple]] | None = None,
        opener_stop_evt: "threading.Event | None" = None,
        opener_thread: "threading.Thread | None" = None,
    ) -> None:
        """Play one or more phrases (streaming when enabled)."""
        phrases = [p.strip() for p in phrases if (p or "").strip()]
        if not phrases:
            return
        # Zoom virtual mic: one continuous clip — phrase boundaries sound choppy/glitchy.
        if self._zoom_virtual_mic_output() and len(phrases) > 1:
            phrases = [" ".join(phrases)]
        _use_streaming = (
            self._config.eleven_tts_stream
            and hasattr(tts_engine, "synthesize_streaming")
            and not self._zoom_virtual_mic_output()
        )
        _logged_first = False

        for i, phrase in enumerate(phrases):
            _pseg = self._phrase_tts_segment(i, len(phrases))
            if self._tts_playback_aborted():
                log.info(
                    "TTS playback aborted before phrase %d/%d",
                    i + 1,
                    len(phrases),
                )
                break
            if i == 0 and presynth_audio is not None and not _use_streaming:
                if opener_stop_evt is not None or opener_thread is not None:
                    self._release_wake_opener_bridge(
                        opener_stop_evt,
                        opener_thread,
                        timeout_s=4.0,
                        keep_mic_for_handoff=True,
                    )
                    opener_stop_evt = None
                    opener_thread = None
                seg = _pseg
                if i == 0 and self._wake_opener_played and seg == "full":
                    seg = "reply_after_opener"
                audio_f32, play_sr = self._tts_audio_for_playback(
                    presynth_audio.audio_f32,
                    presynth_audio.sample_rate_hz,
                    segment=seg,
                )
                if audio_f32.size > 0:
                    self._log_time_to_first_audio(tts_source or "wake", phase=tts_phase)
                self._wake_opener_played = False
                self._audio_out.play_blocking(
                    audio_f32,
                    sample_rate_hz=play_sr,
                    gain=self._config.tts_output_gain,
                )
                if i < len(phrases) - 1 and not self._tts_playback_aborted():
                    self._play_inter_phrase_pause(phrase)
                if not self._tts_playback_aborted():
                    self._note_phrase_spoken_aloud(phrase)
                continue
            if self._tts_playback_aborted():
                break
            if _use_streaming:
                prefetch = (stream_prefetch or {}).get(phrase)
                self._play_streaming_phrase_smooth(
                    tts_engine,
                    phrase,
                    tts_source=tts_source,
                    tts_phase=tts_phase,
                    prefetched=prefetch,
                    opener_stop_evt=opener_stop_evt if i == 0 else None,
                    opener_thread=opener_thread if i == 0 else None,
                    speech_segment=_pseg,
                )
                if i == 0:
                    opener_stop_evt = None
                    opener_thread = None
            else:
                if i == 0 and (opener_stop_evt is not None or opener_thread is not None):
                    self._release_wake_opener_bridge(
                        opener_stop_evt,
                        opener_thread,
                        timeout_s=4.0,
                        keep_mic_for_handoff=True,
                    )
                    opener_stop_evt = None
                    opener_thread = None
                seg = _pseg
                if i == 0 and self._wake_opener_played and seg == "full":
                    seg = "reply_after_opener"
                audio = tts_engine.synthesize(phrase)
                audio_f32, play_sr = self._tts_audio_for_playback(
                    audio.audio_f32,
                    audio.sample_rate_hz,
                    segment=seg,
                )
                if i == 0 and audio_f32.size > 0:
                    self._log_time_to_first_audio(tts_source or "wake", phase=tts_phase)
                self._wake_opener_played = False
                self._audio_out.play_blocking(
                    audio_f32,
                    sample_rate_hz=play_sr,
                    gain=self._config.tts_output_gain,
                )
            if i < len(phrases) - 1 and not self._tts_playback_aborted():
                self._play_inter_phrase_pause(phrase)
            if not self._tts_playback_aborted():
                self._note_phrase_spoken_aloud(phrase)

    def _wait_for_silence(self, *, required_silence_s: float | None = None) -> bool:
        need = (
            self._config.min_silence_before_speaking_s
            if required_silence_s is None
            else required_silence_s
        )
        start = time.monotonic()
        warned = False
        while not self._stop.is_set():
            if self._silence.silent_for(need):
                return True
            if not warned and (time.monotonic() - start) > self._config.max_wait_for_silence_s:
                warned = True
                log.info("Waiting for a speaking gap")
            _ = self._read_frame(timeout_s=0.5)
        return False

    def _capture_request_pcm16(self, seed_pcm16: bytes) -> bytes:
        # Wake was already confirmed by Vosk, so skip VAD voice-detection requirement.
        # Zoom audio can be too quiet for webrtcvad even when ElevenLabs can transcribe it.
        # record_from_seed consumes live frames from the queue; we must not call _frame_stream()
        # a second time or those frames will be gone and record_next will always time out.
        seeded = self._recorder.record_from_seed(seed_pcm16, frames=self._frame_stream(), require_voice=False)
        if seeded:
            return seeded
        # Fallback: no seed (e.g. rolling buffer disabled) — wait for speech onset in live stream.
        return self._recorder.record_next(frames=self._frame_stream(), wait_for_speech_s=self._config.request_wait_seconds)

    def _run_wake_cycle(
        self,
        context_pcm16: bytes,
        seed_pcm16: bytes,
        *,
        trigger_text: str = "",
        wake_text_max_dist: int = 2,
        wake_source: str = "vosk",
    ) -> WakeCycleResult | None:
        """
        Clean 7-step voice pipeline:
          1. Get transcript  (wake trigger text from Vosk or meeting STT, OR fresh STT recording)
          2. Normalize       (lowercase, strip punctuation)
          3. Detect wake     (fuzzy text-level confirmation)
          4. Strip wake      (remove wake phrase -> command text)
          5. Classify intent (presence_check, recap, decision, general, ...)
          6. Route           (instant reply or LLM responder)
          7. Return result
        """
        # -- Step 1: Transcript -----------------------------------------------
        # Primary: use trigger text when the wake path already has it (Vosk final, or meeting
        # transcript STT) so we have the full utterance without re-capture lag.
        t0 = time.perf_counter()
        # Transcript wakes already have full utterance text — do not block on a stale
        # thinking-preroll thread from a prior turn (adds up to 1s dead air).
        if wake_source in ("transcript_stt", "transcript_followup"):
            self._join_thinking_preroll_thread(timeout_s=0.05)
        else:
            self._join_thinking_preroll_thread(timeout_s=1.0)
        trigger_text = (trigger_text or "").strip()

        if trigger_text:
            request_raw = trigger_text.strip()
            t_stt = (time.perf_counter() - t0) * 1000
            log.info("[TRANSCRIPT] from wake trigger text: %s", request_raw)
            self._update_user_language(None, text=request_raw)
        else:
            request_pcm16 = self._capture_request_pcm16(seed_pcm16=seed_pcm16)
            if not request_pcm16:
                log.info("[TRANSCRIPT] No request audio captured; returning dormant")
                return None
            req16 = pcm16_resample_linear(request_pcm16, src_sr=self._config.sample_rate_hz, dst_sr=self._wake_sr)
            _stt_t = self._stt.transcribe_pcm16(req16, self._wake_sr)
            request_raw = (_stt_t.text or "").strip()
            self._update_user_language(getattr(_stt_t, "language", None), text=request_raw)
            t_stt = (time.perf_counter() - t0) * 1000
            log.info("[TRANSCRIPT] from STT recording: %s", request_raw)

        # -- Step 2: Normalize ------------------------------------------------
        normalized = normalize_for_wake(request_raw)
        all_phrases = self._all_wake_phrases or tuple(self._config.wake_phrases)

        if wake_source == "transcript_followup":
            persona = self._personas.default()
            command_text = normalize_stt_command_text(request_raw)
            norm_follow = normalize_for_wake(request_raw)
            if detect_wake(norm_follow, all_phrases, max_dist=2):
                command_text = normalize_stt_command_text(
                    wd_strip_wake_last(norm_follow, all_phrases, max_dist=2)
                )
            log.info("[FOLLOWUP] persona=%s text=%s", persona.id, command_text)
        else:
            # -- Step 3: Wake detection (text-level) ------------------------------
            # Vosk already confirmed the wake - text-level is a safety net for the
            # STT-recording fallback path only. Never discard when Vosk fired.
            if wake_source == "transcript_stt":
                matched_phrase = wd_matched_wake_last(
                    normalized, all_phrases, max_dist=wake_text_max_dist
                )
            else:
                matched_phrase = matched_wake_phrase(
                    normalized, all_phrases, max_dist=wake_text_max_dist
                )
            wake_confirmed_in_text = matched_phrase is not None
            if wake_source == "transcript_stt":
                if not wake_confirmed_in_text:
                    if trigger_text:
                        # Scheduler already validated wake+command; STT chunk boundaries can
                        # make text-level re-check fail on an otherwise valid utterance.
                        wake_confirmed_in_text = True
                        matched_phrase = matched_phrase or wd_matched_wake_last(
                            normalized, all_phrases, max_dist=wake_text_max_dist
                        )
                    elif qualifies_as_wake_command(
                        normalized,
                        all_phrases,
                        max_dist=wake_text_max_dist,
                        max_leading_words_before_wake=self._transcript_wake_max_leading_words(),
                    ):
                        matched_phrase = matched_phrase or wd_matched_wake_last(
                            normalized, all_phrases, max_dist=wake_text_max_dist
                        )
                        wake_confirmed_in_text = matched_phrase is not None
                if not wake_confirmed_in_text:
                    log.info("[WAKE] Transcript path: wake phrase not confirmed; discarding")
                    return None
            elif not wake_confirmed_in_text and not trigger_text:
                log.info("[WAKE] Not confirmed in transcript text; discarding false wake")
                return None
            persona = (
                self._personas.resolve_by_wake(matched_phrase)
                if matched_phrase
                else self._personas.default()
            )
            log.info(
                "[WAKE DETECTED] persona=%s phrase=%s in: %s",
                persona.id,
                matched_phrase or "(vosk)",
                normalized,
            )

            # -- Step 4: Strip wake phrase -> command text ------------------------
            if wake_source == "transcript_stt":
                command_text = wd_strip_wake_last(
                    normalized, all_phrases, max_dist=wake_text_max_dist
                )
            else:
                command_text = wd_strip_wake(
                    normalized, all_phrases, max_dist=wake_text_max_dist
                )
            command_text = normalize_stt_command_text(command_text)
            log.info("[COMMAND TEXT] %s", command_text)

        if is_repeat_request(command_text):
            last = self._last_mechus_reply_for_repeat()
            if last:
                rephrased = self._rephrase_last_response(last, command_text)
                log.info("[REPEAT] replaying previous reply (%d → %d chars)", len(last), len(rephrased))
                return WakeCycleResult(
                    request_text=command_text,
                    response_text=rephrased,
                    is_instant=True,
                    persona=self._personas.default(),
                )

        if is_interrupt_request(command_text):
            log.info("[ROUTE] interrupt during follow-up/wake")
            self._hard_interrupt_tts()
            return None

        # -- Step 5: Classify intent ------------------------------------------
        t_router0 = time.perf_counter()
        intent = classify_pipeline_intent(command_text)
        t_router = (time.perf_counter() - t_router0) * 1000
        log.info("[INTENT] %s", intent)
        if command_text:
            self.emit_thought(
                "thinking",
                f"Intent: {intent} — {command_text[:200].strip()}",
            )
            self._remember_place_from_request(command_text)

        if not (command_text or "").strip():
            log.info("[ROUTE] wake with no command; staying silent")
            return None

        # FaceTime Video (Chert): answer questions about what the caller's camera shows.
        _frame_provider = getattr(self, "_vision_frame_provider", None)
        if _frame_provider is not None:
            try:
                from mechus.vision import answer_with_frame, looks_visual, vision_always

                if vision_always() or looks_visual(command_text):
                    _jpeg = _frame_provider()
                    if _jpeg:
                        _t_vis = time.monotonic()
                        _seen = answer_with_frame(
                            api_key=self._config.openai_api_key or "",
                            question=command_text,
                            jpeg=_jpeg,
                        )
                        if _seen:
                            log.info(
                                "[ROUTE] vision → camera frame answer (%d ms, %d KB frame)",
                                int((time.monotonic() - _t_vis) * 1000), len(_jpeg) // 1024,
                            )
                            log.info("[RESPONSE] %s", _seen)
                            self.emit_thought("thinking", "Looked at the caller's camera.")
                            return WakeCycleResult(
                                request_text=command_text,
                                response_text=_seen,
                                is_instant=True,
                                persona=persona,
                            )
                    else:
                        log.info("[ROUTE] vision: no camera frame available yet")
            except Exception as exc:  # noqa: BLE001
                log.warning("Vision short-circuit failed: %s", exc)

        try:
            from mechus.realtime import fetch_deterministic_spoken_answer

            _wake_st = self._enrich_wake_response_state({})
            _det_spoken = fetch_deterministic_spoken_answer(
                command_text,
                context_location=(_wake_st.get("last_place_context") or "").strip(),
                fallback_timezone=(_wake_st.get("fallback_timezone") or "").strip(),
            )
            if _det_spoken:
                log.info("[ROUTE] deterministic realtime → instant reply")
                self.emit_thought("thinking", "Answered directly from deterministic real-time data.")
                self.emit_thought(
                    "planned",
                    f"Responding to: {command_text[:100]}",
                    planned_speech=_det_spoken[:300],
                )
                return WakeCycleResult(
                    request_text=command_text,
                    response_text=_det_spoken,
                    is_instant=True,
                    persona=persona,
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("Deterministic realtime short-circuit failed: %s", exc)

        # Meeting transcript wakes: greetings/help/unknown get instant replies from
        # route_command. Scheduler (_meeting_action_wake_allowed) already filtered
        # non-directed chitchat.

        # Stop: cut any in-progress reply; stay silent (no "I'm listening" ack).
        if intent == INTENT_STOP:
            log.info("[ROUTE] stop intent")
            self._hard_interrupt_tts()
            if command_text:
                self._get_memory().add_segment(f"Meta: {command_text}")
            return None

        # Mic control (Zoom / FaceTime virtual mic backends)
        # If the user says "unmute yourself" and the host allows it, toggle the meeting mic.
        low_cmd = (command_text or "").strip().lower()
        if low_cmd in {"unmute", "unmute yourself", "unmute your mic", "unmute the mic", "unmute mic"}:
            out = self._audio_out
            fn = getattr(out, "unmute_mic", None)
            ok = False
            try:
                ok = bool(fn()) if callable(fn) else False
            except Exception:
                ok = False
            return WakeCycleResult(
                request_text=command_text,
                response_text=("Unmuted." if ok else "I tried, but Zoom won't let me unmute myself. Ask the host to allow participants to unmute."),
                is_instant=True,
                persona=self._personas.default(),
            )
        if low_cmd in {"mute", "mute yourself", "mute your mic", "mute the mic", "mute mic"}:
            out = self._audio_out
            fn = getattr(out, "mute_mic", None)
            ok = False
            try:
                ok = bool(fn()) if callable(fn) else False
            except Exception:
                ok = False
            return WakeCycleResult(
                request_text=command_text,
                response_text=("Muted." if ok else "I couldn't mute my mic."),
                is_instant=True,
                persona=self._personas.default(),
            )

        # Visual questions ("Mechus, look at the whiteboard") — FaceTime only, and
        # only when the worker registered a handler. The handler blocks this wake
        # cycle while the worker captures one frame and asks the vision model.
        _visual_handler = getattr(self, "_visual_query_handler", None)
        if _visual_handler is not None:
            try:
                from mechus.facetime_vision import is_visual_request

                _is_visual = is_visual_request(command_text)
            except Exception:  # noqa: BLE001
                _is_visual = False
            if _is_visual:
                log.info("[ROUTE] visual question → one-frame capture")
                self.emit_thought("thinking", "Taking one look at the call to answer a visual question.")
                _answer, _vision_err = None, "vision_failed"
                try:
                    _answer, _vision_err = _visual_handler(command_text, timeout_s=20.0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Visual question handler failed: %s", exc)
                if _answer:
                    _spoken = _answer
                elif _vision_err == "vision_rate_limited":
                    _spoken = "I've looked a few times already. Give me a minute before the next one."
                else:
                    _spoken = "I couldn't see that clearly. Try holding it closer to the camera."
                return WakeCycleResult(
                    request_text=command_text,
                    response_text=_spoken,
                    is_instant=True,
                    persona=persona,
                )

        # -- Step 6: Route ----------------------------------------------------
        # Shared mutable state for the nested responder function
        context_text: str = ""
        t_context: float = 0.0
        t_memory: float = 0.0
        t_llm: float = 0.0
        _event_ids: list = []

        def _llm_responder(cmd: str) -> str:
            nonlocal context_text, t_context, t_memory, t_llm, _event_ids

            # Context audio
            t_ctx0 = time.perf_counter()
            ctx = ""
            needs_context = any(
                k in cmd
                for k in [
                    "summarize", "summary", "recap", "action", "what did", "decide",
                    "decisions", "timeline", "deadline", "risk", "concern",
                    "open question", "tbd", "next steps",
                ]
            )
            if needs_context and self._config.context_seconds > 0 and context_pcm16:
                if not self._silence.silent_for(self._config.context_seconds):
                    trimmed = trim_silence_pcm16(
                        context_pcm16,
                        sample_rate_hz=self._config.sample_rate_hz,
                        frame_ms=self._config.frame_ms,
                        vad_mode=2,
                    ) or context_pcm16
                    ctx16 = pcm16_resample_linear(trimmed, src_sr=self._config.sample_rate_hz, dst_sr=self._wake_sr)
                    ctx = self._stt.transcribe_pcm16(ctx16, self._wake_sr).text.strip()
            context_text = ctx
            t_context = (time.perf_counter() - t_ctx0) * 1000

            if ctx:
                self._get_memory().add_segment(ctx)
            if cmd:
                self._get_memory().add_segment(f"User: {cmd}")

            # Retrieval (meeting_now_ts aligns time-window recaps with chunk timestamps from STT)
            t_mem0 = time.perf_counter()
            merged_for_query = merge_salient(
                self._dialog_salient_terms,
                extract_salient_terms(cmd),
            )
            if is_dialog_continuation(cmd):
                merged_for_query = merge_salient(
                    merged_for_query,
                    extract_salient_terms(self._dialog_last_user or ""),
                )
                merged_for_query = merge_salient(
                    merged_for_query,
                    extract_salient_terms(self._dialog_last_mechus or ""),
                )
            retrieval_query = build_retrieval_query(
                cmd,
                last_user=self._dialog_last_user,
                last_mechus=self._dialog_last_mechus,
                salient_terms=merged_for_query,
            )
            use_response_cache = (
                (not is_vague_followup(cmd))
                and (not is_dialog_continuation(cmd))
                and retrieval_query.strip() == cmd.strip()
            )
            _eng = self._get_context_engine()
            _now_ts = _eng.latest_chunk_timestamp() if _eng is not None else None
            recent = (ctx or "").strip()
            preamble = (self._transcript_wake_preamble_ctx or "").strip()
            if preamble:
                recent = f"{preamble}\n{recent}".strip() if recent else preamble
                log.info("[PREAMBLE] feeding recent transcript context (%d chars)", len(preamble))
            # Meeting scope for recap / summary / action-item intents.
            # Default for these intents is "current_only" — recaps should ONLY
            # cover this meeting unless the user explicitly asks for cross-meeting
            # context ("from last time", "all our sessions", etc.).  Cross-meeting
            # memory is also irrelevant for time-windowed recaps ("last 5 minutes").
            # For all other intents (general Q&A, decisions, etc.) keep "full" so
            # the LLM can pull relevant facts from prior sessions when asked.
            _SCOPE_INTENTS = (
                "recap_query", "action_item_query", "person_action_query",
                "decision_query", "open_question_query",
            )
            if intent in _SCOPE_INTENTS:
                _detected_scope = detect_meeting_scope(cmd)
                # Default to current_only; only widen to full/all_sessions/prior_only
                # when the user explicitly asked for cross-meeting context.
                _meeting_scope = _detected_scope if _detected_scope != "full" else "current_only"
            else:
                _meeting_scope = "full"
            _shared_ctx: str | None
            if _meeting_scope == "current_only":
                _shared_ctx = None
            elif _meeting_scope == "prior_only":
                _shared_ctx = self._cross_meeting_memory_text or None
            else:
                # "all_sessions" — include everything
                _shared_ctx = self._combined_shared_context_text() or None
            bundle = retrieve_for_question(
                cmd,
                retrieval_query=retrieval_query,
                salient_terms_used=merged_for_query,
                retrieval_extra_meta={
                    "cache_skipped": not use_response_cache,
                    "turn_salient_terms": extract_salient_terms(cmd),
                    "rolling_salient_terms_used": merged_for_query,
                    "wake_source": wake_source,
                    "meeting_scope": _meeting_scope,
                },
                context_engine=_eng,
                memory=self._get_memory(),
                recent_transcript=recent,
                shared_context_text=_shared_ctx,
                max_chars=self._config.retrieval_max_chars,
                general_max_chars=self._config.retrieval_general_max_chars,
                retrieval_mode=self._config.retrieval_mode,
                bm25_weight=self._config.retrieval_bm25_weight,
                tag_weight=self._config.retrieval_tag_weight,
                embed_weight=self._config.retrieval_embed_weight,
                embed_rerank_top_k=self._config.retrieval_embed_rerank_top_k,
                openai_api_key=self._config.openai_api_key,
                embedding_model=self._config.embedding_model,
                retrieval_auto_lexical_gap_min=self._config.retrieval_auto_lexical_gap_min,
                retrieval_auto_top_floor_hybrid=self._config.retrieval_auto_top_floor_hybrid,
                query_embed_cache_max=self._config.retrieval_query_embed_cache_max,
                meeting_now_ts=_now_ts,
                dialog_last_user=self._dialog_last_user,
                dialog_last_mechus=self._dialog_last_mechus,
            )
            t_memory = (time.perf_counter() - t_mem0) * 1000
            log.info(
                "Retrieval: intent=%s light_path=%s engine_snip_chars=%s ctx_chars=%s mem_retrieve_chars=%s",
                bundle.intent,
                bundle.meta.get("used_light_path"),
                bundle.meta.get("engine_snippet_chars"),
                bundle.meta.get("formatted_context_chars"),
                bundle.meta.get("memory_retrieve_chars"),
            )
            # Emit context thought from retrieval bundle (connections = top chunk labels)
            _ctx_chars = int(bundle.meta.get("engine_snippet_chars") or bundle.meta.get("formatted_context_chars") or 0)
            if _ctx_chars > 0:
                _top_chunks = getattr(bundle, "chunks", None) or []
                _conns: list[str] = []
                for _ch in _top_chunks[:3]:
                    _label = ""
                    if isinstance(_ch, dict):
                        _label = (str(_ch.get("topic") or _ch.get("speaker") or "")).strip()
                        if not _label:
                            _label = (str(_ch.get("text") or ""))[:60].strip()
                    elif hasattr(_ch, "topic"):
                        _label = (str(getattr(_ch, "topic", "") or getattr(_ch, "speaker", "") or "")).strip()
                    if _label:
                        _conns.append(_label)
                self.emit_thought(
                    "context",
                    f"Retrieved {_ctx_chars} chars of context for: {cmd[:120]}",
                    connections=_conns or None,
                )

            self._maybe_warm_wake_cache(persona.id, bundle)

            from mechus.responder import intent_wants_extended_spoken

            _extended_spoken = intent_wants_extended_spoken(
                getattr(bundle, "intent", None) or "", cmd
            )
            if _extended_spoken:
                use_response_cache = False
            self._last_extended_spoken = _extended_spoken

            # LLM / rule responder
            t_llm0 = time.perf_counter()
            # Cache key is persona-scoped so two personas don't share replies for the same words.
            cache_key = f"{persona.id}::{cmd}"
            cached = self._response_cache.get(cache_key) if use_response_cache else None
            # Phrases list is the union (so the responder can strip any persona's wake from the request).
            _resp_phrases = all_phrases or self._config.wake_phrases
            _resp_primary = persona.primary_wake_phrase() or self._config.wake_phrase
            if cached:
                # Pass through responder for repeat variation instead of returning directly
                _state = self._enrich_wake_response_state(
                    getattr(bundle, "state", None), request_text=cmd
                )
                resp = self._responder.respond(
                    ResponseInput(
                        wake_phrase=_resp_primary,
                        wake_phrases=_resp_phrases,
                        request_text=cmd,
                        context_text=bundle.formatted_context or ctx,
                        memory_text=bundle.memory_text,
                        context_chunks=getattr(bundle, "chunks", None) or [],
                        intent=getattr(bundle, "intent", None) or "",
                        state=_state,
                        retrieval_meta=bundle.meta,
                        persona=persona,
                        conversational_mode=(
                            False
                            if self._transcript_wake_only
                            else bool(self._config.conversational_mode)
                        ),
                        user_language=self._last_user_language,
                    )
                ).strip()
                _event_ids = bundle.event_ids
                if resp:
                    _pt = getattr(self._responder, "_last_private_thoughts", None)
                    if _pt:
                        self.emit_thought("thinking", _pt)
                    self.emit_thought("planned", f"Responding to: {cmd[:100]}", planned_speech=resp[:300])
            else:
                _state = self._enrich_wake_response_state(
                    getattr(bundle, "state", None), request_text=cmd
                )
                # Cache miss: real LLM — start cached earcon in parallel (cache hits stay silent/snap).
                _preroll_ok = self._config.thinking_preroll_enabled and (
                    wake_source != "transcript_stt" or self._config.thinking_preroll_transcript_stt
                )
                if _preroll_ok and (
                    self._wake_opener_owns_bridge(wake_source)
                    or self._wake_opener_bridging_audio()
                ):
                    _preroll_ok = False
                _pr_cap: float | None = None
                _pr_short = False
                if _preroll_ok and wake_source == "transcript_stt":
                    _tc = float(self._config.thinking_preroll_transcript_stt_max_audio_s)
                    if _tc > 0.0:
                        _pr_cap = _tc
                    _pr_short = True
                elif _preroll_ok:
                    _cat_cap = self._thinking_preroll_cap_for_category(cmd)
                    if _cat_cap and _cat_cap > 0.0:
                        _pr_cap = _cat_cap
                if _preroll_ok:
                    try:
                        from mechus.realtime import needs_realtime

                        if needs_realtime(cmd):
                            _preroll_ok = False
                    except Exception:
                        pass
                if _preroll_ok:
                    self._maybe_start_thinking_preroll_thread(
                        persona,
                        request_text=cmd,
                        max_audio_cap_s=_pr_cap,
                        prefer_short_fillers=_pr_short,
                        wake_source=wake_source,
                    )
                resp = self._responder.respond(
                    ResponseInput(
                        wake_phrase=_resp_primary,
                        wake_phrases=_resp_phrases,
                        request_text=cmd,
                        context_text=bundle.formatted_context or ctx,
                        memory_text=bundle.memory_text,
                        context_chunks=getattr(bundle, "chunks", None) or [],
                        intent=getattr(bundle, "intent", None) or "",
                        state=_state,
                        retrieval_meta=bundle.meta,
                        persona=persona,
                        conversational_mode=(
                            False
                            if self._transcript_wake_only
                            else bool(self._config.conversational_mode)
                        ),
                        user_language=self._last_user_language,
                    )
                ).strip()
                _event_ids = bundle.event_ids
                if resp:
                    _pt = getattr(self._responder, "_last_private_thoughts", None)
                    if _pt:
                        self.emit_thought("thinking", _pt)
                    self.emit_thought("planned", f"Responding to: {cmd[:100]}", planned_speech=resp[:300])
                if resp and use_response_cache:
                    self._response_cache.put(cache_key, resp)
            t_llm = (time.perf_counter() - t_llm0) * 1000
            if t_llm >= float(self._config.llm_outlier_log_ms):
                log.warning(
                    "LLM latency outlier: %.0fms (threshold=%.0f) request=%r",
                    t_llm,
                    self._config.llm_outlier_log_ms,
                    cmd[:100],
                )

            return resp
        
        # Only play thinking sound for LLM intents, not instant replies (including
        # presence/hear-me on FaceTime — must stay sub-second, no preroll overlap).
        _instant_intents = (
            INTENT_STOP,
            INTENT_GREETING,
            INTENT_HELP,
            INTENT_CLARIFICATION,
            INTENT_CONFIRM,
            INTENT_UNKNOWN,
            INTENT_PRESENCE_CHECK,
        )
        _needs_thinking = intent not in _instant_intents

        # Thinking earcon starts inside _llm_responder on cache-miss only (parallel with LLM).
        if _needs_thinking:
            self._tts_playing = True

        _mechus_answer_t0 = time.monotonic()
        try:
            route_result = route_command(
                intent,
                command_text,
                responder_fn=_llm_responder,
                meeting_transcript=self._transcript_wake_only
                and wake_source in ("transcript_stt", "transcript_followup"),
                user_language=self._last_user_language,
            )
        except BaseException:
            if _needs_thinking:
                self._mark_tts_not_playing()
            self._join_thinking_preroll_thread(timeout_s=2.0)
            raise

        response_text = route_result.response
        _use_tags = bool(getattr(persona, "audio_tags", False) or self._config.eleven_audio_tags)
        response_text = clean_spoken_text(
            response_text,
            preserve_audio_tags=_use_tags and not route_result.is_instant,
        )
        log.info("[RESPONSE] %s", response_text)
        try:
            from mechus.tiger_store import record_event as _tiger_event

            _tiger_event(
                self._current_meeting_id,
                "mechus_response",
                (time.monotonic() - _mechus_answer_t0) * 1000.0,
                intent=str(intent),
                wake_source=wake_source,
                instant=bool(route_result.is_instant),
                chars=len(response_text or ""),
            )
        except Exception:
            pass

        if intent == INTENT_RECAP and (response_text or "").strip():
            from mechus.retrieval import _extract_time_window_minutes

            _cmd_low = (command_text or "").lower()
            _full_meeting = any(
                p in _cmd_low
                for p in (
                    "summarize the meeting",
                    "summarize this meeting",
                    "summarize meeting",
                    "entire meeting",
                    "whole meeting",
                    "full recap",
                    "recap the meeting",
                    "recap this meeting",
                    "summarize everything",
                )
            ) or _cmd_low.strip() in ("summarize the meeting", "summarize meeting", "recap the meeting")
            if _full_meeting and _extract_time_window_minutes(command_text) is None:
                self._spoken_executive_recap = (response_text or "").strip()

        # -- Step 7: Return ---------------------------------------------------
        if not response_text:
            if _needs_thinking:
                self._mark_tts_not_playing()
            self._join_thinking_preroll_thread(timeout_s=2.0)
            return None

        # Overlap HTTP TTS with memory / notes / latency logging + post-cycle silence waits.
        self._start_main_response_presynth_early(persona, response_text)
        try:
            self._get_memory().add_segment(f"Mechus: {response_text}")

            self._dialog_salient_terms = merge_salient(
                self._dialog_salient_terms,
                extract_salient_terms(command_text),
            )
            self._dialog_last_user = command_text
            self._dialog_last_mechus = response_text[:500] if len(response_text) > 500 else response_text
            self._last_dialog_wake_monotonic_s = time.monotonic()

            # Persist snapshot + note OFF the critical path. These are pure disk
            # I/O not needed before Mechus speaks; doing them inline added latency
            # between the LLM finishing and playback (and grew with meeting size).
            store = self._get_meeting_store()
            _eng_for_snap = self._get_context_engine()
            _note_meta = {
                "request_raw": request_raw,
                "request": command_text,
                "response": response_text,
                "intent": intent,
                "context": context_text,
            }
            _note_text = f"User: {command_text}\nMechus: {response_text}"

            def _persist_turn() -> None:
                if store is not None and _eng_for_snap is not None:
                    try:
                        store.save_snapshot(_eng_for_snap)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Meeting store snapshot save skipped: %s", exc)
                try:
                    self._note_store.add(
                        kind="wake_cycle", text=_note_text, meta=_note_meta
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to write note: %s", exc)

            threading.Thread(
                target=_persist_turn, daemon=True, name="mechus-persist-turn"
            ).start()

            total_ms = t_stt + t_context + t_router + t_memory + t_llm
            self._last_latency = {
                "stt_ms": round(t_stt, 2),
                "context_ms": round(t_context, 2),
                "router_ms": round(t_router, 2),
                "memory_ms": round(t_memory, 2),
                "llm_ms": round(t_llm, 2),
                "total_ms": round(total_ms, 2),
            }
            log.info(
                "latency ms: stt=%.0f context=%.0f router=%.0f memory=%.0f llm=%.0f total=%.0f",
                t_stt, t_context, t_router, t_memory, t_llm, total_ms,
            )
            return WakeCycleResult(
                request_text=command_text,
                response_text=response_text,
                is_instant=route_result.is_instant,
                persona=persona,
            )
        except BaseException:
            self._discard_main_response_presynth()
            raise

    def _transcript_noise_extra_frozen(self) -> frozenset[str] | None:
        t = self._config.transcript_noise_extra
        if not t:
            return None
        return frozenset(t)

    def _should_drop_noise_segment(self, text: str) -> bool:
        if not self._config.transcript_drop_noise:
            return False
        return is_noise_only_transcript(text, extra_noise=self._transcript_noise_extra_frozen())

    def _on_async_diarize_result(
        self,
        chunk_id: int,
        segments: list[SpeakerSegment],
    ) -> None:
        """Callback from AsyncCloudDiarizer — runs in its worker thread.

        Patches speaker labels into MeetingStore / notes for the segments that
        were already ingested by the fast path.  We use a best-effort heuristic:
        if one speaker dominates the chunk (>60% of duration) we attribute the
        whole chunk to them; otherwise we emit per-segment entries.
        """
        if not segments:
            return
        try:
            store = self._get_meeting_store()
            if store is None:
                return
            total_dur = sum(max(0.0, s.end_s - s.start_s) for s in segments)
            if total_dur <= 0:
                return
            speaker_dur: dict[str, float] = {}
            for seg in segments:
                dur = max(0.0, seg.end_s - seg.start_s)
                lbl = self._speaker_label(seg.speaker)
                speaker_dur[lbl] = speaker_dur.get(lbl, 0.0) + dur
            dominant = max(speaker_dur, key=lambda k: speaker_dur[k])
            dominant_frac = speaker_dur[dominant] / total_dur
            log.debug(
                "_on_async_diarize_result chunk_id=%d segments=%d dominant=%r (%.0f%%)",
                chunk_id,
                len(segments),
                dominant,
                dominant_frac * 100,
            )
            # Patch the store entry for this chunk_id with the resolved speaker.
            # MeetingStore.patch_speaker_label is a no-op if not implemented — safe to call.
            try:
                if dominant_frac >= 0.6:
                    store.patch_speaker_label(chunk_id=chunk_id, speaker_id=dominant)
                else:
                    for seg in segments:
                        lbl = self._speaker_label(seg.speaker)
                        store.patch_speaker_label(chunk_id=chunk_id, speaker_id=lbl, start_s=seg.start_s, end_s=seg.end_s)
            except AttributeError:
                pass  # older store version without patch_speaker_label
        except Exception as exc:
            log.debug("_on_async_diarize_result error (non-fatal): %s", exc)

    def _is_duplicate_transcript_write(self, text: str, speaker: str, ts: float) -> bool:
        prev = self._last_transcript_write
        if prev is None:
            return False
        prev_text, prev_speaker, prev_ts = prev
        if (
            text.strip() == prev_text.strip()
            and speaker == prev_speaker
            and abs(ts - prev_ts) < 3.0
        ):
            return True
        return False

    def _handle_transcript_chunk(
        self,
        chunk: TranscriptChunk,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
    ) -> None:
        """Write transcript lines to engine, memory, MeetingStore, and notes."""
        _barge_in_light = bool(
            self._tts_playing
            and self._transcript_wake_only
            and getattr(self._config, "transcript_barge_in_stop_only", False)
        )
        pcm16_16k = pcm16_resample_linear(chunk.pcm16, src_sr=self._config.sample_rate_hz, dst_sr=self._wake_sr)
        raw_text = ""
        lines: list[str] = []
        tag_text_parts: list[str] = []
        segments_meta: list[dict[str, object]] = []

        # ── Async diarization path (non-blocking) ────────────────────────────
        # Submit the chunk to the background thread now. The fast path (STT +
        # wake + memory) below runs immediately without waiting; when the cloud
        # returns, _on_async_diarize_result patches speaker labels into the
        # meeting store and notes.  We only skip the synchronous diarizer block
        # when async mode is active so that latency is unaffected.
        if self._async_diarizer is not None and not speaker_label:
            audio_f32 = pcm16_to_float32(chunk.pcm16)
            chunk_id_snap = self._transcript_chunk_id  # stable snapshot
            self._async_diarizer.submit(
                chunk_id_snap,
                audio_f32,
                self._config.sample_rate_hz,
                chunk.start_ts,
                self._on_async_diarize_result,
            )

        # ── Synchronous diarization path (blocking, skipped in async mode) ──
        # Never run the blocking diarizer in transcript-wake-only mode (FaceTime):
        # per-chunk pyannote inference stalls the listen loop and Mechus stops hearing.
        # FaceTime should use the async cloud diarizer or rely on the roster scrape.
        if (
            self._diarizer is not None
            and not speaker_label
            and self._async_diarizer is None
            and not getattr(self, "_transcript_wake_only", False)
        ):
            audio_f32 = pcm16_to_float32(chunk.pcm16)
            audio_16k = resample_f32_linear(audio_f32, src_sr=self._config.sample_rate_hz, dst_sr=self._wake_sr)
            diarized = self._diarizer.diarize(audio_16k, self._wake_sr)
            
            # Process segments in chronological order
            # Diarization module already handles overlapping speech
            for seg in sorted(diarized, key=lambda s: (s.start_s, s.end_s)):
                # Extract audio for this segment
                start = int(max(0.0, seg.start_s) * self._wake_sr)
                end = int(min(seg.end_s, float(audio_16k.shape[0]) / self._wake_sr) * self._wake_sr)
                if end <= start:
                    continue
                
                seg_audio = audio_16k[start:end]
                if seg_audio.size == 0:
                    continue
                
                # Transcribe this segment
                seg_pcm16 = float32_to_pcm16(seg_audio)
                _seg_t = self._transcribe_stt_chunk(seg_pcm16, stream_commit=False)
                seg_text = (_seg_t.text or "").strip()
                self._update_user_language(getattr(_seg_t, "language", None), text=seg_text)
                if not seg_text:
                    continue
                
                if self._transcript_refiner is not None:
                    seg_text = self._transcript_refiner.refine(seg_text).strip()
                seg_text = clean_transcript_text(seg_text)
                if not seg_text:
                    continue
                if self._should_drop_noise_segment(seg_text):
                    continue
                # Resolve label BEFORE using it (avoids UnboundLocalError in first iteration).
                _seg_label = self._speaker_label(seg.speaker)
                self._try_schedule_transcript_wake(seg_text, chunk, speaker_label=_seg_label)

                label = self._speaker_label(seg.speaker)
                line = f"{label}: {seg_text}"
                lines.append(line)
                tag_text_parts.append(seg_text)
                segments_meta.append(
                    {
                        "speaker": label,
                        "start_s": round(seg.start_s, 3),
                        "end_s": round(seg.end_s, 3),
                        "text": seg_text,
                    }
                )
                self._get_memory().add_segment(line)
                eng = self._get_context_engine()
                if eng is not None:
                    _mu, _end, tagged = eng.ingest_chunk(seg_text, speaker_id=label, timestamp=chunk.start_ts)
                    store = self._get_meeting_store()
                    if store is not None:
                        if tagged is not None:
                            store.append_enriched_chunk(
                                label,
                                seg_text,
                                chunk.start_ts,
                                chunk_id=tagged.chunk_id,
                                topic=tagged.topic,
                                tags=tagged.tags,
                            )
                        else:
                            store.append_chunk(speaker_id=label, text=seg_text, timestamp=chunk.start_ts)
                    self._maybe_roll_chapter_after_ingest(eng)

        if not lines:
            _stt_t = self._transcribe_stt_chunk(pcm16_16k)
            raw_text = (_stt_t.text or "").strip()
            self._update_user_language(getattr(_stt_t, "language", None), text=raw_text)
            if not raw_text:
                return
            cleaned = raw_text
            if self._transcript_refiner is not None:
                cleaned = self._transcript_refiner.refine(raw_text).strip()
            cleaned = clean_transcript_text(cleaned)
            if not cleaned:
                return
            if self._should_drop_noise_segment(cleaned):
                return
            eff_label = speaker_label
            if not eff_label and chunk.speaker_id:
                eff_label = self._speaker_map.get(chunk.speaker_id, chunk.speaker_id)
            if not eff_label:
                # Mixed-audio platforms (FaceTime/Meet): attribute the line to the
                # participant the platform showed as speaking during this window.
                _active = self._resolve_active_speaker_for_window(
                    chunk.start_ts, chunk.end_ts
                )
                if _active:
                    eff_label = _active
                    log.debug(
                        "Active-speaker attribution: %r → %s",
                        cleaned[:48],
                        _active,
                    )
            wake_speaker_label = str(eff_label).strip() if eff_label else None
            self._try_schedule_transcript_wake(
                cleaned, chunk, speaker_label=wake_speaker_label
            )
            if _barge_in_light:
                return
            if eff_label:
                label = str(eff_label).strip() or (speaker_id or chunk.speaker_id or "unknown")
                lines = [f"{label}: {cleaned}"]
                self._maybe_learn_timezone_from_utterance(cleaned, label)
                self._track_timezone_conflicts(cleaned, label)
                segments_meta = [
                    {
                        "speaker": label,
                        "speaker_id": speaker_id or label,
                        "start_s": 0.0,
                        "end_s": round(max(0.0, chunk.end_ts - chunk.start_ts), 3),
                        "text": cleaned,
                    }
                ]
            else:
                lines = [cleaned]
                self._track_timezone_conflicts(cleaned, None)
            tag_text_parts = [cleaned]
            self._get_memory().add_segment(lines[0])
            eng = self._get_context_engine()
            if eng is not None:
                context_speaker_id = eff_label or speaker_label or speaker_id or chunk.speaker_id or "unknown"
                _mu, _end, tagged = eng.ingest_chunk(cleaned, speaker_id=context_speaker_id, timestamp=chunk.start_ts)
                store = self._get_meeting_store()
                if store is not None:
                    if tagged is not None:
                        store.append_enriched_chunk(
                            context_speaker_id,
                            cleaned,
                            chunk.start_ts,
                            chunk_id=tagged.chunk_id,
                            topic=tagged.topic,
                            tags=tagged.tags,
                        )
                    else:
                        store.append_chunk(speaker_id=context_speaker_id, text=cleaned, timestamp=chunk.start_ts)
                self._maybe_roll_chapter_after_ingest(eng)

        tag_text = " ".join(tag_text_parts).strip()
        tags = extract_tags(
            tag_text,
            max_tags=self._config.transcript_tag_max,
            min_len=self._config.transcript_tag_min_len,
        )
        self._transcript_chunk_id += 1
        
        # Create LLM-optimized context format
        llm_context = self._format_llm_context(lines, segments_meta, tags, chunk)
        
        meta = {
            "chunk_id": self._transcript_chunk_id,
            "start_ts": chunk.start_ts,
            "end_ts": chunk.end_ts,
        }
        if tags:
            meta["tags"] = tags
        if segments_meta:
            meta["segments"] = segments_meta
        if raw_text:
            meta["raw"] = raw_text
        meta["llm_context"] = llm_context  # Add LLM-optimized format
        
        text = "\n".join(lines)
        write_speaker = ""
        if lines:
            first = lines[0]
            if ": " in first:
                write_speaker, _ = first.split(": ", 1)
            else:
                write_speaker = speaker_label or speaker_id or ""
        if self._is_duplicate_transcript_write(text, write_speaker, chunk.start_ts):
            log.debug("Skipping duplicate transcript write (%d chars)", len(text))
            return
        self._last_transcript_write = (text.strip(), write_speaker, chunk.start_ts)
        self._note_store.add(kind="transcript", text=text, meta=meta)
        # Log chunk summary at INFO; full text only at DEBUG (privacy)
        if tags:
            log.info("Transcript chunk %s [%s]: %d chars", self._transcript_chunk_id, ", ".join(tags), len(text))
        else:
            log.info("Transcript chunk %s: %d chars", self._transcript_chunk_id, len(text))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Transcript chunk %s: %s", self._transcript_chunk_id, text)

    def _speaker_label(self, raw_label: str) -> str:
        if raw_label in self._speaker_map:
            return self._speaker_map[raw_label]
        self._speaker_count += 1
        if self._speaker_count <= 26:
            label = f"Speaker {chr(ord('A') + self._speaker_count - 1)}"
        else:
            label = f"Speaker {self._speaker_count}"
        self._speaker_map[raw_label] = label
        return label

    def _format_llm_context(
        self,
        lines: list[str],
        segments_meta: list[dict[str, object]],
        tags: list[str],
        chunk: TranscriptChunk,
    ) -> str:
        """Format transcript in a way that's optimized for LLM context extraction."""
        if not lines:
            return ""
        
        # Remove noise from transcript refiner artifacts
        cleaned_lines = []
        for line in lines:
            cleaned = line
            # Remove refiner artifacts
            cleaned = re.sub(
                r"(Sure!?\s*)?(Here is|Here's)\s+(the\s+)?(cleaned\s+)?(transcript|version):\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
            cleaned = cleaned.strip('"').strip()
            if cleaned and ":" in cleaned:
                # Check if it's a speaker line
                parts = cleaned.split(":", 1)
                if len(parts) == 2:
                    speaker = parts[0].strip()
                    content = parts[1].strip()
                    if content:
                        cleaned_lines.append(f"{speaker}: {content}")
                else:
                    if cleaned:
                        cleaned_lines.append(cleaned)
            elif cleaned:
                cleaned_lines.append(cleaned)
        
        # Format with clear structure for LLM
        parts = []
        
        # Add topic tags if available
        if tags:
            parts.append(f"Topics: {', '.join(tags)}")
        
        # Add structured conversation
        if segments_meta:
            for seg in segments_meta:
                speaker = seg.get("speaker", "Unknown")
                text = str(seg.get("text", "")).strip()
                if text:
                    # Remove refiner artifacts
                    text = re.sub(
                        r"(Sure!?\s*)?(Here is|Here's)\s+(the\s+)?(cleaned\s+)?(transcript|version):\s*",
                        "",
                        text,
                        flags=re.IGNORECASE,
                    )
                    text = text.strip('"').strip()
                    if text:
                        parts.append(f"{speaker}: {text}")
        else:
            for line in cleaned_lines:
                if line.strip():
                    parts.append(line)
        
        return "\n".join(parts)
    
    def _topic_from_request(self, request_text: str) -> str | None:
        """Infer topic namespace from question for focused retrieval (e.g. API, pricing)."""
        return _topic_from_request_impl(request_text)

    def _wake_cooldown_ready(self) -> bool:
        cd = (
            self._config.wake_cooldown_transcript_stt_s
            if self._transcript_wake_only
            else self._config.wake_cooldown_s
        )
        return (time.monotonic() - self._last_wake_fire_monotonic_s) >= cd

    def _in_followup_window(self) -> bool:
        if not self._dialog_last_user:
            return False
        window = self._config.followup_window_s
        if window <= 0:
            return False
        return (time.monotonic() - self._last_dialog_wake_monotonic_s) <= window

    def _qualifies_as_transcript_followup(self, raw_speech: str) -> bool:
        """True when recent dialog exists and this line is clearly directed at Mechus."""
        if not (self._dialog_last_mechus or "").strip():
            return False
        cmd = normalize_stt_command_text((raw_speech or "").strip())
        if not cmd:
            return False
        if is_interrupt_request(cmd):
            return False
        norm = normalize_for_wake(cmd)
        phrases = self._all_wake_phrases or tuple(self._config.wake_phrases)
        max_dist = self._transcript_wake_max_dist()
        if detect_wake(norm, phrases, max_dist=max_dist):
            rem = normalize_stt_command_text(
                wd_strip_wake_last(norm, phrases, max_dist=max_dist)
            )
            if is_repeat_request(rem):
                return True
            return False
        return qualifies_as_transcript_followup_without_wake(cmd)

    # If a new transcript chunk starts with these, do not VAD-merge with the prior chunk
    # (avoids "Uh, computer, who's" + "Oh, yes…" inventing a false wake).
    _TRANSCRIPT_NEW_UTTERANCE_STARTERS: frozenset[str] = frozenset({
        "oh",
        "ah",
        "uh",
        "um",
        "hmm",
        "hm",
        "yeah",
        "yep",
        "yes",
        "no",
        "nah",
        "well",
        "so",
        "okay",
        "ok",
        "right",
        "bro",
        "like",
    })

    _MEETING_HESITATION_REMAINDERS: frozenset[str] = frozenset({
        "wait",
        "hold on",
        "hang on",
        "one sec",
        "one second",
        "one moment",
        "let me think",
        "let me check",
        "let me see",
        "give me a sec",
        "give me a second",
        "give me a moment",
        "um",
        "uh",
        "uhm",
        "hmm",
        "okay",
        "ok",
        "alright",
        "right",
        "yeah",
        "yep",
        "sure",
        "well",
        "so",
        "anyway",
        "anyways",
        "thanks",
        "thank you",
        "ah",
        "oh",
        "oh okay",
        "oh right",
    })

    def _meeting_action_wake_allowed(
        self,
        norm: str,
        wake_phrases: tuple[str, ...] | list[str],
        *,
        max_dist: int,
    ) -> bool:
        """True when wake was said with a directed question (incl. hear-me), not promo/chitchat."""
        remainder = normalize_stt_command_text(
            wd_strip_wake_last(norm, wake_phrases, max_dist=max_dist)
        )
        rem = (remainder or "").strip()
        if not rem:
            return False
        if is_incomplete_transcript_command(rem):
            return False
        if is_repeat_request(rem):
            return True
        low = re.sub(r"\s+", " ", rem.lower()).strip(" .,!?")
        intent = classify_pipeline_intent(remainder)
        if intent == INTENT_STOP:
            return True
        if low in self._MEETING_HESITATION_REMAINDERS:
            return False
        tokens = [t for t in re.split(r"\s+", low) if t]
        substantive = [t for t in tokens if len(t) >= 2]
        if not substantive:
            return False
        if _is_presence_check(remainder):
            return True
        # Multilingual relaxation: when STT detected a non-English language,
        # the English short-action keyword list won't catch the user's intent
        # (e.g. Spanish "qué hora", "dame eso"). Two substantive tokens are
        # enough — the wake itself plus a real follow is signal enough.
        if self._is_non_english_user_language():
            return len(substantive) >= 2
        if len(substantive) >= 2 and tokens:
            if tokens[0] in (
                "what",
                "who",
                "when",
                "where",
                "why",
                "how",
                "which",
                "can",
                "could",
                "would",
                "will",
                "tell",
                "give",
                "is",
                "are",
                "do",
                "does",
            ):
                return True
        if len(substantive) < 3:
            short_action_keywords = (
                "recap",
                "summary",
                "summarize",
                "stop",
                "decisions",
                "decision",
                "actions",
                "agenda",
                "risks",
                "timeline",
                "weather",
                "time",
                "date",
                "forecast",
                "temperature",
                "largest",
                "smallest",
                "biggest",
                "planet",
                "hear",
                "listening",
                "repeat",
                "explain",
                "define",
                "translate",
            )
            if low not in short_action_keywords and not _is_presence_check(remainder):
                return False
        if intent in _MEETING_NON_ACTION_INTENTS:
            return False
        return True

    def _transcript_wake_merge_window_s(self) -> float:
        return max(0.5, float(self._config.wake_transcript_merge_s))

    def _update_user_language(self, lang: object, *, text: str = "") -> None:
        """Record the language detected by STT for the most recent utterance.

        Falls back to script-based guessing when STT omits language (common on
        short ElevenLabs realtime segments). Single-letter / unknown values are dropped.
        """
        from mechus.language import guess_language_from_text, normalize_language_code

        code = normalize_language_code(lang)
        if not code and (text or "").strip():
            code = guess_language_from_text(text)
        if not code:
            return
        if code != (self._last_user_language or ""):
            self._last_user_language = code
        self._refresh_accent_adaptation(text=text)

    @staticmethod
    def _text_has_heavy_non_latin(text: str) -> bool:
        """True when the transcript chunk is mostly non-Latin script (code-switching)."""
        letters = 0
        non_latin = 0
        for ch in (text or ""):
            if not ch.isalpha():
                continue
            letters += 1
            if ord(ch) > 0x024F:
                non_latin += 1
        return letters >= 4 and (non_latin / letters) >= 0.35

    def _refresh_accent_adaptation(self, *, text: str = "") -> None:
        """Enable slower/clearer replies when the speaker uses another language or script."""
        if self._is_non_english_user_language():
            self._accent_adapt_speech = True
            return
        if text and self._text_has_heavy_non_latin(text):
            self._accent_adapt_speech = True

    def _is_non_english_user_language(self) -> bool:
        """True when the most recent STT detected a non-English language.

        Used to relax the English-only wake/command qualifier so utterances
        like "computer, qué hora es?" or "computer, wie spät ist es?" still
        wake. We accept anything that isn't an obvious English code; treat
        empty/unknown as English (safe default — Mechus's English heuristics
        still apply).
        """
        code = (self._last_user_language or "").strip().lower()
        if not code:
            return False
        # Normalize "en-US", "en_GB", etc. to "en" prefix.
        prefix = code.split("-", 1)[0].split("_", 1)[0]
        return prefix not in ("", "en", "und", "unk", "nn")

    def _transcript_chunk_starts_new_utterance(self, raw_speech: str) -> bool:
        """True when this chunk is a new sentence, not a continuation of the prior VAD fragment."""
        words = normalize_for_wake((raw_speech or "").strip()).split()
        if not words:
            return False
        return words[0] in self._TRANSCRIPT_NEW_UTTERANCE_STARTERS

    def _transcript_preamble_window_s(self) -> float:
        return max(0.0, float(self._config.wake_transcript_preamble_s))

    def _transcript_wake_max_leading_words(self) -> int:
        return max(0, int(self._config.wake_transcript_max_leading_words))

    def _transcript_wake_max_dist(self) -> int:
        """Fuzzy wake tolerance for meeting STT (config default 1). Local Vosk keeps ≥2."""
        cfg = max(0, int(self._config.wake_transcript_max_dist))
        if self._transcript_wake_only:
            return cfg
        return max(cfg, 2)

    def _pending_transcript_wake_is_barge_in_eligible(self) -> bool:
        """When stop-only barge-in is on, only explicit stop directives may cut TTS."""
        pending = self._pending_transcript_wake
        if pending is None:
            return False
        if not self._config.transcript_barge_in_stop_only:
            return True
        trig = (pending[0] or "").strip()
        if not trig:
            return False
        wake_phrases = self._all_wake_phrases or self._config.wake_phrases
        action_norm = normalize_for_wake(trig)
        rem = wd_strip_wake_last(action_norm, wake_phrases, self._transcript_wake_max_dist())
        rem_cmd = normalize_stt_command_text(rem)
        if remainder_is_stop_directive(rem) or is_repeat_request(rem_cmd):
            return True
        return False

    def _preamble_speaker_key(self, speaker_label: str | None) -> str:
        if not speaker_label:
            return ""
        return speaker_label.strip().lower()

    def _line_contains_wake_address(self, raw_speech: str) -> bool:
        norm = normalize_for_wake((raw_speech or "").strip())
        if not norm:
            return False
        phrases = self._all_wake_phrases or self._config.wake_phrases
        return detect_wake(norm, phrases, self._transcript_wake_max_dist())

    def _record_transcript_preamble_line(
        self,
        raw_speech: str,
        now: float,
        *,
        speaker_label: str | None = None,
    ) -> None:
        """Keep recent human STT lines to prepend when wake arrives in a later chunk."""
        text = (raw_speech or "").strip()
        if len(text) < 2:
            return
        spk = self._preamble_speaker_key(speaker_label)
        buf = self._transcript_preamble_buf
        if buf and buf[-1][0] == text and buf[-1][2] == spk:
            buf[-1] = (text, now, spk)
            return
        buf.append((text, now, spk))
        window = self._transcript_preamble_window_s()
        if window <= 0:
            return
        while buf and now - buf[0][1] > window:
            buf.popleft()

    def _recent_transcript_preamble_text(self, speaker_label: str | None = None) -> str:
        """Join buffered lines from the same speaker that do not already contain a wake phrase."""
        wake_phrases = self._all_wake_phrases or self._config.wake_phrases
        max_dist = self._transcript_wake_max_dist()
        spk = self._preamble_speaker_key(speaker_label)
        use_any_speaker = not bool(spk)
        parts: list[str] = []
        for text, _ts, line_spk in self._transcript_preamble_buf:
            if not use_any_speaker and line_spk != spk:
                continue
            chunk = text.strip()
            if not chunk:
                continue
            norm = normalize_for_wake(chunk)
            if detect_wake(norm, wake_phrases, max_dist):
                continue
            parts.append(chunk)
        joined = " ".join(parts).strip()
        if len(joined) > 320:
            joined = joined[-320:].lstrip()
        return joined

    def _vad_merge_transcript_wake_text(self, raw_speech: str) -> str:
        """Join only VAD-adjacent fragments (no cross-chunk preamble buffer)."""
        now = time.monotonic()
        window = self._transcript_wake_merge_window_s()
        parts: list[str] = []
        prev = self._transcript_wake_merge_prev
        if prev is not None:
            prev_text, prev_ts = prev
            if (
                now - prev_ts <= window
                and prev_text
                and not self._transcript_chunk_starts_new_utterance(raw_speech)
            ):
                parts.append(prev_text)
        chunk = (raw_speech or "").strip()
        if chunk:
            parts.append(chunk)
        return " ".join(parts).strip()

    def _transcript_wake_trigger_text(
        self, raw_speech: str, *, speaker_label: str | None = None
    ) -> str:
        """Utterance queued for wake: same-speaker preamble + VAD merge (not duplicate wake lines)."""
        parts: list[str] = []
        pre = self._recent_transcript_preamble_text(speaker_label)
        if pre:
            parts.append(pre)
        vad = self._vad_merge_transcript_wake_text(raw_speech)
        if vad:
            parts.append(vad)
        return " ".join(parts).strip()

    def _combined_transcript_wake_text(
        self, raw_speech: str, *, speaker_label: str | None = None
    ) -> str:
        """Alias for trigger text (logging / thoughts)."""
        return self._transcript_wake_trigger_text(
            raw_speech, speaker_label=speaker_label
        )

    def _transcript_line_qualifies_as_wake(
        self,
        raw_speech: str,
        wake_phrases: tuple[str, ...] | list[str],
        *,
        max_dist: int,
        max_leading: int,
    ) -> bool:
        """Wake+command on this utterance (not cross-chunk preamble — that only adds context)."""
        bypass = self._is_non_english_user_language()
        line_norm = normalize_for_wake((raw_speech or "").strip())
        if qualifies_as_directed_transcript_wake(
            line_norm,
            wake_phrases,
            max_dist=max_dist,
            max_leading_words_before_wake=max_leading,
            bypass_command_qualifier=bypass,
        ):
            return True
        vad_norm = normalize_for_wake(self._vad_merge_transcript_wake_text(raw_speech))
        if vad_norm != line_norm and qualifies_as_directed_transcript_wake(
            vad_norm,
            wake_phrases,
            max_dist=max_dist,
            max_leading_words_before_wake=max_leading,
            bypass_command_qualifier=bypass,
        ):
            return True
        return False

    def _transcript_wake_echo_mute_active(self) -> bool:
        return time.monotonic() < self._transcript_wake_echo_mute_until

    def _should_ignore_transcript_wake_line(self, raw_speech: str) -> bool:
        """Drop lines likely to be Mechus's own TTS echo or promo instant-reply text in the mix."""
        if self._line_contains_wake_address(raw_speech):
            return False
        norm_cmd = normalize_stt_command_text((raw_speech or "").strip())
        if (
            self._tts_playing
            and self._config.transcript_barge_in_enabled
            and (
                is_interrupt_request(norm_cmd)
                or remainder_is_stop_directive(norm_cmd)
                or is_repeat_request(norm_cmd)
            )
        ):
            return False
        if self._transcript_wake_echo_mute_active():
            if (
                not self._tts_playing
                and self._in_followup_window()
                and self._qualifies_as_transcript_followup(raw_speech)
            ):
                return False
            return True
        from mechus.dialog_memory import is_stt_noise_annotation

        if is_stt_noise_annotation(raw_speech):
            return True
        t = normalize_for_wake((raw_speech or "").strip()).lower()
        if not t:
            return True
        last = (self._dialog_last_mechus or "").strip().lower()
        if last and len(last) >= 24:
            # Substantial overlap with Mechus's last spoken line → echo, not a new human wake.
            if t in last or last in t:
                return True
            lt = last.split()
            tt = t.split()
            if len(lt) >= 6 and len(tt) >= 6:
                overlap = len(set(lt) & set(tt))
                if overlap >= min(8, len(lt) // 2, len(tt) // 2):
                    return True
        for frag in _MECHUS_SELF_ECHO_FRAGMENTS:
            if frag in t:
                return True
        return False

    def _is_mechus_speaker_label(self, speaker_label: str | None) -> bool:
        """Ignore transcript wakes attributed to Mechus's own Zoom participant (TTS echo)."""
        if not speaker_label:
            return False
        lab = speaker_label.strip().lower()
        if not lab:
            return False
        if lab in ("mechus", _ASSISTANT_NAME.lower()):
            return True
        return lab == self._mechus_display_name_lc

    def _try_schedule_transcript_wake(
        self,
        raw_speech: str,
        chunk: TranscriptChunk,
        *,
        speaker_label: str | None = None,
    ) -> None:
        """If meeting STT text contains wake + command, queue a wake cycle (Vosk often misses long lines)."""
        if self._is_mechus_speaker_label(speaker_label):
            return
        if not self._config.transcript_wake_enabled and not self._force_transcript_wake:
            return
        if not raw_speech:
            return
        if self._pending_transcript_wake is not None:
            log.debug("Replacing pending transcript wake with newer line")
        if self._tts_playing:
            if not (
                self._transcript_wake_only
                and self._config.transcript_barge_in_enabled
            ):
                log.debug(
                    "Transcript wake blocked (_tts_playing; barge-in off): %s",
                    raw_speech[:80],
                )
                return
        if not self._wake_cooldown_ready():
            if not (
                self._tts_playing
                and self._config.transcript_barge_in_enabled
            ):
                return
        raw_speech = normalize_stt_command_text(raw_speech.strip())
        if not raw_speech:
            return
        if self._should_ignore_transcript_wake_line(raw_speech):
            return
        now = time.monotonic()
        self._record_transcript_preamble_line(raw_speech, now, speaker_label=speaker_label)
        wake_phrases = self._all_wake_phrases or self._config.wake_phrases
        max_dist = self._transcript_wake_max_dist()
        max_leading = self._transcript_wake_max_leading_words()
        line_norm = normalize_for_wake(raw_speech)
        vad_line = self._vad_merge_transcript_wake_text(raw_speech) or raw_speech
        vad_norm = normalize_for_wake(vad_line)
        qualifies_line = self._transcript_line_qualifies_as_wake(
            raw_speech,
            wake_phrases,
            max_dist=max_dist,
            max_leading=max_leading,
        )
        qualifies_vad = False
        if vad_norm != line_norm:
            qualifies_vad = qualifies_as_directed_transcript_wake(
                vad_norm,
                wake_phrases,
                max_dist=max_dist,
                max_leading_words_before_wake=max_leading,
                bypass_command_qualifier=self._is_non_english_user_language(),
            )
        # VAD merge can invent a wake (e.g. prior "I want…" + "cool love me edit" → cool≈mechus).
        if qualifies_vad and not qualifies_line:
            if not line_has_explicit_wake_token(line_norm, wake_phrases, max_dist):
                log.debug(
                    "Transcript wake: ignore VAD-only fuzzy match (no wake on this line): %s",
                    raw_speech[:120],
                )
                qualifies_vad = False
        if qualifies_line or qualifies_vad:
            guard_norm = line_norm if qualifies_line else vad_norm
            if should_suppress_transcript_wake(guard_norm, wake_phrases, max_dist):
                reason = "meta/product/device mention"
                if is_false_address_use(guard_norm):
                    reason = "device/topic mention"
                elif is_product_mention_use(guard_norm):
                    reason = "product mention"
                log.info(
                    "Transcript wake skipped (%s, not address): %s",
                    reason,
                    guard_norm[:120],
                )
                return
            trigger = (
                raw_speech.strip()
                if qualifies_line
                else self._transcript_wake_trigger_text(
                    raw_speech, speaker_label=speaker_label
                )
            )
            action_norm = line_norm if qualifies_line else vad_norm
            _cmd_probe = normalize_stt_command_text(
                wd_strip_wake_last(action_norm, wake_phrases, max_dist)
            )
            if is_incomplete_transcript_command(_cmd_probe):
                vad_hold = self._vad_merge_transcript_wake_text(raw_speech)
                self._transcript_wake_merge_prev = (vad_hold, now)
                log.debug(
                    "Transcript wake held (incomplete command): %s",
                    _cmd_probe[:120],
                )
                return
            if self._transcript_wake_only and not self._meeting_action_wake_allowed(
                action_norm, wake_phrases, max_dist=max_dist
            ):
                log.info(
                    "Transcript wake skipped (no action after wake): %s",
                    trigger[:120],
                )
                return
            # Stop-only barge-in: while a reply is playing, only stop or repeat
            # may cut Mechus off. New questions wait until the turn ends.
            if (
                self._tts_playing
                and getattr(self._config, "transcript_barge_in_stop_only", False)
            ):
                _rem = wd_strip_wake_last(action_norm, wake_phrases, max_dist)
                _rem_cmd = normalize_stt_command_text(_rem)
                if not remainder_is_stop_directive(_rem) and not is_repeat_request(_rem_cmd):
                    log.info(
                        "Barge-in ignored (not stop/repeat; finishing turn): %s",
                        trigger[:120],
                    )
                    return
            self._transcript_wake_merge_prev = None
            self._pending_transcript_preamble_snapshot = self._recent_transcript_preamble_text(
                speaker_label
            )
            self._transcript_preamble_buf.clear()
            self._pending_transcript_wake = (trigger, chunk.pcm16, "transcript_stt")
            if self._tts_playing and self._pending_transcript_wake_is_barge_in_eligible():
                self._hard_interrupt_tts()
            if self._tts_playing:
                log.info("Transcript barge-in queued during TTS: %s", trigger[:160])
            else:
                log.info("Transcript wake scheduled (will run after this frame): %s", trigger[:160])
            self.emit_thought("thinking", f"Heard: {trigger[:200].strip()}")
            return
        # Wakeless interrupt while Mechus is speaking ("stop", "shut up", "hold on").
        _norm_line = normalize_stt_command_text(raw_speech)
        if (
            self._tts_playing
            and self._config.transcript_barge_in_enabled
            and (is_interrupt_request(_norm_line) or is_repeat_request(_norm_line))
        ):
            self._pending_transcript_wake = (
                raw_speech.strip(),
                chunk.pcm16,
                "transcript_followup",
            )
            self._hard_interrupt_tts()
            log.info("Interrupt/repeat barge-in scheduled: %s", raw_speech[:160])
            return
        # Wakeless follow-ups ("how big is it?") only count when Mechus is idle. We
        # do NOT let a wakeless line barge in mid-reply — ambient chatter would
        # cut Mechus off. Interrupting mid-reply requires an explicit "Mechus …" or
        # "Mechus, stop" (handled by the directed-wake path above).
        if (
            not self._tts_playing
            and self._in_followup_window()
            and self._qualifies_as_transcript_followup(raw_speech)
        ):
            self._pending_transcript_preamble_snapshot = self._recent_transcript_preamble_text(
                speaker_label
            )
            self._pending_transcript_wake = (raw_speech.strip(), chunk.pcm16, "transcript_followup")
            log.info("Transcript follow-up scheduled: %s", raw_speech[:160])
            self.emit_thought("thinking", f"Follow-up: {raw_speech[:200].strip()}")
            return
        # Hold short lines so the next VAD chunk can complete the utterance (wake + question).
        vad_hold = self._vad_merge_transcript_wake_text(raw_speech)
        merge_window = self._transcript_wake_merge_window_s()
        if len(vad_hold) < 120:
            prev = self._transcript_wake_merge_prev
            if prev is not None:
                prev_text, prev_ts = prev
                if (
                    now - prev_ts <= merge_window
                    and not self._transcript_chunk_starts_new_utterance(raw_speech)
                ):
                    vad_hold = f"{prev_text} {raw_speech}".strip()
            self._transcript_wake_merge_prev = (vad_hold, now)

    def _run_wake_pipeline_after_detect(
        self,
        *,
        trigger_text: str,
        seed_pcm16: bytes,
        context_pcm16: bytes,
        source: str,
    ) -> bool:
        """Flush buffers, run command pipeline, speak. Caller sets _last_wake_fire_monotonic_s.

        Returns False if the main run loop should stop (e.g. shutdown while waiting for silence).
        """
        log.info("Wake detected (%s)", source)
        trigger_text = (trigger_text or "").strip()
        self._transcript_wake_preamble_ctx = ""
        if source in ("transcript_stt", "transcript_followup"):
            phrases = self._all_wake_phrases or self._config.wake_phrases
            md = (
                self._transcript_wake_max_dist()
                if source == "transcript_stt"
                else 2
            )
            pre = preamble_before_wake_last(
                normalize_for_wake(trigger_text), phrases, max_dist=md
            )
            snap = (getattr(self, "_pending_transcript_preamble_snapshot", None) or "").strip()
            parts = [p for p in (snap, pre) if p]
            if parts:
                self._transcript_wake_preamble_ctx = "\n".join(parts)
                log.info(
                    "[PREAMBLE] meeting context (%d chars)%s",
                    len(self._transcript_wake_preamble_ctx),
                    f" before wake: {pre[:120]}" if pre else "",
                )
            self._pending_transcript_preamble_snapshot = ""
        self._wake_pipeline_t0 = time.perf_counter()
        self._wake_opener_played = False
        self._bridge_finale_preroll_played = False
        self._last_thinking_phrase_spoken = ""
        self._flush_transcript_chunkers()
        self._wake.reset()
        wake_text_max_dist = (
            self._transcript_wake_max_dist()
            if source in ("transcript_stt", "transcript_followup")
            else 2
        )
        _opener_persona = self._persona_from_trigger(trigger_text)
        _cycle_box: list = []
        _cycle_exc: list[BaseException] = []

        def _cycle_worker() -> None:
            try:
                _cycle_box.append(
                    self._run_wake_cycle(
                        context_pcm16=context_pcm16,
                        seed_pcm16=seed_pcm16,
                        trigger_text=trigger_text,
                        wake_text_max_dist=wake_text_max_dist,
                        wake_source=source,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                _cycle_exc.append(exc)

        _opener_stop_evt = threading.Event()
        _opener_th: threading.Thread | None = None
        self._wake_opener_thread = None
        if self._should_play_wake_opener(trigger_text, source=source):
            _opener_th = threading.Thread(
                target=self._run_wake_opener,
                args=(_opener_persona,),
                kwargs={
                    "source": source,
                    "trigger_text": trigger_text,
                    "stop_evt": _opener_stop_evt,
                },
                daemon=True,
                name="mechus-wake-opener",
            )
            self._wake_opener_thread = _opener_th
            _opener_th.start()

        _cycle_th = threading.Thread(target=_cycle_worker, daemon=True, name="mechus-wake-cycle")
        _cycle_th.start()
        _cycle_th.join()
        if _cycle_exc:
            self._release_wake_opener_bridge(_opener_stop_evt, _opener_th, timeout_s=4.0)
            raise _cycle_exc[0]
        cycle = _cycle_box[0] if _cycle_box else None
        if cycle is None:
            self._release_wake_opener_bridge(_opener_stop_evt, _opener_th, timeout_s=4.0)
            # Opener thread sets _tts_playing; without this, transcript wakes stay blocked
            # until the next full TTS reply (MECHUS_TRANSCRIPT_BARGE_IN defaults off).
            self._mark_tts_not_playing()
            self._wake_opener_played = False
            log.info("Returned dormant (no speech — non-action or empty response)")
            return True

        if cycle.response_text and (
            not cycle.is_instant
            and (
            self._wake_opener_played
            or getattr(self, "_bridge_finale_preroll_played", False)
            or (_opener_th is not None and _opener_th.is_alive())
            or self._config.thinking_preroll_enabled
            )
        ):
            _stripped = strip_preroll_echo_from_response(
                cycle.response_text,
                self._last_thinking_phrase_spoken or "",
            )
            if _stripped != (cycle.response_text or ""):
                if log.isEnabledFor(logging.DEBUG):
                    log.debug(
                        "Stripped preroll echo from response (%d -> %d chars)",
                        len(cycle.response_text or ""),
                        len(_stripped),
                    )
                cycle = WakeCycleResult(
                    request_text=cycle.request_text,
                    response_text=_stripped,
                    is_instant=cycle.is_instant,
                    persona=cycle.persona,
                )

        _main_text_early = (cycle.response_text or "").strip()
        # Stream TTS as chunks arrive. Keep the opener extend loop running until the
        # first synthesized chunk is ready — avoids dead air between bridge and reply.
        _stream_prefetch: dict[str, list[tuple]] = {}
        _defer_opener_handoff = bool(
            _opener_th is not None
            and _opener_th.is_alive()
            and _main_text_early
        )
        if not _defer_opener_handoff:
            self._release_wake_opener_bridge(_opener_stop_evt, _opener_th)
        else:
            log.info("Wake bridge: holding extend until first TTS chunk is ready")
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Request: %s", cycle.request_text)
            log.debug("Response: %s", cycle.response_text)

        # Instant replies skip in-LLM thinking; they still need the guard so a new transcript
        # wake cannot start another Zoom play and cancel this response mid-flight.
        if not self._tts_playing:
            self._tts_playing = True
        # Pick the TTS engine that matches the persona who was addressed (cycle.persona).
        # Falls back to default voice for instant replies / when no persona is on the result.
        tts_engine = self._tts_for(cycle.persona)
        t_tts0: float | None = None
        transcript_barge_in = False
        interrupted = False
        vad_interrupted = False
        barge_stop_reason: str | None = None
        playback_completed = False
        _opener_for_handoff = (
            (_opener_stop_evt, _opener_th) if _defer_opener_handoff else (None, None)
        )
        _reply_phase = "instant" if cycle.is_instant else "reply"
        try:
            if cycle.is_instant:
                silence_s = self._config.min_silence_before_speaking_instant_s
            elif source in ("transcript_stt", "transcript_followup"):
                # FaceTime/meeting chunk already implies end-of-question; 0.6s local wait stacks on top of LLM time.
                silence_s = self._config.min_silence_before_speaking_transcript_stt_s
            else:
                silence_s = self._config.min_silence_before_speaking_s
            # Overlap main ElevenLabs/Piper HTTP synthesize with the speaking-gap wait (streaming
            # engines skip this — they need sequential chunk playback).
            _main_text = cycle.response_text or ""
            _can_presynth = bool(_main_text) and (not hasattr(tts_engine, "synthesize_streaming"))
            _presynth_box: list = []
            _ps_th: threading.Thread | None = None

            if _can_presynth:
                if self._main_tts_presynth_thread is not None:
                    _ps_th = self._main_tts_presynth_thread
                    _presynth_box = self._main_tts_presynth_box
                    self._main_tts_presynth_thread = None
                    self._main_tts_presynth_box = []
                else:
                    # Wake cycle did not start early synthesize (tests / unusual paths).
                    def _main_synth_worker() -> None:
                        try:
                            _presynth_box.append(tts_engine.synthesize(_main_text))
                        except Exception as exc:  # noqa: BLE001
                            _presynth_box.append(exc)

                    _ps_th = threading.Thread(target=_main_synth_worker, daemon=True)
                    _ps_th.start()

            if not self._skip_post_llm_silence_wait(source):
                if not self._wait_for_silence(required_silence_s=silence_s):
                    if _defer_opener_handoff:
                        self._release_wake_opener_bridge(_opener_stop_evt, _opener_th)
                    if _ps_th is not None:
                        _ps_th.join(timeout=120.0)
                    self._join_thinking_preroll_thread(timeout_s=2.0)
                    return False
            elif log.isEnabledFor(logging.DEBUG):
                log.debug("Skipping post-LLM silence wait (transcript wake, low_latency)")

            # Parallel in-LLM preroll (no wake opener) must finish before main audio.
            if (
                self._config.thinking_preroll_enabled
                and not _defer_opener_handoff
                and not cycle.is_instant
                and not getattr(self, "_wake_opener_played", False)
            ):
                self._join_thinking_preroll_thread(timeout_s=8.0)
            if not _defer_opener_handoff and not cycle.is_instant:
                # When the unified preroll pipeline is running, it owns the audio
                # device and bridges directly into the TTS response — _finished is
                # cleared for the duration of the pipeline, so calling
                # _wait_audio_output_idle() here would block on _finished.wait()
                # until the response ends (deadlock).  Skip the wait; the pipeline
                # handles sequencing.  For Zoom / wake-opener paths the pipeline is
                # not used and the previous clip has already finished (play_blocking
                # is synchronous), so the wait is still correct there.
                _pipeline_bridging = (
                    not self._zoom_virtual_mic_output()
                    and getattr(self, "_preroll_pipeline_q", None) is not None
                    and getattr(self, "_preroll_pipeline_thread", None) is not None
                    and getattr(self, "_preroll_pipeline_thread").is_alive()
                )
                if not _pipeline_bridging:
                    self._wait_audio_output_idle()

            if _ps_th is not None:
                _ps_th.join(timeout=180.0)
            _presynth_audio = None
            if _presynth_box:
                if isinstance(_presynth_box[0], Exception):
                    raise _presynth_box[0]
                _presynth_audio = _presynth_box[0]

            self._reassert_facetime_tts_routing()
            if cycle.is_instant:
                self._teardown_preroll_pipeline(wait=False)
                try:
                    self._audio_out.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._wait_output_ready_for_tts(timeout_s=10.0)
            t_tts0 = time.perf_counter()
            self._clear_tts_resume_state()
            self._tts_cancel_requested = False
            self._reset_spoken_aloud_buffer()
            _vad_interrupt = self._can_interrupt_tts()
            _barge_in = _vad_interrupt or self._barge_in_enabled()
            _tail = ""
            if self._config.wake_tts_continuation or getattr(
                self, "_last_extended_spoken", False
            ):
                _tail = (
                    getattr(getattr(self, "_responder", None), "_last_spoken_continuation", None)
                    or ""
                ).strip()
            _phrase_max = int(getattr(self._config, "tts_phrase_max_chars", 220) or 220)
            if cycle.is_instant:
                _instant_text = (cycle.response_text or "").strip()
                _phrases = [_instant_text] if _instant_text else []
            else:
                _phrases = phrases_for_natural_speech(
                    cycle.response_text or "",
                    max_chars=_phrase_max,
                )
                if _tail and not self._tts_cancel_requested:
                    _phrases.extend(
                        phrases_for_natural_speech(_tail, max_chars=_phrase_max)
                    )
            if not _phrases:
                _phrases = [cycle.response_text or ""]
            _zoom_full_reply = (
                self._zoom_virtual_mic_output() and not cycle.is_instant
            )
            _use_streaming = (
                self._config.eleven_tts_stream
                and hasattr(tts_engine, "synthesize_streaming")
                and not cycle.is_instant
                and not _zoom_full_reply
            )
            _use_frame_loop = (_barge_in or _zoom_full_reply) and not _use_streaming
            log.info(
                "TTS: %s %d chars via %s persona=%s (vad_interrupt=%s barge_in=%s frame_loop=%s stream=%s phrases=%d)",
                "play (presynth)" if _presynth_audio is not None else "synthesize+play",
                len(cycle.response_text) + len(_tail),
                type(tts_engine).__name__,
                cycle.persona.id if cycle.persona is not None else "(none)",
                _vad_interrupt,
                _barge_in,
                _use_frame_loop,
                _use_streaming,
                len(_phrases),
            )
            if _use_streaming and not _use_frame_loop:
                # Stream for low latency; watcher cuts playback on interrupt.
                self._tts_resume_state = {
                    "kind": "streaming",
                    "tts_engine": tts_engine,
                    "phrases": list(_phrases),
                    "presynth_audio": _presynth_audio if len(_phrases) == 1 else None,
                    "source": source,
                    "stream_prefetch": _stream_prefetch,
                }
                _watcher = None
                if _barge_in:
                    _watcher = self._start_tts_barge_in_watcher(allow_vad=_vad_interrupt)
                try:
                    self._play_spoken_phrases(
                        tts_engine,
                        _phrases,
                        presynth_audio=_presynth_audio if len(_phrases) == 1 else None,
                        tts_source=source,
                        tts_phase=_reply_phase,
                        stream_prefetch=_stream_prefetch,
                        opener_stop_evt=_opener_for_handoff[0],
                        opener_thread=_opener_for_handoff[1],
                    )
                finally:
                    det, reason = self._stop_tts_barge_in_watcher(_watcher)
                    if det:
                        barge_stop_reason = reason or "directed"
                        if reason == "vad":
                            vad_interrupted = True
                        else:
                            transcript_barge_in = True
                if not barge_stop_reason and self._tts_playback_aborted():
                    barge_stop_reason = "directed"
                    transcript_barge_in = True
                if barge_stop_reason:
                    try:
                        self._audio_out.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    # Pipeline may still be open if barge-in fires during response push.
                    self._teardown_preroll_pipeline(wait=False)
                    log.info(
                        "Interrupted during streaming TTS (%s)",
                        barge_stop_reason,
                    )
                else:
                    playback_completed = True
                    self._clear_tts_resume_state()
            elif _use_frame_loop:
                if _defer_opener_handoff:
                    self._release_wake_opener_bridge(_opener_stop_evt, _opener_th)
                    _defer_opener_handoff = False
                audio = _presynth_audio
                _speak_text = (cycle.response_text or "").strip()
                if _tail and _speak_text:
                    _speak_text = f"{_speak_text} {_tail}".strip()
                elif _tail:
                    _speak_text = _tail
                if audio is None:
                    audio = tts_engine.synthesize(_speak_text or cycle.response_text)
                _seg = "reply_after_opener" if self._wake_opener_played else "full"
                audio_f32, play_sr = self._tts_audio_for_playback(
                    audio.audio_f32,
                    audio.sample_rate_hz,
                    segment=_seg,
                )
                if self._wake_opener_played:
                    self._wake_opener_played = False
                    self._log_time_to_first_audio(source, phase="reply")
                duration_s = audio_f32.shape[0] / float(play_sr)
                _out_label = getattr(self._audio_out, "_device", None) or type(self._audio_out).__name__
                if audio_f32.size == 0:
                    log.error(
                        "TTS produced no audio for %d-char reply (engine=%s); "
                        "check ElevenLabs API key, voice id, model, and ft-logs",
                        len(cycle.response_text or ""),
                        type(tts_engine).__name__,
                    )
                    self.emit_thought(
                        "error",
                        "TTS returned no audio — check worker logs and MECHUS_ELEVEN_* settings.",
                    )
                else:
                    log.info("TTS: playing %.2fs of audio via %r", duration_s, _out_label)
                play_start_mono = time.monotonic()
                end_time = play_start_mono + duration_s
                self._tts_resume_state = {
                    "kind": "frame",
                    "audio_f32": audio_f32,
                    "play_sr": play_sr,
                    "play_start_mono": play_start_mono,
                }
                self._audio_out.play(
                    audio_f32,
                    sample_rate_hz=play_sr,
                    gain=self._config.tts_output_gain,
                )
                grace_s = max(0.0, self._config.tts_interrupt_grace_ms / 1000.0)
                grace_until = time.monotonic() + grace_s
                barge_after_s = (
                    0.15
                    if self._transcript_wake_only and not self._can_interrupt_tts()
                    else grace_s
                )
                barge_until = time.monotonic() + barge_after_s
                speech_frames = 0
                self._tts_frame_loop_active = True
                try:
                    while time.monotonic() < end_time and not self._stop.is_set():
                        frame = self._read_frame(timeout_s=0.2)
                        if frame is None:
                            continue
                        self._handle_live_frame(frame)
                        now = time.monotonic()
                        if (
                            self._config.transcript_barge_in_enabled
                            and self._transcript_wake_only
                            and now >= barge_until
                            and self._pending_transcript_wake_is_barge_in_eligible()
                        ):
                            transcript_barge_in = True
                            break
                        if not self._can_interrupt_tts() or now < grace_until:
                            continue
                        if self._interrupt_vad.is_speech(frame.pcm16, self._config.sample_rate_hz):
                            speech_frames += 1
                            if speech_frames >= self._interrupt_min_frames:
                                interrupted = True
                                break
                        else:
                            speech_frames = 0
                finally:
                    self._tts_frame_loop_active = False
                if interrupted or transcript_barge_in:
                    self._audio_out.stop()
                    if transcript_barge_in:
                        barge_stop_reason = "directed"
                        log.info("Interrupted during TTS (transcript barge-in)")
                    else:
                        barge_stop_reason = "vad"
                        vad_interrupted = True
                        log.info("Interrupted during TTS (VAD)")
                else:
                    wait_fn = getattr(self._audio_out, "wait", None)
                    if callable(wait_fn):
                        wait_fn()
                    log.info("TTS: playback done")
                    playback_completed = True
                    self._clear_tts_resume_state()
            else:
                self._play_spoken_phrases(
                    tts_engine,
                    _phrases,
                    presynth_audio=_presynth_audio if len(_phrases) == 1 else None,
                    tts_source=source,
                    tts_phase=_reply_phase,
                    opener_stop_evt=_opener_for_handoff[0],
                    opener_thread=_opener_for_handoff[1],
                )
                if self._tts_playback_aborted():
                    barge_stop_reason = barge_stop_reason or "directed"
                else:
                    log.info("TTS: playback done")
                    playback_completed = True
        finally:
            if _defer_opener_handoff:
                self._release_wake_opener_bridge(_opener_stop_evt, _opener_th)
            self._mark_tts_not_playing()
            self._wake_opener_thread = None
            # Only re-anchor timers when the reply actually finished — not when we
            # stopped mid-sentence for barge-in (we may resume or run a new command).
            if playback_completed:
                if getattr(self, "_dialog_last_mechus", None):
                    self._last_dialog_wake_monotonic_s = time.monotonic()
                if self._transcript_wake_only:
                    self._last_wake_fire_monotonic_s = time.monotonic()
                    _mute = float(self._config.post_tts_transcript_wake_mute_s)
                    if cycle.is_instant:
                        _mute = min(_mute, 0.75)
                    if _mute > 0:
                        self._transcript_wake_echo_mute_until = time.monotonic() + _mute
            self._join_thinking_preroll_thread(timeout_s=0.5)
            self._discard_main_response_presynth()

        if t_tts0 is not None:
            t_tts = (time.perf_counter() - t_tts0) * 1000
            if self._last_latency is not None:
                self._last_latency["tts_ms"] = round(t_tts, 1)
                self._last_latency["total_ms"] = round(
                    (self._last_latency.get("stt_ms") or 0)
                    + (self._last_latency.get("context_ms") or 0)
                    + (self._last_latency.get("memory_ms") or 0)
                    + (self._last_latency.get("llm_ms") or 0)
                    + t_tts,
                    1,
                )
        if barge_stop_reason or transcript_barge_in or interrupted:
            wait_s = 0.85 if (barge_stop_reason == "vad" or vad_interrupted) else 1.25
            cmd = self._collect_barge_in_command_after_stop(max_wait_s=wait_s)
            if cmd is not None:
                trig, seed, pending_source = cmd
                self._clear_tts_resume_state()
                log.info("Barge-in command accepted: %s", trig[:160])
                return self._run_wake_pipeline_after_detect(
                    trigger_text=trig,
                    seed_pcm16=seed,
                    context_pcm16=b"",
                    source=pending_source,
                )
            if self._resume_interrupted_tts():
                log.info("False barge-in; resumed Mechus reply from interruption point")
                return True
            log.info("Barge-in stop with no valid command; not resuming")

        self._transcript_wake_preamble_ctx = ""
        log.info("Returned dormant")
        return True

    def _enqueue_wake_pipeline_after_detect(
        self,
        *,
        trigger_text: str,
        seed_pcm16: bytes,
        context_pcm16: bytes,
        source: str,
        arm_cooldown: bool = False,
    ) -> None:
        """Run wake pipeline on a worker thread so the listen loop keeps hearing STT."""
        with self._wake_pipeline_lock:
            th = self._wake_pipeline_thread
            if th is not None and th.is_alive():
                log.debug("Wake pipeline already running; dropping duplicate (%s)", source)
                return

            def _worker() -> None:
                try:
                    ok = self._run_wake_pipeline_after_detect(
                        trigger_text=trigger_text,
                        seed_pcm16=seed_pcm16,
                        context_pcm16=context_pcm16,
                        source=source,
                    )
                    if not ok:
                        self._stop.set()
                except Exception:  # noqa: BLE001
                    if not self._stop.is_set():
                        log.exception("Wake pipeline crashed (source=%s); staying alive", source)
                finally:
                    with self._wake_pipeline_lock:
                        if self._wake_pipeline_thread is threading.current_thread():
                            self._wake_pipeline_thread = None

            t = threading.Thread(
                target=_worker,
                daemon=True,
                name=f"mechus-wake-pipeline-{source}",
            )
            self._wake_pipeline_thread = t
            t.start()

    def _prewarm_external_apis(self) -> None:
        """Open TLS connections + send a tiny LLM ping so the first wake cycle is fast.

        Runs on a background thread; failures are logged at debug and do not block
        startup. Saves ~100-400 ms on the first user interaction (which is the one
        users notice most).
        """

        def _worker() -> None:
            try:
                from mechus.http_pool import prewarm_hosts

                prewarm_hosts(
                    [
                        __import__("mechus.openai_compat", fromlist=["origin"]).origin(),
                        "https://api.elevenlabs.io/",
                    ]
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("HTTP prewarm failed (non-fatal): %s", exc)

        threading.Thread(
            target=_worker, daemon=True, name="mechus-prewarm-apis"
        ).start()

    def run_forever(self) -> None:
        self._install_signal_handlers()
        self._audio_in.start()
        self._prewarm_external_apis()
        _phrase_list = self._all_wake_phrases or tuple(self._config.wake_phrases)
        log.info("Mechus dormant. Wake phrase(s): %s", ", ".join(_phrase_list))

        try:
            while not self._stop.is_set():
                if self._tts_frame_loop_active:
                    if self._pending_transcript_wake is not None:
                        if not self._wake_cooldown_ready():
                            if not (
                                self._tts_playing
                                and self._pending_transcript_wake_is_barge_in_eligible()
                            ):
                                time.sleep(0.04)
                                continue
                        if (
                            self._tts_playing
                            and self._pending_transcript_wake_is_barge_in_eligible()
                        ):
                            self._hard_interrupt_tts()
                    time.sleep(0.04)
                    continue
                frame = self._read_frame(timeout_s=0.5)
                if frame is None:
                    continue
                first_audio = self._last_audio_frame_at == 0.0
                self._last_audio_frame_at = time.time()
                if first_audio:
                    log.info("Mechus agent: first audio frame received; listening for wake word")
                if self._transcript_chunker is not None:
                    self._handle_live_frame(frame)

                if self._pending_transcript_wake is not None:
                    if not self._wake_cooldown_ready():
                        if not (
                            self._tts_playing
                            and self._pending_transcript_wake_is_barge_in_eligible()
                        ):
                            continue
                    if (
                        self._tts_playing
                        and self._pending_transcript_wake_is_barge_in_eligible()
                    ):
                        self._hard_interrupt_tts()
                        continue
                    with self._wake_pipeline_lock:
                        if (
                            self._wake_pipeline_thread is not None
                            and self._wake_pipeline_thread.is_alive()
                        ):
                            continue
                    if self._tts_playing:
                        continue
                    trig, seed, pending_source = self._pending_transcript_wake
                    self._pending_transcript_wake = None
                    context_pcm16 = b""
                    if self._rolling is not None and self._config.context_seconds > 0:
                        context_pcm16 = self._rolling.snapshot_last(self._config.context_seconds)
                    self._enqueue_wake_pipeline_after_detect(
                        trigger_text=trig,
                        seed_pcm16=seed,
                        context_pcm16=context_pcm16,
                        source=pending_source,
                    )
                    continue

                if self._transcript_wake_only:
                    continue
                wake_pcm16 = pcm16_resample_linear(frame.pcm16, src_sr=self._config.sample_rate_hz, dst_sr=self._wake_sr)
                if self._wake.process(wake_pcm16, frame.timestamp_s):
                    if not self._wake_cooldown_ready():
                        log.debug("Audio wake ignored (cooldown)")
                        continue
                    trigger_text = (getattr(self._wake, "last_trigger_text", "") or "").strip()
                    seed_pcm16 = frame.pcm16
                    if self._rolling is not None and self._config.wake_pre_roll_seconds > 0:
                        seed_pcm16 = self._rolling.snapshot_last(self._config.wake_pre_roll_seconds)
                    context_pcm16 = b""
                    if self._rolling is not None and self._config.context_seconds > 0:
                        context_pcm16 = self._rolling.snapshot_last(self._config.context_seconds)
                    self._enqueue_wake_pipeline_after_detect(
                        trigger_text=trigger_text,
                        seed_pcm16=seed_pcm16,
                        context_pcm16=context_pcm16,
                        source="vosk",
                    )

        finally:
            with self._wake_pipeline_lock:
                _pipe_th = self._wake_pipeline_thread
            if _pipe_th is not None and _pipe_th.is_alive():
                _pipe_th.join(timeout=30.0)
            try:
                self._flush_transcript_chunkers()
            except Exception:
                log.debug("Transcript chunker flush on shutdown failed", exc_info=True)
            try:
                self._note_store.sync()
            except Exception:
                log.debug("NoteStore sync on shutdown failed", exc_info=True)
            self._audio_in.stop()
