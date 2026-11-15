"""Let the tenant talk to Mechus during a walkthrough: "skip", "repeat", "that's it".

On inspection calls the wake-word agent is not running, so nothing was listening. This
reads the call's audio, cuts it into utterances with a simple loudness gate, transcribes
each one, and hands the text to ``Inspector.hear``.

Audio that arrives while Mechus is speaking (and for a moment after) is thrown away: on
FaceTime the caller's audio can contain Mechus's own voice, and it must not obey itself.

Env:
  MECHUS_INSPECT_LISTEN        0 to turn listening off (default on)
  MECHUS_INSPECT_LISTEN_RMS    loudness (0-32767) that counts as speech (default 550)
"""
from __future__ import annotations

import array
import logging
import math
import os
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


def rms(pcm16: bytes) -> float:
    """Root-mean-square loudness of 16-bit mono PCM."""
    if len(pcm16) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - (len(pcm16) % 2)])
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class UtteranceCutter:
    """Feed fixed-size frames; get back a whole utterance when the speaker pauses."""

    def __init__(self, frame_ms: int, *, threshold: float = 550.0, start_ms: int = 150,
                 end_silence_ms: int = 700, min_ms: int = 350, max_ms: int = 8000) -> None:
        self.frame_ms = max(1, int(frame_ms))
        self.threshold = float(threshold)
        self._start_frames = max(1, start_ms // self.frame_ms)
        self._end_frames = max(1, end_silence_ms // self.frame_ms)
        self._min_frames = max(1, min_ms // self.frame_ms)
        self._max_frames = max(1, max_ms // self.frame_ms)
        self.reset()

    def reset(self) -> None:
        self._frames: list[bytes] = []
        self._loud_run = 0
        self._quiet_run = 0
        self._voiced = 0
        self._speaking = False

    def feed(self, pcm16: bytes) -> bytes | None:
        loud = rms(pcm16) >= self.threshold
        if not self._speaking:
            self._loud_run = self._loud_run + 1 if loud else 0
            self._frames.append(pcm16)
            del self._frames[: -self._start_frames - 2]  # keep a short lead-in
            if self._loud_run >= self._start_frames:
                self._speaking, self._quiet_run, self._voiced = True, 0, self._loud_run
            return None
        self._frames.append(pcm16)
        if loud:
            self._voiced += 1
            self._quiet_run = 0
        else:
            self._quiet_run += 1
        if self._quiet_run >= self._end_frames or len(self._frames) >= self._max_frames:
            frames, voiced = self._frames, self._voiced
            self.reset()
            return b"".join(frames) if voiced >= self._min_frames else None
        return None


class TenantListener:
    """Background thread: call audio -> utterances -> text -> ``on_text``."""

    def __init__(self, *, audio_in: Any, transcribe: Callable[[bytes, int], Any], on_text: Callable[[str], Any],
                 is_speaking: Callable[[], bool], sample_rate_hz: int, frame_ms: int, after_speech_s: float = 1.0) -> None:
        self._audio_in = audio_in
        self._transcribe = transcribe
        self._on_text = on_text
        self._is_speaking = is_speaking
        self._sample_rate_hz = int(sample_rate_hz)
        self._after_speech_s = float(after_speech_s)
        self._cutter = UtteranceCutter(
            frame_ms, threshold=float(os.environ.get("MECHUS_INSPECT_LISTEN_RMS", "550") or "550"))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mechus-tenant-listener")
        self.utterances = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        quiet_until = 0.0
        while not self._stop.is_set():
            frame = self._audio_in.read(timeout_s=0.5)
            if frame is None:
                continue
            now = time.monotonic()
            speaking = self._is_speaking()
            if speaking:
                quiet_until = now + self._after_speech_s
            if speaking or now < quiet_until:
                self._cutter.reset()
                continue
            utterance = self._cutter.feed(frame.pcm16)
            if utterance is None:
                continue
            self.utterances += 1
            try:
                result = self._transcribe(utterance, self._sample_rate_hz)
                text = str(getattr(result, "text", result) or "").strip()
            except Exception as exc:  # noqa: BLE001
                log.warning("Tenant listener: transcription failed: %s", exc)
                continue
            if not text:
                continue
            log.info("[HEARD] %s", text)
            try:
                self._on_text(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("Tenant listener: could not act on %r: %s", text, exc)
