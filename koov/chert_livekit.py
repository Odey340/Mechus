"""LiveKit room transport for Chert FaceTime calls.

Provides ``LiveKitAudioInput`` / ``LiveKitAudioOutput`` with the same interface as
``koov.audio.AudioInput`` / ``AudioOutput`` (and the Zoom equivalents), so ``KoovAgent``
runs unchanged: meeting audio in, TTS out, no sound card and no browser.

The LiveKit SDK is asyncio-based while KoovAgent is thread-based, so the room lives on a
dedicated event-loop thread and the audio classes bridge into it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import io
import threading
import time
from typing import Any, Optional

import numpy as np

from koov.audio import AudioFrame
from koov.chert_client import REMOTE_AUDIO_TRACK

log = logging.getLogger(__name__)

# Frames handed to LiveKit per capture call. 20 ms keeps stop()/barge-in responsive.
_SEND_FRAME_MS = 20
# How much audio LiveKit may buffer ahead of real time. Small enough that stop() is
# near-instant even before clear_queue(), large enough to ride out thread jitter.
_SOURCE_QUEUE_MS = 400


class LiveKitAudioInput:
    """PCM frames from the FaceTime caller, fed by the room's audio reader task."""

    def __init__(self, sample_rate_hz: int, frame_ms: int) -> None:
        self._sample_rate_hz = int(sample_rate_hz)
        self._frame_ms = int(frame_ms)
        self._frame_bytes = int(self._sample_rate_hz * (self._frame_ms / 1000.0) * 2)
        self._queue: "queue.Queue[AudioFrame]" = queue.Queue(maxsize=500)
        self._running = True
        self._frames_received = 0
        self._frames_total = 0
        self._last_frame_at: float | None = None
        self._last_log_time: float | None = None
        self._pending = bytearray()

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def frames_total(self) -> int:
        return self._frames_total

    @property
    def last_frame_at(self) -> float | None:
        """Wall-clock time of the most recent caller audio frame."""
        return self._last_frame_at

    def set_label_override(self, fn: object) -> None:  # parity with ZoomAudioInput
        return None

    def push_pcm16(self, pcm16: bytes) -> None:
        """Accept arbitrary-length PCM at the agent sample rate; emit exact agent frames."""
        if not self._running or not pcm16:
            return
        self._pending.extend(pcm16)
        fb = self._frame_bytes
        while len(self._pending) >= fb:
            chunk = bytes(self._pending[:fb])
            del self._pending[:fb]
            self._push_frame(chunk)

    def _push_frame(self, pcm16: bytes) -> None:
        self._frames_received += 1
        self._frames_total += 1
        self._last_frame_at = time.time()
        now = time.monotonic()
        if self._last_log_time is None:
            self._last_log_time = now
            log.info("Chert audio: first frame received; Koov can hear the FaceTime call")
        elif now - self._last_log_time >= 30.0:
            log.debug("Chert audio: %s frames in last 30s", self._frames_received)
            self._frames_received = 0
            self._last_log_time = now
        frame = AudioFrame(pcm16=pcm16, timestamp_s=now)
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # Agent is behind: drop the oldest frame rather than stall the room reader.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                pass

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._pending.clear()
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


