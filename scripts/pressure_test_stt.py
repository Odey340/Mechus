#!/usr/bin/env python3
"""
Pressure test STT (speech-to-text) backends.

Measures per-call latency and throughput for the configured transcriber.
Use --mock with elevenlabs to test code path without API calls.

Usage:
  # ElevenLabs (real API; set MECHUS_ELEVEN_API_KEY)
  python3 scripts/pressure_test_stt.py --backend elevenlabs --iterations 10 --duration-sec 1.5

  # ElevenLabs with mock HTTP (no network, tests multipart build + parse)
  python3 scripts/pressure_test_stt.py --backend elevenlabs --iterations 50 --mock

  # Faster-whisper (local)
  python3 scripts/pressure_test_stt.py --backend faster-whisper --iterations 5 --duration-sec 2
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _make_pcm16_silence(duration_sec: float, sample_rate_hz: int = 16000) -> bytes:
    """Generate silent PCM16 (zeros) for duration_sec at sample_rate_hz."""
    n_frames = int(duration_sec * sample_rate_hz)
    return b"\x00\x00" * n_frames


def _run_stt_test(
    backend: str,
    iterations: int,
    duration_sec: float,
    mock: bool,
) -> dict:
    from mechus.config import MechusConfig

    config = MechusConfig.from_env()
    sample_rate = 16000
    pcm16 = _make_pcm16_silence(duration_sec, sample_rate)
    if not pcm16:
        return {"error": "empty audio", "iterations": 0}

    if backend == "elevenlabs":
        import urllib.request
        import json
        from mechus.stt import ElevenLabsTranscriber

        api_key = os.getenv("MECHUS_ELEVEN_API_KEY") or config.eleven_api_key
        if not api_key and not mock:
            return {"error": "MECHUS_ELEVEN_API_KEY required for elevenlabs (or use --mock)", "iterations": 0}

        transcriber = ElevenLabsTranscriber(
            api_key=api_key or "mock-key",
            model_id=config.eleven_stt_model or "scribe_v2",
            language=config.whisper_language,
            base_url=config.eleven_base_url or "https://api.elevenlabs.io",
        )
        if mock:
            _original_urlopen = urllib.request.urlopen
            def _mock_urlopen(req, timeout=None):
                class MockResponse:
                    def read(self):
                        return json.dumps({"text": "mock transcript", "language_code": "en"}).encode("utf-8")
                return MockResponse()
            urllib.request.urlopen = _mock_urlopen
    elif backend == "faster-whisper":
        from mechus.stt import FasterWhisperTranscriber

        transcriber = FasterWhisperTranscriber(
            model=config.whisper_model,
            compute_type=config.whisper_compute_type,
            language=config.whisper_language,
        )
    else:
        return {"error": f"unsupported backend: {backend}", "iterations": 0}

    latencies_ms: list[float] = []
    errors = 0
    for i in range(iterations):
        t0 = time.perf_counter()
        try:
            result = transcriber.transcribe_pcm16(pcm16, sample_rate)
            if not hasattr(result, "text"):
                errors += 1
        except Exception:
            errors += 1
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)

    if mock and backend == "elevenlabs":
        urllib.request.urlopen = _original_urlopen

    def _p50(lat: list[float]) -> float:
        if not lat:
            return 0.0
        s = sorted(lat)
        return s[int(len(s) * 0.5)]

    def _p95(lat: list[float]) -> float:
        if not lat:
            return 0.0
        s = sorted(lat)
        return s[min(int(len(s) * 0.95), len(s) - 1)]

    return {
        "backend": backend,
        "mock": mock,
        "iterations": iterations,
        "errors": errors,
        "duration_sec": duration_sec,
        "audio_bytes": len(pcm16),
        "latency_p50_ms": _p50(latencies_ms),
        "latency_p95_ms": _p95(latencies_ms),
        "latency_mean_ms": sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pressure test STT backends")
    parser.add_argument("--backend", default="elevenlabs", help="elevenlabs or faster-whisper")
    parser.add_argument("--iterations", type=int, default=10, help="Number of transcribe calls")
    parser.add_argument("--duration-sec", type=float, default=1.5, help="Audio length in seconds (synthetic silence)")
    parser.add_argument("--mock", action="store_true", help="Mock HTTP for elevenlabs (no API call)")
    args = parser.parse_args()

    print(f"STT pressure test: backend={args.backend} iterations={args.iterations} duration={args.duration_sec}s mock={args.mock}")
    result = _run_stt_test(
        backend=args.backend,
        iterations=args.iterations,
        duration_sec=args.duration_sec,
        mock=args.mock,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    print("Results:")
    print(f"  Backend:        {result['backend']}" + (" (mock)" if result.get("mock") else ""))
    print(f"  Iterations:     {result['iterations']} (errors: {result.get('errors', 0)})")
    print(f"  Audio:          {result['duration_sec']}s, {result['audio_bytes']} bytes")
    print(f"  Latency p50:    {result['latency_p50_ms']:.1f} ms")
    print(f"  Latency p95:    {result['latency_p95_ms']:.1f} ms")
    print(f"  Latency mean:   {result['latency_mean_ms']:.1f} ms")
    return 0


if __name__ == "__main__":
    exit(main())
