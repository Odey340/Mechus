"""API contract for FaceTime camera/vision control (requires Flask + flask_cors)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask_cors")

from koov import api_server as api  # noqa: E402
from koov.facetime_control import read_camera_control  # noqa: E402

FT_LINK = "https://facetime.apple.com/join#v=1&p=abc123&k=def456"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_notes_dir", lambda: str(tmp_path))
    monkeypatch.setattr(api, "_API_KEY", None)
    monkeypatch.setenv("KOOV_FT_VIDEO_DEVICES", "/dev/video20,/dev/video21")
    for key in ("KOOV_FT_CAMERA_FEATURE", "KOOV_FT_VISION_ENABLED", "KOOV_FT_CAMERA_AUTO_ON",
                "KOOV_FT_CAMERA_REQUIRED"):
        monkeypatch.delenv(key, raising=False)
    # Never shell out to docker from tests.
    monkeypatch.setattr(api, "_reconcile_facetime_camera_leases", lambda: None)
    with api._session_lock:
        saved = dict(api._active_sessions)
        api._active_sessions.clear()
    api._vision_request_times.clear()
    yield api.app.test_client()
    with api._session_lock:
        api._active_sessions.clear()
        api._active_sessions.update(saved)


def _worker_status(tmp_path, meeting_id, **fields):
    payload = {"status": "active", "meeting_id": meeting_id, "join_backend": "facetime_web", **fields}
    (tmp_path / f"worker-status-{meeting_id}.json").write_text(json.dumps(payload))


def _session(meeting_id, backend="facetime_web", device="/dev/video20", vision=False):
    sess = api.KoovSession(meeting_link=FT_LINK, user_id="u")
    sess.join_backend = backend
    sess.status = "active"
    sess.worker_container = f"koov-facetime-{meeting_id}"
    sess.facetime_camera_device = device
    sess.facetime_vision_requested = vision
    with api._session_lock:
        api._active_sessions[meeting_id] = sess
    return sess


# --- camera endpoint ---------------------------------------------------------


def test_camera_endpoint_disabled_returns_503(client):
    r = client.post("/api/meetings/m1/camera", json={"action": "turn_on"})
    assert r.status_code == 503


def test_camera_endpoint_unknown_meeting_404(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    r = client.post("/api/meetings/nope/camera", json={"action": "turn_on"})
    assert r.status_code == 404


def test_camera_endpoint_rejects_non_facetime(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    _session("z1", backend="zoom_sdk")
    r = client.post("/api/meetings/z1/camera", json={"action": "turn_on"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "camera_not_facetime"


def test_camera_endpoint_rejects_unprovisioned_worker(client, monkeypatch, tmp_path):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    _session("m1", device="")
    r = client.post("/api/meetings/m1/camera", json={"action": "turn_on"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "camera_not_provisioned"

    # Disk-only (API restarted) worker that reported no device.
    _worker_status(tmp_path, "m2", facetime_camera_device_assigned=False)
    r = client.post("/api/meetings/m2/camera", json={"action": "turn_on"})
    assert r.status_code == 409


def test_camera_endpoint_writes_revisions(client, monkeypatch, tmp_path):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    _session("m1")
    r = client.post("/api/meetings/m1/camera", json={"action": "turn_on"})
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "accepted"
    assert body["camera"] == {
        "revision": 1, "enabled": True, "mode": "avatar", "text": "", "expires_at": None,
    }

    r = client.post(
        "/api/meetings/m1/camera",
        json={"action": "show_card", "text": "Koov is summarizing", "duration_seconds": 10},
    )
    assert r.status_code == 202
    state = read_camera_control(tmp_path, "m1")
    assert state.revision == 2 and state.mode == "status_card" and state.expires_at

    r = client.post("/api/meetings/m1/camera", json={"action": "turn_off"})
    assert read_camera_control(tmp_path, "m1").enabled is False
    r = client.post("/api/meetings/m1/camera", json={"action": "turn_on"})
    assert read_camera_control(tmp_path, "m1").mode == "avatar"


@pytest.mark.parametrize(
    "body",
    [
        {"action": "explode"},
        {"action": "set_mode", "mode": "hologram"},
        {"action": "show_card", "mode": "avatar", "text": "x"},
        {"action": "show_card", "text": "x" * 201},
        {"action": "show_card", "text": "hi", "duration_seconds": 3600},
        ["not", "an", "object"],
    ],
)
def test_camera_endpoint_validation(client, monkeypatch, body):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    _session("m1")
    r = client.post("/api/meetings/m1/camera", json=body)
    assert r.status_code == 400


def test_camera_endpoint_requires_api_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_CAMERA_FEATURE", "1")
    monkeypatch.setattr(api, "_API_KEY", "secret")
    _session("m1")
    assert client.post("/api/meetings/m1/camera", json={"action": "turn_on"}).status_code == 401
    r = client.post(
        "/api/meetings/m1/camera", json={"action": "turn_on"}, headers={"X-API-Key": "secret"}
    )
    assert r.status_code == 202


# --- vision endpoints --------------------------------------------------------


def test_vision_disabled_503(client):
    assert client.post("/api/meetings/m1/vision/query", json={"question": "q"}).status_code == 503


def test_vision_requires_meeting_opt_in(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_VISION_ENABLED", "1")
    _session("m1", vision=False)
    r = client.post("/api/meetings/m1/vision/query", json={"question": "What is visible?"})
    assert r.status_code == 409


def test_vision_query_rate_limit_and_status(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_VISION_ENABLED", "1")
    monkeypatch.setenv("KOOV_FT_VISION_MAX_REQUESTS_PER_MINUTE", "2")
    _session("m1", vision=True)
    ids = []
    for _ in range(2):
        r = client.post("/api/meetings/m1/vision/query", json={"question": "What is on the board?"})
        assert r.status_code == 202
        ids.append(r.get_json()["request_id"])
    r = client.post("/api/meetings/m1/vision/query", json={"question": "again"})
    assert r.status_code == 429

    r = client.get(f"/api/meetings/m1/vision/requests/{ids[0]}")
    assert r.status_code == 200 and r.get_json()["status"] == "pending"
    assert client.get("/api/meetings/m1/vision/requests/not-a-uuid").status_code == 404


def test_vision_query_validation(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_VISION_ENABLED", "1")
    _session("m1", vision=True)
    assert client.post("/api/meetings/m1/vision/query", json={"question": ""}).status_code == 400
    assert client.post("/api/meetings/m1/vision/query", json={"question": "x" * 501}).status_code == 400


# --- join validation ---------------------------------------------------------


def test_join_rejects_non_object_camera(client):
    r = client.post("/api/join-meeting", json={"meeting_link": FT_LINK, "camera": "on"})
    assert r.status_code == 400
    assert "camera must be an object" in r.get_json()["error"]


def test_join_rejects_unknown_camera_mode_for_facetime(client):
    r = client.post(
        "/api/join-meeting", json={"meeting_link": FT_LINK, "camera": {"mode": "hologram"}}
    )
    assert r.status_code == 400


def test_join_required_camera_when_feature_off_is_503(client):
    r = client.post(
        "/api/join-meeting",
        json={"meeting_link": FT_LINK, "camera": {"enabled": True, "required": True}},
    )
    assert r.status_code == 503


# --- launch args helper ------------------------------------------------------


def test_launch_args_with_lease(client, monkeypatch):
    env, args, lease = api._facetime_camera_launch_args("m1", "koov-facetime-m1", provision=True, required=False)
    assert lease is not None and lease.device == "/dev/video20"
    assert env["KOOV_FT_VIDEO_DEVICE"] == "/dev/video20"
    assert env["KOOV_FT_CAMERA_FEATURE"] == "1"
    assert "--device" in args and "/dev/video20:/dev/video20:rwm" in args
    assert "koov.video_device=/dev/video20" in args
    assert "koov.meeting_id=m1" in args

    api._release_facetime_camera_lease("m1")
    env2, _, lease2 = api._facetime_camera_launch_args("m2", "c2", provision=True, required=False)
    assert lease2 is not None and lease2.device == "/dev/video20"


def test_launch_args_without_provision_has_no_device(client):
    env, args, lease = api._facetime_camera_launch_args("m1", "c1", provision=False, required=False)
    assert lease is None
    assert "--device" not in args and "KOOV_FT_VIDEO_DEVICE" not in env
    assert env["KOOV_FT_CAMERA_FEATURE"] == "0"
    assert "koov.facetime=1" in args


def test_launch_args_pool_exhausted(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_VIDEO_DEVICES", "/dev/video20")
    api._facetime_camera_launch_args("m1", "c1", provision=True, required=False)
    env, args, lease = api._facetime_camera_launch_args("m2", "c2", provision=True, required=False)
    assert lease is None and env["KOOV_FT_CAMERA_ERROR_CODE"] == "camera_pool_exhausted"
    with pytest.raises(RuntimeError):
        api._facetime_camera_launch_args("m3", "c3", provision=True, required=True)


def test_launch_args_misconfigured_pool_degrades(client, monkeypatch):
    monkeypatch.setenv("KOOV_FT_VIDEO_DEVICES", "/etc/passwd")
    env, args, lease = api._facetime_camera_launch_args("m1", "c1", provision=True, required=False)
    assert lease is None and "--device" not in args


def test_status_fields_filter(client):
    fields = api._facetime_camera_status_fields(
        {"facetime_camera_enabled": True, "facetime_camera_device": "/dev/video20", "other": 1}
    )
    assert fields == {"facetime_camera_enabled": True}
