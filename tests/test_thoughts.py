"""Tests for koov/thoughts.py — ThoughtStream and related helpers.

Run with:  .venv/bin/python -m pytest tests/test_thoughts.py -v
"""
from __future__ import annotations

import json
import time
import threading

import pytest

from koov.thoughts import ThoughtStream, ThoughtEntry


# ── ThoughtEntry ──────────────────────────────────────────────────────────────

class TestThoughtEntry:
    def test_minimal_dict(self):
        e = ThoughtEntry(ts=100, kind="thinking", text="hello")
        d = e.to_dict()
        assert d == {"ts": 100, "kind": "thinking", "text": "hello"}

    def test_optional_fields_included_when_set(self):
        e = ThoughtEntry(
            ts=200,
            kind="planned",
            text="deciding",
            planned_speech="Yes, done.",
            connections=["prior mention at 14:02"],
        )
        d = e.to_dict()
        assert d["planned_speech"] == "Yes, done."
        assert d["connections"] == ["prior mention at 14:02"]

    def test_none_optional_fields_omitted(self):
        e = ThoughtEntry(ts=1, kind="context", text="background fact")
        d = e.to_dict()
        assert "planned_speech" not in d
        assert "connections" not in d

    def test_empty_connections_omitted(self):
        e = ThoughtEntry(ts=1, kind="connection", text="linked", connections=[])
        # connections=[] → stored as None by ThoughtStream, so entry has None
        assert e.to_dict().get("connections") is None or "connections" not in e.to_dict()


# ── ThoughtStream ─────────────────────────────────────────────────────────────

