"""Tests for AsyncCloudDiarizer — queue submit + callback via mock cloud."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

from mechus.diarization import SpeakerSegment
from mechus.diarization_async import AsyncCloudDiarizer


class _MockCloudDiarizer:
    """Synchronous mock that returns a predictable segment list."""

    def __init__(self, segments: list[SpeakerSegment], delay_s: float = 0.0) -> None:
        self._segments = segments
        self._delay_s = delay_s
        self.call_count = 0

    def diarize(self, audio_f32: np.ndarray, sample_rate: int) -> list[SpeakerSegment]:
        self.call_count += 1
        if self._delay_s:
            time.sleep(self._delay_s)
        return list(self._segments)


_FAKE_SEGMENTS = [
    SpeakerSegment(start_s=0.0, end_s=2.5, speaker="SPEAKER_00"),
    SpeakerSegment(start_s=2.5, end_s=5.0, speaker="SPEAKER_01"),
]


def _make_audio(duration_s: float = 3.0, sample_rate: int = 16000) -> np.ndarray:
    """Return silence audio of the given duration."""
    return np.zeros(int(duration_s * sample_rate), dtype=np.float32)


# ── Basic submit + callback ──────────────────────────────────────────────────

def test_submit_fires_callback() -> None:
    mock = _MockCloudDiarizer(_FAKE_SEGMENTS)
    received: list[tuple[int, list[SpeakerSegment]]] = []
    done = threading.Event()

    def callback(chunk_id: int, segments: list[SpeakerSegment]) -> None:
        received.append((chunk_id, segments))
        done.set()

    diarizer = AsyncCloudDiarizer(mock, workers=1)
    diarizer.start()
    try:
        submitted = diarizer.submit(
            chunk_id=42,
            audio_f32=_make_audio(3.0),
            sample_rate=16000,
            chunk_ts=0.0,
            callback=callback,
        )
        assert submitted is True
        assert done.wait(timeout=5.0), "callback never fired within 5s"
    finally:
        diarizer.stop()

    assert len(received) == 1
    chunk_id, segs = received[0]
    assert chunk_id == 42
    assert segs == _FAKE_SEGMENTS
    assert mock.call_count == 1


def test_short_audio_not_submitted() -> None:
    """Chunks shorter than _MIN_SUBMIT_DURATION_S should be silently dropped."""
    mock = _MockCloudDiarizer(_FAKE_SEGMENTS)
    diarizer = AsyncCloudDiarizer(mock, workers=1)
    diarizer.start()
    try:
        # 1s < _MIN_SUBMIT_DURATION_S (2s)
        result = diarizer.submit(
            chunk_id=1,
            audio_f32=_make_audio(1.0),
            sample_rate=16000,
            chunk_ts=0.0,
            callback=lambda *a: None,
        )
        assert result is False
        # Give worker a moment to drain
        time.sleep(0.1)
    finally:
        diarizer.stop()

    assert mock.call_count == 0


def test_not_started_submit_returns_false() -> None:
    mock = _MockCloudDiarizer(_FAKE_SEGMENTS)
    diarizer = AsyncCloudDiarizer(mock, workers=1)
    # Never call .start()
    result = diarizer.submit(42, _make_audio(3.0), 16000, 0.0, lambda *a: None)
    assert result is False


def test_stop_before_start_is_noop() -> None:
    mock = _MockCloudDiarizer([])
    diarizer = AsyncCloudDiarizer(mock, workers=1)
    diarizer.stop()  # should not raise


def test_multiple_chunks_all_fire() -> None:
    mock = _MockCloudDiarizer(_FAKE_SEGMENTS, delay_s=0.02)
    seen: set[int] = set()
    lock = threading.Lock()
    expected = {10, 20, 30}
    done = threading.Event()

    def callback(chunk_id: int, segments: list[SpeakerSegment]) -> None:
        with lock:
            seen.add(chunk_id)
            if seen >= expected:
                done.set()

    diarizer = AsyncCloudDiarizer(mock, workers=2)
    diarizer.start()
    try:
        for cid in expected:
            diarizer.submit(cid, _make_audio(3.0), 16000, 0.0, callback)
        assert done.wait(timeout=10.0), "not all callbacks fired"
    finally:
        diarizer.stop()

    assert seen == expected


def test_callback_exception_does_not_crash_worker() -> None:
    """A callback that raises should not kill the worker thread."""
    mock = _MockCloudDiarizer(_FAKE_SEGMENTS)
    second_done = threading.Event()
    results: list[int] = []

    def bad_callback(chunk_id: int, segments: list[SpeakerSegment]) -> None:
        if chunk_id == 1:
            raise RuntimeError("simulated callback error")
        results.append(chunk_id)
        second_done.set()

    diarizer = AsyncCloudDiarizer(mock, workers=1)
    diarizer.start()
    try:
        diarizer.submit(1, _make_audio(3.0), 16000, 0.0, bad_callback)
        time.sleep(0.05)  # let first job process
        diarizer.submit(2, _make_audio(3.0), 16000, 0.0, bad_callback)
        assert second_done.wait(timeout=5.0), "second callback never fired"
    finally:
        diarizer.stop()

    assert 2 in results