class LiveKitAudioOutput:
    """Publishes Koov's TTS into the LiveKit room (heard by the FaceTime caller).

    ``rtc.AudioSource.capture_frame`` buffers up to ``_SOURCE_QUEUE_MS`` and then applies
    back-pressure, which paces playback at real time; ``stop()`` flushes that buffer so
    barge-in cuts speech immediately.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, source: Any, sample_rate_hz: int) -> None:
        self._loop = loop
        self._source = source
        self._sample_rate_hz = int(sample_rate_hz)
        self._samples_per_send = max(1, int(self._sample_rate_hz * _SEND_FRAME_MS / 1000))
        # Mirrors koov.audio.AudioOutput: the agent reads ``_stop_event`` to detect barge-in.
        self._stop_event = threading.Event()
        self._play_lock = threading.RLock()
        self._play_thread: threading.Thread | None = None
        # Number of in-flight play calls (blocking, threaded, or streaming).
        self._active = 0
        self._closed = False

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    def is_ready_to_send(self) -> bool:
        return not self._closed and self._loop.is_running()

    def close(self) -> None:
        self._closed = True
        self.stop()

    # ── PCM helpers ──────────────────────────────────────────────────────────

    def _to_pcm16(self, audio_f32: Any, sample_rate_hz: int, gain: float) -> bytes:
        arr = np.asarray(audio_f32, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return b""
        if int(sample_rate_hz) != self._sample_rate_hz:
            from koov.dsp import resample_f32_linear

            arr = resample_f32_linear(arr, src_sr=int(sample_rate_hz), dst_sr=self._sample_rate_hz)
        arr = np.clip(arr * float(gain), -1.0, 1.0)
        return (arr * 32767.0).astype(np.int16).tobytes()

    def _capture(self, pcm16: bytes) -> bool:
        """Send one PCM buffer in 20 ms frames. Returns False if cancelled or the room died."""
        from livekit import rtc

        step = self._samples_per_send * 2
        for off in range(0, len(pcm16), step):
            if self._stop_event.is_set() or self._closed:
                return False
            chunk = pcm16[off : off + step]
            if len(chunk) % 2:
                chunk = chunk[:-1]
            if not chunk:
                break
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=self._sample_rate_hz,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    self._source.capture_frame(frame), self._loop
                ).result(timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("Chert audio out: capture_frame failed: %s", exc)
                return False
        return True

    def _wait_for_playout(self) -> None:
        """Block until LiveKit has actually sent what we queued (or stop() was called)."""
        try:
            fut = asyncio.run_coroutine_threadsafe(self._source.wait_for_playout(), self._loop)
        except Exception:  # noqa: BLE001
            return
        deadline = time.monotonic() + (_SOURCE_QUEUE_MS / 1000.0) + 2.0
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                fut.cancel()
                return
            try:
                fut.result(timeout=0.02)
                return
            except (concurrent.futures.TimeoutError, asyncio.TimeoutError, TimeoutError):
                continue
            except Exception:  # noqa: BLE001
                return

    def _flush_source(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._source.clear_queue)
        except Exception:  # noqa: BLE001
            pass

    def _begin(self) -> None:
        with self._play_lock:
            self._active += 1

    def _end(self) -> None:
        with self._play_lock:
            self._active = max(0, self._active - 1)
            if threading.current_thread() is self._play_thread:
                self._play_thread = None
        self._stop_event.clear()

    def _play_pcm_blocking(self, pcm16: bytes) -> None:
        try:
            if self._capture(pcm16):
                self._wait_for_playout()
        finally:
            self._end()

    # ── AudioOutput interface ────────────────────────────────────────────────

    def play(self, audio_f32: Any, sample_rate_hz: int, gain: float = 1.0, **_: Any) -> None:
        pcm16 = self._to_pcm16(audio_f32, sample_rate_hz, gain)
        if not pcm16:
            return
        self.stop()
        thread = threading.Thread(
            target=self._play_pcm_blocking, args=(pcm16,), daemon=True, name="koov-chert-tts"
        )
        self._begin()
        with self._play_lock:
            self._play_thread = thread
        thread.start()

    def play_blocking(self, audio_f32: Any, sample_rate_hz: int, gain: float = 1.0, **_: Any) -> None:
        pcm16 = self._to_pcm16(audio_f32, sample_rate_hz, gain)
        self.stop()
        if not pcm16:
            return
        self._begin()
        self._play_pcm_blocking(pcm16)

    def play_streaming(
        self,
        chunks_iter: Any,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        skip_edge_ramp_in: bool = False,
        handoff_from_bridge: bool = False,
        keep_mic_live: bool = False,
    ) -> None:
        """Send TTS chunks as they arrive. Returns when the iterator ends or ``stop()`` fires.

        ``handoff_from_bridge`` continues straight after the wake-opener clip, so the
        previous audio must not be flushed. ``keep_mic_live`` means more audio follows
        immediately; skip the playout wait so the next clip joins gaplessly.
        """
        if not handoff_from_bridge:
            self.stop()
        self._stop_event.clear()
        self._begin()
        try:
            for raw_chunk in chunks_iter:
                if self._stop_event.is_set() or self._closed:
                    break
                pcm16 = self._to_pcm16(raw_chunk, sample_rate_hz, gain)
                if not pcm16:
                    continue
                if not self._capture(pcm16):
                    break
            else:
                if not keep_mic_live:
                    self._wait_for_playout()
        finally:
            self._end()

    def wait(self) -> None:
        with self._play_lock:
            thread = self._play_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=120.0)

    def stop(self) -> None:
        self._stop_event.set()
        self._flush_source()
        with self._play_lock:
            thread = self._play_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._play_lock:
            if thread is self._play_thread and (thread is None or not thread.is_alive()):
                self._play_thread = None
            idle = self._active == 0
        # A stop with nothing playing must not poison the next clip; an in-flight
        # player (e.g. play_streaming on the agent thread) clears the flag itself.
        if idle:
            self._stop_event.clear()


class LiveKitVideoCard:
    """Publishes a portrait Pillow-rendered status card into the LiveKit room."""

    WIDTH = 720
    HEIGHT = 1280

    def __init__(self, loop: asyncio.AbstractEventLoop, source: Any) -> None:
        self._loop = loop
        self._source = source
        self._closed = False
        self._lock = threading.Lock()
        self._issue_count = 0
        self._mode = "INSPECTION"
        self._unit = "Property walkthrough"
        self._status = "Waiting for camera"

    @staticmethod
    def _draw(issue_count: int, mode: str, unit: str, status: str):
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGBA", (LiveKitVideoCard.WIDTH, LiveKitVideoCard.HEIGHT), (13, 18, 28, 255))
        draw = ImageDraw.Draw(image)
        try:
            title_font = ImageFont.truetype("arial.ttf", 38)
            number_font = ImageFont.truetype("arial.ttf", 150)
            body_font = ImageFont.truetype("arial.ttf", 28)
            small_font = ImageFont.truetype("arial.ttf", 22)
        except OSError:
            title_font = ImageFont.load_default()
            number_font = title_font
            body_font = title_font
            small_font = title_font

        margin = 54
        draw.rounded_rectangle((margin, 70, 262, 118), radius=22, fill=(34, 75, 63, 255))
        from koov.brand import ASSISTANT_NAME

        brand = ASSISTANT_NAME.upper()
        draw.text((margin + 18, 82), f"{brand}  •  LIVE", font=small_font, fill=(178, 245, 209, 255))
        draw.text((margin, 190), "PROPERTY", font=title_font, fill=(246, 248, 252, 255))
        draw.text((margin, 238), "INSPECTION", font=title_font, fill=(246, 248, 252, 255))
        draw.text((margin, 330), str(mode or "INSPECTION").replace("_", " ").upper(), font=body_font, fill=(157, 174, 198, 255))
        draw.rounded_rectangle((margin, 440, LiveKitVideoCard.WIDTH - margin, 850), radius=34, fill=(25, 34, 49, 255), outline=(57, 72, 94, 255), width=2)
        draw.text((margin + 42, 490), "ISSUES FOUND", font=body_font, fill=(174, 190, 211, 255))
        draw.text((margin + 38, 555), str(max(0, int(issue_count))), font=number_font, fill=(255, 255, 255, 255))
        draw.text((margin + 42, 775), "New findings are called out aloud", font=small_font, fill=(157, 174, 198, 255))
        draw.text((margin, 930), str(unit or "Property walkthrough")[:42], font=body_font, fill=(235, 240, 248, 255))
        draw.line((margin, 1000, LiveKitVideoCard.WIDTH - margin, 1000), fill=(57, 72, 94, 255), width=2)
        draw.ellipse((margin, 1050, margin + 18, 1068), fill=(78, 211, 145, 255))
        draw.text((margin + 34, 1044), str(status or "Inspection active")[:48], font=small_font, fill=(178, 194, 214, 255))
        draw.text((margin, 1170), f"{brand}  /  RENTAL CONDITION RECORD", font=small_font, fill=(111, 128, 151, 255))
        return image

    async def _capture(self, issue_count: int, mode: str, unit: str, status: str) -> None:
        from livekit import rtc

        image = self._draw(issue_count, mode, unit, status)
        frame = rtc.VideoFrame(
            self.WIDTH, self.HEIGHT, rtc.VideoBufferType.RGBA, image.tobytes()
        )
        # rtc.VideoSource.capture_frame is synchronous (unlike AudioSource.capture_frame).
        self._source.capture_frame(frame)

    def update(self, *, issue_count: int, mode: str, unit: str, status: str) -> None:
        """Queue a fresh Pillow card from a worker thread."""
        if self._closed or not self._loop.is_running():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._capture(issue_count, mode, unit, status), self._loop
            )
            future.add_done_callback(
                lambda f: log.warning("Chert video card update failed: %s", f.exception())
                if not f.cancelled() and f.exception() else None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert video card update could not be queued: %s", exc)

    def close(self) -> None:
        self._closed = True



class ChertLiveKitSession:
    """Koov's connection to the LiveKit room that Chert's FaceTime bridge joins."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        agent_identity: str,
        bridge_identity: str,
        sample_rate_hz: int,
        frame_ms: int,
        video: bool = False,
    ) -> None:
        self._video = bool(video)
        self._frame_lock = threading.Lock()
        self._latest_frame: tuple[int, int, bytes, float] | None = None  # w, h, RGBA, time
        self.video_frames_total = 0
        # Set when the first frame of the caller's camera has arrived.
        self.remote_video = threading.Event()
        self._url = url
        self._token = token
        self._agent_identity = agent_identity
        self._bridge_identity = bridge_identity
        self._sample_rate_hz = int(sample_rate_hz)
        self._frame_ms = int(frame_ms)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="koov-chert-livekit"
        )
        self._room: Any = None
        self._source: Any = None
        self._track: Any = None
        self._video_source: Any = None
        self.video_card: LiveKitVideoCard | None = None
        self._video_track: Any = None
        self._reader_tasks: set[asyncio.Task] = set()

        self.audio_in = LiveKitAudioInput(self._sample_rate_hz, self._frame_ms)
        self.audio_out: LiveKitAudioOutput | None = None

        # Set when Chert's bridge participant is in the room.
        self.bridge_joined = threading.Event()
        # Set when the caller's audio track is subscribed (the call is live).
        self.remote_audio = threading.Event()
        # Set when the bridge leaves or the room disconnects (the call is over).
        self.call_ended = threading.Event()
        self.end_reason = ""
        self.room_metadata = ""

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, timeout_s: float = 45.0) -> None:
        """Connect to the room and publish Koov's (silent) audio track. Raises on failure."""
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop).result(timeout=timeout_s)

    async def _connect(self) -> None:
        from livekit import rtc

        room = rtc.Room(loop=self._loop)
        self._room = room
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_published", self._on_track_published)
        room.on("participant_connected", self._on_participant_connected)
        room.on("participant_disconnected", self._on_participant_disconnected)
        room.on("disconnected", self._on_disconnected)
        # Diagnostics: Chert signals call phase through room metadata / participant state.
        for _ev in (
            "room_metadata_changed",
            "participant_metadata_changed",
            "participant_attributes_changed",
            "data_received",
            "track_muted",
            "track_unmuted",
            "track_unpublished",
            "connection_state_changed",
        ):
            room.on(_ev, lambda *args, _ev=_ev: self._log_room_event(_ev, args))

        await room.connect(self._url, self._token, rtc.RoomOptions(auto_subscribe=True))
        log.info("Chert: connected to LiveKit room %r as %r", room.name, self._agent_identity)

        self._source = rtc.AudioSource(
            self._sample_rate_hz, 1, queue_size_ms=_SOURCE_QUEUE_MS, loop=self._loop
        )
        self._track = rtc.LocalAudioTrack.create_audio_track("koov-voice", self._source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(self._track, opts)
        self.audio_out = LiveKitAudioOutput(self._loop, self._source, self._sample_rate_hz)

        # Publish Koov's own portrait tile. Start with a plain issue-count card;
        # the worker refreshes it as inspection findings arrive.
        if self._video:
            self._video_source = rtc.VideoSource(720, 1280)
            self._video_track = rtc.LocalVideoTrack.create_video_track("card", self._video_source)
            video_opts = rtc.TrackPublishOptions()
            video_opts.source = rtc.TrackSource.SOURCE_CAMERA
            await room.local_participant.publish_track(self._video_track, video_opts)
            self.video_card = LiveKitVideoCard(self._loop, self._video_source)
            await self.video_card._capture(0, "INSPECTION", "Property walkthrough", "Connecting to FaceTime")

        # Chert needs exactly one unmuted audio publication from the agent before it dials.
        # Prime the track with a little silence so the publication carries media.
        n = int(self._sample_rate_hz * 0.2)
        await self._source.capture_frame(
            rtc.AudioFrame(
                data=bytes(n * 2),
                sample_rate=self._sample_rate_hz,
                num_channels=1,
                samples_per_channel=n,
            )
        )

        for participant in room.remote_participants.values():
            self._note_participant(participant)

    def update_video_card(self, *, issue_count: int, mode: str, unit: str, status: str) -> None:
        if self.video_card is not None:
            self.video_card.update(issue_count=issue_count, mode=mode, unit=unit, status=status)

    def close(self) -> None:
        if self.video_card is not None:
            self.video_card.close()
        if self.audio_out is not None:
            self.audio_out.close()
        self.audio_in.stop()
        if self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=8.0)
            except Exception as exc:  # noqa: BLE001
                log.debug("Chert: LiveKit shutdown: %s", exc)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

    async def _shutdown(self) -> None:
        for task in list(self._reader_tasks):
            task.cancel()
        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ── room events (run on the loop thread) ─────────────────────────────────

    def _log_room_event(self, name: str, args: tuple) -> None:
        parts = []
        for x in args:
            ident = getattr(x, "identity", None)
            if ident is not None:
                parts.append(f"participant={ident}")
                continue
            data = getattr(x, "data", None)
            if isinstance(data, (bytes, bytearray)):
                parts.append(
                    f"topic={getattr(x, 'topic', '')!r} data={bytes(data)[:300]!r}"
                )
                continue
            sid = getattr(x, "sid", None)
            if sid is not None and hasattr(x, "kind"):
                parts.append(f"track={getattr(x, 'name', '')!r}")
                continue
            parts.append(repr(x)[:300])
        log.info("Chert room event %s: %s", name, " | ".join(parts))
        if name == "room_metadata_changed" and self._room is not None:
            self.room_metadata = str(getattr(self._room, "metadata", "") or "")

    def _is_bridge(self, participant: Any) -> bool:
        return str(getattr(participant, "identity", "") or "") == self._bridge_identity

    def _note_participant(self, participant: Any) -> None:
        if self._is_bridge(participant):
            if not self.bridge_joined.is_set():
                log.info(
                    "Chert: FaceTime bridge joined the room (kind=%s attributes=%s metadata=%r)",
                    getattr(participant, "kind", None),
                    dict(getattr(participant, "attributes", {}) or {}),
                    str(getattr(participant, "metadata", "") or "")[:300],
                )
            self.bridge_joined.set()

    def _on_participant_connected(self, participant: Any) -> None:
        self._note_participant(participant)

    def _on_participant_disconnected(self, participant: Any) -> None:
        if self._is_bridge(participant):
            log.info("Chert: FaceTime bridge left the room (call ended)")
            self.end_reason = self.end_reason or "bridge_left"
            self.call_ended.set()

    def _on_disconnected(self, *args: Any) -> None:
        log.info("Chert: LiveKit room disconnected %s", args if args else "")
        self.end_reason = self.end_reason or "room_disconnected"
        self.call_ended.set()

    def _on_track_published(self, publication: Any, participant: Any) -> None:
        # Audio-only agent: don't pull the caller's video until vision is implemented.
        try:
            from livekit import rtc

            if publication.kind == rtc.TrackKind.KIND_VIDEO:
                log.info(
                    "Chert: video track %r published by %r (subscribe=%s)",
                    getattr(publication, "name", ""),
                    getattr(participant, "identity", ""),
                    self._video,
                )
                if not self._video:
                    publication.set_subscribed(False)
        except Exception:  # noqa: BLE001
            pass

    def _on_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        from livekit import rtc

        self._note_participant(participant)
        if track.kind == rtc.TrackKind.KIND_VIDEO and self._video:
            log.info(
                "Chert: subscribed to video track %r from %r",
                str(getattr(publication, "name", "") or ""),
                getattr(participant, "identity", ""),
            )
            vtask = self._loop.create_task(self._read_video(track))
            self._reader_tasks.add(vtask)
            vtask.add_done_callback(self._reader_tasks.discard)
            return
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        name = str(getattr(publication, "name", "") or getattr(track, "name", "") or "")
        log.info(
            "Chert: subscribed to audio track %r from %r%s",
            name,
            getattr(participant, "identity", ""),
            "" if name == REMOTE_AUDIO_TRACK else " (unexpected track name; using it anyway)",
        )
        task = self._loop.create_task(self._read_audio(track))
        self._reader_tasks.add(task)
        task.add_done_callback(self._reader_tasks.discard)
        self.remote_audio.set()

    async def _read_audio(self, track: Any) -> None:
        from livekit import rtc

        stream = rtc.AudioStream(
            track,
            loop=self._loop,
            sample_rate=self._sample_rate_hz,
            num_channels=1,
            frame_size_ms=self._frame_ms,
        )
        try:
            async for event in stream:
                self.audio_in.push_pcm16(bytes(event.frame.data))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert: caller audio reader stopped: %s", exc)
        finally:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _read_video(self, track: Any) -> None:
        """Keep only the newest frame of the caller's camera (RGBA) for vision."""
        from livekit import rtc

        stream = rtc.VideoStream(
            track, loop=self._loop, capacity=1, format=rtc.VideoBufferType.RGBA
        )
        try:
            async for event in stream:
                frame = event.frame
                with self._frame_lock:
                    self._latest_frame = (
                        int(frame.width), int(frame.height), bytes(frame.data), time.time()
                    )
                self.video_frames_total += 1
                if not self.remote_video.is_set():
                    log.info(
                        "Chert video: first frame received (%dx%d); Koov can see the caller",
                        frame.width, frame.height,
                    )
                    self.remote_video.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Chert: caller video reader stopped: %s", exc)
        finally:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                pass

    def latest_frame_jpeg(
        self, max_side: int = 1024, quality: int = 80, crop: bool = True
    ) -> tuple[bytes, float] | None:
        """Newest caller-camera frame as JPEG bytes plus its wall-clock time, or None."""
        with self._frame_lock:
            snap = self._latest_frame
        if snap is None:
            return None
        import io

        from PIL import Image

        w, h, rgba, at = snap
        img = Image.frombytes("RGBA", (w, h), rgba).convert("RGB")
        if crop:
            # Chert sends a capture of its FaceTime window: the caller's (often portrait)
            # video sits inside black bars. Trim them so the model sees only the camera.
            bbox = img.convert("L").point(lambda p: 255 if p > 24 else 0).getbbox()
            if bbox and (bbox[2] - bbox[0]) >= 64 and (bbox[3] - bbox[1]) >= 64:
                img = img.crop(bbox)
                w, h = img.size
        if max(w, h) > max_side:
            img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), at

    # ── status ───────────────────────────────────────────────────────────────

    def participants_snapshot(self) -> dict[str, Any]:
        room = self._room
        if room is None:
            return {}
        out: dict[str, Any] = {}
        try:
            for participant in list(room.remote_participants.values()):
                ident = str(getattr(participant, "identity", "") or "")
                if ident:
                    out[ident] = {
                        "name": str(getattr(participant, "name", "") or ident),
                        "is_bridge": ident == self._bridge_identity,
                    }
        except Exception:  # noqa: BLE001
            pass
        return out
