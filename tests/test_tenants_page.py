"""The simple tenants view: who lives where, and how their move-in / move-out checks went."""
import json

import pytest

from koov.estates import store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("KOOV_ESTATES_DIR", str(tmp_path / "estates"))
    monkeypatch.setenv("KOOV_NOTES_DIR", str(tmp_path / "notes"))
    (tmp_path / "notes").mkdir()
    from koov import api_server

    monkeypatch.setattr(api_server, "_API_KEY", None)
    return api_server.app.test_client(), tmp_path / "notes"


def _call(pid, mid, mode):
    store.append_event(pid, {"kind": "facetime", "direction": "out", "actor": "Mechus", "text": "call",
                             "meeting_id": mid, "inspect_mode": mode})


def _status(notes, mid, status, inspection=None, **extra):
    (notes / f"worker-status-{mid}.json").write_text(
        json.dumps({"status": status, "meeting_id": mid, "inspection": inspection, **extra}), encoding="utf-8")


def test_tenants_is_the_first_dashboard_tab(client):
    c, _ = client
    for path in ("/", "/tenants"):
        r = c.get(path)
        assert r.status_code in (301, 302) and r.headers["Location"].endswith("/dashboard#/tenants")
    page = c.get("/dashboard").data
    assert b'const NAV = [["tenants", "Tenants"]' in page
    assert b"recipient_not_verified" in page and b"call-error" in page


def test_tenant_rows_show_the_latest_check_of_each_kind(client):
    c, notes = client
    prop = store.create_property({"name": "Apt 4B", "tenant": {"name": "Joey", "phone": "+15551230000"}})
    pid = prop["id"]
    assert c.get("/api/estates/tenants").get_json()["tenants"][0]["move_out"] is None

    _call(pid, "in-1", "move_in")
    _status(notes, "in-1", "stopped", {"frames_analysed": 30, "verdict": "passed", "findings": [],
                                       "tour": {"current": "Tour complete", "rooms": [], "not_shown": []}})
    _call(pid, "out-1", "move_out")
    _status(notes, "out-1", "stopped", {
        "frames_analysed": 40, "verdict": "needs_review",
        "findings": [{"surface": "door", "issue": "hole in bedroom door", "severity": "high", "scene": "bedroom", "say": "x"}],
        "tour": {"current": "Tour complete", "rooms": [], "not_shown": ["bathroom: floor"]}})

    row = c.get("/api/estates/tenants").get_json()["tenants"][0]
    assert row["tenant"]["name"] == "Joey" and row["unit"] == "Apt 4B"
    assert row["move_in"]["result"] == "passed"
    assert row["move_out"]["result"] == "needs_review"
    assert row["move_out"]["findings"] == [{"surface": "door", "issue": "hole in bedroom door", "severity": "high", "scene": "bedroom",
                                         "photo": "", "room": "", "item": ""}]
    assert row["move_out"]["not_shown"] == ["bathroom: floor"]


def test_call_states(client):
    c, notes = client
    pid = store.create_property({"name": "Apt 1", "tenant": {"name": "Ann", "phone": "+15551230001"}})["id"]
    _call(pid, "a", "move_out")  # worker has not written anything yet
    assert c.get(f"/api/estates/properties/{pid}/checks").get_json()["checks"][0]["result"] == "in_progress"
    _status(notes, "a", "error", None, worker_error="call ended before it was answered")
    check = c.get(f"/api/estates/properties/{pid}/checks").get_json()["checks"][0]
    assert check["result"] == "no_answer" and "answered" in check["error"]
    _status(notes, "a", "stopped", {"frames_analysed": 9, "verdict": "in_progress", "findings": [], "tour": {}})
    assert c.get(f"/api/estates/properties/{pid}/checks").get_json()["checks"][0]["result"] == "incomplete"
    assert c.get("/api/estates/properties/nope/checks").status_code == 404
