#!/usr/bin/env python3
"""Measure wake → first audio (opener + reply), matching production FaceTime settings.

Simulates:
  - Parallel wake opener (cached filler) + LLM
  - No post-LLM silence wait (transcript wake, low latency)
  - Streaming TTS for the short reply

Usage:
  .venv/bin/python scripts/benchmark_wake_to_first_audio.py --iterations 8
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_env = os.path.join(_REPO, ".env")
if os.path.exists(_env):
    from mechus.envfile import load_env_file

    load_env_file(_env)

# Production FaceTime worker defaults for this benchmark
os.environ.setdefault("MECHUS_LOW_LATENCY", "1")
os.environ.setdefault("MECHUS_WAKE_OPENER", "1")
os.environ.setdefault("MECHUS_WAKE_OPENER_MAX_AUDIO_S", "1.0")
os.environ.setdefault("MECHUS_MIN_SILENCE_BEFORE_SPEAKING_TRANSCRIPT_ST_S", "0")
os.environ.setdefault("MECHUS_ELEVEN_TTS_STREAM", "1")
os.environ.setdefault("MECHUS_WAKE_SPOKEN_MAX_WORDS", "12")
os.environ.setdefault("MECHUS_WAKE_SPOKEN_MAX_CHARS", "40")
os.environ.setdefault("MECHUS_RESPONSE_JSON_MODE", "1")

from mechus.config import MechusConfig  # noqa: E402
from mechus.memory import ConversationMemory  # noqa: E402
from mechus.responder import OpenAIResponder, ResponseInput  # noqa: E402
from mechus.tts import ElevenLabsTTSEngine, SynthesizedAudio  # noqa: E402


def _p50(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _warm_opener_cache(tts: ElevenLabsTTSEngine, phrase: str) -> SynthesizedAudio | None:
    audio = tts.synthesize(phrase)
    if audio.audio_f32.size == 0:
        return None
    return audio


def _play_cached_duration(audio: SynthesizedAudio) -> float:
    """Wall time to 'play' cached audio (compute duration; no sound device needed)."""
    return float(audio.audio_f32.shape[0]) / float(audio.sample_rate_hz)


def _run_iteration(
    *,
    config: MechusConfig,
    responder: OpenAIResponder,
    tts: ElevenLabsTTSEngine,
    opener_audio: SynthesizedAudio,
    opener_play_s: float,
    request: str,
) -> dict[str, float]:
    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    memory.add_segment("Alex: Mechus, quick update on the beta launch.")
    memory.add_segment("Sarah: Engineering is targeting July 25th.")

    state = {
        "wake_brevity": True,
        "wake_spoken_max_words": config.wake_spoken_max_words,
        "wake_spoken_max_chars": config.wake_spoken_max_chars,
    }
    inp = ResponseInput(
        wake_phrase=config.wake_phrase,
        wake_phrases=config.wake_phrases or (config.wake_phrase,),
        request_text=request,
        context_text="",
        memory_text=memory.get_recent(),
        context_chunks=[],
        intent="decision_query",
        state=state,
        retrieval_meta={},
    )

    llm_box: list[str] = []
    llm_exc: list[BaseException] = []

    def _llm_worker() -> None:
        try:
            llm_box.append(responder.respond(inp).strip())
        except BaseException as exc:  # noqa: BLE001
            llm_exc.append(exc)

    t_wake = time.perf_counter()
    first_audio_at: float | None = None
    opener_done_at: float | None = None
    llm_done_at: float | None = None

    llm_th = threading.Thread(target=_llm_worker, daemon=True)
    llm_th.start()

    # Opener (parallel with LLM) — cached clip; first audio is immediate
    if config.wake_opener_enabled and opener_audio is not None:
        first_audio_at = t_wake
        time.sleep(opener_play_s)
        opener_done_at = time.perf_counter()

    llm_th.join()
    llm_done_at = time.perf_counter()
    if llm_exc:
        raise llm_exc[0]
    spoken = llm_box[0] if llm_box else ""
    if not spoken:
        spoken = "Targeting July 25th for launch."

    # No silence wait (transcript + low_latency)
    first_byte_holder: list[float] = []

    def _on_chunk(audio_f32, sr: int) -> None:
        if first_byte_holder or audio_f32.size == 0:
            return
        first_byte_holder.append(time.perf_counter())

    if config.eleven_tts_stream and hasattr(tts, "synthesize_streaming"):
        tts.synthesize_streaming(spoken, _on_chunk, first_chunk_ms=80.0)
    else:
        t0 = time.perf_counter()
        tts.synthesize(spoken)
        first_byte_holder.append(t0)

    if first_byte_holder:
        first_reply_at = first_byte_holder[0]
        if first_audio_at is None:
            first_audio_at = first_reply_at
    else:
        first_reply_at = None

    return {
        "wake_to_first_audio_ms": ((first_audio_at or t_wake) - t_wake) * 1000,
        "wake_to_opener_end_ms": ((opener_done_at or t_wake) - t_wake) * 1000,
        "wake_to_first_reply_ms": ((first_reply_at or t_wake) - t_wake) * 1000,
        "llm_ms": (llm_done_at - t_wake) * 1000 if llm_done_at else 0.0,
        "spoken_chars": float(len(spoken)),
        "spoken_words": float(len(spoken.split())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=8)
    args = ap.parse_args()

    config = MechusConfig.from_env()
    if not config.openai_api_key or not config.eleven_api_key or not config.eleven_voice_id:
        print("Need MECHUS_OPENAI_API_KEY, MECHUS_ELEVEN_API_KEY, MECHUS_ELEVEN_VOICE_ID", file=sys.stderr)
        return 1

    tts = ElevenLabsTTSEngine(
        api_key=config.eleven_api_key,
        voice_id=config.eleven_voice_id,
        model_id=config.eleven_model_id,
        base_url=config.eleven_base_url,
        output_format=config.eleven_output_format,
    )
    responder = OpenAIResponder(
        api_key=config.openai_api_key,
        model=config.openai_chat_model or "gpt-4o-mini",
        max_response_words=config.max_response_words,
        openai_max_tokens=config.response_openai_max_tokens,
        timeout_s=config.response_openai_timeout_s,
        prompt_max_chars=config.response_prompt_max_chars,
        json_mode=True,
        audio_tags_enabled=config.eleven_audio_tags,
    )

    from mechus.personas import PersonaRegistry

    persona = PersonaRegistry.load(config).default()
    opener_phrase = "Yeah."
    if persona.filler_phrases:
        short = sorted(persona.filler_phrases, key=len)[0]
        opener_phrase = short if len(short) <= 20 else "Yeah."

    print("Warming opener cache (%r)..." % opener_phrase)
    t_w = time.perf_counter()
    opener_audio = _warm_opener_cache(tts, opener_phrase)
    if opener_audio is None:
        print("Failed to synthesize opener; aborting", file=sys.stderr)
        return 1
    cap_s = min(
        float(config.wake_opener_max_audio_s),
        opener_audio.audio_f32.shape[0] / opener_audio.sample_rate_hz,
    )
    opener_play_s = cap_s
    print(
        "Opener cached in %.0fms (play %.2fs)\n"
        % ((time.perf_counter() - t_w) * 1000, opener_play_s)
    )

    request = "what did we decide about the launch date?"
    rows: list[dict[str, float]] = []

    print("=" * 72)
    print("  Wake → first audio (production FaceTime timing model)")
    print(
        f"  opener={config.wake_opener_enabled}  stream={config.eleven_tts_stream}  "
        f"model={config.eleven_model_id}  wake≤{config.wake_spoken_max_words}w"
    )
    print("=" * 72)

    for i in range(args.iterations):
        row = _run_iteration(
            config=config,
            responder=responder,
            tts=tts,
            opener_audio=opener_audio,
            opener_play_s=opener_play_s,
            request=request,
        )
        rows.append(row)
        print(
            "  [%d] first_audio=%.0fms  llm=%.0fms  reply_audio=%.0fms  words=%.0f"
            % (
                i + 1,
                row["wake_to_first_audio_ms"],
                row["llm_ms"],
                row["wake_to_first_reply_ms"],
                row["spoken_words"],
            )
        )

    fa = [r["wake_to_first_audio_ms"] for r in rows]
    llm = [r["llm_ms"] for r in rows]
    r = [r["wake_to_first_reply_ms"] for r in rows]
    print()
    print("Summary (p50):")
    print("  Wake → first audio:     %.0f ms  (opener or reply)" % _p50(fa))
    print("  LLM (parallel):         %.0f ms" % _p50(llm))
    print("  Wake → reply audio:     %.0f ms  (main answer)" % _p50(r))
    print()
    print("Targets: opener <500ms, reply start <1500ms (needs fast LLM + cache hits help)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
