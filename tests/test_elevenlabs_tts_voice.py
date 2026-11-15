"""ElevenLabs TTS invalid-voice detection and fail-fast behavior."""

from __future__ import annotations

import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import numpy as np

from koov.tts import ElevenLabsTTSEngine, _elevenlabs_error_indicates_invalid_voice


def test_invalid_voice_detector_matches_api_payload() -> None:
    body = (
        '{"detail":{"type":"invalid_request","code":"bad_request",'
        '"message":"An invalid ID has been received: \'x\'.","status":"invalid_uid"}}'
    )
    assert _elevenlabs_error_indicates_invalid_voice(400, body) is True
    assert _elevenlabs_error_indicates_invalid_voice(401, body) is False
    assert _elevenlabs_error_indicates_invalid_voice(400, "quota exceeded") is False


def test_synthesize_skips_network_after_invalid_voice() -> None:
    eng = ElevenLabsTTSEngine(api_key="k", voice_id="bad-voice")
    body = b'{"detail":{"status":"invalid_uid","message":"An invalid ID has been received."}}'

    class _BadResp:
        status = 400
        data = body

    with patch("koov.http_pool.http_request", return_value=_BadResp()):
        a = eng.synthesize("hello")
    assert np.asarray(a.audio_f32).size == 0
    assert eng.skip_further_requests() is True

    with patch("koov.http_pool.http_request") as op:
        b = eng.synthesize("again")
    op.assert_not_called()
    assert np.asarray(b.audio_f32).size == 0


def test_flash_voice_settings_default_expressive() -> None:
    eng = ElevenLabsTTSEngine(api_key="k", voice_id="voice-1", model_id="eleven_flash_v2_5")
    settings = eng._voice_settings()
    assert settings["style"] == 0.86
    assert settings["stability"] == 0.12
    assert settings["similarity_boost"] == 0.72
    assert settings["use_speaker_boost"] is True


def test_synthesize_retries_without_latency_opt_on_empty() -> None:
    with patch.dict(os.environ, {"KOOV_ELEVEN_TTS_LATENCY_OPT": "3"}, clear=False):
        eng = ElevenLabsTTSEngine(api_key="k", voice_id="voice-1", model_id="eleven_flash_v2_5")
    pcm = (np.zeros(320, dtype=np.int16)).tobytes()
    calls: list[dict[str, str]] = []

    def _fake_http_request(method, url, *, body=None, headers=None, timeout_s=None, **kwargs):
        qs = parse_qs(urlparse(url).query)
        calls.append({k: v[0] for k, v in qs.items()})

        class _R:
            status = 200
            data = pcm if "optimize_streaming_latency" not in qs else b'{"detail":"bad latency combo"}'

        return _R()

    with patch("koov.http_pool.http_request", side_effect=_fake_http_request):
        out = eng.synthesize("[ready] Yeah, go ahead.")
    assert np.asarray(out.audio_f32).size > 0
    assert any("optimize_streaming_latency" in c for c in calls)
    assert any("optimize_streaming_latency" not in c for c in calls)
