"""Tests for FaceTime join guard helpers in api_server and config."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mechus.api_server import (
    _facetime_worker_docker_env_flags,
    infer_join_backend_from_meeting_link,
)


# ── infer_join_backend_from_meeting_link ────────────────────────────────────

@pytest.mark.parametrize(
    "url,expected",
    [
        # FaceTime https link
        ("https://facetime.apple.com/join#v=1&k=abc", "facetime_web"),
        # FaceTime scheme
        ("facetime://join?k=abc", "facetime_web"),
        # Zoom
        ("https://zoom.us/j/1234567890?pwd=foo", "zoom_sdk"),
        ("https://us02web.zoom.us/j/1234567890", "zoom_sdk"),
        # Unknown — returns None
        ("https://meet.google.com/abc-defg-hij", None),
        # Empty string — returns None (no crash)
        ("", None),
    ],
)
def test_infer_join_backend(url: str, expected: str | None) -> None:
    assert infer_join_backend_from_meeting_link(url) == expected


# ── FaceTime env bundle defaults (worker forces these) ──────────────────────

def test_facetime_worker_env_forces_join_backend() -> None:
    """facetime_worker.py sets MECHUS_ZOOM_STREAM_ATTRIBUTION=0 and pulse devices."""
    from mechus.config import MechusConfig

    with patch.dict(
        os.environ,
        {
            "MECHUS_JOIN_BACKEND": "facetime_web",
            "MECHUS_INPUT_DEVICE": "pulse",
            "MECHUS_OUTPUT_DEVICE": "pulse",
            "MECHUS_ZOOM_STREAM_ATTRIBUTION": "0",
        },
        clear=False,
    ):
        cfg = MechusConfig.from_env()

    # MechusConfig does not store join_backend, but the worker env overrides are verified here.
    assert cfg.input_device == "pulse"
    assert cfg.output_device == "pulse"
    assert not cfg.zoom_stream_attribution


def test_facetime_diarization_async_flag() -> None:
    """MECHUS_DIARIZATION_ASYNC=1 sets diarization_async=True on config."""
    from mechus.config import MechusConfig

    with patch.dict(
        os.environ,
        {
            "MECHUS_DIARIZATION_ASYNC": "1",
        },
        clear=False,
    ):
        cfg = MechusConfig.from_env()

    assert cfg.diarization_async is True


def test_facetime_diarization_async_default_false() -> None:
    """MECHUS_DIARIZATION_ASYNC defaults to False."""
    from mechus.config import MechusConfig

    env = {k: v for k, v in os.environ.items()}
    env.pop("MECHUS_DIARIZATION_ASYNC", None)
    with patch.dict(os.environ, env, clear=True):
        cfg = MechusConfig.from_env()

    assert cfg.diarization_async is False


# ── FaceTime TTS smooth env bundle ──────────────────────────────────────────

def test_facetime_docker_env_includes_meeting_id_without_env_file_on_disk() -> None:
    """Compose injects env into the API process but often does not mount .env — overrides must still pass."""
    overrides = {
        "MECHUS_MEETING_ID": "4128295d-fa0e-4a2a-b611-16c2b9c2093f",
        "MECHUS_MEETING_LINK": "https://facetime.apple.com/join#v=1",
        "MECHUS_DISPLAY_NAME": "Mechus",
    }
    flags = _facetime_worker_docker_env_flags(
        env_file_abs="/nonexistent/mechus.env",
        overrides=overrides,
        environ={"MECHUS_ELEVEN_API_KEY": "sk-test", "MECHUS_TTS_BACKEND": "elevenlabs"},
    )
    joined = " ".join(flags)
    assert "MECHUS_MEETING_ID=4128295d-fa0e-4a2a-b611-16c2b9c2093f" in joined
    assert "MECHUS_MEETING_LINK=https://facetime.apple.com/join#v=1" in joined
    assert "MECHUS_ELEVEN_API_KEY=sk-test" in joined
    assert "--env-file" not in joined


def test_display_name_from_join_url_l_param() -> None:
    from mechus.facetime_env import display_name_from_join_url

    url = "https://facetime.apple.com/join#v=1&p=abc&k=def&l=mechus%20test%209"
    assert display_name_from_join_url(url) == "mechus test 9"


def test_page_is_name_lobby_detects_prejoin_screen() -> None:
    from mechus.facetime_env import page_is_name_lobby

    html = '<p class="app-description">Enter your name to join the conversation.</p>'
    html += '<input id="name-entry" value=""><ui-button class="continue-button">'
    assert page_is_name_lobby(html) is True
    assert page_is_name_lobby("leave call microphone mute") is False


def test_facetime_eleven_overrides_respects_non_v3_model() -> None:
    from mechus.facetime_env import facetime_worker_eleven_overrides

    with patch.dict(os.environ, {"MECHUS_ELEVEN_MODEL_ID": "eleven_flash_v2_5"}, clear=False):
        o = facetime_worker_eleven_overrides()
    assert o["MECHUS_ELEVEN_MODEL_ID"] == "eleven_flash_v2_5"
    assert o["MECHUS_ELEVEN_AUDIO_TAGS"] == "0"


def test_facetime_runtime_overrides_are_canonical() -> None:
    from mechus.facetime_env import facetime_worker_runtime_overrides

    o = facetime_worker_runtime_overrides()
    assert o["MECHUS_WAKE_OPENER"] == "1"
    assert o["MECHUS_THINKING_PREROLL_TRANSCRIPT_STT"] == "0"
    assert o["MECHUS_POST_TTS_TRANSCRIPT_WAKE_MUTE_S"] == "0.8"
    assert o["MECHUS_WAKE_COOLDOWN_TRANSCRIPT_STT_S"] == "1.0"
    assert o["MECHUS_TRANSCRIPT_CHUNK_SECONDS"] == "1.8"
    assert o["MECHUS_TRANSCRIPT_END_SILENCE_SECONDS"] == "0.28"


def test_v3_skips_latency_query_param() -> None:
    from mechus.tts import ElevenLabsTTSEngine

    with patch.dict(os.environ, {"MECHUS_ELEVEN_TTS_LATENCY_OPT": "3"}, clear=False):
        eng = ElevenLabsTTSEngine(api_key="k", voice_id="v", model_id="eleven_v3")
    assert eng._latency_query == {}


def test_facetime_tts_smooth_bundle() -> None:
    """Full FaceTime-style env bundle produces correct config."""
    from mechus.config import MechusConfig

    overrides = {
        "MECHUS_JOIN_BACKEND": "facetime_web",
        "MECHUS_ZOOM_STREAM_ATTRIBUTION": "0",
        "MECHUS_DIARIZATION_ASYNC": "1",
        "MECHUS_TRANSCRIPT_WAKE_ENABLED": "1",
        "MECHUS_TTS_SMOOTH": "1",
        "MECHUS_ELEVEN_OUTPUT_FORMAT": "pcm_32000",
    }
    # Remove keys that TTS_SMOOTH auto-sets so we can confirm the preset kicks in.
    for k in ("MECHUS_THINKING_PREROLL", "MECHUS_TTS_FADE_IN_MS", "MECHUS_TTS_FADE_OUT_MS"):
        os.environ.pop(k, None)

    with patch.dict(os.environ, overrides, clear=False):
        cfg = MechusConfig.from_env()

    assert not cfg.zoom_stream_attribution
    assert cfg.diarization_async is True
    assert cfg.tts_smooth is True
    assert cfg.eleven_output_format == "pcm_32000"
    assert cfg.thinking_preroll_enabled is False
