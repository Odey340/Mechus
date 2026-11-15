"""Wake opener bridge must stay alive until the first TTS chunk is ready."""

from __future__ import annotations

import sys
import threading
import time
import types
from unittest.mock import MagicMock

import numpy as np


def _agent_class():
    if "sounddevice" not in sys.modules:
        sys.modules["sounddevice"] = types.SimpleNamespace(
            PortAudioError=Exception,
            query_devices=lambda *args, **kwargs: {"default_samplerate": 44100},
        )
    if "webrtcvad" not in sys.modules:
        class _Vad:
            def is_speech(self, *args, **kwargs):
                return False

        sys.modules["webrtcvad"] = types.SimpleNamespace(Vad=lambda *args, **kwargs: _Vad())
    if "soundfile" not in sys.modules:
        sys.modules["soundfile"] = types.SimpleNamespace(
            read=lambda *args, **kwargs: (np.zeros((0,), dtype=np.float32), 16000)
        )

    from mechus.agent import MechusAgent

    return MechusAgent


def _make_streaming_agent(*, chunk_delay_s: float = 0.35):
    MechusAgent = _agent_class()
    agent = object.__new__(MechusAgent)
    agent._config = types.SimpleNamespace(
        tts_output_gain=1.0,
        tts_fade_in_ms=0.0,
        tts_fade_out_ms=0.0,
        tts_fade_in_after_opener_ms=0.0,
        eleven_tts_stream=True,
        sample_rate_hz=16000,
    )
    agent._wake_opener_played = True
    agent._bridge_finale_preroll_played = True
    agent._wake_pipeline_t0 = None
    agent._last_latency = None
    agent._wake_opener_thread = None
    agent._pending_transcript_wake = None

    stop_evt = threading.Event()
    opener_alive = {"running": True}

    def _fake_opener() -> None:
        while not stop_evt.is_set():
            time.sleep(0.02)
        opener_alive["running"] = False

    opener_th = threading.Thread(target=_fake_opener, daemon=True)
    opener_th.start()

    play_streaming_calls: list[dict] = []

    class _AudioOut:
        def play_streaming(self, chunks_iter, **kwargs):
            play_streaming_calls.append(kwargs)
            for chunk in chunks_iter:
                if chunk.size:
                    pass

        def play_blocking(self, audio_f32, sample_rate_hz: int, gain: float = 1.0) -> None:
            # Silence tail played after streaming so the last phoneme is not
            # clipped (MechusAgent._play_phrase_end_tail).
            return

        def wait(self) -> None:
            return

    agent._audio_out = _AudioOut()
    agent._zoom_virtual_mic_output = lambda: False
    agent._playback_output_sample_rate = lambda: 32000
    agent._resample_audio_for_playback = lambda a, sr: (a, sr)
    agent._tts_audio_for_playback = lambda a, sr, **kw: (a, sr)
    agent._log_time_to_first_audio = lambda *a, **kw: None

    class _TTS:
        def synthesize_streaming(self, phrase, on_chunk):
            time.sleep(chunk_delay_s)
            on_chunk(np.ones(800, dtype=np.float32) * 0.1, 32000)

    return agent, _TTS(), stop_evt, opener_th, opener_alive, play_streaming_calls


def test_streaming_keeps_opener_alive_until_first_chunk() -> None:
    agent, tts, stop_evt, opener_th, opener_alive, _calls = _make_streaming_agent(
        chunk_delay_s=0.4
    )
    t0 = time.monotonic()
    assert not stop_evt.is_set()
    assert opener_alive["running"]

    agent._play_streaming_phrase_smooth(
        tts,
        "Hello from Mechus.",
        opener_stop_evt=stop_evt,
        opener_thread=opener_th,
    )

    elapsed = time.monotonic() - t0
    assert not opener_alive["running"]
    assert stop_evt.is_set()
    assert elapsed >= 0.35, "should wait for TTS chunk before releasing opener"


def test_release_wake_opener_bridge_sets_stop_and_joins() -> None:
    MechusAgent = _agent_class()
    agent = object.__new__(MechusAgent)
    agent._wake_opener_thread = None
    agent._wait_audio_output_idle = MagicMock()

    stop_evt = threading.Event()
    done = threading.Event()

    def _worker() -> None:
        stop_evt.wait(timeout=2.0)
        done.set()

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    agent._release_wake_opener_bridge(stop_evt, th, timeout_s=2.0)

    assert stop_evt.is_set()
    assert done.is_set()
    assert not th.is_alive()
    agent._wait_audio_output_idle.assert_called_once()


def test_streaming_handoff_passes_bridge_flags() -> None:
    agent, tts, stop_evt, opener_th, _, calls = _make_streaming_agent(chunk_delay_s=0.05)
    agent._play_streaming_phrase_smooth(
        tts,
        "Quick reply.",
        opener_stop_evt=stop_evt,
        opener_thread=opener_th,
    )
    assert calls, "expected play_streaming to run"
    assert calls[0].get("skip_edge_ramp_in") is True
    assert calls[0].get("handoff_from_bridge") is True
