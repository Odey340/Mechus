#!/usr/bin/env python3
"""Benchmark wake-path latency: LLM + TTS (streaming first-byte when enabled).

Usage:
  .venv/bin/python scripts/benchmark_wake_latency.py --iterations 15
  .venv/bin/python scripts/benchmark_wake_latency.py --no-stream --iterations 10

Run blocking TTS separately (do not put ``#`` comments on the same shell line):
  .venv/bin/python scripts/benchmark_wake_latency.py --no-stream --iterations 10
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_env = os.path.join(_REPO, ".env")
if os.path.exists(_env):
    from koov.envfile import load_env_file

    load_env_file(_env)

from koov.audio import apply_f32_linear_fade_ends, trim_f32_silence_edges  # noqa: E402
from koov.config import KoovConfig  # noqa: E402
from koov.memory import ConversationMemory  # noqa: E402
from koov.responder import OpenAIResponder, ResponseInput  # noqa: E402
from koov.tts import ElevenLabsTTSEngine  # noqa: E402


def _p50(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(0.95 * len(s)))
    return s[i]


def _wake_state(config: KoovConfig) -> dict:
    if not config.low_latency:
        return {}
    return {
        "wake_brevity": True,
        "wake_spoken_max_words": config.wake_spoken_max_words,
        "wake_spoken_max_chars": config.wake_spoken_max_chars,
    }


def _run_case(
    *,
    label: str,
    config: KoovConfig,
    responder: OpenAIResponder,
    tts: ElevenLabsTTSEngine,
    request: str,
    iterations: int,
    stream: bool,
) -> None:
    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    memory.add_segment("Alex: Koov, we need a quick status on the beta launch timeline.")
    memory.add_segment("Sarah: Engineering is targeting July 25th for the release.")

    llm_ms: list[float] = []
    tts_full_ms: list[float] = []
    first_byte_ms: list[float] = []
    audio_s: list[float] = []
    spoken_lens: list[int] = []
    outliers: list[float] = []

    print(f"\n--- {label} ({iterations} runs, stream={stream}) ---")

    for i in range(iterations):
        inp = ResponseInput(
            wake_phrase=config.wake_phrase,
            wake_phrases=config.wake_phrases or (config.wake_phrase,),
            request_text=request,
            context_text="",
            memory_text=memory.get_recent(),
            context_chunks=[],
            intent="general",
            state=_wake_state(config),
            retrieval_meta={},
        )
        t0 = time.perf_counter()
        spoken = responder.respond(inp).strip()
        t_llm = (time.perf_counter() - t0) * 1000
        if t_llm >= config.llm_outlier_log_ms:
            outliers.append(t_llm)
            print(f"  [{i + 1}] LLM OUTLIER {t_llm:.0f}ms")

        t1 = time.perf_counter()
        first_at: float | None = None
        total_samples = 0
        sr = 16000

        if stream and hasattr(tts, "synthesize_streaming"):

            def _on_chunk(audio_f32, sample_rate_hz):
                nonlocal first_at, total_samples, sr
                sr = sample_rate_hz
                if first_at is None and audio_f32.size:
                    first_at = time.perf_counter()
                total_samples += int(audio_f32.size)

            synth = tts.synthesize_streaming(spoken, _on_chunk)
        else:
            synth = tts.synthesize(spoken)
            total_samples = int(synth.audio_f32.size)
            sr = synth.sample_rate_hz

        t_tts = (time.perf_counter() - t1) * 1000
        t_fb = ((first_at - t1) * 1000) if first_at is not None else t_tts

        trimmed, _ = _prep(synth.audio_f32, sr, config)
        duration_s = trimmed.shape[0] / float(sr) if sr else 0.0

        llm_ms.append(t_llm)
        tts_full_ms.append(t_tts)
        first_byte_ms.append(t_fb)
        audio_s.append(duration_s)
        spoken_lens.append(len(spoken))

        if i == 0:
            print(f"       spoken ({len(spoken.split())}w): {spoken!r}")

        print(
            f"  [{i + 1}] llm={t_llm:.0f}ms first_audio={t_fb:.0f}ms tts_done={t_tts:.0f}ms "
            f"audio={duration_s:.2f}s chars={len(spoken)}"
        )

    print(
        f"  p50 llm={_p50(llm_ms):.0f}ms  first_audio={_p50(first_byte_ms):.0f}ms  "
        f"tts_done={_p50(tts_full_ms):.0f}ms  audio={_p50(audio_s):.2f}s  chars={_p50(spoken_lens):.0f}"
    )
    print(
        f"  p95 llm={_p95(llm_ms):.0f}ms  first_audio={_p95(first_byte_ms):.0f}ms  "
        f"tts_done={_p95(tts_full_ms):.0f}ms"
    )
    ready = _p50(llm_ms) + _p50(first_byte_ms)
    perceived = ready + _p50(audio_s) * 1000
    print(f"  → speech starts (p50 llm+first_audio): {ready:.0f}ms")
    print(f"  → full reply heard (p50 llm+first_audio+audio): {perceived:.0f}ms")
    if outliers:
        print(f"  → LLM outliers (>{config.llm_outlier_log_ms:.0f}ms): {len(outliers)} runs")


def _prep(audio_f32, tts_sr: int, config: KoovConfig):
    trimmed = trim_f32_silence_edges(audio_f32, tts_sr, pad_ms=22.0)
    if config.tts_fade_in_ms > 0 or config.tts_fade_out_ms > 0:
        trimmed = apply_f32_linear_fade_ends(
            trimmed,
            tts_sr,
            fade_in_ms=config.tts_fade_in_ms,
            fade_out_ms=config.tts_fade_out_ms,
        )
    duration_s = trimmed.shape[0] / float(tts_sr) if tts_sr else 0.0
    return trimmed, duration_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=15)
    ap.add_argument("--no-stream", action="store_true")
    args = ap.parse_args()

    if not os.getenv("KOOV_LOW_LATENCY"):
        os.environ["KOOV_LOW_LATENCY"] = "1"
    if not args.no_stream:
        os.environ.setdefault("KOOV_ELEVEN_TTS_STREAM", "1")
    os.environ.setdefault("KOOV_WAKE_SPOKEN_MAX_WORDS", "12")
    os.environ.setdefault("KOOV_WAKE_SPOKEN_MAX_CHARS", "40")

    config = KoovConfig.from_env()
    if not config.openai_api_key or not config.eleven_api_key or not config.eleven_voice_id:
        print("Need KOOV_OPENAI_API_KEY, KOOV_ELEVEN_API_KEY, KOOV_ELEVEN_VOICE_ID", file=sys.stderr)
        return 1

    stream = config.eleven_tts_stream and not args.no_stream
    responder = OpenAIResponder(
        api_key=config.openai_api_key,
        model=config.openai_chat_model or "gpt-4o-mini",
        max_response_words=config.max_response_words,
        openai_max_tokens=config.response_openai_max_tokens,
        timeout_s=config.response_openai_timeout_s,
        prompt_max_chars=config.response_prompt_max_chars,
        json_mode=config.response_json_mode or config.eleven_audio_tags,
        audio_tags_enabled=config.eleven_audio_tags,
    )
    tts = ElevenLabsTTSEngine(
        api_key=config.eleven_api_key,
        voice_id=config.eleven_voice_id,
        model_id=config.eleven_model_id,
        base_url=config.eleven_base_url,
        output_format=config.eleven_output_format,
    )

    print("=" * 72)
    print("  Wake latency benchmark (live APIs)")
    print(
        f"  LLM={config.openai_chat_model}  json={config.response_json_mode or config.eleven_audio_tags}  "
        f"audio_tags={config.eleven_audio_tags}"
    )
    print(
        f"  TTS={config.eleven_model_id}  format={config.eleven_output_format}  "
        f"low_latency={config.low_latency}  wake≤{config.wake_spoken_max_words}w/"
        f"{config.wake_spoken_max_chars}c  stream={stream}"
    )
    if not config.low_latency:
        print("  WARNING: set KOOV_LOW_LATENCY=1 for production FaceTime wake limits")
    print("=" * 72)

    prompts = [
        "what did we decide about the launch date?",
        "give me a one-sentence recap of the meeting so far",
        "what are the main risks people mentioned?",
    ]
    for p in prompts:
        _run_case(
            label=f"Q: {p[:48]}",
            config=config,
            responder=responder,
            tts=tts,
            request=p,
            iterations=args.iterations,
            stream=stream,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
