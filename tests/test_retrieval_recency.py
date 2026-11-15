"""Recency bias and dialog anchoring in retrieval."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.retrieval import retrieve_for_question


@patch("mechus.retrieval.classify_intent", return_value="general_query")
def test_general_retrieval_prefers_recent_chunks(mock_ci: MagicMock) -> None:
    eng = ContextIntegrityEngine(meeting_id="recency")
    eng.ingest_chunk("Early topic Innichen weather in India.", speaker_id="A", timestamp=1.0)
    eng.ingest_chunk("Middle unrelated chatter.", speaker_id="B", timestamp=2.0)
    eng.ingest_chunk("Latest Chennai weather is sunny with warnings.", speaker_id="A", timestamp=3.0)

    b = retrieve_for_question(
        "what about Chennai now",
        context_engine=eng,
        memory=MagicMock(retrieve=MagicMock(return_value="")),
        max_chars=4000,
        dialog_last_user="weather in Chennai",
        dialog_last_mechus="It is sunny in Chennai.",
    )

    ctx = b.formatted_context.lower()
    assert "chennai" in ctx
    assert b.meta.get("ensure_recent_tail_chunks", 0) >= 4
    mock_ci.assert_called_once()


@patch("mechus.retrieval.classify_intent", return_value="general_query")
def test_last_ten_seconds_uses_recent_window(mock_ci: MagicMock) -> None:
    eng = ContextIntegrityEngine(meeting_id="ten_sec")
    eng.ingest_chunk("Old weather in India.", speaker_id="A", timestamp=1.0)
    eng.ingest_chunk("FIFA 2026 free access tips.", speaker_id="A", timestamp=50.0)
    eng.ingest_chunk("4K monitor with 240 refresh rate.", speaker_id="A", timestamp=98.0)

    b = retrieve_for_question(
        "what did you just hear in the last 10 seconds",
        context_engine=eng,
        memory=MagicMock(retrieve=MagicMock(return_value="")),
        meeting_now_ts=100.0,
        max_chars=4000,
    )

    ctx = b.formatted_context.lower()
    assert "monitor" in ctx or "240" in ctx or "4k" in ctx
    assert b.meta.get("time_window_seconds") == 10.0
    assert "weather" not in ctx or "india" not in ctx


@patch("mechus.retrieval.classify_intent", return_value="general_query")
def test_followup_includes_current_exchange(mock_ci: MagicMock) -> None:
    eng = ContextIntegrityEngine(meeting_id="exchange")
    eng.ingest_chunk("Some meeting line.", speaker_id="A", timestamp=1.0)

    b = retrieve_for_question(
        "what about that?",
        retrieval_query="what about that?\nContext user: Chennai weather",
        context_engine=eng,
        memory=MagicMock(retrieve=MagicMock(return_value="")),
        max_chars=4000,
        retrieval_extra_meta={"wake_source": "transcript_followup"},
        dialog_last_user="what's the weather in Chennai",
        dialog_last_mechus="Sunny in Chennai with outage warnings.",
    )

    assert "Current exchange" in b.formatted_context
    assert "Chennai" in b.formatted_context
    assert b.meta.get("prior_exchange_reference") is False
    assert b.meta.get("vague_followup") is True
