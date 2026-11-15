"""Phase 0 host probe: virtual camera -> Chromium, without a FaceTime call.

Runs inside the FaceTime worker image (entrypoint with ``MECHUS_FT_PROBE=1``)
after PulseAudio and Xvfb are up. Prints one JSON report and exits 0 only when
every check passes.

    docker run --rm --device /dev/video20:/dev/video20:rwm \
        -e MECHUS_FT_PROBE=1 -e MECHUS_FT_VIDEO_DEVICE=/dev/video20 mechus-facetime-worker:latest
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import numpy as np

from mechus.facetime_video import VideoScene, VirtualCameraOutput

TEST_RGB = (220, 40, 160)  # saturated magenta; easy to distinguish after YUYV conversion
TOLERANCE = 45


class _Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def run(self, name: str, fn: Callable[[], tuple[bool, str]]) -> bool:
        started = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.checks.append({
            "name": name,
            "ok": bool(ok),
            "detail": str(detail)[:800],
            "ms": int((time.monotonic() - started) * 1000),
        })
        return bool(ok)

    def dump(self) -> int:
        passed = all(c["ok"] for c in self.checks) and bool(self.checks)
        print(json.dumps({"passed": passed, "checks": self.checks}, indent=2))
        return 0 if passed else 1


def _serve_blank_page() -> tuple[http.server.HTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"<!doctype html><title>probe</title><canvas id=c></canvas>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _chrome() -> Any:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    opts = webdriver.ChromeOptions()
    for path in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(path):
            opts.binary_location = path
            break
    # Same media-relevant flags as the production worker. Deliberately NOT
    # --use-fake-device-for-media-stream (that would replace the Pulse mic).
    for arg in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,900",
        "--use-fake-ui-for-media-stream",
        "--alsa-output-device=pulse",
        "--alsa-input-device=pulse",
        "--disable-features=AudioServiceSandbox,AudioServiceOutOfProcess",
    ):
        opts.add_argument(arg)
    driver_path = shutil.which("chromedriver") or "/usr/bin/chromedriver"
    driver = webdriver.Chrome(service=Service(driver_path), options=opts)
    driver.set_script_timeout(20)
    return driver


_SAMPLE_JS = r"""
const done = arguments[arguments.length - 1];
const hint = String(arguments[0] || 'mechus').toLowerCase();
(async () => {
  try {
    // A first getUserMedia call is needed before labels are populated.
    const warm = await navigator.mediaDevices.getUserMedia({video: true, audio: false});
    warm.getTracks().forEach(t => t.stop());
    const devices = await navigator.mediaDevices.enumerateDevices();
    const videos = devices.filter(d => d.kind === 'videoinput');
    const mechus = videos.filter(d => (d.label || '').toLowerCase().includes(hint));
    if (mechus.length !== 1) {
      done({ok: false, labels: videos.map(d => d.label), error: 'expected exactly one Mechus camera'});
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true, video: {deviceId: {exact: mechus[0].deviceId}}
    });
    const video = document.createElement('video');
    video.muted = true; video.playsInline = true; video.srcObject = stream;
    await video.play();
    await new Promise(r => setTimeout(r, 1200));
    const c = document.getElementById('c');
    c.width = video.videoWidth; c.height = video.videoHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(video, 0, 0);
    const px = ctx.getImageData(0, 0, c.width, c.height).data;
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < px.length; i += 4 * 97) { r += px[i]; g += px[i+1]; b += px[i+2]; n++; }
    const audio = stream.getAudioTracks().map(t => ({label: t.label, state: t.readyState}));
    stream.getTracks().forEach(t => t.stop());
    done({ok: true, labels: videos.map(d => d.label), width: c.width, height: c.height,
          mean: [r / n, g / n, b / n], audio: audio});
  } catch (e) {
    done({ok: false, error: String(e && e.name || e) + ': ' + String(e && e.message || '')});
  }
})();
"""


def main() -> int:
    report = _Report()
    device = (os.environ.get("MECHUS_FT_VIDEO_DEVICE") or "").strip()
    quick = (os.environ.get("MECHUS_FT_PROBE_QUICK") or "0").strip() in ("1", "true", "yes")
    hint = (os.environ.get("MECHUS_FT_CAMERA_LABEL_HINT") or "mechus").strip()

    if not report.run(
        "device_present",
        lambda: (bool(device) and os.path.exists(device), f"MECHUS_FT_VIDEO_DEVICE={device!r}"),
    ):
        return report.dump()

    def _formats() -> tuple[bool, str]:
        out = subprocess.run(
            ["v4l2-ctl", "--device", device, "--info"],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0, (out.stdout or out.stderr).strip()[:600]

    report.run("v4l2_info", _formats)

    width = int(os.environ.get("MECHUS_FT_VIDEO_WIDTH", "640"))
    height = int(os.environ.get("MECHUS_FT_VIDEO_HEIGHT", "360"))
    solid = np.empty((height, width, 3), dtype=np.uint8)
    solid[:, :] = TEST_RGB
    camera = VirtualCameraOutput(
        device,
        width=width,
        height=height,
        fps=int(os.environ.get("MECHUS_FT_VIDEO_FPS", "12")),
        frame_source=lambda _scene, _phase, _level: solid,
    )
    camera.set_scene(VideoScene(True))

    def _start() -> tuple[bool, str]:
        health = camera.start()
        time.sleep(0.5)
        health = camera.health()
        return health.producer_ok and health.last_frame_at is not None, json.dumps(health.to_status())

    if not report.run("producer_started", _start):
        camera.stop()
        return report.dump()

    server, port = _serve_blank_page()
    driver = None
    try:
        driver = _chrome()
        driver.get(f"http://127.0.0.1:{port}/")
        sample: dict[str, Any] = {}

        def _enumerate_and_sample() -> tuple[bool, str]:
            nonlocal sample
            sample = driver.execute_async_script(_SAMPLE_JS, hint) or {}
            return bool(sample.get("ok")), json.dumps(sample)

        if report.run("chromium_enumerates_and_streams", _enumerate_and_sample):
            def _colour() -> tuple[bool, str]:
                mean = [float(x) for x in sample.get("mean") or [0, 0, 0]]
                diff = [abs(m - e) for m, e in zip(mean, TEST_RGB)]
                return max(diff) <= TOLERANCE, f"mean={mean} expected={TEST_RGB} maxdiff={max(diff):.1f}"

            report.run("frame_matches_test_pattern", _colour)

            def _audio() -> tuple[bool, str]:
                tracks = sample.get("audio") or []
                live = [t for t in tracks if t.get("state") == "live"]
                expected = (os.environ.get("MECHUS_FT_PULSE_SOURCE") or "mechus_mic_source").strip()
                sources = subprocess.run(
                    ["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=10
                ).stdout
                return bool(live) and expected in sources, (
                    f"audio_tracks={tracks} pulse_source_present={expected in sources}"
                )

            report.run("pulse_mic_unaffected", _audio)

        if not quick:
            def _restart() -> tuple[bool, str]:
                pid = camera.producer_pid()
                if pid is None:
                    return False, "no producer pid"
                os.kill(pid, signal.SIGKILL)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    health = camera.health()
                    new_pid = camera.producer_pid()
                    if health.producer_ok and new_pid and new_pid != pid and health.restarts >= 1:
                        return True, f"restarted pid {pid} -> {new_pid} restarts={health.restarts}"
                    time.sleep(0.2)
                return False, json.dumps(camera.health().to_status())

            report.run("producer_restarts_after_kill", _restart)
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        server.shutdown()
        camera.stop()

    report.run(
        "device_released",
        lambda: (camera.producer_pid() is None, "producer stopped"),
    )
    return report.dump()


if __name__ == "__main__":
    sys.exit(main())
