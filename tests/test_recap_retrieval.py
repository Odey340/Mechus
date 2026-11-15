"""Tests for time-window recap, topic-scoped recap, and tail-first generic recap."""

from __future__ import annotations

from unittest.mock import patch

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.intent import INTENT_RECAP, classify_intent
from mechus.retrieval import (
    _extract_freeform_topic_hint,
    _extract_time_window_minutes,
    _recap_scope,
    _recap_uses_standard_path,
    retrieve_for_question,
)


def _ingest(engine: ContextIntegrityEngine, lines: list[tuple[str, str, float]]) -> None:
    for spk, text, ts in lines:
        engine.ingest_chunk(text, speaker_id=spk, timestamp=ts)


def test_time_window_last_five_minutes_filters_old_chunks() -> None:
    """Only chunks from the last N minutes appear after retrieval filter."""
    now = 1_000_000.0
    # Oldest first: 400s ago (outside 5 min), then 120s ago (inside 5 min)
    t_old = now - 400.0
    t_new = now - 120.0
    eng = ContextIntegrityEngine(meeting_id="tw1")
    _ingest(
        eng,
        [
            ("A", "OLDBLOCK early discussion only.", t_old),
            ("B", "RECENTBLOCK only this part matters for the last five minutes.", t_new),
        ],
    )
    with patch("time.time", return_value=now):
        bundle = retrieve_for_question(
            "Mechus recap the last 5 minutes",
            context_engine=eng,
            memory=None,
            max_chars=8000,
            retrieval_mode="lexical",
        )
    assert bundle.intent == INTENT_RECAP
    assert bundle.meta.get("time_window_minutes") == 5.0
    assert bundle.meta.get("recap_scope") == "window"
    ctx = (bundle.formatted_context or "").lower()
    assert "recentblock" in ctx
    assert "oldblock" not in ctx


def test_time_window_uses_explicit_meeting_now_ts_session_clock() -> None:
    """Session-relative chunk timestamps + meeting_now_ts (no time.time patch)."""
    eng = ContextIntegrityEngine(meeting_id="tw_session")
    _ingest(
        eng,
        [
            ("A", "OLDBLOCK early discussion only.", 0.0),
            ("B", "MIDDLEBLOCK middle content.", 400.0),
            ("C", "RECENTBLOCK only this part matters for the last five minutes.", 500.0),
        ],
    )
    bundle = retrieve_for_question(
        "Mechus recap the last 5 minutes",
        context_engine=eng,
        memory=None,
        max_chars=8000,
        retrieval_mode="lexical",
        meeting_now_ts=500.0,
    )
    assert bundle.intent == INTENT_RECAP
    assert bundle.meta.get("time_window_minutes") == 5.0
    assert bundle.meta.get("recap_reference_now_ts") == 500.0
    assert bundle.meta.get("meeting_now_ts") == 500.0
    ctx = (bundle.formatted_context or "").lower()
    assert "recentblock" in ctx
    assert "middleblock" in ctx
    assert "oldblock" not in ctx


def test_topic_scoped_recap_includes_distinctive_token() -> None:
    """Topic-scoped recap uses BM25 path so a rare token in a later chunk is retrievable."""
    eng = ContextIntegrityEngine(meeting_id="tp1")
    t0 = 100.0
    _ingest(
        eng,
        [
            ("A", "We opened with general pleasantries and nothing else.", t0),
            ("B", "Later we discussed WIDGETXTOKEN and agreed to ship it Friday.", t0 + 50.0),
        ],
    )
    bundle = retrieve_for_question(
        "Mechus what did we say about WIDGETXTOKEN",
        context_engine=eng,
        memory=None,
        max_chars=8000,
        retrieval_mode="lexical",
    )
    assert bundle.intent == INTENT_RECAP
    assert bundle.meta.get("recap_scope") == "topic"
    assert bundle.meta.get("recap_use_standard_path") is True
    assert "WIDGETXTOKEN" in (bundle.formatted_context or "")


def test_generic_recap_tail_prefers_recent_when_budget_tight() -> None:
    """Without topic scope, recap path fills from the end; tight budget drops old opening."""
    eng = ContextIntegrityEngine(meeting_id="tail1")
    base = 5000.0
    _ingest(
        eng,
        [
            ("A", "OPENING_ONLY_ZZZ filler " * 30, base),
            ("B", "MIDDLE_YYY filler " * 30, base + 1.0),
            ("C", "FINAL_RECENT_XXX closing remarks here.", base + 2.0),
        ],
    )
    bundle = retrieve_for_question(
        "recap the meeting",
        context_engine=eng,
        memory=None,
        max_chars=800,
        retrieval_mode="lexical",
    )
    assert bundle.intent == INTENT_RECAP
    assert bundle.meta.get("recap_scope") == "full"
    ctx = bundle.formatted_context or ""
    assert "FINAL_RECENT_XXX" in ctx
    assert "OPENING_ONLY_ZZZ" not in ctx


def test_extract_time_window_spoken_numbers_stt() -> None:
    """STT often emits words; must match digit forms for window recaps."""
    assert _extract_time_window_minutes("can you recap the last five minutes for me") == 5.0
    assert _extract_time_window_minutes("recap the last one minute") == 1.0
    assert _extract_time_window_minutes("last two minutes what happened") == 2.0
    assert _extract_time_window_minutes("past ten minutes") == 10.0
    assert _extract_time_window_minutes("last 5 minutes") == 5.0


def test_helpers_freeform_and_scope() -> None:
    assert _extract_freeform_topic_hint("recap what we said about AcmeCorp") == "AcmeCorp"
    assert _recap_uses_standard_path(INTENT_RECAP, "recap", "recap about AcmeCorp") is True
    assert _recap_scope(INTENT_RECAP, 5.0, False) == "window"
    assert _recap_scope(INTENT_RECAP, None, True) == "topic"
    assert _recap_scope(INTENT_RECAP, None, False) == "full"


def test_intent_time_phrases_are_recap() -> None:
    for q in (
        "recap the last 5 mins",
        "past 10 minutes what happened",
        "recap what we said about the budget",
    ):
        assert classify_intent(q) == INTENT_RECAP
