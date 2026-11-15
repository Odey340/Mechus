import io
import json
import os
import threading
import time
import urllib.error

import pytest
from PIL import Image, ImageDraw

from mechus import facetime_vision as fv
from mechus.facetime_control import enqueue_vision_request, vision_request_status
from mechus.facetime_vision import (
    VisionResult,
    VoiceVisionBridge,
    analyze_image,
    claim_next_request,
    complete_request,
    crop_to_region,
    fail_inflight,
    is_visual_request,
    prepare_screenshot,
    prune_done,
    recover_stale_requests,
    usable_screenshot,
)


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _scene_png(w=1280, h=720) -> bytes:
    image = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 900, 600), fill="blue")
    return _png(image)


# --- image handling ----------------------------------------------------------


def test_blank_screenshot_is_rejected():
    ok, reason = usable_screenshot(_png(Image.new("RGB", (640, 360), "black")))
    assert ok is False
    assert "blank" in reason


def test_visible_screenshot_is_resized_without_retention():
    source = _scene_png(2000, 1200)
    ok, reason = usable_screenshot(source)
    assert ok is True, reason
    jpeg = prepare_screenshot(source, max_width=800, max_height=600)
    prepared = Image.open(io.BytesIO(jpeg))
    assert prepared.width <= 800
    assert prepared.height <= 600


def test_crop_uses_largest_reliable_rect():
    out = crop_to_region(
        _scene_png(),
        [
            {"x": 10, "y": 10, "width": 100, "height": 80},  # small self-view
            {"x": 100, "y": 100, "width": 800, "height": 500},
        ],
    )
    assert Image.open(io.BytesIO(out)).size == (800, 500)


def test_crop_clamps_rects_to_image_bounds():
    out = crop_to_region(_scene_png(), [{"x": -50, "y": 300, "width": 2000, "height": 900}])
    assert Image.open(io.BytesIO(out)).size == (1280, 420)


def test_crop_falls_back_to_viewport_for_small_or_bad_rects():
    src = _scene_png()
    assert crop_to_region(src, [{"x": 0, "y": 0, "width": 200, "height": 100}]) == src
    assert crop_to_region(src, [{"x": "nan?"}, None, {"width": 5}]) == src
    assert crop_to_region(src, []) == src


# --- intent ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "look at this",
        "what is on the whiteboard",
        "read the text I'm showing",
        "what am I holding",
        "can you see the diagram",
        "take a look at this chart",
        "what's written on the board",
        "describe what's on my screen",
    ],
)
def test_visual_requests_detected(text):
    assert is_visual_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "I see that the deadline moved",
        "show me the action items",
        "what did we decide",
        "read back the summary",
        "see you tomorrow",
        "what are the risks",
        "let's see what Sam thinks",
        "who owns the launch",
        "",
    ],
)
def test_non_visual_requests_ignored(text):
    assert not is_visual_request(text)


# --- provider ----------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_analyze_image_sends_detail_and_parses_answer(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["payload"] = json.loads(req.data.decode())
        seen["timeout"] = timeout
        return _Resp(json.dumps({"choices": [{"message": {"content": " A blue square. "}}]}).encode())

    monkeypatch.setattr(fv.urllib.request, "urlopen", fake_urlopen)
    answer = analyze_image(b"jpeg", "What is this?", api_key="k", model="m", detail="high")
    assert answer == "A blue square."
    image_part = seen["payload"]["messages"][1]["content"][1]["image_url"]
    assert image_part["detail"] == "high"
    assert seen["payload"]["model"] == "m"


def test_analyze_image_errors_never_include_image(monkeypatch):
    def timeout(req, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(fv.urllib.request, "urlopen", timeout)
    with pytest.raises(RuntimeError) as exc:
        analyze_image(b"SECRETIMAGEBYTES" * 10, "q", api_key="k", model="m")
    assert "U0VDUkVU" not in str(exc.value)  # base64 of "SECRET"
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    "body",
    [b"not json", json.dumps({"choices": []}).encode(), json.dumps({"choices": [{"message": {}}]}).encode()],
)
def test_analyze_image_malformed_responses(monkeypatch, body):
    monkeypatch.setattr(fv.urllib.request, "urlopen", lambda req, timeout: _Resp(body))
    with pytest.raises(RuntimeError):
        analyze_image(b"x", "q", api_key="k", model="m")


def test_analyze_image_requires_key():
    with pytest.raises(RuntimeError, match="MECHUS_OPENAI_API_KEY"):
        analyze_image(b"x", "q", api_key=" ", model="m")


