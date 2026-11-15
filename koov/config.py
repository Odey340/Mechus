from __future__ import annotations

from dataclasses import dataclass
import os


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    value = _env_str(name)
    if value is None:
        return list(default) if default else []
    parts = [part.strip().lower() for part in value.split(",")]
    return [part for part in parts if part]


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if value is None:
        return default
    return int(value)


def _env_int_positive(name: str, default: int) -> int:
    """Like _env_int but treat 0 or negative as unset (use default)."""
    v = _env_int(name, default)
    return default if v <= 0 else v


def _env_float(name: str, default: float) -> float:
    value = _env_str(name)
    if value is None:
        return default
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = _env_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _env_csv_phrases_lower(name: str) -> tuple[str, ...]:
    """Comma-separated tokens or multi-word phrases; trimmed and lowercased."""
    value = _env_str(name)
    if not value:
        return ()
    out: list[str] = []
    for part in value.split(","):
        t = part.strip().lower()
        if t:
            out.append(t)
    return tuple(out)


def _normalize_llm_backend(value: str | None) -> str:
    """Return 'openai' or 'rules'. Legacy 'ollama' is mapped to 'rules'."""
    if not value:
        return "rules"
    v = value.strip().lower()
    if v == "openai":
        return "openai"
    return "rules"  # rules, ollama (legacy), or anything else


