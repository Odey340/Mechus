"""The properties dashboard's FaceTime button starts the live Mechus call worker."""
import pytest

from koov import chert_client
from koov.chert import facetime as ft
from koov.estates import messaging, store

CHERT_ENV = (
    "CHERT_API_KEY", "CHERT_PROJECT_ID", "CHERT_LINE_ID", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "KOOV_CHERT_FACETIME_API_KEY", "KOOV_CHERT_PROJECT_ID", "KOOV_CHERT_FACETIME_LINE_ID", "KOOV_CHERT_LINE_ID",
    "KOOV_CHERT_API_KEY", "KOOV_LIVEKIT_URL", "KOOV_LIVEKIT_API_KEY", "KOOV_LIVEKIT_API_SECRET",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in CHERT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KOOV_ESTATES_DIR", str(tmp_path / "estates"))
    return monkeypatch


def _worker_style(env):
    for name in ("CHERT_API_KEY", "CHERT_PROJECT_ID", "CHERT_LINE_ID", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        env.setenv(name, f"value-of-{name}")


def test_dashboard_accepts_the_call_workers_env_names(env):
    assert ft.missing_config()  # nothing set
    _worker_style(env)
    assert ft.missing_config() == []
    assert ft.config()["api_key"] == "value-of-CHERT_API_KEY"
    assert ft.config()["livekit_url"] == "value-of-LIVEKIT_URL"


def test_call_worker_accepts_the_dashboards_env_names(env):
    env.setenv("KOOV_CHERT_FACETIME_API_KEY", "k")
    env.setenv("KOOV_CHERT_PROJECT_ID", "p")
    env.setenv("KOOV_CHERT_FACETIME_LINE_ID", "facetime-line")
    env.setenv("KOOV_CHERT_LINE_ID", "imessage-line")
    env.setenv("KOOV_LIVEKIT_URL", "wss://x")
    env.setenv("KOOV_LIVEKIT_API_KEY", "lk")
    env.setenv("KOOV_LIVEKIT_API_SECRET", "ls")
    cfg = chert_client.ChertConfig.from_env()
    assert cfg.missing() == []
    assert cfg.line_id == "facetime-line"  # not the iMessage line


def _property():
    return store.create_property({"name": "Apt 4B", "tenant": {"name": "Joey", "phone": "+15551230000"}})


def test_move_out_walkthrough_starts_the_agent_call_keyed_to_the_property(env):
    _worker_style(env)
    prop = _property()
    sent = []

    def start_call(payload):
        sent.append(payload)
        return {"meeting_id": "m-1"}

    res = messaging.facetime(prop["id"], "tenant", True, inspect_mode="move_out", start_call=start_call)
    assert res["meeting_id"] == "m-1" and res["inspect_mode"] == "move_out"
    assert sent == [{
        "destination": "+15551230000", "join_backend": "facetime_chert", "user_id": f"property-{prop['id']}",
        "video": True, "inspect_mode": "move_out", "inspect_unit": prop["id"],
    }]
    events = [e for e in store.list_events(prop["id"]) if e.get("kind") == "facetime"]
    assert events and events[-1]["meeting_id"] == "m-1" and events[-1]["actor"] == "Mechus"


def test_plain_call_has_no_inspection(env):
    _worker_style(env)
    prop = _property()
    sent = []
    messaging.facetime(prop["id"], "tenant", True, start_call=lambda p: sent.append(p) or {"meeting_id": "m-2"})
    assert sent[0]["inspect_mode"] == "" and sent[0]["inspect_unit"] == ""


def test_not_configured_and_bad_mode_are_refused_before_any_call(env):
    prop = _property()
    called = []
    with pytest.raises(RuntimeError, match="not configured"):
        messaging.facetime(prop["id"], "tenant", start_call=lambda p: called.append(p) or {})
    _worker_style(env)
    with pytest.raises(ValueError):
        messaging.facetime(prop["id"], "tenant", inspect_mode="demolition", start_call=lambda p: called.append(p) or {})
    assert called == []


def test_imessage_questions_are_addressed_to_mechus(env):
    from koov.estates import ai

    env.setenv("KOOV_ESTATES_FAKE_AI", "1")
    assert ai.classify_message("Mechus, what did we agree about the oven?", has_media=False, role="tenant")["addressed_to_koov"] is True
    assert ai.classify_message("Koov, what did we agree about the oven?", has_media=False, role="tenant")["addressed_to_koov"] is False
