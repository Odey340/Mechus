"""Async cloud diarization wrapper for FaceTime/single-stream meetings.

Speaker attribution is decoupled from the wake/TTS path: every transcript
chunk is submitted to the background thread pool immediately; when the cloud
result arrives the speaker labels are patched into MeetingStore and notes
without touching latency-sensitive code.

Usage inside KoovAgent
----------------------
    # Build once (after _diarizer is constructed):
    from koov.diarization_async import AsyncCloudDiarizer
    self._async_diarizer = AsyncCloudDiarizer(self._diarizer)
    self._async_diarizer.start()

    # In _handle_transcript_chunk fast path (after wake is scheduled):
    if self._async_diarizer is not None and duration_s >= 2.0:
        self._async_diarizer.submit(chunk_id, audio_f32, sample_rate, chunk.start_ts, callback)

    # On shutdown:
    self._async_diarizer.stop()
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from koov.diarization import SpeakerSegment

log = logging.getLogger(__name__)

# Minimum chunk duration before we bother submitting to the cloud (short audio
# rarely produces meaningful diarization and wastes API quota).
_MIN_SUBMIT_DURATION_S: float = 2.0

# How many chunks can queue up before we start dropping (prevents unbounded
# memory growth if the cloud is slow or the call is long).
_QUEUE_MAX: int = 32


@dataclass
class _DiarizeJob:
    chunk_id: int
    audio_f32: np.ndarray
    sample_rate: int
    chunk_ts: float
    callback: Callable[[int, list[SpeakerSegment]], None]


class AsyncCloudDiarizer:
    """Background thread pool that submits audio to a cloud diarizer and fires
    a callback with speaker segments when the result is ready.

    The callback is ``callback(chunk_id, segments)`` where *segments* is a list
    of :class:`~koov.diarization.SpeakerSegment` objects.  It is invoked from
    the worker thread — callers must be thread-safe.
    """

    def __init__(
        self,
        diarizer: Any,  # PyannoteCloudDiarizer
        *,
        workers: int = 2,
    ) -> None:
        self._diarizer = diarizer
        self._workers = workers
        self._queue: queue.Queue[_DiarizeJob | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._threads: list[threading.Thread] = []
        self._running = False
        self._submitted = 0
        self._completed = 0
        self._dropped = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self._workers):
            t = threading.Thread(
                target=self._run,
                daemon=True,
                name=f"async-diarize-{i}",
            )
            t.start()
            self._threads.append(t)
        log.info("AsyncCloudDiarizer started with %d worker(s)", self._workers)

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        # Poison-pill each worker.
        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        log.info(
            "AsyncCloudDiarizer stopped — submitted=%d completed=%d dropped=%d",
            self._submitted,
            self._completed,
            self._dropped,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        chunk_id: int,
        audio_f32: np.ndarray,
        sample_rate: int,
        chunk_ts: float,
        callback: Callable[[int, list[SpeakerSegment]], None],
    ) -> bool:
        """Submit a chunk for async diarization.  Returns False if the chunk is
        too short or the queue is full (drop silently to protect latency)."""
        if not self._running:
            return False
        duration_s = audio_f32.shape[0] / max(1, sample_rate)
        if duration_s < _MIN_SUBMIT_DURATION_S:
            return False
        job = _DiarizeJob(
            chunk_id=chunk_id,
            audio_f32=audio_f32.copy(),  # avoid race with caller mutating buffer
            sample_rate=sample_rate,
            chunk_ts=chunk_ts,
            callback=callback,
        )
        try:
            self._queue.put_nowait(job)
            self._submitted += 1
            return True
        except queue.Full:
            self._dropped += 1
            log.debug(
                "AsyncCloudDiarizer: queue full — dropping chunk_id=%d (submitted=%d dropped=%d)",
                chunk_id,
                self._submitted,
                self._dropped,
            )
            return False

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if job is None:
                break
            try:
                segments = self._diarize_job(job)
                self._completed += 1
                try:
                    job.callback(job.chunk_id, segments)
                except Exception as cb_exc:
                    log.warning(
                        "AsyncCloudDiarizer: callback raised for chunk_id=%d: %s",
                        job.chunk_id,
                        cb_exc,
                    )
            except Exception as exc:
                log.warning(
                    "AsyncCloudDiarizer: diarization failed for chunk_id=%d: %s",
                    job.chunk_id,
                    exc,
                )
            finally:
                self._queue.task_done()

    def _diarize_job(self, job: _DiarizeJob) -> list[SpeakerSegment]:
        t0 = time.monotonic()
        segments: list[SpeakerSegment] = self._diarizer.diarize(
            job.audio_f32, job.sample_rate
        )
        elapsed = time.monotonic() - t0
        log.debug(
            "AsyncCloudDiarizer: chunk_id=%d → %d segment(s) in %.1fs",
            job.chunk_id,
            len(segments),
            elapsed,
        )
        return segments
