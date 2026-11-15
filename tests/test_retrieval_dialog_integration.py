"""Integration: original request drives intent; expanded query drives memory + chunks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.retrieval import retrieve_for_question


@patch("mechus.retrieval.classify_intent", return_value="general_query")
def test_split_original_vs_expanded(mock_ci: MagicMock) -> None:
    mem = MagicMock()
    mem.retrieve = MagicMock(return_value="")

    eng = ContextIntegrityEngine(meeting_id="int_split")
    eng.ingest_chunk("NIH budget discussion", speaker_id="A", timestamp=1.0)

    expanded = (
        "what about that?\n"
        "Context user: NIH rollout blockers\n"
        "Salient: Alice, NIH rollout"
    )

    with patch.object(
        ContextIntegrityEngine,
        "get_relevant_chunks",
        return_value=("", [], []),
    ) as mock_chunks:
        b = retrieve_for_question(
            "what about that?",
            retrieval_query=expanded,
            context_engine=eng,
            memory=mem,
            max_chars=4000,
            retrieval_extra_meta={"cache_skipped": True},
        )

    mock_ci.assert_called_once_with("what about that?")
    mem.retrieve.assert_called_once_with(request_text=expanded)
    mock_chunks.assert_called_once()
    first_arg = mock_chunks.call_args[0][0]
    assert first_arg == expanded
    assert b.meta.get("cache_skipped") is True
    assert b.meta.get("request_text") == "what about that?"
    assert b.meta.get("retrieval_effective_query") == expanded


@patch("mechus.retrieval.classify_intent", return_value="recap_query")
def test_freeform_topic_hint_from_original_request_only(mock_ci: MagicMock) -> None:
    """Scoped recap hint (about X) must come from request_text, not retrieval_query."""
    mem = MagicMock()
    mem.retrieve = MagicMock(return_value="")

    eng = ContextIntegrityEngine(meeting_id="int_ff")
    eng.ingest_chunk("We discussed budgets and NIH.", speaker_id="A", timestamp=1.0)

    with patch.object(
        ContextIntegrityEngine,
        "get_relevant_chunks",
        return_value=("", [], []),
    ) as mock_chunks:
        retrieve_for_question(
            "what did we say about budgets",
            retrieval_query="noise unrelated query without budget words",
            context_engine=eng,
            memory=mem,
            max_chars=4000,
        )

    mock_ci.assert_called_once_with("what did we say about budgets")
    first_arg = mock_chunks.call_args[0][0]
    assert "budget" in first_arg.lower() or "budgets" in first_arg.lower()
