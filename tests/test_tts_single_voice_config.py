"""Shared ElevenLabs voice (MECHUS_ELEVEN_VOICE_ID) vs optional per-persona engines."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mechus.agent import MechusAgent
from mechus.config import MechusConfig


@pytest.fixture()
def eleven_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MECHUS_TTS_BACKEND", "elevenlabs")
    monkeypatch.setenv("MECHUS_ELEVEN_API_KEY", "test-key")
    monkeypatch.setenv("MECHUS_ELEVEN_VOICE_ID", "voice-main")


def test_default_uses_only_main_eleven_voice(eleven_env: None) -> None:
    cfg = MechusConfig.from_env()
    assert cfg.per_persona_eleven_voices is False

    agent = object.__new__(MechusAgent)
    agent._config = cfg
    agent._tts = MagicMock(name="main_engine")
    agent._tts_by_voice = {"voice-main": agent._tts}

    penny = SimpleNamespace(id="penny", voice_id="voice-penny-should-be-ignored", model_id=None)
    assert MechusAgent._tts_for(agent, penny) is agent._tts


def test_per_persona_flag_enables_distinct_engines(eleven_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MECHUS_PER_PERSONA_ELEVEN_VOICES", "1")
    cfg = MechusConfig.from_env()
    assert cfg.per_persona_eleven_voices is True

    agent = object.__new__(MechusAgent)
    agent._config = cfg
    agent._tts = MagicMock(name="main_engine")
    other = MagicMock(name="other_engine")
    agent._tts_by_voice = {"voice-main": agent._tts, "voice-penny": other}

    penny = SimpleNamespace(id="penny", voice_id="voice-penny", model_id=None)
    assert MechusAgent._tts_for(agent, penny) is other
