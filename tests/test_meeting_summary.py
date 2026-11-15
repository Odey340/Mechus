"""Tests for the meeting summary pipeline.

Covers:
- generate_final_summary (state-only path, no OpenAI key needed)
- title generation (LLM path mocked + _infer_title_from_topics fallback)
- write_final_summary + read_final_summary round-trip
- generated_at ISO format
- merge_final_report_into_summary
- _fallback_summary_from_state (action items / decisions / open questions)
- list_past_meetings via MechusAgent.list_past_meetings
- Sample meeting scenario (product launch + hiring discussion)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mechus.meeting_summary import (
    _dedupe_strings,
    _even_time_sample,
    _fallback_summary_from_state,
    _infer_title_from_topics,
    _merge_action_item_sources,
    _normalize_action_items,
    _normalize_decisions,
    _normalize_questions,
    build_summary_evidence_text,
    extract_action_items_with_llm,
    generate_final_summary,
    is_executive_summary_raw_transcript_dump,
    merge_final_report_into_summary,
    read_final_summary,
    write_final_summary,
)
from mechus.meeting_store import StoredChunk


# ---------------------------------------------------------------------------
# Sample meeting fixtures
# ---------------------------------------------------------------------------

SAMPLE_DECISIONS = [
    {"id": "d1", "value": "Launch the v2 API on July 15", "rationale": "Team agreed on two-week buffer", "source": "state"},
    {"id": "d2", "value": "Hire two backend engineers by Q3", "rationale": None, "source": "state"},
]

SAMPLE_ACTION_ITEMS = [
    {"owner": "Alex", "task": "finalize API contract document", "deadline": "2026-07-01", "source": "state", "confidence": 0.95},
    {"owner": "Priya", "task": "schedule candidate interviews", "deadline": None, "source": "state", "confidence": 0.9},
    {"owner": "Marcus", "task": "update staging environment for v2", "deadline": "2026-06-25", "source": "state", "confidence": 0.85},
]

SAMPLE_OPEN_QUESTIONS = [
    "Who will handle customer migration from v1 to v2?",
    "Do we need a rollback plan for the launch?",
]

SAMPLE_OPEN_ISSUES = [
    {"display_summary": q, "status": "open", "topic_key": f"q{i}"}
    for i, q in enumerate(SAMPLE_OPEN_QUESTIONS)
]

SAMPLE_STATE = {
    "decisions": SAMPLE_DECISIONS,
    "action_items": SAMPLE_ACTION_ITEMS,
    "open_issues": SAMPLE_OPEN_ISSUES,
}

SAMPLE_TRANSCRIPT = """
[00:01] Alex: Let's kick off. Main agenda: v2 API launch timeline and hiring.
[00:45] Alex: I think July 15 works if we lock in the API contract by July 1.
[01:10] Priya: Agreed. I'll handle the contract document.
[01:30] Marcus: Staging needs to be ready before that — I can get it done by June 25.
[02:00] Alex: Great. On hiring — we need two backend engineers.
[02:20] Priya: I can start scheduling interviews this week.
[03:00] Alex: One thing we didn't nail down — who handles the v1 migration?
[03:15] Marcus: Good point. Let's revisit that next week.
[03:45] Alex: Do we need a rollback plan? Let's think about it.
[04:00] Alex: Ok, wrapping up. July 15 launch, Priya on contracts, Marcus on staging, hiring in progress.
"""


class TestMergeActionItemSources:
    def test_unions_and_dedupes(self) -> None:
        llm = [{"owner": "Alex", "task": "finalize API contract", "deadline": None, "confidence": 0.8}]
        state = [{"owner": "Alex", "task": "finalize API contract", "deadline": "July 1", "source": "state"}]
        report = [("Priya", "schedule interviews", "this week", 0.0)]
        merged = _merge_action_item_sources(llm, state, report)
        assert len(merged) == 2
        alex = next(a for a in merged if a["owner"] == "Alex")
        assert alex["deadline"] == "July 1"
        assert merged[0]["owner"] == "Alex"  # assigned before unassigned-ish ordering

    def test_assignee_and_due_date_aliases(self) -> None:
        out = _normalize_action_items([
            {"assignee": "Sam", "action": "send the deck", "due": "Friday"},
        ])
        assert out[0]["owner"] == "Sam"
        assert out[0]["assignee"] == "Sam"
        assert out[0]["deadline"] == "Friday"
        assert out[0]["due_date"] == "Friday"


class TestExtractActionItemsLLM:
    @pytest.fixture
    def mock_extract(self):
        import urllib.request

        payload = {
            "action_items": [
                {
                    "owner": "Marcus",
                    "task": "update staging for v2",
                    "deadline": "June 25",
                    "confidence": 0.9,
                }
            ]
        }
        fake_body = json.dumps({
            "choices": [{"message": {"content": json.dumps(payload)}}]
        }).encode("utf-8")

        class _FakeResp:
            def read(self):
                return fake_body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            yield

    def test_extract_returns_normalized(self, mock_extract) -> None:
        items = extract_action_items_with_llm(
            SAMPLE_TRANSCRIPT,
            api_key="sk-test",
            participant_names=["Alex", "Priya", "Marcus"],
        )
        assert len(items) == 1
        assert items[0]["owner"] == "Marcus"
        assert items[0]["deadline"] == "June 25"


# ---------------------------------------------------------------------------
# _infer_title_from_topics
# ---------------------------------------------------------------------------

class TestInferTitle:
    def test_single_topic(self) -> None:
        title = _infer_title_from_topics(["pricing tiers"], None)
        assert title == "Pricing Tiers"

    def test_two_topics_joined(self) -> None:
        title = _infer_title_from_topics(["launch planning", "hiring"], None)
        assert "+" in title or "and" in title.lower() or "Launch" in title

    def test_fallback_to_exec_summary(self) -> None:
        title = _infer_title_from_topics(None, "We discussed the Q3 roadmap and hiring plans.")
        assert len(title) > 0
        assert title != "Meeting"

    def test_long_exec_summary_trimmed(self) -> None:
        long_summary = "A" * 200
        title = _infer_title_from_topics(None, long_summary)
        assert len(title) <= 63  # 60 + ellipsis

    def test_empty_topics_falls_to_exec(self) -> None:
        title = _infer_title_from_topics([], "Launch strategy was the main topic.")
        # The function strips the trailing period in the trimming path
        assert "Launch strategy" in title

    def test_no_inputs_returns_meeting(self) -> None:
        title = _infer_title_from_topics(None, None)
        assert title == "Meeting"

    def test_empty_executive_returns_meeting(self) -> None:
        title = _infer_title_from_topics([], "")
        assert title == "Meeting"


# ---------------------------------------------------------------------------
# _fallback_summary_from_state
# ---------------------------------------------------------------------------

class TestFallbackSummary:
    def test_decisions_included_in_exec(self) -> None:
        result = _fallback_summary_from_state(SAMPLE_DECISIONS, [], [])
        assert "v2 API" in result["executive_summary"] or "decision" in result["executive_summary"].lower()
        assert result["source"] == "fallback"

    def test_action_items_reflected(self) -> None:
        result = _fallback_summary_from_state([], SAMPLE_ACTION_ITEMS, [])
        assert result["action_items"]
        assert result["source"] == "fallback"

    def test_open_questions_in_output(self) -> None:
        result = _fallback_summary_from_state([], [], SAMPLE_OPEN_QUESTIONS)
        assert result["open_questions"] == SAMPLE_OPEN_QUESTIONS

    def test_no_inputs_returns_stub(self) -> None:
        result = _fallback_summary_from_state([], [], [], transcript_text=SAMPLE_TRANSCRIPT)
        assert result["executive_summary"]
        assert not is_executive_summary_raw_transcript_dump(result["executive_summary"])
        assert "summary from transcript" not in result["executive_summary"].lower()

    def test_title_present(self) -> None:
        result = _fallback_summary_from_state(SAMPLE_DECISIONS, SAMPLE_ACTION_ITEMS, SAMPLE_OPEN_QUESTIONS)
        assert "title" in result
        assert result["title"]


# ---------------------------------------------------------------------------
# Transcript dump detection + sanitization
# ---------------------------------------------------------------------------

class TestTranscriptDumpGuard:
    def test_detects_timestamp_transcript(self) -> None:
        raw = (
            "[00:01] Alex: Let's kick off.\n"
            "[00:45] Priya: Agreed.\n"
            "[01:10] Marcus: Staging needs to be ready."
        )
        assert is_executive_summary_raw_transcript_dump(raw)

    def test_detects_legacy_stub_prefix(self) -> None:
        assert is_executive_summary_raw_transcript_dump(
            "Summary from transcript (LLM unavailable): Alex said hello"
        )

    def test_real_summary_not_flagged(self) -> None:
        assert not is_executive_summary_raw_transcript_dump(
            "The team aligned on a July 15 API launch. Priya owns the contract; "
            "Marcus will ready staging by June 25. Hiring two backend engineers remains open."
        )

    def test_write_final_summary_sanitizes_transcript_dump(self, tmp_path) -> None:
        meeting_id = "dump-guard-001"
        bad = {
            "title": "Meeting",
            "executive_summary": (
                "Summary from transcript (LLM unavailable): "
                + " ".join(SAMPLE_TRANSCRIPT.split())
            ),
            "action_items": [],
            "decisions": [],
            "open_questions": [],
            "source": "state_only",
        }
        write_final_summary(meeting_id, bad, base_dir=str(tmp_path))
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        assert not is_executive_summary_raw_transcript_dump(loaded["executive_summary"])
        assert "summary from transcript" not in loaded["executive_summary"].lower()


# ---------------------------------------------------------------------------
# generate_final_summary — state-only (no OpenAI key)
# ---------------------------------------------------------------------------

class TestGenerateFinalSummaryStateOnly:
    def test_state_mode_returns_valid_shape(self) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            mode="state",
        )
        assert "executive_summary" in result
        assert isinstance(result["action_items"], list)
        assert isinstance(result["decisions"], list)
        assert isinstance(result["open_questions"], list)
        assert result["source"] == "state_only"

    def test_state_mode_has_title(self) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            mode="state",
        )
        assert "title" in result
        assert result["title"]

    def test_no_api_key_falls_back_to_state(self) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="",
        )
        assert result["source"] in ("state_only", "fallback", "llm", "llm_fast")

    def test_action_items_in_state_mode(self) -> None:
        result = generate_final_summary(
            transcript_text="",
            state=SAMPLE_STATE,
            mode="state",
        )
        assert len(result["action_items"]) == len(SAMPLE_ACTION_ITEMS)
        owners = {a["owner"] for a in result["action_items"]}
        assert "Alex" in owners
        assert "Priya" in owners
        assert "Marcus" in owners


# ---------------------------------------------------------------------------
# generate_final_summary — mocked LLM response
# ---------------------------------------------------------------------------

_MOCK_LLM_RESPONSE = {
    "title": "v2 API Launch + Hiring Planning",
    "executive_summary": (
        "The team locked in July 15 for the v2 API launch, with Alex handling the contract by July 1 "
        "and Marcus updating staging by June 25. Hiring two backend engineers by Q3 was confirmed, "
        "with Priya starting interviews this week. Key open items include who manages the v1 migration "
        "and whether a rollback plan is needed."
    ),
    "action_items": [
        {"owner": "Alex", "task": "finalize API contract document", "deadline": "2026-07-01", "source": "transcript", "confidence": 0.95},
        {"owner": "Priya", "task": "schedule candidate interviews", "deadline": None, "source": "transcript", "confidence": 0.9},
        {"owner": "Marcus", "task": "update staging environment for v2", "deadline": "2026-06-25", "source": "transcript", "confidence": 0.85},
    ],
    "decisions": [
        {"id": "d1", "value": "Launch v2 API on July 15", "rationale": "Two-week buffer agreed", "source": "transcript"},
        {"id": "d2", "value": "Hire two backend engineers by Q3", "rationale": None, "source": "transcript"},
    ],
    "open_questions": [
        "Who will handle customer migration from v1 to v2?",
        "Do we need a rollback plan for the launch?",
    ],
    "risks": ["v1 migration may slip if ownership not assigned soon"],
    "topics": ["v2 API launch timeline", "backend hiring", "staging environment", "v1 migration risk"],
    "meeting_tone": "mostly aligned, one open ownership gap",
}


def _build_mock_openai_response(content: dict) -> dict:
    """Build a fake OpenAI chat completion response dict."""
    return {
        "choices": [
            {"message": {"content": json.dumps(content)}}
        ]
    }


class TestGenerateFinalSummaryLLM:
    """Mock urllib.request.urlopen so we never hit OpenAI in CI."""

    @pytest.fixture
    def mock_openai(self):
        import io
        import urllib.request

        fake_body = json.dumps(_build_mock_openai_response(_MOCK_LLM_RESPONSE)).encode("utf-8")

        class _FakeResp:
            def read(self):
                return fake_body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            yield

    def test_llm_title_propagated(self, mock_openai) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="sk-test-fake",
        )
        assert result.get("title") == "v2 API Launch + Hiring Planning"

    def test_llm_exec_summary_present(self, mock_openai) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="sk-test-fake",
        )
        assert "July 15" in result["executive_summary"]

    def test_llm_transcript_dump_replaced(self) -> None:
        import urllib.request

        dump = dict(_MOCK_LLM_RESPONSE)
        dump["executive_summary"] = SAMPLE_TRANSCRIPT.strip()
        fake_body = json.dumps(_build_mock_openai_response(dump)).encode("utf-8")

        class _FakeResp:
            def read(self):
                return fake_body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = generate_final_summary(
                transcript_text=SAMPLE_TRANSCRIPT,
                state=SAMPLE_STATE,
                api_key="sk-test-fake",
            )
        assert not is_executive_summary_raw_transcript_dump(result["executive_summary"])
        assert "[00:01]" not in result["executive_summary"]

    def test_llm_action_items_shape(self, mock_openai) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="sk-test-fake",
        )
        assert len(result["action_items"]) == 3
        for item in result["action_items"]:
            assert "owner" in item
            assert "task" in item

    def test_llm_risks_and_topics(self, mock_openai) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="sk-test-fake",
        )
        assert "risks" in result
        assert isinstance(result.get("topics"), list)

    def test_prior_session_context_included(self, mock_openai) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            api_key="sk-test-fake",
            prior_sessions_context="Last session: agreed to delay launch to Q3 pending API review.",
        )
        assert result.get("title")
        assert result.get("executive_summary")


# ---------------------------------------------------------------------------
# write_final_summary / read_final_summary round-trip
# ---------------------------------------------------------------------------

class TestWriteReadSummary:
    def test_round_trip(self, tmp_path) -> None:
        meeting_id = "test-meeting-001"
        summary = {
            "title": "Product Roadmap Q3",
            "executive_summary": "We discussed and locked in the Q3 roadmap.",
            "action_items": [{"owner": "Alex", "task": "write spec", "deadline": None}],
            "decisions": [{"id": "d1", "value": "Launch in August"}],
            "open_questions": ["Budget approved?"],
            "source": "llm",
        }
        path = write_final_summary(meeting_id, summary, base_dir=str(tmp_path))
        assert os.path.isfile(path)
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded["title"] == "Product Roadmap Q3"
        assert loaded["meeting_id"] == meeting_id

    def test_generated_at_is_iso_string(self, tmp_path) -> None:
        meeting_id = "test-meeting-002"
        summary = {"title": "Test", "executive_summary": "x", "action_items": [], "decisions": [], "open_questions": [], "source": "state_only"}
        write_final_summary(meeting_id, summary, base_dir=str(tmp_path))
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        gen_at = loaded.get("generated_at", "")
        # ISO 8601 UTC format: 2026-06-13T...Z
        assert "T" in gen_at and gen_at.endswith("Z"), f"generated_at not ISO: {gen_at!r}"

    def test_generated_at_unix_also_present(self, tmp_path) -> None:
        meeting_id = "test-meeting-003"
        summary = {"title": "Test", "executive_summary": "x", "action_items": [], "decisions": [], "open_questions": [], "source": "state_only"}
        write_final_summary(meeting_id, summary, base_dir=str(tmp_path))
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        unix_ts = loaded.get("generated_at_unix")
        assert isinstance(unix_ts, float) and unix_ts > 1_700_000_000

    def test_meeting_link_stamped(self, tmp_path) -> None:
        meeting_id = "test-meeting-004"
        summary = {"title": "Test", "executive_summary": "x", "action_items": [], "decisions": [], "open_questions": [], "source": "state_only"}
        write_final_summary(meeting_id, summary, base_dir=str(tmp_path), meeting_link="https://zoom.us/j/12345?pwd=abc")
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.get("meeting_link") == "https://zoom.us/j/12345?pwd=abc"
        assert loaded.get("link_key")

    def test_read_missing_returns_none(self, tmp_path) -> None:
        result = read_final_summary("nonexistent-id", base_dir=str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# merge_final_report_into_summary
# ---------------------------------------------------------------------------

class TestMergeFinalReport:
    def _make_report(self) -> Any:
        report = MagicMock()
        report.executive_summary = "Fallback exec summary from report."
        report.final_decisions = ["Decision A from report"]
        report.unresolved_questions = ["Is the deploy safe?"]
        report.action_items_table = [("Alice", "Deploy hotfix", "June 20", 1718000000.0)]
        report.contradictions_identified = []
        report.risks_identified = ["Memory leak possible"]
        report.confidence_score = 0.82
        report.confidence_reasoning = "Strong transcript coverage"
        return report

    def test_merge_fills_empty_summary(self) -> None:
        empty_summary = {
            "executive_summary": "",
            "action_items": [],
            "decisions": [],
            "open_questions": [],
            "source": "fallback",
        }
        result = merge_final_report_into_summary(empty_summary, self._make_report())
        assert result["executive_summary"] == "Fallback exec summary from report."
        assert result["decisions"]
        assert result["action_items"]

    def test_merge_appends_open_questions(self) -> None:
        summary = {
            "executive_summary": "We discussed things.",
            "action_items": [],
            "decisions": [],
            "open_questions": ["Will the rollout work?"],
            "source": "llm",
        }
        result = merge_final_report_into_summary(summary, self._make_report())
        assert "Is the deploy safe?" in result["open_questions"]
        assert "Will the rollout work?" in result["open_questions"]

    def test_merge_does_not_overwrite_good_llm_summary(self) -> None:
        summary = {
            "executive_summary": "LLM generated a good summary with detail.",
            "action_items": [{"owner": "Bob", "task": "write docs", "deadline": None}],
            "decisions": [{"id": "d1", "value": "Ship it"}],
            "open_questions": [],
            "source": "llm",
        }
        result = merge_final_report_into_summary(summary, self._make_report())
        assert result["executive_summary"] == "LLM generated a good summary with detail."
        assert result["decisions"] == [{"id": "d1", "value": "Ship it"}]

    def test_merge_none_report_noop(self) -> None:
        summary = {"executive_summary": "Fine.", "source": "llm"}
        result = merge_final_report_into_summary(summary, None)
        assert result == summary

    def test_extras_always_added(self) -> None:
        summary = {"executive_summary": "x", "action_items": [], "decisions": [], "open_questions": [], "source": "llm"}
        result = merge_final_report_into_summary(summary, self._make_report())
        assert "final_report_extras" in result
        assert result["final_report_extras"]["confidence_score"] == 0.82


# ---------------------------------------------------------------------------
# Full sample meeting pipeline (state-only, no network)
# ---------------------------------------------------------------------------

class TestFullSampleMeetingPipeline:
    """End-to-end test of the write → generate → read cycle using a realistic
    product-team meeting transcript. No OpenAI key required."""

    def test_sample_meeting_write_read_roundtrip(self, tmp_path) -> None:
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            mode="state",
        )
        assert result["title"]
        assert result["executive_summary"]
        assert len(result["action_items"]) == len(SAMPLE_ACTION_ITEMS)

        meeting_id = "sample-product-launch-001"
        path = write_final_summary(
            meeting_id,
            result,
            base_dir=str(tmp_path),
            meeting_link="https://zoom.us/j/99999",
        )
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))

        assert loaded is not None
        assert loaded["title"]
        assert loaded["meeting_id"] == meeting_id
        assert "T" in loaded["generated_at"] and loaded["generated_at"].endswith("Z")
        assert loaded["meeting_link"] == "https://zoom.us/j/99999"
        assert loaded["link_key"]
        assert loaded["action_items"]
        assert loaded["decisions"]
        assert loaded["open_questions"]

    def test_list_past_meetings_returns_title(self, tmp_path) -> None:
        """MechusAgent.list_past_meetings should expose the title field."""
        from mechus.agent import MechusAgent

        # Write two sample summaries
        for i, title in enumerate(["Q3 Roadmap Review", "Hiring Kickoff"], start=1):
            mid = f"sample-meeting-{i:03d}"
            summary = {
                "title": title,
                "executive_summary": f"Summary for meeting {i}.",
                "action_items": [],
                "decisions": [],
                "open_questions": [],
                "source": "llm",
            }
            write_final_summary(mid, summary, base_dir=str(tmp_path))

        with patch.dict(os.environ, {"MECHUS_MEETING_STORE_DIR": str(tmp_path)}):
            meetings = MechusAgent.list_past_meetings()

        assert len(meetings) == 2
        titles = {m.get("title") for m in meetings}
        assert "Q3 Roadmap Review" in titles
        assert "Hiring Kickoff" in titles

    def test_list_past_meetings_excludes_current(self, tmp_path) -> None:
        from mechus.agent import MechusAgent

        for i in range(3):
            mid = f"meeting-{i:03d}"
            summary = {
                "title": f"Meeting {i}",
                "executive_summary": f"Summary {i}.",
                "action_items": [],
                "decisions": [],
                "open_questions": [],
                "source": "state_only",
            }
            write_final_summary(mid, summary, base_dir=str(tmp_path))

        with patch.dict(os.environ, {"MECHUS_MEETING_STORE_DIR": str(tmp_path)}):
            meetings = MechusAgent.list_past_meetings(exclude_meeting_id="meeting-001")

        ids = {m["meeting_id"] for m in meetings}
        assert "meeting-001" not in ids
        assert "meeting-000" in ids
        assert "meeting-002" in ids

    def test_interactions_field_accepted_by_write(self, tmp_path) -> None:
        """Dashboard expects final_summary to include an 'interactions' key."""
        meeting_id = "sample-with-interactions"
        result = generate_final_summary(
            transcript_text=SAMPLE_TRANSCRIPT,
            state=SAMPLE_STATE,
            mode="state",
        )
        result["interactions"] = [
            {"user_query": "What did we decide?", "mechus_response": "July 15 launch.", "ts": time.time()}
        ]
        write_final_summary(meeting_id, result, base_dir=str(tmp_path))
        loaded = read_final_summary(meeting_id, base_dir=str(tmp_path))
        assert loaded is not None
        assert "interactions" in loaded
        assert loaded["interactions"][0]["user_query"] == "What did we decide?"


# ---------------------------------------------------------------------------
# Even time sampling (no end-bias) — the core "don't focus on the end" fix
# ---------------------------------------------------------------------------

class TestEvenTimeSample:
    def test_short_returns_all(self) -> None:
        lines = ["line one", "line two", "line three"]
        out = _even_time_sample(lines, budget=10_000)
        assert out == "\n".join(lines)

    def test_zero_budget_returns_empty(self) -> None:
        assert _even_time_sample(["a", "b"], budget=0) == ""

    def test_always_keeps_first_and_last(self) -> None:
        lines = [f"line-{i:03d}" for i in range(200)]
        out = _even_time_sample(lines, budget=300)
        assert "line-000" in out
        assert "line-199" in out

    def test_covers_whole_timeline_not_just_end(self) -> None:
        # 300 lines; force heavy sampling. Beginning, middle, and end must all appear.
        lines = [f"utterance number {i:04d} talking about topic {i}" for i in range(300)]
        out = _even_time_sample(lines, budget=2000)
        # Beginning
        assert "0000" in out
        # Middle (~150)
        assert any(f"{m:04d}" in out for m in range(140, 160))
        # End
        assert "0299" in out

    def test_respects_budget(self) -> None:
        lines = [f"line-{i:04d}-padding-padding" for i in range(500)]
        out = _even_time_sample(lines, budget=1500)
        assert len(out) <= 1600  # budget + small marker overhead

    def test_marks_skipped_spans(self) -> None:
        lines = [f"line-{i:04d}" for i in range(300)]
        out = _even_time_sample(lines, budget=400)
        assert "…" in out


class TestBuildEvidenceEvenCoverage:
    def _long_meeting_chunks(self, n: int = 400) -> list:
        chunks = []
        for i in range(n):
            # Mark the opening as a key decision so we can verify it survives
            tags = ["decision"] if i == 2 else []
            text = f"At minute {i}, speaker discussed agenda item {i} in detail with specifics."
            if i == 2:
                text = "We decided to launch the product on August 1st — that's locked in."
            chunks.append(StoredChunk(ts=float(i), speaker_id=f"S{i % 3}", text=text, chunk_id=i, tags=tags))
        return chunks

    def test_long_meeting_includes_opening_and_end(self) -> None:
        chunks = self._long_meeting_chunks(400)
        out = build_summary_evidence_text(chunks, max_chars=3000)
        # Opening decision must survive (it's salient)
        assert "August 1st" in out
        # The end must appear too
        assert "minute 399" in out or "399" in out

    def test_even_section_header_present(self) -> None:
        chunks = self._long_meeting_chunks(400)
        out = build_summary_evidence_text(chunks, max_chars=3000)
        assert "even coverage" in out.lower() or "full meeting sample" in out.lower()

    def test_middle_not_dropped(self) -> None:
        chunks = self._long_meeting_chunks(400)
        out = build_summary_evidence_text(chunks, max_chars=4000)
        # Some middle minute should appear (not just head + tail)
        assert any(f"minute {m}" in out for m in range(180, 220))


# ---------------------------------------------------------------------------
# Action item normalization & dedup (the "best as possible" quality layer)
# ---------------------------------------------------------------------------

class TestNormalizeActionItems:
    def test_empty_and_junk_dropped(self) -> None:
        items = [
            {"owner": "Alex", "task": ""},
            {"owner": "Bob", "task": "it"},
            {"owner": "Cara", "task": "do it"},
            {"owner": "Dee", "task": "tbd"},
            {"owner": "Eve", "task": "ship the v2 API"},
        ]
        out = _normalize_action_items(items)
        tasks = [a["task"] for a in out]
        assert tasks == ["ship the v2 API"]

    def test_owner_defaults_to_unassigned(self) -> None:
        out = _normalize_action_items([
            {"owner": "", "task": "write the spec"},
            {"owner": "someone", "task": "review the PR"},
            {"owner": "TBD", "task": "deploy to staging"},
        ])
        assert all(a["owner"] == "Unassigned" for a in out)

    def test_deadline_placeholders_become_none(self) -> None:
        out = _normalize_action_items([
            {"owner": "Alex", "task": "write spec", "deadline": "TBD"},
            {"owner": "Bob", "task": "review PR", "deadline": "none"},
            {"owner": "Cara", "task": "deploy", "deadline": "2026-07-01"},
        ])
        by_owner = {a["owner"]: a for a in out}
        assert by_owner["Alex"]["deadline"] is None
        assert by_owner["Bob"]["deadline"] is None
        assert by_owner["Cara"]["deadline"] == "2026-07-01"

    def test_strips_leading_modal(self) -> None:
        out = _normalize_action_items([
            {"owner": "Alex", "task": "will finalize the contract"},
            {"owner": "Bob", "task": "to update the docs"},
            {"owner": "Cara", "task": "needs to schedule interviews"},
        ])
        tasks = {a["owner"]: a["task"] for a in out}
        assert tasks["Alex"] == "finalize the contract"
        assert tasks["Bob"] == "update the docs"
        assert tasks["Cara"] == "schedule interviews"

    def test_dedup_same_owner_and_task(self) -> None:
        out = _normalize_action_items([
            {"owner": "Alex", "task": "finalize the API contract", "confidence": 0.7},
            {"owner": "Alex", "task": "Finalize the API contract.", "confidence": 0.9},
        ])
        assert len(out) == 1
        # Keeps the higher-confidence one
        assert out[0]["confidence"] == 0.9

    def test_dedup_prefers_entry_with_deadline(self) -> None:
        out = _normalize_action_items([
            {"owner": "Alex", "task": "finalize contract", "deadline": None, "confidence": 0.95},
            {"owner": "Alex", "task": "finalize contract", "deadline": "2026-07-01", "confidence": 0.6},
        ])
        assert len(out) == 1
        assert out[0]["deadline"] == "2026-07-01"

    def test_accepts_plain_strings(self) -> None:
        out = _normalize_action_items(["ship the release", "update the changelog"])
        assert len(out) == 2
        assert all(a["owner"] == "Unassigned" for a in out)

    def test_assigned_sorted_before_unassigned(self) -> None:
        out = _normalize_action_items([
            {"owner": "Unassigned", "task": "investigate the latency spike", "confidence": 0.9},
            {"owner": "Priya", "task": "send the deck", "confidence": 0.8},
        ])
        assert out[0]["owner"] == "Priya"
        assert out[1]["owner"] == "Unassigned"

    def test_task_equal_to_owner_dropped(self) -> None:
        out = _normalize_action_items([{"owner": "Marcus", "task": "Marcus"}])
        assert out == []

    def test_alternate_field_names(self) -> None:
        out = _normalize_action_items([
            {"assignee": "Sam", "action": "draft the rollout plan", "due": "Friday"},
        ])
        assert len(out) == 1
        assert out[0]["owner"] == "Sam"
        assert out[0]["task"] == "draft the rollout plan"
        assert out[0]["deadline"] == "Friday"

    def test_non_list_returns_empty(self) -> None:
        assert _normalize_action_items(None) == []
        assert _normalize_action_items("nope") == []


class TestNormalizeDecisions:
    def test_dedup_and_clean(self) -> None:
        out = _normalize_decisions([
            {"value": "Launch on July 15"},
            {"value": "Launch on July 15."},
            "Hire two engineers",
        ])
        assert len(out) == 2
        assert out[0]["value"] == "Launch on July 15"
        assert out[1]["value"] == "Hire two engineers"

    def test_rationale_preserved(self) -> None:
        out = _normalize_decisions([
            {"value": "Use Postgres", "rationale": "Better for relational data"},
        ])
        assert out[0]["rationale"] == "Better for relational data"

    def test_empty_rationale_omitted(self) -> None:
        out = _normalize_decisions([{"value": "Ship it", "rationale": "none"}])
        assert "rationale" not in out[0]

    def test_ids_assigned(self) -> None:
        out = _normalize_decisions(["A", "decision one", "decision two"])
        # "A" is too short, dropped
        ids = [d["id"] for d in out]
        assert len(ids) == len(set(ids))


class TestNormalizeQuestions:
    def test_dedup_and_clean(self) -> None:
        out = _normalize_questions([
            "Who owns migration?",
            "who owns migration?",
            {"question": "Do we need a rollback plan?"},
        ])
        assert len(out) == 2

    def test_short_dropped(self) -> None:
        assert _normalize_questions(["ok?", "why"]) == []

    def test_stt_fragments_dropped(self) -> None:
        assert _normalize_questions([
            "hey cool",
            "time right",
            "why sound",
            "What is the launch date for Q3?",
        ]) == ["What is the launch date for Q3?"]

    def test_nested_object_coerced(self) -> None:
        out = _normalize_questions([{"question": {"text": "Who owns the API contract?"}}])
        assert out == ["Who owns the API contract?"]


class TestJunkActionItems:
    def test_mechus_qa_not_action_items(self) -> None:
        out = _normalize_action_items([
            {"owner": "Sahasra", "task": "ask Mechus to tell more about the solar system"},
            {"owner": "Sahasra", "task": "clarify audio issues"},
            {"owner": "Priya", "task": "send the pricing deck to Sarah"},
        ])
        assert len(out) == 1
        assert out[0]["task"] == "send the pricing deck to Sarah"

    def test_nested_task_coerced(self) -> None:
        out = _normalize_action_items([
            {"owner": "Alex", "task": {"description": "update the API spec"}},
        ])
        assert len(out) == 1
        assert out[0]["task"] == "update the API spec"

    def test_object_object_dropped(self) -> None:
        assert _normalize_action_items([{"owner": "X", "task": {"nested": True}}]) == []


class TestDedupeStrings:
    def test_basic(self) -> None:
        assert _dedupe_strings(["pricing", "Pricing", "launch"]) == ["pricing", "launch"]

    def test_min_len(self) -> None:
        assert _dedupe_strings(["a", "ok"], min_len=2) == ["ok"]
