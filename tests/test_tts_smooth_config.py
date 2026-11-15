"""MECHUS_TTS_SMOOTH preset: fades, pcm_32000, preroll off by default."""

from __future__ import annotations

import os
from unittest.mock import patch

from mechus.config import MechusConfig


def test_tts_smooth_defaults() -> None:
    with patch.dict(
        os.environ,
        {"MECHUS_TTS_SMOOTH": "1"},
        clear=False,
    ):
        for key in (
            "MECHUS_ELEVEN_OUTPUT_FORMAT",
            "MECHUS_THINKING_PREROLL",
            "MECHUS_TTS_FADE_IN_MS",
            "MECHUS_TTS_FADE_OUT_MS",
        ):
            os.environ.pop(key, None)
        cfg = MechusConfig.from_env()
    assert cfg.tts_smooth is True
    assert cfg.eleven_output_format == "pcm_32000"
    assert cfg.thinking_preroll_enabled is False
    assert cfg.tts_fade_in_ms >= 34.0
    assert cfg.tts_fade_out_ms >= 26.0


def test_thinking_preroll_off_by_default_without_smooth() -> None:
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("MECHUS_THINKING_PREROLL", None)
        os.environ.pop("MECHUS_TTS_SMOOTH", None)
        cfg = MechusConfig.from_env()
    assert cfg.thinking_preroll_enabled is False
