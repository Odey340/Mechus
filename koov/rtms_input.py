"""
Zoom RTMS (Realtime Media Streams) audio input for Koov.

Provides real-time meeting audio via Zoom RTMS WebSockets when the user
has a Zoom App installed. No bot joins—Zoom streams audio to your server.

Requires: pip install rtms
See: docs/RTMS_SETUP.md
"""
from __future__ import annotations

import logging
import queue
import time
from typing import Optional

from koov.audio import AudioFrame

log = logging.getLogger(__name__)

# RTMS default audio format (check Zoom docs; often 16kHz mono PCM)
RTMS_DEFAULT_SAMPLE_RATE = 16000


class RTMSAudioInput:
    """
    Audio input that reads PCM frames from a queue filled by the RTMS onAudioData callback.
    Same interface as ZoomAudioInput / koov.audio.AudioInput.
    """

    def __init__(self, sample_rate_hz: int, frame_ms: int):
        self._sample_rate_hz = sample_rate_hz
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
        self._queue: queue.Queue[AudioFrame] = queue.Queue(maxsize=500)
        self._running = True
        self._frames_received = 0
        self._last_log_time: Optional[float] = None

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    def push_frame(self, pcm16: bytes, timestamp_s: float) -> None:
        """Called by RTMS onAudioData when audio is received (already at agent sample rate)."""
        if not self._running or not pcm16:
            return
        self._frames_received += 1
        now = time.monotonic()
        if self._last_log_time is None:
            self._last_log_time = now
            log.info("RTMS audio: first frame received; Koov can hear the meeting")
        elif now - self._last_log_time >= 5.0:
            log.info("RTMS audio: %s frames in last 5s", self._frames_received)
            self._frames_received = 0
            self._last_log_time = now
        try:
            self._queue.put_nowait(AudioFrame(pcm16=pcm16, timestamp_s=timestamp_s))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(AudioFrame(pcm16=pcm16, timestamp_s=timestamp_s))
            except queue.Full:
                pass

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def read(self, timeout_s: float = 1.0) -> Optional[AudioFrame]:
        try:
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None


def rtms_available() -> bool:
    """Return True if rtms package is installed and usable."""
    try:
        import rtms  # noqa: F401
        return True
    except ImportError:
        return False
