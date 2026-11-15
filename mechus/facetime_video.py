"""Generated virtual-camera frames for FaceTime web workers."""

from __future__ import annotations

import functools
import math
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont


class CameraMode(str, Enum):
    OFF = "off"
    AVATAR = "avatar"
    ACTIVITY = "activity"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    STATUS_CARD = "status_card"
    MESSAGE_CARD = "message_card"


@dataclass(frozen=True)
class VideoScene:
    enabled: bool = False
    mode: CameraMode = CameraMode.AVATAR
    display_name: str = "Mechus"
    text: str = ""
    expires_at: float | None = None


@dataclass
class CameraHealth:
    available: bool = False
    producer_ok: bool = False
    last_frame_at: float | None = None
    restarts: int = 0
    error_code: str = ""
    error: str = ""

    def to_status(self) -> dict[str, object]:
        return asdict(self)


@functools.lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = " ".join(str(text or "").split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:5]


@functools.lru_cache(maxsize=4)
def _background(width: int, height: int) -> Image.Image:
    """Soft vertical gradient, built once per frame size (callers must copy)."""
    image = Image.new("RGB", (width, height), (11, 18, 31))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        p = y / max(1, height - 1)
        draw.line((0, y, width, y), fill=(11 + int(8 * p), 18 + int(12 * p), 31 + int(18 * p)))
    return image


