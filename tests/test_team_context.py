from __future__ import annotations

import json
from pathlib import Path

from mechus.meeting_store import MeetingStore
from mechus.retrieval import retrieve_for_question
from mechus.team_context import TeamContextStore


def test_team_context_manual_and_meeting_import(monkeypatch, tmp_path: Path) -> None:
    meeting_base = tmp_path / "meetings"
    notes_base = tmp_path / "notes"
    team_base = notes_base / "team_context"
    monkeypatch.setenv("MECHUS_MEETING_STORE_DIR", str(meeting_base))
    monkeypatch.setenv("MECHUS_NOTES_DIR", str(notes_base))

    meeting = MeetingStore("m1")
    meeting.append_enriched_chunk(
        "alice",
        "We decided Enterprise customers need shared rollout context.",
        100.0,
        chunk_id=1,
        topic="enterprise",
        tags=["decision"],
    )

    store = TeamContextStore("team-a", base_dir=str(team_base))
    team = store.upsert_team(name="Acme", plan="enterprise", created_by="u1")
    manual = store.append_item(text="Acme prefers SOC2-ready vendors.", title="Account note", created_by="u1")
    imported = store.add_meeting("m1", title="Enterprise kickoff", created_by="u1")

    prompt = store.format_for_prompt(max_chars=4000)

    assert team["plan"] == "enterprise"
    assert manual["source_type"] == "manual"
    assert imported["source_type"] == "meeting"
    assert "SOC2-ready" in prompt
    assert "shared rollout context" in prompt
    assert len(store.list_meetings()) == 1


def test_retrieval_includes_shared_team_context() -> None:
    bundle = retrieve_for_question(
        "What does the team know about Acme?",
        shared_context_text="Acme is on the enterprise plan and needs audit logs.",
    )

    assert "TEAM_SHARED_CONTEXT" in bundle.formatted_context
    assert "audit logs" in bundle.formatted_context
    assert bundle.meta["team_shared_context_chars"] > 0


def test_team_context_meeting_import_falls_back_to_notes_jsonl(monkeypatch, tmp_path: Path) -> None:
    meeting_base = tmp_path / "empty_meetings"
    notes_base = tmp_path / "notes"
    monkeypatch.setenv("MECHUS_MEETING_STORE_DIR", str(meeting_base))
    monkeypatch.setenv("MECHUS_NOTES_DIR", str(notes_base))
    notes_base.mkdir(parents=True)
    transcript_path = notes_base / "facetime-m1.jsonl"
    transcript_path.write_text(
        json.dumps({
            "kind": "transcript",
            "text": "Dana: The team agreed that customer onboarding needs shared context.",
        })
        + "\n",
        encoding="utf-8",
    )
    (notes_base / "worker-status-facetime-m1.json").write_text(
        json.dumps({"transcript_path": "/notes/facetime-m1.jsonl"}),
        encoding="utf-8",
    )

    store = TeamContextStore("team-f", base_dir=str(notes_base / "team_context"))
    item = store.add_meeting("facetime-m1", title="FaceTime sync")

    assert item["source_type"] == "meeting"
    assert "customer onboarding" in item["text"]
