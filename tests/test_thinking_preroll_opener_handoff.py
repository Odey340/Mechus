"""Thinking preroll must not run while wake opener bridges (single audio owner)."""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock


def _agent_and_config():
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
        import numpy as np

        sys.modules["soundfile"] = types.SimpleNamespace(
            read=lambda *args, **kwargs: (np.zeros((0,), dtype=np.float32), 16000)
        )

    from mechus.agent import MechusAgent
    from mechus.config import MechusConfig

    return MechusAgent, MechusConfig


def _agent_with_preroll_enabled():
    MechusAgent, MechusConfig = _agent_and_config()
    cfg = MechusConfig.from_env()
    agent = object.__new__(MechusAgent)
    agent._config = cfg
    agent._config = cfg.__class__(**{**cfg.__dict__, "thinking_preroll_enabled": True, "wake_opener_enabled": True})
    agent._thinking_preroll_thread = None
    agent._wake_opener_thread = None
    agent._audio_out = MagicMock()
    agent._personas = MagicMock()
    agent._personas.default.return_value = SimpleNamespace(id="mechus", filler_phrases=("one moment",))
    agent._play_cached_thinking_preroll = MagicMock(return_value=True)
    return agent


def test_thinking_preroll_skipped_while_opener_bridge_active() -> None:
    agent = _agent_with_preroll_enabled()
    agent._wake_opener_bridge_active = True
    agent._maybe_start_thinking_preroll_thread(
        agent._personas.default(),
        request_text="weather in mumbai",
        wake_source="transcript_stt",
    )
    assert agent._thinking_preroll_thread is None
    agent._play_cached_thinking_preroll.assert_not_called()


def test_thinking_preroll_skipped_for_transcript_stt_when_wake_opener_enabled() -> None:
    """Gapless opener extend owns the LLM wait — no parallel preroll on meeting paths."""
    agent = _agent_with_preroll_enabled()
    agent._wake_opener_thread = None
    agent._wake_opener_bridge_active = False
    agent._maybe_start_thinking_preroll_thread(
        agent._personas.default(),
        request_text="weather in mumbai",
        wake_source="transcript_stt",
    )
    assert agent._thinking_preroll_thread is None
    agent._play_cached_thinking_preroll.assert_not_called()


def test_thinking_preroll_starts_without_opener_thread() -> None:
    agent = _agent_with_preroll_enabled()
    agent._config = agent._config.__class__(
        **{**agent._config.__dict__, "wake_opener_enabled": False}
    )
    agent._maybe_start_thinking_preroll_thread(
        agent._personas.default(),
        request_text="weather",
        wake_source="transcript_stt",
    )
    th = agent._thinking_preroll_thread
    assert th is not None
    th.join(timeout=2.0)
    agent._play_cached_thinking_preroll.assert_called()