def test_vision_env_defaults(monkeypatch):
    monkeypatch.delenv("MECHUS_OPENAI_VISION_MODEL", raising=False)
    monkeypatch.setenv("MECHUS_OPENAI_RESPONSE_MODEL", "text-only-model")
    monkeypatch.setenv("MECHUS_FT_VISION_DETAIL", "bogus")
    assert fv.vision_model_from_env() == fv.DEFAULT_VISION_MODEL
    assert fv.vision_detail_from_env() == "auto"


# --- queue -------------------------------------------------------------------


def test_claim_complete_and_status(tmp_path):
    req = enqueue_vision_request(tmp_path, "m1", "What is visible?")
    claimed = claim_next_request(str(tmp_path), "m1")
    assert claimed is not None
    path, value = claimed
    assert value["request_id"] == req["request_id"]
    assert vision_request_status(tmp_path, "m1", req["request_id"])["status"] == "processing"
    assert claim_next_request(str(tmp_path), "m1") is None  # no double claim
    complete_request(path, VisionResult(req["request_id"], "completed", answer="A whiteboard"))
    status = vision_request_status(tmp_path, "m1", req["request_id"])
    assert status["status"] == "completed" and status["answer"] == "A whiteboard"
    done = (tmp_path / "worker-commands-m1" / "done" / f"{req['request_id']}.json").read_text()
    assert "image" not in done and "base64" not in done


def test_stale_processing_recovered_once_then_failed(tmp_path):
    req = enqueue_vision_request(tmp_path, "m1", "q", ttl_s=60)
    claim_next_request(str(tmp_path), "m1")
    assert recover_stale_requests(str(tmp_path), "m1") == 1
    assert vision_request_status(tmp_path, "m1", req["request_id"])["status"] == "pending"
    claim_next_request(str(tmp_path), "m1")
    assert recover_stale_requests(str(tmp_path), "m1") == 1  # already recovered -> fail
    status = vision_request_status(tmp_path, "m1", req["request_id"])
    assert status["status"] == "failed" and status["error_code"] == "vision_worker_restarted"


def test_expired_processing_is_failed(tmp_path):
    req = enqueue_vision_request(tmp_path, "m1", "q", ttl_s=5)
    claim_next_request(str(tmp_path), "m1")
    recover_stale_requests(str(tmp_path), "m1", now=time.time() + 60)
    assert vision_request_status(tmp_path, "m1", req["request_id"])["status"] == "failed"


def test_fail_inflight_and_prune(tmp_path):
    a = enqueue_vision_request(tmp_path, "m1", "a")
    b = enqueue_vision_request(tmp_path, "m1", "b")
    claim_next_request(str(tmp_path), "m1")
    assert fail_inflight(str(tmp_path), "m1") == 2
    for req in (a, b):
        assert vision_request_status(tmp_path, "m1", req["request_id"])["error_code"] == "vision_meeting_ended"
    done = tmp_path / "worker-commands-m1" / "done"
    old = next(done.glob("*.json"))
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert prune_done(str(tmp_path), "m1") == 1
    assert len(list(done.glob("*.json"))) == 1


# --- voice bridge ------------------------------------------------------------


def _fake_worker(tmp_path, answer=None, error_code="", stop=None):
    """Simulates the worker main loop answering queued requests."""
    def loop():
        while not stop.is_set():
            claimed = claim_next_request(str(tmp_path), "m1")
            if claimed:
                path, value = claimed
                if answer is not None:
                    complete_request(path, VisionResult(value["request_id"], "completed", answer=answer))
                else:
                    complete_request(path, VisionResult(value["request_id"], "failed", error_code=error_code))
            time.sleep(0.02)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def test_voice_bridge_returns_answer(tmp_path):
    stop = threading.Event()
    _fake_worker(tmp_path, answer="It says Q3 launch", stop=stop)
    try:
        bridge = VoiceVisionBridge(str(tmp_path), "m1", poll_interval_s=0.02)
        assert bridge.ask("read the whiteboard", timeout_s=3) == ("It says Q3 launch", "")
    finally:
        stop.set()


def test_voice_bridge_reports_failure_code(tmp_path):
    stop = threading.Event()
    _fake_worker(tmp_path, error_code="vision_blank_frame", stop=stop)
    try:
        bridge = VoiceVisionBridge(str(tmp_path), "m1", poll_interval_s=0.02)
        assert bridge.ask("look at this", timeout_s=3) == (None, "vision_blank_frame")
    finally:
        stop.set()


def test_voice_bridge_timeout_and_rate_limit(tmp_path):
    bridge = VoiceVisionBridge(str(tmp_path), "m1", max_per_minute=1, poll_interval_s=0.02)
    assert bridge.ask("look at this", timeout_s=1) == (None, "vision_timeout")
    assert bridge.ask("look at this", timeout_s=1) == (None, "vision_rate_limited")
