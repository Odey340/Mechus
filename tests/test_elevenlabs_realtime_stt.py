"""ElevenLabs Scribe v2 Realtime STT session + transcriber."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np


def test_realtime_transcriber_commit_segment_with_pcm() -> None:
    from koov.stt import ElevenLabsRealtimeTranscriber

    mock_session = MagicMock()
    mock_session.commit_pcm_segment.return_value = "koov what is the weather"
    mock_session.last_committed_language = "es"

    with patch("koov.elevenlabs_realtime_stt.ElevenLabsRealtimeSTTSession", return_value=mock_session):
        tr = ElevenLabsRealtimeTranscriber(api_key="test-key")
    pcm = (np.zeros(1600, dtype=np.int16)).tobytes()
    out = tr.commit_segment(pcm, 16000)
    assert out.text == "koov what is the weather"
    assert out.language == "es"
    mock_session.commit_pcm_segment.assert_called_once_with(pcm, 16000, timeout_s=2.0)


def test_realtime_transcriber_batch_fallback_on_empty_commit() -> None:
    from koov.stt import ElevenLabsRealtimeTranscriber

    mock_session = MagicMock()
    mock_session.commit_pcm_segment.return_value = ""
    mock_session.last_committed_language = None

    with patch("koov.elevenlabs_realtime_stt.ElevenLabsRealtimeSTTSession", return_value=mock_session):
        tr = ElevenLabsRealtimeTranscriber(api_key="test-key")
    pcm = (np.ones(3200, dtype=np.int16) * 100).tobytes()
    with patch.object(tr, "_batch_transcribe", return_value=type("T", (), {"text": "fallback ok", "language": "en"})()):
        out = tr.commit_segment(pcm, 16000)
    assert out.text == "fallback ok"


def test_realtime_transcriber_one_shot_transcribe() -> None:
    from koov.stt import ElevenLabsRealtimeTranscriber

    mock_session = MagicMock()
    mock_session.commit_pcm_segment.return_value = "hello team"

    with patch("koov.elevenlabs_realtime_stt.ElevenLabsRealtimeSTTSession", return_value=mock_session):
        tr = ElevenLabsRealtimeTranscriber(api_key="test-key")
    pcm = (np.ones(3200, dtype=np.int16) * 100).tobytes()
    out = tr.transcribe_pcm16(pcm, 16000)
    assert out.text == "hello team"
    mock_session.commit_pcm_segment.assert_called()
