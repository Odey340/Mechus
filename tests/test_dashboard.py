"""Built-in dashboard: page is served, and every endpoint it calls exists and works."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("flask_cors")

from werkzeug.exceptions import MethodNotAllowed, NotFound  # noqa: E402

from mechus import api_server as api  # noqa: E402

DASHBOARD_HTML = os.path.join(os.path.dirname(api.__file__), "static", "properties.html")
MEETING_HTML = os.path.join(os.path.dirname(api.__file__), "static", "dashboard.html")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_notes_dir", lambda: str(tmp_path))
    monkeypatch.setattr(api, "_API_KEY", None)
    monkeypatch.setenv("MECHUS_NOTES_DIR", str(tmp_path))
    monkeypatch.setenv("MECHUS_TEAM_STORE_DIR", str(tmp_path / "team_context"))
    with api._session_lock:
        saved = dict(api._active_sessions)
        api._active_sessions.clear()
    yield api.app.test_client()
    with api._session_lock:
        api._active_sessions.clear()
        api._active_sessions.update(saved)


def test_dashboard_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    assert b"<title>Mechus \xc2\xb7 Property dashboard</title>" in r.data


def test_meetings_are_native_dashboard_tab(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b'["meetings", "Meetings"]' in r.data
    assert b"async function viewMeetings()" in r.data
    assert b'api("/api/join-meeting"' in r.data
    assert b'api("/api/stop"' in r.data
    assert b"Send Mechus to a meeting" in r.data


def test_dashboard_uses_imessage_as_the_real_chat(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b'["messages", "iMessage"]' in r.data
    assert b"The conversation lives in Messages" in r.data
    assert b'href: "sms:"' in r.data
    assert b"Keep tenant and landlord conversations in the Messages app" in r.data


def test_meeting_workspace_served(client):
    r = client.get("/meetings")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/dashboard#/meetings")


def test_meeting_workspace_demo_query_is_preserved(client):
    r = client.get("/meetings?demo=1")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/dashboard?demo=1#/meetings")


def test_root_redirects_to_dashboard(client):
    r = client.get("/")
    assert r.status_code in (301, 302)
    assert r.headers["Location"].endswith("/tenants")


def test_dashboard_public_but_api_still_protected(client, monkeypatch):
    monkeypatch.setattr(api, "_API_KEY", "secret")
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": "secret"}).status_code == 200


def test_scroll_round_trip(client):
    r = client.post("/api/teams", json={"team_id": "ithaca", "user_id": "odysseus"})
    assert r.status_code == 200 and r.get_json()["success"]

    r = client.post(
        "/api/teams/ithaca/context",
        json={"title": "Launch", "text": "Launch is Dec 1.", "user_id": "odysseus", "source_type": "manual"},
    )
    assert r.status_code == 200 and r.get_json()["success"]

    items = client.get("/api/teams/ithaca/context?limit=100").get_json()["items"]
    assert [i["text"] for i in items] == ["Launch is Dec 1."]
    assert items[0]["title"] == "Launch"


def test_bad_team_id_gives_readable_error(client):
    r = client.post("/api/teams/bad%20crew/context", json={"text": "x"})
    assert r.status_code == 400
    assert "team_id" in r.get_json()["error"]


def test_session_memory_uses_active_sessions(client):
    agent = SimpleNamespace(_pinned_memory_ids=["m1"], _cross_meeting_memory_text="abc")
    with api._session_lock:
        api._active_sessions["voyage-1"] = SimpleNamespace(agent=agent)
    r = client.get("/api/sessions/voyage-1/memory")
    assert r.status_code == 200
    body = r.get_json()
    assert body["pinned_meeting_ids"] == ["m1"]
    assert body["memory_chars"] == 3


def test_session_memory_worker_session_reports_no_agent(client):
    with api._session_lock:
        api._active_sessions["voyage-2"] = SimpleNamespace(agent=None)
    r = client.post("/api/sessions/voyage-2/memory", json={"meeting_ids": ["m1"]})
    assert r.status_code == 404
    assert r.get_json()["error"] == "agent not available"


def test_session_memory_unknown_session(client):
    r = client.get("/api/sessions/nope/memory")
    assert r.status_code == 404


def _dashboard_api_paths() -> set[str]:
    with open(DASHBOARD_HTML, encoding="utf-8") as f:
        src = f.read()
    raw = re.findall(r"""["'`](/(?:api|health)[^"'`]*)["'`]""", src)
    paths = set()
    for p in raw:
        p = re.sub(r"\$\{[^}]*\}", "x", p)  # template params -> placeholder
        p = p.split("?", 1)[0]
        paths.add(p)
    return paths


def test_every_dashboard_endpoint_exists():
    paths = _dashboard_api_paths()
    # Sanity: the scan found the endpoints the page depends on.
    for expected in ("/api/join-meeting", "/api/status", "/api/stop",
                     "/api/meetings/list", "/api/estates/overview", "/api/estates/properties"):
        assert expected in paths, expected
    adapter = api.app.url_map.bind("localhost")
    missing = []
    for p in sorted(paths):
        try:
            adapter.match(p, method="GET")
        except MethodNotAllowed:
            pass  # route exists, POST-only
        except NotFound:
            missing.append(p)
    assert not missing, f"dashboard calls unknown endpoints: {missing}"


def test_welcome_served_and_public(client, monkeypatch):
    r = client.get("/welcome")
    assert r.status_code == 200
    assert b"<title>Welcome to Mechus</title>" in r.data
    monkeypatch.setattr(api, "_API_KEY", "secret")
    assert client.get("/welcome").status_code == 200


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/static/vendor/three.module.min.js", b'const t="160"'),
        ("/static/vendor/lenis.min.js", b"Lenis"),
        ("/static/js/blob.js", b"export function createBlob"),
        ("/static/js/sea.js", b"export function createSea"),
        ("/static/js/dashboard-scene.js", b"__mechusSceneReady"),
    ],
)
def test_dashboard_assets_served_and_public(client, monkeypatch, path, needle):
    monkeypatch.setattr(api, "_API_KEY", "secret")
    r = client.get(path)
    assert r.status_code == 200, path
    assert needle in r.data
    r.close()


def test_dashboard_redirects_first_visit_to_welcome():
    with open(MEETING_HTML, encoding="utf-8") as f:
        src = f.read()
    assert 'location.replace("/welcome")' in src
    assert 'q.has("demo")' in src  # demo mode skips the intro