def render_scene(
    scene: VideoScene,
    *,
    width: int = 640,
    height: int = 360,
    phase: float = 0.0,
    audio_level: float = 0.0,
) -> np.ndarray:
    """Render one RGB24 frame. Pure enough for deterministic unit tests."""
    width = max(160, int(width))
    height = max(120, int(height))
    image = _background(width, height).copy()
    draw = ImageDraw.Draw(image)

    accent = (103, 232, 249)
    muted = (148, 163, 184)
    white = (241, 245, 249)
    cx, cy = width // 2, int(height * 0.43)
    radius = max(38, int(min(width, height) * 0.16))
    pulse = 1.0
    if scene.mode in (CameraMode.LISTENING, CameraMode.THINKING, CameraMode.SPEAKING, CameraMode.ACTIVITY):
        pulse += 0.05 * math.sin(phase * math.tau)
    rr = int(radius * pulse)
    draw.ellipse((cx - rr - 8, cy - rr - 8, cx + rr + 8, cy + rr + 8), outline=(30, 64, 90), width=5)
    draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(15, 33, 51), outline=accent, width=4)

    logo_font = _font(max(24, int(radius * 0.70)))
    label = "K"
    box = draw.textbbox((0, 0), label, font=logo_font)
    draw.text((cx - (box[2] - box[0]) / 2, cy - (box[3] - box[1]) / 2 - box[1]), label, font=logo_font, fill=white)

    mode = scene.mode
    if mode == CameraMode.SPEAKING:
        level = max(0.0, min(float(audio_level), 1.0))
        # Keep the tallest bar clear of the name label drawn at 73% height
        # (at small frame sizes the bars shrink rather than cover the name).
        name_top = int(height * 0.73)
        base_y = min(cy + rr + 22, name_top - 10)
        full_h = max(8, 2 * (name_top - 8 - base_y))
        bars = 17
        bar_w = max(3, width // 150)
        gap = max(3, bar_w)
        total = bars * bar_w + (bars - 1) * gap
        left = cx - total // 2
        for idx in range(bars):
            wave = 0.35 + 0.65 * abs(math.sin(phase * 8.0 + idx * 0.65))
            h = int(6 + (full_h - 6) * (0.3 + 0.7 * level) * wave)
            x = left + idx * (bar_w + gap)
            draw.rounded_rectangle((x, base_y - h // 2, x + bar_w, base_y + h // 2), radius=2, fill=accent)
    elif mode == CameraMode.THINKING:
        orbit = radius + 20
        for idx in range(3):
            angle = phase * math.tau + idx * math.tau / 3
            x = cx + int(math.cos(angle) * orbit)
            y = cy + int(math.sin(angle) * orbit)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=accent)
    elif mode == CameraMode.LISTENING:
        arc_r = radius + 20 + int(4 * math.sin(phase * math.tau))
        draw.arc((cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r), 205, 335, fill=accent, width=5)

    title_font = _font(max(18, int(height * 0.060)))
    name = " ".join(str(scene.display_name or "Mechus").split())[:80]
    title_box = draw.textbbox((0, 0), name, font=title_font)
    draw.text((cx - (title_box[2] - title_box[0]) / 2, int(height * 0.73)), name, font=title_font, fill=white)

    if mode in (CameraMode.STATUS_CARD, CameraMode.MESSAGE_CARD) and scene.text:
        card_margin = max(24, width // 16)
        card_top = int(height * 0.12)
        card_bottom = int(height * 0.88)
        draw.rounded_rectangle(
            (card_margin, card_top, width - card_margin, card_bottom),
            radius=18,
            fill=(15, 33, 51),
            outline=accent,
            width=3,
        )
        card_font = _font(max(18, int(height * 0.062)))
        lines = _fit_text(draw, scene.text[:200], card_font, width - card_margin * 3)
        line_h = max(24, int(height * 0.085))
        y = height // 2 - len(lines) * line_h // 2
        for line in lines:
            b = draw.textbbox((0, 0), line, font=card_font)
            draw.text((cx - (b[2] - b[0]) / 2, y), line, font=card_font, fill=white)
            y += line_h
    else:
        sub_font = _font(max(13, int(height * 0.040)))
        labels = {
            CameraMode.SPEAKING: "Speaking",
            CameraMode.THINKING: "Thinking",
            CameraMode.LISTENING: "Listening",
            CameraMode.ACTIVITY: "Ready",
            CameraMode.AVATAR: "Meeting assistant",
            CameraMode.OFF: "Camera off",
        }
        sub = labels.get(mode, "Meeting assistant")
        b = draw.textbbox((0, 0), sub, font=sub_font)
        draw.text((cx - (b[2] - b[0]) / 2, int(height * 0.82)), sub, font=sub_font, fill=muted)

    return np.asarray(image, dtype=np.uint8)


def _fallback_frame(width: int, height: int) -> np.ndarray:
    return np.asarray(_background(width, height), dtype=np.uint8)


class VirtualCameraOutput:
    """Own and supervise an FFmpeg process writing a generated V4L2 stream."""

    def __init__(
        self,
        device: str,
        *,
        width: int = 640,
        height: int = 360,
        fps: int = 12,
        display_name: str = "Mechus",
        ffmpeg_binary: str = "ffmpeg",
        frame_source: Callable[[VideoScene, float, float], np.ndarray] | None = None,
        output_args: list[str] | None = None,
    ) -> None:
        self.device = str(device or "").strip()
        self.width = max(160, min(int(width), 1920))
        self.height = max(120, min(int(height), 1080))
        self.fps = max(5, min(int(fps), 20))
        self.ffmpeg_binary = ffmpeg_binary
        # Tests and the host probe can substitute a known pattern / sink.
        self._frame_source = frame_source
        self._output_args = list(output_args) if output_args is not None else None
        self._scene = VideoScene(False, CameraMode.AVATAR, display_name)
        self._scene_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._health = CameraHealth()
        self._health_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=40)
        self._audio_level = 0.0

    def set_scene(self, scene: VideoScene) -> None:
        with self._scene_lock:
            self._scene = scene

    def set_audio_level(self, rms: float) -> None:
        target = max(0.0, min(float(rms), 1.0))
        self._audio_level = self._audio_level * 0.75 + target * 0.25

    def _set_health(self, **values: object) -> None:
        with self._health_lock:
            for key, value in values.items():
                if hasattr(self._health, key):
                    setattr(self._health, key, value)

    def health(self) -> CameraHealth:
        with self._health_lock:
            return CameraHealth(**asdict(self._health))

    def _command(self) -> list[str]:
        return [
            self.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.fps),
            "-i",
            "pipe:0",
            "-an",
            "-vf",
            "format=yuyv422",
            *(self._output_args if self._output_args is not None else ["-f", "v4l2", self.device]),
        ]

    def _start_process(self) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
        )
        self._proc = proc
        if proc.stderr is not None:
            threading.Thread(target=self._drain_stderr, args=(proc.stderr,), daemon=True).start()
        return proc

    def _drain_stderr(self, stream: BinaryIO) -> None:
        try:
            for raw in iter(stream.readline, b""):
                self._stderr.append(raw.decode("utf-8", errors="replace").strip()[:500])
        except Exception:
            return

    def start(self) -> CameraHealth:
        if self._thread is not None and self._thread.is_alive():
            return self.health()
        if not self.device:
            self._set_health(error_code="camera_disabled", error="No virtual camera device assigned")
            return self.health()
        if self._output_args is None and not Path(self.device).exists():
            self._set_health(error_code="camera_device_missing", error=f"Camera device is missing: {self.device}")
            return self.health()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mechus-virtual-camera")
        self._thread.start()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            health = self.health()
            if health.producer_ok or health.error_code:
                return health
            time.sleep(0.05)
        return self.health()

    def _run(self) -> None:
        restarts = 0
        frame_interval = 1.0 / self.fps
        started = time.monotonic()
        while not self._stop.is_set() and restarts <= 3:
            try:
                proc = self._start_process()
                self._set_health(available=True, producer_ok=True, error_code="", error="", restarts=restarts)
                next_frame = time.monotonic()
                while not self._stop.is_set():
                    if proc.poll() is not None:
                        detail = "; ".join(x for x in list(self._stderr)[-3:] if x)
                        raise RuntimeError(detail or f"FFmpeg exited with {proc.returncode}")
                    with self._scene_lock:
                        scene = self._scene
                    phase = (time.monotonic() - started) % 1.0
                    try:
                        if self._frame_source is not None:
                            frame = self._frame_source(scene, phase, self._audio_level)
                        else:
                            frame = render_scene(
                                scene,
                                width=self.width,
                                height=self.height,
                                phase=phase,
                                audio_level=self._audio_level,
                            )
                        if frame.shape != (self.height, self.width, 3):
                            raise ValueError(f"frame shape {frame.shape} does not match output")
                    except Exception:
                        # A renderer bug must not kill the stream: send a safe frame.
                        frame = _fallback_frame(self.width, self.height)
                    if proc.stdin is None:
                        raise RuntimeError("FFmpeg stdin unavailable")
                    proc.stdin.write(frame.tobytes(order="C"))
                    now = time.time()
                    self._set_health(last_frame_at=now, producer_ok=True)
                    next_frame += frame_interval
                    wait = next_frame - time.monotonic()
                    if wait > 0:
                        self._stop.wait(wait)
                    else:
                        next_frame = time.monotonic()
                break
            except Exception as exc:
                restarts += 1
                self._set_health(
                    producer_ok=False,
                    restarts=restarts,
                    error_code="camera_producer_failed",
                    error=str(exc)[:500],
                )
                self._terminate_process()
                if restarts <= 3:
                    self._stop.wait(min(0.5 * (2 ** (restarts - 1)), 2.0))
        if not self._stop.is_set():
            # Restart budget exhausted: the call continues audio-only.
            self._set_health(available=False, producer_ok=False, error_code="camera_producer_failed")
        self._terminate_process()

    def producer_pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._terminate_process()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(0.1, timeout_s))
        self._set_health(producer_ok=False)

