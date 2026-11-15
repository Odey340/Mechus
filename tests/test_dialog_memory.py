"""Tests for dialog_memory helpers and retrieval query expansion wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.dialog_memory import (
    build_retrieval_query,
    extract_salient_terms,
    is_dialog_continuation,
    is_prior_exchange_reference,
    is_recent_auditory_query,
    is_vague_followup,
    merge_salient,
    qualifies_as_transcript_followup_without_wake,
)
from mechus.retrieval import retrieve_for_question


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what about that?", True),
        ("same thing", True),
        ("the first point", True),
        ("can you explain that again", True),
        ("what did he mean", True),
        ("earlier one", True),
    ],
)
def test_is_vague_followup_true_table(text: str, expected: bool) -> None:
    assert is_vague_followup(text) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("what did Alice say about the NIH migration", False),
        ("summarize the security concerns about Supabase persistence", False),
        ("who owns sprint 24 follow-ups", False),
        ("explain the first law of thermodynamics", False),
    ],
)
def test_is_vague_followup_false_table(text: str, expected: bool) -> None:
    assert is_vague_followup(text) is expected


def test_extract_salient_nih_sprint_sample() -> None:
    text = 'Discuss "NIH rollout", Sprint 24, H1B issues, and ABC-123. NIH rollout is blocked.'
    t = extract_salient_terms(text, max_terms=8)
    joined = " ".join(x.lower() for x in t)
    assert "nih rollout" in joined
    assert any("sprint 24" in x.lower() for x in t)
    assert any("h1b" in x.lower() for x in t)
    assert any("abc-123" in x.lower() for x in t)
    assert len(t) <= 8
    assert len(t) == len({x.lower() for x in t})


def test_extract_salient_drops_hyphen_noise() -> None:
    t = extract_salient_terms(
        "Schedule follow-ups on Sprint 24; round-up after check-in with follow-up owners."
    )
    low = [x.lower() for x in t]
    assert "follow-ups" not in low
    assert "follow-up" not in low
    assert "check-in" not in low
    assert "round-up" not in low
    assert any("sprint" in x.lower() for x in t)


def test_merge_salient_drops_noise_terms() -> None:
    m = merge_salient(["follow-ups", "NIH"], ["What", "H1B"], max_total=12)
    assert "follow-ups" not in [x.lower() for x in m]
    assert "what" not in [x.lower() for x in m]
    assert "H1B" in m or "h1b" in [x.lower() for x in m]
    assert "NIH" in m


def test_build_retrieval_query_filters_noise_in_salient_terms() -> None:
    q = build_retrieval_query(
        "same thing for H1B?",
        last_user="Who owns Sprint 24 follow-ups?",
        last_mechus=None,
        salient_terms=["Sprint 24", "follow-ups", "more", "H1B"],
    )
    # Verbatim Context user may still contain words from the prior line; Salient line must not repeat noise.
    assert "Salient:" in q
    after_salient = q.split("Salient:", 1)[-1].lower()
    assert "follow-ups" not in after_salient
    assert "more" not in after_salient.split(",")
    assert "h1b" in q.lower()


def test_recap_last_n_minutes_not_vague() -> None:
    assert not is_vague_followup("recap the last five minutes")


def test_extract_salient_skips_interrogatives_and_generic_verbs() -> None:
    t = extract_salient_terms('What were the three blockers? Who owns Sprint 24 follow-ups? Explain "NIH rollout".')
    low = [x.lower() for x in t]
    assert "what" not in low
    assert "who" not in low
    assert "explain" not in low
    assert "nih rollout" in " ".join(low) or any("nih" in x for x in low)
    assert any("sprint 24" in x.lower() or "sprint" in x.lower() for x in t)


def test_merge_salient_order_and_cap() -> None:
    rolling = ["NIH", "Supabase", "Sprint 24"]
    new = ["H1B", "NIH", "ABC-123"]
    m = merge_salient(rolling, new, max_total=12)
    assert m[0] == "H1B"
    assert m[1] == "NIH"
    assert m[2] == "ABC-123"
    assert "Supabase" in m
    assert m.count("NIH") == 1


def test_build_retrieval_query_format_and_cap() -> None:
    q = build_retrieval_query(
        "what about that?",
        last_user="What did Alice say about the NIH rollout blockers?",
        last_mechus="You mentioned three blockers: funding, security review, and ownership.",
        salient_terms=["NIH rollout", "Alice", "security review"],
        max_chars=400,
    )
    assert q.startswith("what about that?")
    assert "Context user:" in q
    assert "Salient:" in q
    assert "NIH rollout" in q or "Alice" in q
    assert len(q) <= 400


def test_dialog_continuation_monitor_thread() -> None:
    assert is_dialog_continuation("can you find some for me")
    assert is_dialog_continuation("can you pull that up for me")
    assert is_dialog_continuation("is that possible")
    assert qualifies_as_transcript_followup_without_wake("is that possible")
    q = build_retrieval_query(
        "can you find some for me",
        last_user="how can i get a better monitor",
        last_mechus="Identify your needs like size, resolution, and refresh rate.",
        salient_terms=["monitor"],
    )
    assert "Context user:" in q
    assert "monitor" in q.lower()


def test_recent_auditory_query() -> None:
    assert is_recent_auditory_query("what did you just hear in the last 10 seconds")


def test_prior_exchange_reference_and_retrieval_query() -> None:
    assert is_prior_exchange_reference("I thought you told me it's sunny")
    assert qualifies_as_transcript_followup_without_wake(
        "I thought you told me it's sunny"
    )
    q = build_retrieval_query(
        "I thought you told me it's sunny",
        last_user="what's the weather in Chennai",
        last_mechus="Yeah, it is sunny in Chennai right now.",
        salient_terms=["Chennai"],
    )
    assert "Context user:" in q
    assert "Context mechus:" in q
    assert "Chennai" in q or "sunny" in q.lower()


def test_build_retrieval_query_preserves_original_only_when_no_extra() -> None:
    q = build_retrieval_query(
        "explain the Supabase persistence concerns in detail",
        last_user=None,
        last_mechus=None,
        salient_terms=[],
    )
    assert q == "explain the Supabase persistence concerns in detail"


def test_retrieve_for_question_uses_retrieval_query_for_memory() -> None:
    mem = MagicMock()
    mem.retrieve = MagicMock(return_value="")

    eng = ContextIntegrityEngine(meeting_id="tq")
    eng.ingest_chunk("Hello about budgets.", speaker_id="A", timestamp=1.0)

    expanded = "tell me about that\nContext user: prior\nSalient: ACME, Budgets"
    retrieve_for_question(
        "tell me about that",
        retrieval_query=expanded,
        salient_terms_used=["ACME", "Budgets"],
        context_engine=eng,
        memory=mem,
        max_chars=4000,
    )
    mem.retrieve.assert_called_once()
    assert mem.retrieve.call_args.kwargs.get("request_text") == expanded


def test_retrieve_for_question_intent_from_request_text_not_expanded() -> None:
    mem = MagicMock()
    mem.retrieve = MagicMock(return_value="")

    eng = ContextIntegrityEngine(meeting_id="tw")
    eng.ingest_chunk("Line one.", speaker_id="A", timestamp=100.0)

    b = retrieve_for_question(
        "recap the last five minutes",
        retrieval_query="something else entirely unrelated",
        context_engine=eng,
        memory=mem,
        meeting_now_ts=200.0,
    )
    assert b.intent == "recap_query"
    assert b.meta.get("retrieval_query_expanded") is True
    assert b.meta.get("vague_followup") is False
    assert b.meta.get("request_text") == "recap the last five minutes"


def test_retrieve_for_question_vague_meta() -> None:
    mem = MagicMock()
    mem.retrieve = MagicMock(return_value="")
    eng = ContextIntegrityEngine(meeting_id="tv")
    eng.ingest_chunk("x", speaker_id="A", timestamp=1.0)
    b = retrieve_for_question("what about that?", context_engine=eng, memory=mem, max_chars=4000)
    assert b.meta.get("vague_followup") is True


def test_regression_specific_questions_not_vague() -> None:
    for s in [
        "summarize sprint 24",
        "what did Bob decide about Supabase",
        "list the action items from the meeting",
    ]:
        assert not is_vague_followup(s), s
