"""Tests for live on-the-spot meeting recap (dashboard during call)."""

from koov.meeting_summary import (
    build_live_partial_summary,
    live_recap_to_transcript_payload,
)


def test_build_live_partial_summary_uses_spoken_recap():
    state = {
        "spoken_executive_recap": "We aligned on launching in Q3 and assigned design to Alex.",
        "decisions": [{"id": "d1", "value": "Launch in Q3"}],
        "action_items": [{"owner": "Alex", "task": "Finalize mockups", "deadline": "Friday"}],
        "open_issues": [],
    }
    fs = build_live_partial_summary("", state)
    assert fs.get("live") is True
    assert "Q3" in (fs.get("executive_summary") or "")
    payload = live_recap_to_transcript_payload(fs)
    assert payload["live_recap"] is True
    assert payload["summary"]
    assert payload["final_summary"] is not None
    assert any("Alex" in item for item in payload["actionable_items"])


def test_build_live_partial_summary_from_state_without_spoken():
    state = {
        "decisions": [{"id": "budget", "value": "Cap spend at 50k"}],
        "action_items": [],
        "open_issues": [{"status": "open", "display_summary": "Who owns vendor selection?"}],
    }
    fs = build_live_partial_summary("", state)
    payload = live_recap_to_transcript_payload(fs)
    assert payload["live_recap"] is True
    assert "50k" in (payload["summary"] or "").lower() or payload["final_summary"]


def test_live_recap_empty_when_no_content():
    payload = live_recap_to_transcript_payload(None)
    assert payload["live_recap"] is False
    assert payload["summary"] == ""
    assert payload["final_summary"] is None