@dataclass(frozen=True)
class KoovConfig:
    input_device: str | int | None
    output_device: str | int | None
    sample_rate_hz: int
    frame_ms: int
    wake_phrase: str
    wake_phrases: tuple[str, ...]  # All wake words (e.g. computer, koov) for grammar/validation
    # Levenshtein max edit distance per word for meeting-transcript wake only (Vosk path stays at 2).
    wake_transcript_max_dist: int

    wake_backend: str
    openwakeword_model_path: str | None
    wake_threshold: float
    wake_cooldown_s: float
    # Longer cooldown on Zoom/FaceTime transcript wake (reduces re-wake on Koov's own TTS echo).
    wake_cooldown_transcript_stt_s: float
    # After a successful wake+reply, accept vague follow-ups without repeating the wake phrase.
    followup_window_s: float
    # Merge consecutive transcript STT fragments before testing wake (split across VAD chunks).
    wake_transcript_merge_s: float
    # Max words before wake phrase in a merged transcript line (preamble in same utterance).
    wake_transcript_max_leading_words: int
    # Keep recent human transcript lines for cross-chunk preamble before wake+command.
    wake_transcript_preamble_s: float
    # After Koov TTS ends, ignore transcript wakes briefly (meeting audio often includes bot echo).
    post_tts_transcript_wake_mute_s: float

    vosk_model_path: str | None
    context_seconds: float

    stt_backend: str
    whisper_model: str
    whisper_compute_type: str
    whisper_language: str | None
    # When True (default), STT auto-detects language and replies follow the speaker.
    multilingual: bool
    # Cloud STT API options
    openai_api_key: str | None
    openai_whisper_model: str | None
    openai_chat_model: str | None  # Legacy alias → openai_response_model (live spoken replies)
    openai_response_model: str | None  # Live Q&A / spoken answers (KOOV_OPENAI_RESPONSE_MODEL)
    openai_thinking_model: str | None  # Planning, private_thoughts, chapters, end-of-meeting summary
    deepgram_api_key: str | None
    deepgram_model: str | None
    assemblyai_api_key: str | None

    request_mode: str
    wake_pre_roll_seconds: float

    max_request_seconds: float
    min_request_seconds: float
    end_silence_seconds: float
    pre_roll_seconds: float
    request_wait_seconds: float

    memory_max_chars: int
    memory_recent_chars: int
    retrieval_max_chars: int
    response_cache_ttl_seconds: float
    response_cache_max_size: int

    llm_backend: str  # "openai" or "rules"
    use_llm_always: bool  # When True, always call LLM for answers (ignore structured fast path)
    max_response_words: int
    # OpenAI live response (Chat Completions): tokens, timeout, prompt budget, optional JSON/streaming
    response_openai_max_tokens: int
    response_openai_timeout_s: float
    response_prompt_max_chars: int
    retrieval_general_max_chars: int
    response_json_mode: bool
    response_stream_enabled: bool
    # Post-meeting summary: state | fast | full
    summary_mode: str
    summary_max_transcript_chars: int
    summary_fast_max_tokens: int
    # End-of-meeting LLM (OpenAI) timeout; increase for long meetings / slow networks.
    summary_timeout_seconds: int

    tts_backend: str
    piper_model_path: str | None
    eleven_api_key: str | None
    eleven_voice_id: str | None
    eleven_model_id: str | None
    eleven_base_url: str | None
    eleven_stt_model: str | None  # scribe_v2 / scribe_v2_realtime when STT backend is elevenlabs
    eleven_stt_realtime: bool  # WebSocket Scribe v2 Realtime (~150ms) vs batch HTTP Scribe
    eleven_audio_tags: bool  # Enable ElevenLabs v3 audio tag emission (requires eleven_v3 model)
    # When False (default): every persona uses only ``KOOV_ELEVEN_VOICE_ID`` (one engine, one warmup).
    # Set KOOV_PER_PERSONA_ELEVEN_VOICES=1 if persona JSON / env defines distinct ElevenLabs voices.
    per_persona_eleven_voices: bool
    # ElevenLabs ``output_format`` query param (e.g. pcm_32000 for Zoom virtual mic).
    eleven_output_format: str | None
    # Stream ElevenLabs PCM chunks to playback (time-to-first-audio) instead of full-buffer synth.
    eleven_tts_stream: bool
    # Live wake: cap spoken reply (LLM + TTS). See KOOV_WAKE_SPOKEN_MAX_WORDS / _MAX_CHARS.
    wake_spoken_max_words: int
    wake_spoken_max_chars: int
    # After the short head, optionally speak ``spoken_continuation`` from JSON mode.
    wake_tts_continuation: bool
    low_latency: bool
    llm_outlier_log_ms: float
    # Play a cached short filler immediately on wake (parallel with LLM) so Koov talks fast.
    wake_opener_enabled: bool
    wake_opener_max_audio_s: float
    # Hard cap on continuation fillers the opener plays while LLM + TTS prefetch finish.
    # Bridges the silence between the initial cue and the main reply.
    wake_opener_extend_cap_s: float
    wake_opener_fade_in_ms: float
    wake_opener_fade_out_ms: float
    # Softer attack on main reply right after wake opener (ms); 0 = use full tts_fade_in_ms.
    tts_fade_in_after_opener_ms: float
    # Bundled smooth-TTS defaults (fades, pcm_32000, preroll off). See KOOV_TTS_SMOOTH.
    tts_smooth: bool
    tts_output_gain: float
    # Reserved for future gating; parallel preroll starts only on cache-miss LLM (see agent).
    thinking_preroll_min_llm_ms: float
    # Seconds to cap cached thinking clip; 0 = play full cached clip (avoids mid-phrase chop + dead air).
    thinking_preroll_max_audio_s: float
    # If False: never play thinking filler (KOOV_THINKING_PREROLL=0 or off).
    thinking_preroll_enabled: bool
    # When False: skip thinking preroll on meeting-transcript wakes only (FaceTime/Zoom STT).
    thinking_preroll_transcript_stt: bool
    # Transcript-path preroll max length (seconds); fades smooth the tail. 0 = same cap as
    # thinking_preroll_max_audio_s (often full clip). Default keeps preroll short for mic/silence gate.
    thinking_preroll_transcript_stt_max_audio_s: float
    # Linear fades on thinking preroll playback (ms); reduces clicks at boundaries / after truncate.
    thinking_preroll_fade_in_ms: float
    thinking_preroll_fade_out_ms: float
    # Linear fades on main reply TTS (ms); removes click when Zoom virtual mic starts.
    tts_fade_in_ms: float
    tts_fade_out_ms: float
    # Multiplier on gaps between spoken TTS phrases (Zoom: breathe between sentences).
    tts_phrase_pause_scale: float
    tts_phrase_max_chars: int
    # Ignore barge-in for this many ms after TTS starts (Zoom/meeting audio often contains TTS echo).
    # Set to 0 to allow immediate interrupt (may cut off short replies).
    tts_interrupt_grace_ms: float
    min_silence_before_speaking_s: float
    # Shorter gate for instant routes (presence, greeting) so "can you hear me" feels snappy.
    min_silence_before_speaking_instant_s: float
    # Meeting-transcript wake: utterance is already finalized; long mic silence wait feels like lag after [RESPONSE].
    min_silence_before_speaking_transcript_stt_s: float
    max_wait_for_silence_s: float

    transcript_enabled: bool
    transcript_min_seconds: float
    transcript_chunk_seconds: float
    transcript_end_silence_seconds: float
    transcript_rewrite: bool
    transcript_model: str | None
    transcript_max_words: int
    transcript_tag_max: int
    transcript_tag_min_len: int
    # If True, treat meeting-transcript STT (same path as dashboard) as a second wake source
    # when it contains a wake word + command (Vosk wake can miss long utterances).
    transcript_wake_enabled: bool
    # Drop STT segments that are only non-speech labels (silence, typing, …); see transcript.is_noise_only_transcript.
    transcript_drop_noise: bool
    # Comma-separated extra tokens or phrases (lowercased) to treat as noise-only.
    transcript_noise_extra: tuple[str, ...]

    interrupt_on_speech: bool
    # When False (default): no VAD barge-in during Zoom TTS — meeting audio often includes Koov's own voice.
    interrupt_on_speech_zoom: bool
    interrupt_min_speech_ms: int
    # Meeting transcript only: stop TTS when a new wake+command is recognized (strict; off by default).
    transcript_barge_in_enabled: bool
    # When True, only a stop directive ("Koov, stop" / "hold on") may interrupt a reply in
    # progress — a new question mid-reply does NOT cut Koov off. This is echo-safe on mixed
    # audio (FaceTime): Koov never says "Koov, stop", so its own voice can't self-interrupt.
    transcript_barge_in_stop_only: bool
    # Brief spoken ack after stop/hold-on/wait interrupts (e.g. "Got it, I'll be here.").
    stop_ack_enabled: bool

    diarization_enabled: bool
    diarization_backend: str
    # When True, diarization runs in a background thread and does NOT block the
    # wake/TTS path. Speaker labels arrive asynchronously and are patched into
    # notes/store after the fact. Recommended for FaceTime (set KOOV_DIARIZATION_ASYNC=1).
    diarization_async: bool
    diarization_model: str | None
    diarization_hf_token: str | None
    diarization_api_key: str | None
    diarization_api_base_url: str
    diarization_exclusive: bool
    diarization_timeout_s: float
    diarization_poll_interval_s: float
    diarization_device: str | None
    diarization_min_speakers: int | None
    diarization_max_speakers: int | None
    diarization_min_segment_s: float
    diarization_merge_gap_s: float

    # Per-participant Zoom one-way streams. Disables mixed + pyannote when True.
    zoom_stream_attribution: bool

    actionable_only: bool
    actionable_phrases: list[str]

    # Retrieval: lexical | hybrid | auto (auto = lexical-first, embedding rerank only when weak / semantic)
    retrieval_mode: str
    retrieval_bm25_weight: float
    retrieval_tag_weight: float
    retrieval_embed_weight: float
    retrieval_embed_rerank_top_k: int
    # auto: if top1–top2 gap >= this, skip embedding rerank (lexical confident)
    retrieval_auto_lexical_gap_min: float
    # auto: if top combined score below this (normalized), run hybrid
    retrieval_auto_top_floor_hybrid: float
    # Per-meeting query embedding LRU cap (ContextIntegrityEngine)
    retrieval_query_embed_cache_max: int
    embedding_model: str
    # Response: optional global temperature override (None = use built-in intent curve)
    response_temperature: float | None
    # Lower variance for list-heavy meeting intents when response_temperature is unset
    response_temp_meeting: float
    # Max tokens for action/decision/timeline-style intents (>= response_openai_max_tokens)
    response_openai_max_tokens_list: int
    # Rolling chapter summaries (warm tier): append every N engine tagged chunks
    chapter_every_n_chunks: int
    chapter_llm_max_tokens: int

    # One switch: low latency + snappier transcript timing (does not enable barge-in by default).
    conversational_mode: bool

    # Multi-persona: directory holding per-persona JSON files (one bot per file).
    # When the directory is missing, a synthetic default persona is built from the
    # rest of this config so single-persona behavior is preserved.
    personas_dir: str
    # Allow-list of persona ids from CSV (KOOV_PERSONAS_ENABLED). Empty tuple = load every JSON persona.
    # When KOOV_PERSONAS_DEFAULT_ONLY=1 (default), this is set to ("default",) unless CSV is set.
    personas_enabled: tuple[str, ...]
    # When set (e.g. by multi-bot worker spawn), the agent loads only this persona id.
    bot_persona_id: str | None

    log_level: str

    @staticmethod
    def from_env() -> "KoovConfig":
        from koov.openai_compat import apply_provider_env

        apply_provider_env()
        input_device: str | int | None = _env_str("KOOV_INPUT_DEVICE")
        if input_device is not None and input_device.isdigit():
            input_device = int(input_device)

        output_device: str | int | None = _env_str("KOOV_OUTPUT_DEVICE")
        if output_device is not None and output_device.isdigit():
            output_device = int(output_device)

        # Comma-separated. Prefer a distinctive multi-word phrase plus the short name;
        # very short aliases (koo, coop) increase false wakes from normal English in fuzzy transcript matching.
        from koov.brand import DEFAULT_WAKE_PHRASE, WAKE_WORD

        wake_phrase_raw = (_env_str("KOOV_WAKE_PHRASE", DEFAULT_WAKE_PHRASE) or DEFAULT_WAKE_PHRASE).strip()
        wake_phrases = tuple(p.strip().lower() for p in wake_phrase_raw.split(",") if p.strip())
        wake_phrase = wake_phrases[0] if wake_phrases else WAKE_WORD
        _wtd = _env_int("KOOV_WAKE_TRANSCRIPT_MAX_DIST", 1)
        if _wtd < 1:
            _wtd = 1
        wake_transcript_max_dist = min(_wtd, 2)

        _rm = (_env_str("KOOV_RETRIEVAL_MODE", "lexical") or "lexical").strip().lower()
        if _rm not in ("lexical", "hybrid", "auto"):
            _rm = "lexical"

        # Conversational preset: human-like voice-agent feel (latency + barge-in + snappy gates).
        _conversational = _env_bool("KOOV_CONVERSATIONAL", False)
        # Single switch: minimize retrieval + LLM latency (no embedding rerank, tighter budgets, shorter HTTP timeout).
        _low_latency = _env_bool("KOOV_LOW_LATENCY", False) or _conversational
        _retrieval_embed_w = _env_float("KOOV_RETRIEVAL_EMBED_WEIGHT", 0.32)
        _retrieval_topk = _env_int_positive("KOOV_RETRIEVAL_EMBED_RERANK_TOP_K", 28)
        _retrieval_max_c = _env_int("KOOV_RETRIEVAL_MAX_CHARS", 4000)
        _retrieval_gen_max_c = _env_int("KOOV_RETRIEVAL_GENERAL_MAX_CHARS", 3200)
        _resp_openai_timeout = _env_float("KOOV_RESPONSE_OPENAI_TIMEOUT_S", 22.0)
        _resp_prompt_max_c = _env_int("KOOV_RESPONSE_PROMPT_MAX_CHARS", 12000)
        _wake_spoken_max_words = _env_int_positive("KOOV_WAKE_SPOKEN_MAX_WORDS", 20 if _low_latency else 55)
        _wake_spoken_max_chars = _env_int_positive("KOOV_WAKE_SPOKEN_MAX_CHARS", 40 if _low_latency else 280)
        _eleven_tts_stream = _env_bool("KOOV_ELEVEN_TTS_STREAM", _low_latency)
        if _low_latency:
            _rm = "lexical"
            _retrieval_embed_w = 0.0
            _retrieval_topk = min(_retrieval_topk, 16)
            _retrieval_max_c = min(_retrieval_max_c, 3500)
            _retrieval_gen_max_c = min(_retrieval_gen_max_c, 2400)
            _resp_openai_timeout = min(_resp_openai_timeout, 15.0)
            _resp_prompt_max_c = min(_resp_prompt_max_c, 8000)

        _eleven_stt_realtime = _env_bool("KOOV_ELEVEN_STT_REALTIME", _low_latency)

        _transcript_chunk_s = _env_float("KOOV_TRANSCRIPT_CHUNK_SECONDS", 12.0)
        _transcript_end_silence = _env_float("KOOV_TRANSCRIPT_END_SILENCE_SECONDS", 0.7)
        _min_silence_transcript = _env_float(
            "KOOV_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S",
            0.0 if _low_latency else 0.18,
        )
        _wake_opener_enabled = _env_bool("KOOV_WAKE_OPENER", _low_latency)
        _wake_opener_default_s = 1.0 if _low_latency else 0.45
        _wake_opener_max_audio_s = max(0.1, _env_float("KOOV_WAKE_OPENER_MAX_AUDIO_S", _wake_opener_default_s))
        _wake_opener_extend_cap_s = max(
            0.0,
            _env_float(
                "KOOV_WAKE_OPENER_EXTEND_CAP_S",
                18.0 if _low_latency else 3.5,
            ),
        )
        # extend_cap=0 disables gapless opener+extend bridge — only the short opener
        # plays (~0.55s) then dead air until LLM+TTS. Production used 0 expecting
        # parallel thinking preroll, but that is suppressed while the opener runs
        # and is capped at 0.45s for transcript wakes.
        if _wake_opener_enabled and _wake_opener_extend_cap_s <= 0.0:
            _wake_opener_extend_cap_s = 18.0 if _low_latency else 3.5
        _wake_opener_fi = _env_float("KOOV_WAKE_OPENER_FADE_IN_MS", 14.0 if _low_latency else 10.0)
        # No fade-out on the opener when low-latency bridge extend fills LLM wait (gapless handoff).
        _wake_opener_fo = _env_float(
            "KOOV_WAKE_OPENER_FADE_OUT_MS",
            0.0 if _low_latency else 28.0,
        )
        _tts_fade_after_opener = _env_float("KOOV_TTS_FADE_IN_AFTER_OPENER_MS", 10.0 if _low_latency else 0.0)
        _thinking_preroll_transcript_max = _env_float("KOOV_THINKING_PREROLL_TRANSCRIPT_ST_MAX_S", 0.5)
        _interrupt_grace_ms = _env_float("KOOV_TTS_INTERRUPT_GRACE_MS", 2800.0)
        _interrupt_zoom = _env_bool("KOOV_INTERRUPT_ON_SPEECH_ZOOM", False)
        # Directed barge-in (a new "Koov …" command or "Koov, stop" while Koov is
        # speaking) is now ON by default. It is echo-safe because it routes through
        # the transcript wake detector, which filters out Koov's own TTS. The
        # streaming-TTS watcher (see KoovAgent._start_tts_barge_in_watcher) lets this
        # interrupt mid-reply without falling back to the slower non-streaming path.
        _transcript_barge_in = _env_bool("KOOV_TRANSCRIPT_BARGE_IN", True)
        # Production Zoom/FaceTime: only "Koov, stop" may cut a reply — not ambient speech
        # or a new question mid-answer (echo-safe; avoids interrupting the meeting).
        _transcript_barge_in_stop_only = _env_bool(
            "KOOV_TRANSCRIPT_BARGE_IN_STOP_ONLY", _low_latency
        )
        _tts_fade_in_ms = _env_float("KOOV_TTS_FADE_IN_MS", 30.0)
        _tts_fade_out_ms = _env_float("KOOV_TTS_FADE_OUT_MS", 22.0)
        _wake_cooldown_transcript = _env_float("KOOV_WAKE_COOLDOWN_TRANSCRIPT_STT_S", 5.0)
        _followup_window_s = _env_float(
            "KOOV_FOLLOWUP_WINDOW_S",
            45.0 if _low_latency else 12.0,
        )
        _wake_transcript_merge_s = _env_float("KOOV_WAKE_TRANSCRIPT_MERGE_S", 1.5)
        _wake_transcript_max_leading = _env_int("KOOV_WAKE_TRANSCRIPT_MAX_LEADING_WORDS", 10)
        _wake_transcript_preamble_s = _env_float("KOOV_WAKE_TRANSCRIPT_PREAMBLE_S", 0.0)
        _post_tts_wake_mute_s = _env_float("KOOV_POST_TTS_TRANSCRIPT_WAKE_MUTE_S", 0.0)
        _tts_smooth = _env_bool("KOOV_TTS_SMOOTH", True)
        _eleven_output_format = _env_str("KOOV_ELEVEN_OUTPUT_FORMAT")
        _thinking_preroll_on = _env_bool("KOOV_THINKING_PREROLL", _low_latency)
        _thinking_preroll_max_global = _env_float("KOOV_THINKING_PREROLL_MAX_AUDIO_S", 0.45)
        if _tts_smooth:
            if _eleven_output_format is None:
                _eleven_output_format = "pcm_32000"
            if os.getenv("KOOV_THINKING_PREROLL") is None and not _low_latency:
                _thinking_preroll_on = False
            if os.getenv("KOOV_TTS_FADE_IN_MS") is None:
                _tts_fade_in_ms = max(_tts_fade_in_ms, 34.0)
            if os.getenv("KOOV_TTS_FADE_OUT_MS") is None:
                _tts_fade_out_ms = max(_tts_fade_out_ms, 26.0)
            if os.getenv("KOOV_THINKING_PREROLL_MAX_AUDIO_S") is None:
                _thinking_preroll_max_global = min(_thinking_preroll_max_global, 0.4)
        if _conversational:
            if os.getenv("KOOV_TRANSCRIPT_CHUNK_SECONDS") is None:
                _transcript_chunk_s = min(_transcript_chunk_s, 9.0)
            if os.getenv("KOOV_TRANSCRIPT_END_SILENCE_SECONDS") is None:
                _transcript_end_silence = min(_transcript_end_silence, 0.55)
            if os.getenv("KOOV_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S") is None:
                _min_silence_transcript = min(_min_silence_transcript, 0.12)
            if os.getenv("KOOV_THINKING_PREROLL_TRANSCRIPT_ST_MAX_S") is None:
                _thinking_preroll_transcript_max = min(_thinking_preroll_transcript_max, 0.35)
            if os.getenv("KOOV_TTS_INTERRUPT_GRACE_MS") is None:
                _interrupt_grace_ms = min(_interrupt_grace_ms, 1200.0)
            if os.getenv("KOOV_TTS_FADE_IN_MS") is None:
                _tts_fade_in_ms = max(_tts_fade_in_ms, 28.0)
            if os.getenv("KOOV_WAKE_COOLDOWN_TRANSCRIPT_STT_S") is None:
                _wake_cooldown_transcript = max(_wake_cooldown_transcript, 6.0)

        _rtemp = _env_str("KOOV_RESPONSE_TEMPERATURE")
        response_temperature: float | None = None
        if _rtemp is not None and _rtemp.strip() != "":
            try:
                response_temperature = float(_rtemp.strip())
            except ValueError:
                response_temperature = None

        _personas_csv = _env_csv_phrases_lower("KOOV_PERSONAS_ENABLED")
        if _personas_csv:
            _personas_enabled = _personas_csv
        elif _env_bool("KOOV_PERSONAS_DEFAULT_ONLY", True):
            _personas_enabled = ("default",)
        else:
            # Do not auto-load every JSON persona without an explicit allow-list (prevents stray
            # wake phrases / voice IDs). Set KOOV_PERSONAS_ENABLED=penny,doc or pass enabled_personas at join.
            _personas_enabled = ("default",)

        _multilingual = _env_bool("KOOV_MULTILINGUAL", True)
        _wh_lang = _env_str("KOOV_WHISPER_LANGUAGE")
        if _multilingual:
            if _wh_lang and _wh_lang.lower() in ("auto", "detect", "multilingual"):
                _wh_lang = None
        elif not _wh_lang:
            _wh_lang = "en"

        return KoovConfig(
            input_device=input_device,
            output_device=output_device,
            sample_rate_hz=_env_int("KOOV_SAMPLE_RATE_HZ", 48000),
            frame_ms=_env_int("KOOV_FRAME_MS", 20),
            wake_phrase=wake_phrase,
            wake_phrases=wake_phrases,
            wake_transcript_max_dist=wake_transcript_max_dist,
            wake_backend=(_env_str("KOOV_WAKE_BACKEND", "vosk") or "vosk").lower(),
            openwakeword_model_path=_env_str("KOOV_OPENWAKEWORD_MODEL_PATH"),
            wake_threshold=_env_float("KOOV_WAKE_THRESHOLD", 0.6),
            wake_cooldown_s=_env_float("KOOV_WAKE_COOLDOWN_S", 3.0),
            wake_cooldown_transcript_stt_s=max(0.0, _wake_cooldown_transcript),
            followup_window_s=max(0.0, _followup_window_s),
            wake_transcript_merge_s=max(0.5, _wake_transcript_merge_s),
            wake_transcript_max_leading_words=max(0, _wake_transcript_max_leading),
            wake_transcript_preamble_s=max(
                0.0,
                _wake_transcript_preamble_s
                if _wake_transcript_preamble_s > 0
                else _followup_window_s,
            ),
            post_tts_transcript_wake_mute_s=max(0.0, _post_tts_wake_mute_s),
            vosk_model_path=_env_str("KOOV_VOSK_MODEL_PATH") or "models/vosk-model-small-en-us-0.15",
            context_seconds=_env_float("KOOV_CONTEXT_SECONDS", 0.0),
            stt_backend=(_env_str("KOOV_STT_BACKEND", "faster-whisper") or "faster-whisper").lower(),
            # Default model is the multilingual `base` (NOT `base.en`) so Koov can
            # transcribe non-English speech and stronger accents. The .en variants
            # are smaller/faster but English-only and miss accents/multilingual.
            # Set KOOV_WHISPER_MODEL=base.en for English-only / faster CPU.
            whisper_model=_env_str("KOOV_WHISPER_MODEL", "base") or "base",
            whisper_compute_type=_env_str("KOOV_WHISPER_COMPUTE_TYPE", "int8") or "int8",
            # Empty / unset language => auto-detect per utterance (faster-whisper / Scribe).
            whisper_language=_wh_lang,
            multilingual=_multilingual,
            openai_api_key=_env_str("KOOV_OPENAI_API_KEY"),
            openai_whisper_model=_env_str("KOOV_OPENAI_WHISPER_MODEL", "whisper-1"),
            openai_chat_model=(
                _response_model := (
                    _env_str("KOOV_OPENAI_RESPONSE_MODEL")
                    or _env_str("KOOV_OPENAI_CHAT_MODEL", "gpt-4o-mini")
                    or "gpt-4o-mini"
                )
            ),
            openai_response_model=_response_model,
            openai_thinking_model=(
                _env_str("KOOV_OPENAI_THINKING_MODEL")
                or _env_str("KOOV_OPENAI_CHAT_MODEL", "gpt-4o-mini")
                or "gpt-4o-mini"
            ),
            deepgram_api_key=_env_str("KOOV_DEEPGRAM_API_KEY"),
            deepgram_model=_env_str("KOOV_DEEPGRAM_MODEL", "nova-2"),
            assemblyai_api_key=_env_str("KOOV_ASSEMBLYAI_API_KEY"),
            request_mode=(_env_str("KOOV_REQUEST_MODE", "one_shot") or "one_shot").lower(),
            wake_pre_roll_seconds=_env_float("KOOV_WAKE_PRE_ROLL_SECONDS", 0.8),
            max_request_seconds=_env_float("KOOV_MAX_REQUEST_SECONDS", 10.0),
            min_request_seconds=_env_float("KOOV_MIN_REQUEST_SECONDS", 0.6),
            end_silence_seconds=_env_float("KOOV_END_SILENCE_SECONDS", 0.8),
            pre_roll_seconds=_env_float("KOOV_PRE_ROLL_SECONDS", 0.4),
            request_wait_seconds=_env_float("KOOV_REQUEST_WAIT_SECONDS", 6.0),
            memory_max_chars=_env_int("KOOV_MEMORY_MAX_CHARS", 10000),
            memory_recent_chars=_env_int("KOOV_MEMORY_RECENT_CHARS", 2500),
            retrieval_max_chars=_retrieval_max_c,
            response_cache_ttl_seconds=_env_float("KOOV_RESPONSE_CACHE_TTL_SECONDS", 300.0),
            response_cache_max_size=_env_int("KOOV_RESPONSE_CACHE_MAX_SIZE", 100),
            llm_backend=_normalize_llm_backend(_env_str("KOOV_LLM_BACKEND") or ("openai" if _env_str("KOOV_OPENAI_API_KEY") else "rules")),
            use_llm_always=_env_bool("KOOV_LLM_ALWAYS", False),
            max_response_words=_env_int_positive("KOOV_MAX_RESPONSE_WORDS", 40),
            response_openai_max_tokens=_env_int("KOOV_RESPONSE_OPENAI_MAX_TOKENS", 150),
            response_openai_timeout_s=_resp_openai_timeout,
            response_prompt_max_chars=_resp_prompt_max_c,
            retrieval_general_max_chars=_retrieval_gen_max_c,
            # Default off: plain completions are faster and use fewer completion tokens than
            # JSON plan-then-speak. Set KOOV_RESPONSE_JSON_MODE=1 with KOOV_ELEVEN_AUDIO_TAGS for private_thoughts.
            response_json_mode=_env_bool("KOOV_RESPONSE_JSON_MODE", False),
            response_stream_enabled=_env_bool("KOOV_RESPONSE_STREAM", False),
            summary_mode=(_env_str("KOOV_SUMMARY_MODE", "full") or "full").strip().lower(),
            summary_max_transcript_chars=_env_int("KOOV_SUMMARY_MAX_TRANSCRIPT_CHARS", 28000),
            summary_fast_max_tokens=_env_int("KOOV_SUMMARY_FAST_MAX_TOKENS", 1400),
            summary_timeout_seconds=max(15, _env_int("KOOV_SUMMARY_TIMEOUT_S", 120)),
            tts_backend=(_env_str("KOOV_TTS_BACKEND", "piper") or "piper").lower(),
            piper_model_path=_env_str("KOOV_PIPER_MODEL_PATH"),
            eleven_api_key=_env_str("KOOV_ELEVEN_API_KEY"),
            eleven_voice_id=_env_str("KOOV_ELEVEN_VOICE_ID"),
            eleven_model_id=(
                _mid := (
                    _env_str(
                        "KOOV_ELEVEN_MODEL_ID",
                        "eleven_flash_v2_5" if _low_latency else "eleven_v3",
                    )
                    or ("eleven_flash_v2_5" if _low_latency else "eleven_v3")
                )
            ),
            eleven_base_url=_env_str("KOOV_ELEVEN_BASE_URL", "https://api.elevenlabs.io"),
            eleven_stt_realtime=_eleven_stt_realtime,
            eleven_stt_model=_env_str(
                "KOOV_ELEVEN_STT_MODEL",
                "scribe_v2_realtime" if _eleven_stt_realtime else "scribe_v2",
            ),
            # v3 audio tags are ignored by Flash/turbo; auto-off unless model id contains "v3".
            eleven_audio_tags=(
                _env_bool("KOOV_ELEVEN_AUDIO_TAGS", True)
                and "v3" in _mid.lower()
            ),
            per_persona_eleven_voices=_env_bool("KOOV_PER_PERSONA_ELEVEN_VOICES", False),
            eleven_output_format=_eleven_output_format,
            eleven_tts_stream=_eleven_tts_stream,
            wake_spoken_max_words=_wake_spoken_max_words,
            wake_spoken_max_chars=_wake_spoken_max_chars,
            wake_tts_continuation=_env_bool("KOOV_WAKE_TTS_CONTINUATION", False),
            low_latency=_low_latency,
            llm_outlier_log_ms=max(500.0, _env_float("KOOV_LLM_OUTLIER_LOG_MS", 5000.0)),
            wake_opener_enabled=_wake_opener_enabled,
            wake_opener_max_audio_s=_wake_opener_max_audio_s,
            wake_opener_extend_cap_s=_wake_opener_extend_cap_s,
            wake_opener_fade_in_ms=max(0.0, _wake_opener_fi),
            wake_opener_fade_out_ms=max(0.0, _wake_opener_fo),
            tts_fade_in_after_opener_ms=max(0.0, _tts_fade_after_opener),
            tts_smooth=_tts_smooth,
            tts_output_gain=_env_float("KOOV_TTS_OUTPUT_GAIN", 0.9),
            thinking_preroll_min_llm_ms=_env_float("KOOV_THINKING_PREROLL_MIN_LLM_MS", 1200.0),
            # Short cap bounds join-wait when LLM returns before the clip finishes.
            thinking_preroll_max_audio_s=max(0.0, _thinking_preroll_max_global),
            # Off by default: earcon → main handoff often clicks on Zoom; enable with KOOV_THINKING_PREROLL=1.
            thinking_preroll_enabled=_thinking_preroll_on,
            thinking_preroll_transcript_stt=_env_bool(
                "KOOV_THINKING_PREROLL_TRANSCRIPT_STT",
                _low_latency
                and not (_wake_opener_enabled and _wake_opener_extend_cap_s > 0.0),
            ),
            tts_fade_in_ms=max(0.0, _tts_fade_in_ms),
            tts_fade_out_ms=max(0.0, _tts_fade_out_ms),
            tts_phrase_pause_scale=max(0.5, _env_float("KOOV_TTS_PHRASE_PAUSE_SCALE", 1.0)),
            tts_phrase_max_chars=max(40, _env_int("KOOV_TTS_PHRASE_MAX_CHARS", 220)),
            thinking_preroll_transcript_stt_max_audio_s=max(0.0, _thinking_preroll_transcript_max),
            thinking_preroll_fade_in_ms=max(0.0, _env_float("KOOV_THINKING_PREROLL_FADE_IN_MS", 10.0)),
            thinking_preroll_fade_out_ms=max(0.0, _env_float("KOOV_THINKING_PREROLL_FADE_OUT_MS", 22.0)),
            tts_interrupt_grace_ms=_interrupt_grace_ms,
            min_silence_before_speaking_s=_env_float("KOOV_MIN_SILENCE_BEFORE_SPEAKING_S", 0.6),
            min_silence_before_speaking_instant_s=max(
                0.0,
                _env_float(
                    "KOOV_MIN_SILENCE_BEFORE_SPEAKING_INSTANT_S",
                    _env_float("KOOV_MIN_SILENCE_INSTANT_S", 0.2),
                ),
            ),
            min_silence_before_speaking_transcript_stt_s=max(0.0, _min_silence_transcript),
            max_wait_for_silence_s=_env_float("KOOV_MAX_WAIT_FOR_SILENCE_S", 60.0),
            transcript_enabled=_env_bool("KOOV_TRANSCRIPT_ENABLED", True),
            transcript_min_seconds=_env_float("KOOV_TRANSCRIPT_MIN_SECONDS", 0.8),
            transcript_chunk_seconds=_transcript_chunk_s,
            transcript_end_silence_seconds=_transcript_end_silence,
            transcript_rewrite=_env_bool("KOOV_TRANSCRIPT_REWRITE", False),
            transcript_model=_env_str("KOOV_TRANSCRIPT_MODEL"),
            transcript_max_words=_env_int("KOOV_TRANSCRIPT_MAX_WORDS", 120),
            transcript_tag_max=_env_int("KOOV_TRANSCRIPT_TAG_MAX", 5),
            transcript_tag_min_len=_env_int("KOOV_TRANSCRIPT_TAG_MIN_LEN", 3),
            transcript_wake_enabled=_env_bool("KOOV_TRANSCRIPT_WAKE", True),
            transcript_drop_noise=_env_bool("KOOV_TRANSCRIPT_DROP_NOISE", True),
            transcript_noise_extra=_env_csv_phrases_lower("KOOV_TRANSCRIPT_NOISE_EXTRA"),
            interrupt_on_speech=_env_bool("KOOV_INTERRUPT_ON_SPEECH", True),
            interrupt_on_speech_zoom=_interrupt_zoom,
            interrupt_min_speech_ms=_env_int("KOOV_INTERRUPT_MIN_SPEECH_MS", 240),
            transcript_barge_in_enabled=_transcript_barge_in,
            transcript_barge_in_stop_only=_transcript_barge_in_stop_only,
            stop_ack_enabled=_env_bool("KOOV_STOP_ACK", True),
            diarization_enabled=_env_bool("KOOV_DIARIZATION_ENABLED", False),
            diarization_backend=(_env_str("KOOV_DIARIZATION_BACKEND", "pyannote-local") or "pyannote-local").lower(),
            diarization_async=_env_bool("KOOV_DIARIZATION_ASYNC", False),
            diarization_model=_env_str("KOOV_DIARIZATION_MODEL"),
            diarization_hf_token=_env_str("KOOV_DIARIZATION_HF_TOKEN"),
            diarization_api_key=_env_str("KOOV_DIARIZATION_API_KEY"),
            diarization_api_base_url=_env_str("KOOV_DIARIZATION_API_BASE_URL", "https://api.pyannote.ai") or "https://api.pyannote.ai",
            diarization_exclusive=_env_bool("KOOV_DIARIZATION_EXCLUSIVE", True),
            diarization_timeout_s=_env_float("KOOV_DIARIZATION_TIMEOUT_S", 60.0),
            diarization_poll_interval_s=_env_float("KOOV_DIARIZATION_POLL_INTERVAL_S", 0.8),
            diarization_device=_env_str("KOOV_DIARIZATION_DEVICE"),
            diarization_min_speakers=(
                _env_int("KOOV_DIARIZATION_MIN_SPEAKERS", 0) or None
            ),
            diarization_max_speakers=(
                _env_int("KOOV_DIARIZATION_MAX_SPEAKERS", 0) or None
            ),
            diarization_min_segment_s=_env_float("KOOV_DIARIZATION_MIN_SEGMENT_S", 0.6),
            diarization_merge_gap_s=_env_float("KOOV_DIARIZATION_MERGE_GAP_S", 0.2),
            zoom_stream_attribution=_env_bool("KOOV_ZOOM_STREAM_ATTRIBUTION", False),
            actionable_only=_env_bool("KOOV_ACTIONABLE_ONLY", True),
            actionable_phrases=_env_list("KOOV_ACTIONABLE_PHRASES"),
            retrieval_mode=_rm,
            retrieval_bm25_weight=_env_float("KOOV_RETRIEVAL_BM25_WEIGHT", 0.45),
            retrieval_tag_weight=_env_float("KOOV_RETRIEVAL_TAG_WEIGHT", 0.55),
            retrieval_embed_weight=_retrieval_embed_w,
            retrieval_embed_rerank_top_k=_retrieval_topk,
            retrieval_auto_lexical_gap_min=_env_float("KOOV_RETRIEVAL_AUTO_LEXICAL_GAP_MIN", 0.12),
            retrieval_auto_top_floor_hybrid=_env_float("KOOV_RETRIEVAL_AUTO_TOP_FLOOR_HYBRID", 0.18),
            retrieval_query_embed_cache_max=max(8, _env_int("KOOV_RETRIEVAL_QUERY_EMBED_CACHE_MAX", 128)),
            embedding_model=_env_str("KOOV_EMBEDDING_MODEL", "text-embedding-3-small") or "text-embedding-3-small",
            response_temperature=response_temperature,
            response_temp_meeting=_env_float("KOOV_RESPONSE_TEMP_MEETING", 0.28),
            response_openai_max_tokens_list=max(64, _env_int("KOOV_RESPONSE_OPENAI_MAX_TOKENS_LIST", 260)),
            chapter_every_n_chunks=_env_int_positive("KOOV_CHAPTER_EVERY_N_CHUNKS", 12),
            chapter_llm_max_tokens=_env_int("KOOV_CHAPTER_LLM_MAX_TOKENS", 400),
            conversational_mode=_conversational,
            personas_dir=_env_str("KOOV_PERSONAS_DIR", os.path.join("koov", "personas")) or os.path.join("koov", "personas"),
            personas_enabled=_personas_enabled,
            bot_persona_id=(
                (_bp := (_env_str("KOOV_BOT_PERSONA_ID") or "").strip().lower()) and _bp or None
            ),
            log_level=_env_str("KOOV_LOG_LEVEL", "INFO") or "INFO",
        )
