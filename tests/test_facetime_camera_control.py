import json
import time

import pytest

from mechus.facetime_control import (
    enqueue_vision_request,
    read_camera_control,
    vision_request_status,
    write_camera_control,
)


def test_camera_control_revisions_and_validation(tmp_path):
    first = write_camera_control(tmp_path, "m1", enabled=True, mode="avatar")
    second = write_camera_control(
        tmp_path,
        "m1",
        enabled=True,
        mode="status_card",
        text="Mechus is summarizing",
        duration_seconds=10,
    )
    assert first.revision == 1
    assert second.revision == 2
    assert second.expires_at and second.expires_at > time.time()
    assert read_camera_control(tmp_path, "m1") == second

    with pytest.raises(ValueError):
        write_camera_control(tmp_path, "m1", enabled=True, mode="unknown")
    with pytest.raises(ValueError):
        write_camera_control(tmp_path, "m1", enabled=True, mode="message_card", text="x" * 201)
    with pytest.raises(ValueError):
        write_camera_control(
            tmp_path, "m1", enabled=True, mode="status_card", duration_seconds=100
        )


def test_vision_queue_status(tmp_path):
    request = enqueue_vision_request(tmp_path, "m1", "What is visible?")
    status = vision_request_status(tmp_path, "m1", request["request_id"])
    assert status and status["status"] == "pending"
    assert status["question"] == "What is visible?"

    pending = next((tmp_path / "worker-commands-m1" / "pending").glob("*.json"))
    value = json.loads(pending.read_text())
    assert "image" not in value

