import os
import shutil
import stat
import sys
import time

import numpy as np
import pytest

from koov.facetime_video import (
    CameraMode,
    VideoScene,
    VirtualCameraOutput,
    render_scene,
)


def test_render_every_camera_scene_has_expected_shape():
    for mode in CameraMode:
        frame = render_scene(
            VideoScene(True, mode, "Koov", "A safe status card"),
            width=320,
            height=180,
            phase=0.25,
            audio_level=0.7,
        )
        assert frame.shape == (180, 320, 3)
        assert frame.dtype == np.uint8
        assert float(frame.std()) > 1.0


def test_speaking_bars_never_reach_the_name_label():
    accent = np.array([103, 232, 249], dtype=np.int16)
    for width, height in ((640, 360), (1280, 720), (320, 180)):
        name_top = int(height * 0.73)
        for phase in (0.0, 0.1, 0.2, 0.3, 0.45):
            frame = render_scene(
                VideoScene(True, CameraMode.SPEAKING, "Koov"),
                width=width,
                height=height,
                phase=phase,
                audio_level=1.0,
            ).astype(np.int16)
            band = frame[name_top - 4 : height]
            near_accent = (np.abs(band - accent).sum(axis=2) < 40).sum()
            assert near_accent == 0, (width, height, phase)


def test_render_is_deterministic_for_same_inputs():
    scene = VideoScene(True, CameraMode.AVATAR, "Koov")
    first = render_scene(scene, width=320, height=180, phase=0.0)
    second = render_scene(scene, width=320, height=180, phase=0.0)
    assert np.array_equal(first, second)


def test_cached_background_is_not_mutated_between_frames():
    card = render_scene(VideoScene(True, CameraMode.STATUS_CARD, "Koov", "Card"), width=320, height=180)
    plain_a = render_scene(VideoScene(True, CameraMode.AVATAR, "Koov"), width=320, height=180)
    plain_b = render_scene(VideoScene(True, CameraMode.AVATAR, "Koov"), width=320, height=180)
    assert not np.array_equal(card, plain_a)
    assert np.array_equal(plain_a, plain_b)


def test_long_and_unicode_card_text_is_bounded():
    text = ("Ünïcödé 会議 résumé — " * 40)[:600]
    frame = render_scene(
        VideoScene(True, CameraMode.MESSAGE_CARD, "K" * 300, text), width=640, height=360
    )
    assert frame.shape == (360, 640, 3)


def test_render_is_fast_enough_for_12fps():
    scene = VideoScene(True, CameraMode.SPEAKING, "Koov")
    render_scene(scene, width=640, height=360)  # warm caches
    started = time.perf_counter()
    for i in range(20):
        render_scene(scene, width=640, height=360, phase=i / 20, audio_level=0.5)
    per_frame_ms = (time.perf_counter() - started) * 1000 / 20
    assert per_frame_ms < 30, per_frame_ms


# ---------------------------------------------------------------------------
# Producer supervision with a fake FFmpeg
# ---------------------------------------------------------------------------


def _fake_ffmpeg(tmp_path, body: str) -> str:
    path = tmp_path / "fake-ffmpeg"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _wait(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess semantics")
def test_producer_streams_frames_and_stops_cleanly(tmp_path):
    ffmpeg = _fake_ffmpeg(
        tmp_path,
        "while True:\n    chunk = sys.stdin.buffer.read(65536)\n    if not chunk:\n        break",
    )
    camera = VirtualCameraOutput(
        "/dev/video99", width=160, height=120, fps=20, ffmpeg_binary=ffmpeg, output_args=["-"]
    )
    health = camera.start()
    assert health.producer_ok, health
    assert _wait(lambda: camera.health().last_frame_at is not None)
    pid = camera.producer_pid()
    assert pid
    camera.stop(timeout_s=3.0)
    assert camera.producer_pid() is None
    assert camera.health().producer_ok is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess semantics")
def test_producer_restart_budget_then_audio_only(tmp_path):
    # ``false`` exits immediately, so the test measures supervision rather
    # than interpreter start-up time.
    ffmpeg = shutil.which("false") or "/bin/false"
    camera = VirtualCameraOutput(
        "/dev/video99", width=160, height=120, fps=20, ffmpeg_binary=ffmpeg, output_args=["-"]
    )
    camera.start()
    assert _wait(lambda: camera.health().restarts >= 4 and not camera.health().available, timeout=20)
    health = camera.health()
    assert health.error_code == "camera_producer_failed"
    assert health.producer_ok is False
    camera.stop()


def test_renderer_failure_sends_fallback_frame(tmp_path):
    ffmpeg = _fake_ffmpeg(
        tmp_path,
        "while True:\n    chunk = sys.stdin.buffer.read(65536)\n    if not chunk:\n        break",
    )

    def broken(_scene, _phase, _level):
        raise RuntimeError("renderer bug")

    camera = VirtualCameraOutput(
        "/dev/video99", width=160, height=120, fps=20, ffmpeg_binary=ffmpeg,
        output_args=["-"], frame_source=broken,
    )
    camera.start()
    assert _wait(lambda: camera.health().last_frame_at is not None)
    assert camera.health().restarts == 0
    camera.stop()


def test_missing_device_is_reported_without_starting(tmp_path):
    camera = VirtualCameraOutput(str(tmp_path / "nope"), ffmpeg_binary="/bin/false")
    camera.device = "/dev/video_missing_for_test"
    health = camera.start()
    assert health.error_code == "camera_device_missing"
    assert camera.producer_pid() is None


def test_ffmpeg_command_never_uses_shell_and_targets_device():
    camera = VirtualCameraOutput("/dev/video20", width=640, height=360, fps=12)
    cmd = camera._command()
    assert cmd[-3:] == ["-f", "v4l2", "/dev/video20"]
    assert "640x360" in cmd and "rgb24" in cmd