class TestThoughtStream:
    def test_empty_stream(self):
        s = ThoughtStream()
        assert s.snapshot() == []
        assert len(s) == 0

    def test_append_and_snapshot(self):
        s = ThoughtStream()
        s.append("thinking", "Processing request")
        snaps = s.snapshot()
        assert len(snaps) == 1
        assert snaps[0]["kind"] == "thinking"
        assert snaps[0]["text"] == "Processing request"
        assert "ts" in snaps[0]

    def test_snapshot_returns_dicts_not_entries(self):
        s = ThoughtStream()
        s.append("context", "Some context")
        result = s.snapshot()
        assert isinstance(result[0], dict)

    def test_snapshot_is_copy(self):
        s = ThoughtStream()
        s.append("thinking", "step 1")
        snap1 = s.snapshot()
        s.append("thinking", "step 2")
        snap2 = s.snapshot()
        assert len(snap1) == 1
        assert len(snap2) == 2

    def test_cap_enforced(self):
        s = ThoughtStream(max_entries=5)
        for i in range(10):
            s.append("thinking", f"thought {i}")
            time.sleep(0.001)  # ensure distinct ts
        assert len(s) == 5
        snaps = s.snapshot()
        # Oldest entries should have been dropped; newest 5 remain
        texts = [d["text"] for d in snaps]
        assert "thought 9" in texts
        assert "thought 0" not in texts

    def test_empty_text_skipped(self):
        s = ThoughtStream()
        s.append("thinking", "")
        s.append("thinking", "   ")
        assert len(s) == 0

    def test_invalid_kind_normalised_to_thinking(self):
        s = ThoughtStream()
        s.append("robot_brain", "weird kind")
        assert s.snapshot()[0]["kind"] == "thinking"

    def test_dedupe_same_ts_same_prefix(self):
        s = ThoughtStream()
        text = "Same reasoning sentence here"
        s.append("thinking", text)
        # Append immediately — same second, same prefix → should dedupe
        s.append("thinking", text)
        assert len(s) == 1

    def test_dedupe_different_text_not_skipped(self):
        s = ThoughtStream()
        s.append("thinking", "First distinct thought")
        s.append("thinking", "Second different thought")
        assert len(s) == 2

    def test_planned_speech_in_snapshot(self):
        s = ThoughtStream()
        s.append("planned", "Preparing answer", planned_speech="Yes, I can hear you.")
        d = s.snapshot()[0]
        assert d["planned_speech"] == "Yes, I can hear you."

    def test_connections_in_snapshot(self):
        s = ThoughtStream()
        s.append("connection", "Linking context", connections=["mic check at 14:02"])
        d = s.snapshot()[0]
        assert d["connections"] == ["mic check at 14:02"]

    def test_empty_connections_stripped(self):
        s = ThoughtStream()
        s.append("connection", "Some link", connections=["", "   "])
        d = s.snapshot()[0]
        assert "connections" not in d

    def test_all_valid_kinds(self):
        s = ThoughtStream()
        for kind in ("thinking", "connection", "planned", "context"):
            s.append(kind, f"test {kind}")
        kinds = {d["kind"] for d in s.snapshot()}
        assert kinds == {"thinking", "connection", "planned", "context"}

    def test_thread_safety(self):
        s = ThoughtStream(max_entries=100)
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                for j in range(10):
                    s.append("thinking", f"thread-{idx}-thought-{j}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(s) <= 100


# ── _parse_json_voice_response_full ──────────────────────────────────────────

class TestParseJsonVoiceResponseFull:
    """Ensure private_thoughts is extracted alongside spoken_response."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from koov.responder import _parse_json_voice_response_full
        self.parse = _parse_json_voice_response_full

    def test_both_fields(self):
        raw = json.dumps({
            "private_thoughts": "User wants reassurance about audio.",
            "spoken_response": "Yes, I can hear you clearly.",
        })
        spoken, pt, cont = self.parse(raw)
        assert spoken == "Yes, I can hear you clearly."
        assert pt == "User wants reassurance about audio."
        assert cont is None

    def test_no_private_thoughts(self):
        raw = json.dumps({"spoken_response": "Sure, let me check."})
        spoken, pt, cont = self.parse(raw)
        assert spoken == "Sure, let me check."
        assert pt is None
        assert cont is None

    def test_only_private_thoughts_no_spoken(self):
        raw = json.dumps({"private_thoughts": "Thinking about it."})
        spoken, pt, cont = self.parse(raw)
        assert spoken is None
        assert pt == "Thinking about it."
        assert cont is None

    def test_empty_input(self):
        spoken, pt, cont = self.parse("")
        assert spoken is None
        assert pt is None
        assert cont is None

    def test_non_json(self):
        spoken, pt, cont = self.parse("This is not JSON at all.")
        assert spoken is None
        assert pt is None
        assert cont is None

    def test_legacy_answer_key(self):
        raw = json.dumps({"answer": "The meeting starts at 2pm.", "private_thoughts": "Checking schedule."})
        spoken, pt, cont = self.parse(raw)
        assert spoken == "The meeting starts at 2pm."
        assert pt == "Checking schedule."
        assert cont is None

    def test_markdown_fenced_json(self):
        raw = "```json\n" + json.dumps({
            "private_thoughts": "Simple calc.",
            "spoken_response": "It's ten.",
        }) + "\n```"
        spoken, pt, cont = self.parse(raw)
        assert spoken == "It's ten."
        assert pt == "Simple calc."
        assert cont is None

    def test_backward_compat_spoken_only_wrapper(self):
        from koov.responder import _parse_json_voice_response
        raw = json.dumps({"private_thoughts": "Reasoning.", "spoken_response": "Done."})
        assert _parse_json_voice_response(raw) == "Done."


# ── API integration (Flask test client) ──────────────────────────────────────

class TestStatusThoughtsIntegration:
    """Light integration: patch _active_sessions to verify thoughts appear in GET /api/status."""

    def test_thoughts_in_status_payload(self):
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from koov.api_server import app, _active_sessions, KoovSession, _session_lock
        except ImportError as exc:
            pytest.skip(f"api_server import failed: {exc}")

        stream = ThoughtStream()
        stream.append("thinking", "User is testing audio")
        stream.append("planned", "Confirming presence", planned_speech="Yes, I can hear you.")

        class _FakeAgent:
            _last_latency = None
            _last_audio_frame_at = 0.0
            def get_thoughts_for_status(self):
                return stream.snapshot()

        fake_session = KoovSession(
            meeting_link="https://facetime.apple.com/test",
            user_id="test-user",
            status="active",
            agent=_FakeAgent(),
            started_at=time.time(),
        )
        meeting_id = "test-thoughts-meeting-id"
        with _session_lock:
            _active_sessions[meeting_id] = fake_session

        try:
            client = app.test_client()
            resp = client.get(f"/api/status?meeting_id={meeting_id}")
            data = resp.get_json()
            assert data is not None
            assert "thoughts" in data
            thoughts = data["thoughts"]
            assert isinstance(thoughts, list)
            assert len(thoughts) == 2
            assert thoughts[0]["kind"] == "thinking"
            assert thoughts[0]["text"] == "User is testing audio"
            assert thoughts[1]["planned_speech"] == "Yes, I can hear you."
        finally:
            with _session_lock:
                _active_sessions.pop(meeting_id, None)
