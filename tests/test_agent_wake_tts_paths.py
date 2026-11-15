"""Regression: Zoom disables TTS interrupt — playback must use the non-interrupt branch."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from koov.agent import KoovAgent, WakeCycleResult
from koov.config import KoovConfig


def _minimal_config() -> KoovConfig:
    """Stable config for bare-agent tests (no real audio devices)."""
    return KoovConfig.from_env()


def _bare_agent(cfg: KoovConfig) -> KoovAgent:
    agent = object.__new__(KoovAgent)
    agent._config = cfg
    agent._stop = threading.Event()
    agent._tts = MagicMock()
    agent._audio_out = MagicMock()
    agent._audio_out._stop_event = None
    agent._audio_out.wait = MagicMock()
    agent._wake = MagicMock()
    agent._interrupt_vad = MagicMock()
    agent._interrupt_min_frames = 2
    agent._last_latency = {"llm_ms": 0.0}
    agent._tts_playing = False
    agent._thinking_preroll_thread = None
    agent._main_tts_presynth_thread = None
    agent._main_tts_presynth_box = []
    agent._wake_opener_played = False
    agent._all_wake_phrases = tuple(cfg.wake_phrases or (cfg.wake_phrase,))
    agent._join_thinking_preroll_thread = lambda timeout_s=5.0: None
    agent._tts_for = lambda _persona=None: agent._tts
    agent._zoom_virtual_mic_output = lambda: False
    agent._reassert_facetime_tts_routing = lambda: None
    agent._play_cached_thinking_preroll = MagicMock(return_value=False)
    agent._should_play_wake_opener = MagicMock(return_value=False)
    agent._personas = MagicMock()
    agent._personas.default.return_value = SimpleNamespace(id="koov", filler_phrases=())
    agent._transcript_wake_only = False
    agent._pending_transcript_wake = None
    agent._barge_stream_chunkers = {}
    agent._barge_speaker_chunkers = {}
    agent._tts_cancel_requested = False
    agent._pending_transcript_wake_is_barge_in_eligible = lambda: False
    agent._note_phrase_spoken_aloud = lambda _phrase: None
    agent._playback_output_sample_rate = lambda: int(cfg.sample_rate_hz or 48000)
    agent._resample_audio_for_playback = lambda a, sr: (
        np.asarray(a, dtype=np.float32).reshape(-1),
        int(sr),
    )
    agent._log_time_to_first_audio = lambda *a, **k: None
    agent._skip_post_llm_silence_wait = lambda _source: False
    return agent


def _fake_tts_result(samples: int = 960, sr: int = 48000):
    return SimpleNamespace(
        audio_f32=np.linspace(-0.1, 0.1, samples, dtype=np.float32),
        sample_rate_hz=sr,
    )


def test_non_interrupt_path_calls_play_blocking_not_play() -> None:
    """When _can_interrupt_tts is False (default Zoom), response must still play via play_blocking."""
    cfg = _minimal_config()
    agent = _bare_agent(cfg)
    # Avoid MagicMock's arbitrary attrs: hasattr(tts, "synthesize_streaming") must be False.
    agent._tts = MagicMock(spec=["synthesize"])
    agent._flush_transcript_chunkers = lambda: None
    agent._run_wake_cycle = MagicMock(
        return_value=WakeCycleResult("koov", "Test reply phrase.", is_instant=True)
    )
    agent._wait_for_silence = MagicMock(return_value=True)
    agent._can_interrupt_tts = MagicMock(return_value=False)
    agent._tts.synthesize = MagicMock(side_effect=lambda _text: _fake_tts_result())

    ok = KoovAgent._run_wake_pipeline_after_detect(
        agent,
        trigger_text="koov",
        seed_pcm16=b"",
        context_pcm16=b"",
        source="test",
    )
    assert ok is True
    agent._audio_out.play_blocking.assert_called()
    agent._audio_out.play.assert_not_called()
    agent._tts.synthesize.assert_called()


def test_interrupt_path_calls_play_and_wait() -> None:
    """When interrupt is enabled, non-blocking play + wait(); no duplicate play_blocking."""
    cfg = _minimal_config()
    agent = _bare_agent(cfg)
    agent._wait_audio_output_idle = MagicMock()
    agent._tts = MagicMock(spec=["synthesize"])
    agent._flush_transcript_chunkers = lambda: None
    agent._run_wake_cycle = MagicMock(
        return_value=WakeCycleResult("koov", "Short.", is_instant=True)
    )
    agent._wait_for_silence = MagicMock(return_value=True)
    agent._can_interrupt_tts = MagicMock(return_value=True)
    agent._tts.synthesize = MagicMock(side_effect=lambda _text: _fake_tts_result(samples=4800))
    agent._read_frame = MagicMock(return_value=None)
    agent._handle_live_frame = MagicMock()
    agent._interrupt_vad.is_speech = MagicMock(return_value=False)

    ok = KoovAgent._run_wake_pipeline_after_detect(
        agent,
        trigger_text="koov",
        seed_pcm16=b"",
        context_pcm16=b"",
        source="test",
    )
    assert ok is True
    agent._audio_out.play.assert_called_once()
    agent._audio_out.wait.assert_called()
    agent._audio_out.play_blocking.assert_not_called()


def test_non_interrupt_streaming_tts_concatenates_chunks_to_one_play() -> None:
    """Streaming TTS uses one play_streaming session (no per-chunk stop/start clicks)."""

    class _StreamingTTS:
        def synthesize_streaming(self, text, play_chunk):
            play_chunk(np.ones(240, dtype=np.float32), 48000)
            play_chunk(np.ones(240, dtype=np.float32), 48000)

    with patch.dict(os.environ, {"KOOV_ELEVEN_TTS_STREAM": "1"}, clear=False):
        cfg = _minimal_config()
    agent = _bare_agent(cfg)
    agent._playback_output_sample_rate = lambda: 48000
    agent._resample_audio_for_playback = lambda a, sr: (np.asarray(a, dtype=np.float32), int(sr))
    agent._tts_audio_for_playback = lambda a, sr, **kw: (np.asarray(a, dtype=np.float32), int(sr))
    agent._tts = _StreamingTTS()
    agent._flush_transcript_chunkers = lambda: None
    agent._run_wake_cycle = MagicMock(
        return_value=WakeCycleResult("koov", "Streamed.", is_instant=True)
    )
    agent._wait_for_silence = MagicMock(return_value=True)
    agent._can_interrupt_tts = MagicMock(return_value=False)

    ok = KoovAgent._run_wake_pipeline_after_detect(
        agent,
        trigger_text="koov",
        seed_pcm16=b"",
        context_pcm16=b"",
        source="test",
    )
    assert ok is True
    assert agent._audio_out.play_streaming.call_count == 1
    # End-of-phrase tail silence (no fade-out) may use play_blocking once after streaming.
    assert agent._audio_out.play_blocking.call_count <= 1


def test_discarded_transcript_wake_clears_tts_playing() -> None:
    """Opener sets _tts_playing; a discarded cycle must not block later transcript wakes."""
    cfg = _minimal_config()
    agent = _bare_agent(cfg)
    agent._transcript_wake_only = True
    agent._flush_transcript_chunkers = lambda: None
    agent._run_wake_cycle = MagicMock(return_value=None)
    agent._should_play_wake_opener = MagicMock(return_value=True)

    def _fake_opener(*_a, **_k) -> None:
        agent._tts_playing = True

    agent._run_wake_opener = _fake_opener

    ok = KoovAgent._run_wake_pipeline_after_detect(
        agent,
        trigger_text="Oh, yes. And, uh, like, um",
        seed_pcm16=b"",
        context_pcm16=b"",
        source="transcript_stt",
    )
    assert ok is True
    assert agent._tts_playing is False
