"""
Zoom Meeting SDK join backend for Mechus.

Joins Zoom meetings without a browser using the Zoom Meeting SDK (Linux only).
Provides audio in/out that Mechus can use for listening and TTS.

Requires: pip install zoom-meeting-sdk PyJWT
Platform: Linux only (Zoom Meeting SDK for Linux).
"""
from __future__ import annotations

import logging
import os
import platform
import queue
import re
import threading
import time
from typing import Optional
from urllib.parse import unquote

from mechus.audio import AudioFrame

log = logging.getLogger(__name__)

ZOOM_RAW_SAMPLE_RATE = 32000  # Zoom SDK raw audio is 32 kHz


def _zoom_mute_when_idle() -> bool:
    """When true, mute Mechus in Zoom after each TTS clip (legacy on-demand mode). Default: stay unmuted."""
    return os.environ.get("MECHUS_ZOOM_MUTE_WHEN_IDLE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

# Known Zoom Meeting SDK join-failure error codes (from MEETING_STATUS_FAILED iResult)
ZOOM_ERROR_MESSAGES = {
    8: (
        "Zoom join failed (8): meeting may not exist, may have ended, or Mechus is not authorized "
        "for this Zoom account. For org links like osu.zoom.us, connect Zoom in the dashboard "
        "so Mechus receives your OAuth token (ZAK). Same-account meetings can use JWT only."
    ),
    9: (
        "Zoom join failed (9): meeting does not exist. Double-check the meeting link and passcode."
    ),
    61: (
        "Join meeting failed (61): Mechus was removed from this meeting and Zoom is blocking rejoin. "
        "Ask the host to enable 'Allow removed participants to rejoin', or re-invite Mechus with a new display name."
    ),
    63: (
        "Join meeting failed (63): this meeting is on a different Zoom account than the Meeting SDK app. "
        "Add server Zoom OAuth on the API host (ZOOM_ACCOUNT_ID + client id/secret) or publish the "
        "Meeting SDK app on the Zoom Marketplace."
    ),
    6601: (
        "Zoom join failed (6601): external meeting. Publish your Meeting SDK app on the Zoom "
        "Marketplace, or connect Zoom OAuth in the dashboard so Mechus can join with your user token."
    ),
}


def _zoom_fail_message(iResult: int) -> None:
    msg = ZOOM_ERROR_MESSAGES.get(iResult) or f"Zoom join failed with error code {iResult}."
    print(f"[Zoom] {msg}", flush=True)
    log.warning("Zoom MEETING_STATUS_FAILED: %s", msg)


def _zoom_fetch_participant_display_name(meeting_service, user_id: int) -> str:
    """Resolve Zoom SDK user id to in-meeting display name (best-effort across binding variants)."""
    if meeting_service is None:
        return ""
    try:
        ctrl = meeting_service.GetMeetingParticipantsController()
    except Exception:
        return ""
    if ctrl is None:
        return ""
    uid = int(user_id)
    for m in ("GetUserByUserID", "GetUserByUserId", "getUserByUserID"):
        get_u = getattr(ctrl, m, None)
        if not callable(get_u):
            continue
        try:
            info = get_u(uid)
        except Exception:
            continue
        if info is None:
            continue
        for un in ("GetUserName", "getUserName", "GetUserNameW"):
            gn = getattr(info, un, None)
            if not callable(gn):
                continue
            try:
                n = gn()
                if n:
                    s = re.sub(r"[\x00-\x1f]", "", str(n)).strip()
                    if s:
                        return s[:200]
            except Exception:
                pass
    return ""


def zoom_resolve_participant_label(
    meeting_service,
    user_id: int,
    cache: dict[int, tuple[float, str]],
    *,
    ttl_s: float = 30.0,
) -> str:
    """Cached display name for one-way raw audio user id (node_id from SDK)."""
    now = time.monotonic()
    uid = int(user_id)
    ent = cache.get(uid)
    if ent is not None and (now - ent[0]) < ttl_s:
        return ent[1]
    name = _zoom_fetch_participant_display_name(meeting_service, uid)
    if not name:
        name = f"user_{uid}"
    cache[uid] = (now, name)
    return name


def parse_zoom_link(link: str) -> tuple[Optional[int], Optional[str]]:
    """
    Parse a Zoom meeting link into (meeting_number, password).

    Supports:
      - https://zoom.us/j/MEETING_NUMBER
      - https://zoom.us/j/MEETING_NUMBER?pwd=PASSWORD
      - https://us06web.zoom.us/j/MEETING_NUMBER?pwd=PASSWORD
    """
    link = (link or "").strip()
    if not link:
        return None, None
    # Match /j/NUMBER or /j/NUMBER?pwd=... (non-greedy .*? so first ? or & before pwd= is found)
    match = re.search(r"/j/(\d+)(?:.*?[?&]pwd=([^&\s]+))?", link, re.I)
    if not match:
        return None, None
    meeting_number_str = match.group(1)
    raw_password = match.group(2) if match.lastindex and match.lastindex >= 2 else None
    password = unquote(raw_password).strip() if raw_password else None
    if password == "":
        password = None
    try:
        meeting_number = int(meeting_number_str)
    except ValueError:
        return None, None
    return meeting_number, password


def redact_zoom_link_for_log(link: str) -> str:
    """Mask ``pwd=`` query values in Zoom meeting URLs for safe logging."""
    if not link:
        return ""
    return re.sub(r"(?i)([?&]pwd=)([^&\s#]+)", r"\1***", link)


def is_zoom_link(link: str) -> bool:
    """Return True if link looks like a Zoom meeting URL."""
    if not link:
        return False
    return "zoom.us" in link.lower() and "/j/" in link.lower()


def zoom_sdk_available() -> bool:
    """Return True if we're on Linux and zoom_meeting_sdk can be imported."""
    return zoom_sdk_available_reason()[0]


def zoom_sdk_available_reason() -> tuple[bool, str]:
    """Return (available, reason). Use for logging when zoom_sdk is requested but unavailable."""
    if platform.system().lower() != "linux":
        return False, f"platform is {platform.system()!r}, Zoom SDK requires Linux"
    try:
        import zoom_meeting_sdk  # noqa: F401
        return True, "ok"
    except ImportError as e:
        return False, f"zoom_meeting_sdk import failed: {e}"
    except Exception as e:
        return False, f"zoom_meeting_sdk load failed: {type(e).__name__}: {e}"


class ZoomAudioInput:
    """
    Audio input that reads PCM frames from a queue filled by the Zoom SDK callback.
    Implements the same interface as mechus.audio.AudioInput (start, stop, read).
    """

    def __init__(self, sample_rate_hz: int, frame_ms: int):
        self._sample_rate_hz = sample_rate_hz
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
        self._queue: queue.Queue[AudioFrame] = queue.Queue(maxsize=500)
        self._running = True
        self._frames_received = 0
        self._last_log_time: Optional[float] = None
        self._label_override: Optional[object] = None  # Optional[Callable[[str], str]]

    def set_label_override(self, fn: Optional[object]) -> None:
        """Optional ``fn(label: str) -> str`` applied to stream speaker_id (e.g. manual aliases from API)."""
        self._label_override = fn

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    def push_frame(
        self,
        pcm16: bytes,
        timestamp_s: float,
        *,
        speaker_id: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ) -> None:
        """Called by Zoom session when it has a frame (already at agent sample rate)."""
        if not self._running or not pcm16:
            return
        sid = speaker_id
        if sid and self._label_override is not None:
            try:
                sid = self._label_override(sid)  # type: ignore[misc]
            except Exception:
                pass
        self._frames_received += 1
        # Log so you can see "audio detected" even before any words: first frame, then every 5s
        now = time.monotonic()
        if self._last_log_time is None:
            self._last_log_time = now
            log.info("Zoom audio: first frame received; Mechus can hear the meeting")
        elif now - self._last_log_time >= 5.0:
            log.info("Zoom audio: %s frames in last 5s (audio detected)", self._frames_received)
            self._frames_received = 0
            self._last_log_time = now
        try:
            self._queue.put_nowait(
                AudioFrame(
                    pcm16=pcm16,
                    timestamp_s=timestamp_s,
                    speaker_id=sid,
                    speaker_name=speaker_name,
                )
            )
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(
                    AudioFrame(
                        pcm16=pcm16,
                        timestamp_s=timestamp_s,
                        speaker_id=sid,
                        speaker_name=speaker_name,
                    )
                )
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


class ZoomAudioOutput:
    """
    Audio output that sends PCM to the Zoom meeting via the SDK virtual mic.
    Implements the same interface as mechus.audio.AudioOutput (play, play_blocking, wait, stop).

    Non-blocking ``play()`` sends audio on a background thread with real-time pacing (~10 ms
    per chunk) so the agent thread can run VAD and call ``stop()`` for barge-in. ``stop()``
    cancels remaining chunks; ``wait()`` joins the sender thread.

    ``sender.send`` runs on the playback thread; the Zoom SDK must tolerate this (verify if
    your build requires main-thread-only sends).

    Zoom calls onMicInitializeCallback(sender) then onMicStartSendCallback(); we only send after both.
    """

    def __init__(self, sample_rate_hz: int):
        self._sample_rate_hz = sample_rate_hz
        self._sender = None
        self._can_send = False
        self._sender_lock = threading.Lock()
        self._cancel = threading.Event()
        self._play_thread: threading.Thread | None = None
        self._play_lock = threading.Lock()
        # Optional: attached ZoomBot so the agent can toggle mic mute state.
        self._bot = None

    def set_zoom_bot(self, bot) -> None:
        self._bot = bot

    def mute_mic(self) -> bool:
        bot = self._bot
        if bot is None:
            return False
        try:
            return bool(getattr(bot, "mute_audio")())
        except Exception:
            return False

    def unmute_mic(self) -> bool:
        bot = self._bot
        if bot is None:
            return False
        try:
            return bool(getattr(bot, "unmute_audio")())
        except Exception:
            return False

    def set_sender(self, sender) -> None:
        with self._sender_lock:
            self._sender = sender
            if sender is None:
                self._can_send = False
            else:
                log.info("Zoom virtual mic sender initialized (call send after onMicStartSend)")

    def set_can_send(self, can_send: bool) -> None:
        with self._sender_lock:
            self._can_send = can_send
            if can_send:
                log.info("Zoom virtual mic can send; TTS will be heard in the meeting")

    def _get_sender(self):
        with self._sender_lock:
            return self._sender

    def is_ready_to_send(self) -> bool:
        with self._sender_lock:
            return self._sender is not None and self._can_send

    def play(
        self,
        audio_f32,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        self._play_impl(
            audio_f32,
            sample_rate_hz,
            gain,
            blocking=False,
            ramp_in=ramp_in,
            ramp_out=ramp_out,
        )

    def play_blocking(
        self,
        audio_f32,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        self._play_impl(
            audio_f32,
            sample_rate_hz,
            gain,
            blocking=True,
            ramp_in=ramp_in,
            ramp_out=ramp_out,
        )

    def play_streaming(
        self,
        chunks_iter,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        skip_edge_ramp_in: bool = False,
        handoff_from_bridge: bool = False,
        keep_mic_live: bool = False,
    ) -> None:
        """Send TTS chunks to the virtual mic as they arrive (gapless paced send).

        ElevenLabs delivers many small PCM fragments; fading each fragment causes
        audible chopping. We coalesce buffers and only fade the stream edges.

        When ``handoff_from_bridge`` is True, the bridge clip (opener / thinking
        preroll) already finished on a blocking path — do not pulse ``_cancel`` if
        no streaming worker is active, so the handoff into the main reply stays smooth.
        """
        with self._play_lock:
            if handoff_from_bridge and self._play_thread is None:
                self._cancel.clear()
            else:
                self._cancel.set()
                self._join_play_thread()
                self._cancel.clear()

            def _worker() -> None:
                import struct

                import numpy as np

                sender = self._get_sender()
                if sender is None:
                    return
                try:
                    zoom = __import__(
                        "zoom_meeting_sdk", fromlist=["ZoomSDKAudioChannel_Mono"]
                    )
                except Exception as e:
                    log.warning("Zoom send failed: %s", e)
                    return

                pending = bytearray()
                # ~40 ms @ 32 kHz before flushing (aligns with 20 ms paced frames).
                min_flush_bytes = 1280 * 2
                next_send = time.perf_counter()
                first_flush = True

                def _flush(*, ramp_in: bool, ramp_out: bool) -> None:
                    nonlocal pending, next_send, first_flush
                    if not pending:
                        return
                    pcm = bytes(pending)
                    pending.clear()
                    try:
                        next_send = self._send_pcm16_paced(
                            pcm,
                            struct_mod=struct,
                            sender=sender,
                            zoom=zoom,
                            next_send=next_send,
                            ramp_in=ramp_in,
                            ramp_out=ramp_out,
                        )
                    except Exception as e:
                        log.warning("Zoom send failed: %s", e)
                    first_flush = False

                for raw_chunk in chunks_iter:
                    if self._cancel.is_set():
                        break
                    arr = np.asarray(raw_chunk, dtype=np.float32).reshape(-1)
                    if arr.size == 0:
                        continue
                    pcm16 = self._prepare_pcm16(arr, sample_rate_hz, gain)
                    if not pcm16:
                        continue
                    pending.extend(pcm16)
                    if len(pending) >= min_flush_bytes:
                        _flush(
                            ramp_in=first_flush and not skip_edge_ramp_in,
                            ramp_out=False,
                        )

                if pending:
                    _flush(
                        ramp_in=first_flush and not skip_edge_ramp_in,
                        ramp_out=not keep_mic_live,
                    )

            self._play_thread = threading.Thread(
                target=_worker,
                name="ZoomTTS-stream",
                daemon=True,
            )
            self._play_thread.start()
            self._join_play_thread()

    def _prepare_pcm16(self, audio_f32, sample_rate_hz: int, gain: float) -> bytes | None:
        import numpy as np

        sender = self._get_sender()
        with self._sender_lock:
            can_send = self._can_send
        if sender is None:
            log.warning("Zoom audio output: no sender yet (onMicInitialize not called), dropping TTS")
            return None
        if not can_send:
            log.warning("Zoom audio output: onMicStartSend not called yet, dropping TTS")
            return None
        audio_f32 = np.asarray(audio_f32, dtype=np.float32)
        if audio_f32.size == 0:
            return None
        audio_f32 = np.clip(audio_f32 * float(gain), -1.0, 1.0)
        pcm16 = (audio_f32 * 32767.0).astype(np.int16).tobytes()
        if sample_rate_hz != ZOOM_RAW_SAMPLE_RATE:
            from mechus.dsp import pcm16_resample_linear

            pcm16 = pcm16_resample_linear(pcm16, src_sr=sample_rate_hz, dst_sr=ZOOM_RAW_SAMPLE_RATE)
        if len(pcm16) % 2:
            pcm16 = pcm16[: len(pcm16) - 1]
        return pcm16

    @staticmethod
    def _pcm16_ramp_sample_count(pcm16: bytes) -> int:
        return min(len(pcm16) // 2, 384)  # ~12 ms @ 32 kHz

    @classmethod
    def _apply_pcm16_fade_in(cls, pcm16: bytes, struct_mod) -> bytes:
        if len(pcm16) < 4:
            return pcm16
        import math

        n_samples = cls._pcm16_ramp_sample_count(pcm16)
        buf = bytearray(pcm16)
        for i in range(n_samples):
            sample = struct_mod.unpack_from("<h", buf, i * 2)[0]
            gain = 0.5 - 0.5 * math.cos(math.pi * (i + 1) / float(n_samples))
            struct_mod.pack_into("<h", buf, i * 2, int(sample * gain))
        return bytes(buf)

    @classmethod
    def _apply_pcm16_fade_out(cls, pcm16: bytes, struct_mod) -> bytes:
        if len(pcm16) < 4:
            return pcm16
        import math

        n_samples = cls._pcm16_ramp_sample_count(pcm16)
        buf = bytearray(pcm16)
        base = len(buf) - n_samples * 2
        for j in range(n_samples):
            i = base + j * 2
            sample = struct_mod.unpack_from("<h", buf, i)[0]
            gain = 0.5 + 0.5 * math.cos(math.pi * (j + 1) / float(n_samples))
            struct_mod.pack_into("<h", buf, i, int(sample * gain))
        return bytes(buf)

    @classmethod
    def _apply_pcm16_edge_ramps(cls, pcm16: bytes, struct_mod) -> bytes:
        """~12 ms half-cosine ramps at clip edges so the virtual mic does not click."""
        return cls._apply_pcm16_fade_out(
            cls._apply_pcm16_fade_in(pcm16, struct_mod),
            struct_mod,
        )

    def _send_pcm16_paced(
        self,
        pcm16: bytes,
        *,
        struct_mod,
        sender,
        zoom,
        next_send: float,
        ramp_in: bool = False,
        ramp_out: bool = False,
    ) -> float:
        """Send PCM on a continuous clock (no permanent gaps after scheduling jitter).

        Previously ``next_send = max(next_send + dt, now)`` permanently inserted gaps
        whenever a frame ran late — Zoom heard that as choppy/glitchy TTS.
        """
        if ramp_in and ramp_out:
            pcm16 = self._apply_pcm16_edge_ramps(pcm16, struct_mod)
        elif ramp_in:
            pcm16 = self._apply_pcm16_fade_in(pcm16, struct_mod)
        elif ramp_out:
            pcm16 = self._apply_pcm16_fade_out(pcm16, struct_mod)

        # 20 ms frames @ 32 kHz mono s16le — fewer syscalls than 10 ms, less OS jitter.
        chunk_bytes = 1280
        chunk_s = chunk_bytes / float(ZOOM_RAW_SAMPLE_RATE * 2)
        offset = 0
        while offset < len(pcm16) and not self._cancel.is_set():
            now = time.perf_counter()
            if now < next_send:
                time.sleep(next_send - now)
            chunk = pcm16[offset : offset + chunk_bytes]
            if len(chunk) % 2:
                chunk = chunk[: len(chunk) - 1]
            if not chunk:
                break
            try:
                sender.send(chunk, ZOOM_RAW_SAMPLE_RATE, zoom.ZoomSDKAudioChannel_Mono)
            except TypeError:
                sender.send(chunk, len(chunk), ZOOM_RAW_SAMPLE_RATE, zoom.ZoomSDKAudioChannel_Mono)
            offset += len(chunk)
            # Advance the schedule only — if we were late, skip sleeps until caught up.
            next_send += chunk_s
        return next_send

    def _send_pcm16_chunks(
        self,
        pcm16: bytes,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        """Send one buffered clip (blocking play) with optional edge ramps."""
        import struct

        sender = self._get_sender()
        if sender is None:
            return
        try:
            zoom = __import__("zoom_meeting_sdk", fromlist=["ZoomSDKAudioChannel_Mono"])
            self._send_pcm16_paced(
                pcm16,
                struct_mod=struct,
                sender=sender,
                zoom=zoom,
                next_send=time.perf_counter(),
                ramp_in=ramp_in,
                ramp_out=ramp_out,
            )
        except Exception as e:
            log.warning("Zoom send failed: %s", e)

    def _join_play_thread(self, timeout_s: float = 120.0) -> None:
        t = self._play_thread
        if t is not None:
            t.join(timeout=timeout_s)
            if t.is_alive():
                log.warning("Zoom audio playback thread did not finish within %ss", timeout_s)
        self._play_thread = None

    def _play_impl(
        self,
        audio_f32,
        sample_rate_hz: int,
        gain: float,
        blocking: bool,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        pcm16 = self._prepare_pcm16(audio_f32, sample_rate_hz, gain)
        if not pcm16:
            return

        with self._play_lock:
            self._cancel.set()
            self._join_play_thread()
            self._cancel.clear()

            if blocking:
                self._send_pcm16_chunks(pcm16, ramp_in=ramp_in, ramp_out=ramp_out)
                return

            def _worker() -> None:
                self._send_pcm16_chunks(pcm16, ramp_in=ramp_in, ramp_out=ramp_out)

            self._play_thread = threading.Thread(
                target=_worker,
                name="ZoomTTS",
                daemon=True,
            )
            self._play_thread.start()

    def wait(self) -> None:
        with self._play_lock:
            self._join_play_thread(timeout_s=120.0)

    def stop(self) -> None:
        self._cancel.set()
        with self._play_lock:
            self._join_play_thread(timeout_s=120.0)


class OnDemandMuteZoomOutput:
    """
    Wraps ZoomAudioOutput to unmute only when speaking, then mute again.
    Use with RTMS input: bot joins muted; we unmute for TTS, then mute.

    Non-blocking ``play()`` returns immediately while audio streams on a thread; mute only
    after ``wait()`` (normal end) or ``stop()`` (barge-in), not in ``play()``'s ``finally``.
    """

    def __init__(self, inner: ZoomAudioOutput, bot) -> None:
        self._inner = inner
        self._bot = bot
        self._mute_when_idle = _zoom_mute_when_idle()

    def _maybe_mute_after_play(self) -> None:
        if self._mute_when_idle:
            self._bot.mute_audio()

    # Manual mic control endpoints expected by mechus.agent (mute_mic/unmute_mic).
    # The wrapper used to only implement play()/wait()/stop(), which made manual
    # "unmute yourself" / "mute yourself" unable to find these methods.
    def unmute_mic(self) -> bool:
        try:
            return bool(self._bot.unmute_audio())
        except Exception:
            return False

    def mute_mic(self) -> bool:
        try:
            return bool(self._bot.mute_audio())
        except Exception:
            return False

    def play(
        self,
        audio_f32,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        self._bot.unmute_audio()
        self._inner.play(
            audio_f32,
            sample_rate_hz,
            gain,
            ramp_in=ramp_in,
            ramp_out=ramp_out,
        )

    def play_blocking(
        self,
        audio_f32,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        ramp_in: bool = True,
        ramp_out: bool = True,
    ) -> None:
        self._bot.unmute_audio()
        try:
            self._inner.play_blocking(
                audio_f32,
                sample_rate_hz,
                gain,
                ramp_in=ramp_in,
                ramp_out=ramp_out,
            )
        finally:
            # Brief tail so the last PCM chunk is not cut off when muting the virtual mic.
            if ramp_out:
                time.sleep(0.035)
            self._maybe_mute_after_play()

    def play_streaming(
        self,
        chunks_iter,
        sample_rate_hz: int,
        gain: float = 1.0,
        *,
        skip_edge_ramp_in: bool = False,
        handoff_from_bridge: bool = False,
        keep_mic_live: bool = False,
    ) -> None:
        self._bot.unmute_audio()
        try:
            self._inner.play_streaming(
                chunks_iter,
                sample_rate_hz,
                gain,
                skip_edge_ramp_in=skip_edge_ramp_in,
                handoff_from_bridge=handoff_from_bridge,
                keep_mic_live=keep_mic_live,
            )
        finally:
            if not keep_mic_live:
                time.sleep(0.035)
                self._maybe_mute_after_play()

    def wait(self) -> None:
        try:
            self._inner.wait()
        finally:
            time.sleep(0.035)
            self._maybe_mute_after_play()

    def stop(self) -> None:
        try:
            self._inner.stop()
        finally:
            self._maybe_mute_after_play()


def create_zoom_session(
    meeting_number: int,
    password: Optional[str],
    display_name: str,
    client_id: str,
    client_secret: str,
    agent_sample_rate_hz: int,
    agent_frame_ms: int,
    joined_event: Optional[threading.Event] = None,
    join_result: Optional[dict] = None,
    recording_token: Optional[str] = None,
    zak_token: Optional[str] = None,
    join_muted: bool = False,
    stream_attribution: bool = False,
) -> Optional[tuple[ZoomAudioInput, object, object]]:
    """
    Join a Zoom meeting via the SDK and return (ZoomAudioInput, ZoomAudioOutput, zoom_bot).

    Runs Zoom SDK in a background thread with GLib main loop. Caller must call zoom_bot.leave()
    and zoom_bot.cleanup() when done.

    Returns None if Zoom SDK is not available or join fails.
    """
    if not zoom_sdk_available():
        log.warning("Zoom SDK not available (Linux + zoom-meeting-sdk required)")
        return None

    from mechus.dsp import pcm16_resample_linear

    zoom = __import__("zoom_meeting_sdk", fromlist=[
        "InitSDK", "InitParam", "SDKERR_SUCCESS", "SDK_LANGUAGE_ID",
        "CreateMeetingService", "CreateSettingService", "CreateAuthService",
        "DestroyMeetingService", "DestroySettingService", "DestroyAuthService",
        "CleanUPSDK", "MeetingServiceEventCallbacks", "AuthServiceEventCallbacks",
        "AuthContext", "JoinParam", "SDKUserType", "AudioRawdataSamplingRate",
        "GetMeetingRecordingController", "GetAudioRawdataHelper",
        "ZoomSDKAudioRawDataDelegateCallbacks", "ZoomSDKVirtualAudioMicEventCallbacks",
        "GetMeetingReminderController", "MeetingReminderEventCallbacks",
        "LEAVE_MEETING", "MEETING_STATUS_INMEETING", "MEETING_STATUS_IDLE",
        "AUTHRET_SUCCESS",
    ])
    try:
        import jwt
        from datetime import datetime, timedelta
    except ImportError:
        log.warning("PyJWT required for Zoom SDK auth: pip install PyJWT")
        return None

    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
    except (ImportError, ValueError) as e:
        log.warning("GLib (PyGObject) required for Zoom SDK: %s", e)
        return None

    audio_in = ZoomAudioInput(agent_sample_rate_hz, agent_frame_ms)
    audio_out = ZoomAudioOutput(agent_sample_rate_hz)
    frame_bytes_agent = int(agent_sample_rate_hz * (agent_frame_ms / 1000.0) * 2)
    # When stream_attribution=True, mixed is ignored so one-way carries per-user labels for STT.
    # When stream_attribution=False, prefer mixed first (all participants summed); skip one-way when mixed works.
    use_mixed = [False]
    prefer_one_way = [stream_attribution]
    use_mixed_fallback = [False]
    _participant_lock = threading.Lock()
    _participants_by_user_id: dict[int, str] = {}
    _first_frame_logged = [False]  # log once when first raw audio frame is received
    bot_holder: list[Optional[object]] = [None]
    _participant_name_cache: dict[int, tuple[float, str]] = {}

    def _signal_join_done(*, ok: bool, error: Optional[str] = None, error_code: Optional[int] = None) -> None:
        if join_result is not None:
            join_result["done"] = True
            join_result["ok"] = ok
            join_result.setdefault("state", "done")
            if error:
                join_result["error"] = error
            if error_code is not None:
                join_result["error_code"] = error_code
        if joined_event:
            joined_event.set()

    def _set_join_state(state: str, detail: Optional[str] = None) -> None:
        if join_result is None:
            return
        join_result["state"] = state
        if detail is not None:
            join_result["detail"] = detail

    # Accumulate resampled bytes across Zoom SDK callbacks until we have a full agent frame.
    # Zoom SDK delivers 10ms chunks (640 bytes at 32kHz → ~960 bytes at 48kHz), but the agent
    # expects 20ms frames (1920 bytes at 48kHz). Without accumulation the while loop never fires.
    _residual: dict[str, bytearray] = {}

    def _clean_display_name(name: object) -> str:
        text = str(name or "").strip()
        return text or "Unknown speaker"

    def _set_participant_name(user_id: int, name: object) -> None:
        clean = _clean_display_name(name)
        with _participant_lock:
            prev = _participants_by_user_id.get(int(user_id))
            _participants_by_user_id[int(user_id)] = clean
        if prev != clean:
            log.info("Zoom participant mapped: user_id=%s name=%s", user_id, clean)

    def _remove_participant_name(user_id: int) -> None:
        with _participant_lock:
            _participants_by_user_id.pop(int(user_id), None)

    def _get_participant_name(user_id: int) -> str:
        with _participant_lock:
            existing = _participants_by_user_id.get(int(user_id))
        return existing or f"Participant {user_id}"

    def _participants_snapshot() -> dict[str, str]:
        with _participant_lock:
            return {str(k): v for k, v in _participants_by_user_id.items()}

    def _speaker_attribution_mode() -> str:
        if prefer_one_way[0] and not use_mixed_fallback[0]:
            return "zoom_one_way"
        if use_mixed_fallback[0]:
            return "mixed_fallback"
        return "unknown"

    def _iter_user_ids(user_list) -> list[int]:
        if user_list is None:
            return []
        try:
            if isinstance(user_list, (int, str)) and str(user_list).strip().lstrip("-").isdigit():
                return [int(user_list)]
        except Exception:
            pass
        try:
            if isinstance(user_list, (list, tuple, set)):
                return [int(x) for x in user_list]
        except Exception:
            pass
        count_methods = ("GetCount", "GetItemCount", "size")
        get_methods = ("GetItem", "GetData", "__getitem__")
        count = None
        for method_name in count_methods:
            method = getattr(user_list, method_name, None)
            if callable(method):
                try:
                    count = int(method())
                    break
                except Exception:
                    continue
        if count is None:
            return []
        items: list[int] = []
        for idx in range(count):
            value = None
            for method_name in get_methods:
                method = getattr(user_list, method_name, None)
                if callable(method):
                    try:
                        value = method(idx)
                        break
                    except Exception:
                        continue
            if value is None:
                try:
                    value = user_list[idx]
                except Exception:
                    value = None
            if value is None:
                continue
            try:
                items.append(int(value))
            except Exception:
                continue
        return items

    def _participants_controller():
        ctrl = getattr(bot, "participants_ctrl", None)
        if ctrl is not None:
            return ctrl
        if getattr(bot, "meeting_service", None) is None:
            return None
        for method_name in ("GetMeetingParticipantsController", "GetMeetingParticipantsCtrl"):
            method = getattr(bot.meeting_service, method_name, None)
            if callable(method):
                try:
                    ctrl = method()
                except Exception:
                    ctrl = None
                if ctrl is not None:
                    bot.participants_ctrl = ctrl
                    return ctrl
        return None

    def _get_user_info(user_id: int):
        ctrl = _participants_controller()
        if ctrl is None:
            return None
        for method_name in ("GetUserByUserID", "GetUserByID", "GetUserInfoByUserID", "GetUserInfo"):
            method = getattr(ctrl, method_name, None)
            if callable(method):
                try:
                    info = method(int(user_id))
                except TypeError:
                    try:
                        info = method(user_id=int(user_id))
                    except Exception:
                        info = None
                except Exception:
                    info = None
                if info is not None:
                    return info
        return None

    def _extract_user_name(user_info) -> str | None:
        if user_info is None:
            return None
        for attr in ("GetUserName", "getUserName"):
            method = getattr(user_info, attr, None)
            if callable(method):
                try:
                    name = method()
                except Exception:
                    name = None
                if name:
                    return _clean_display_name(name)
        for attr in ("userName", "user_name", "name"):
            value = getattr(user_info, attr, None)
            if value:
                return _clean_display_name(value)
        return None

    def _refresh_participant_map(user_ids: list[int] | None = None, reason: str = "") -> None:
        ctrl = _participants_controller()
        if ctrl is None:
            return
        if user_ids is None:
            list_method = None
            for method_name in ("GetParticipantsList", "GetParticipantsLst"):
                candidate = getattr(ctrl, method_name, None)
                if callable(candidate):
                    list_method = candidate
                    break
            if list_method is None:
                return
            try:
                user_ids = _iter_user_ids(list_method())
            except Exception:
                user_ids = []
        updated = 0
        for user_id in user_ids:
            info = _get_user_info(int(user_id))
            name = _extract_user_name(info)
            if name:
                _set_participant_name(int(user_id), name)
                updated += 1
        # one-way-audio fires every ~10ms per speaker; INFO here floods logs. Join/rename stay INFO.
        if reason and updated:
            if reason == "one-way-audio":
                log.debug("Zoom participant map refreshed (%s): %s user(s)", reason, updated)
            else:
                log.info("Zoom participant map refreshed (%s): %s user(s)", reason, updated)

    def _push_audio_buf(
        buf,
        ts: float,
        *,
        stream_key: str,
        speaker_id: str | None = None,
        speaker_name: str | None = None,
    ) -> None:
        if not buf:
            return
        resampled = pcm16_resample_linear(buf, ZOOM_RAW_SAMPLE_RATE, agent_sample_rate_hz)
        residual = _residual.setdefault(stream_key, bytearray())
        residual.extend(resampled)
        while len(residual) >= frame_bytes_agent:
            chunk = bytes(residual[:frame_bytes_agent])
            del residual[:frame_bytes_agent]
            audio_in.push_frame(
                chunk,
                ts,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
            )

    def on_mixed_audio_raw_data_received_callback(data):
        """Mixed audio from all participants — primary path when not using stream attribution."""
        if stream_attribution:
            return
        use_mixed[0] = True
        try:
            buf = data.GetBuffer()
            if not _first_frame_logged[0]:
                _first_frame_logged[0] = True
                n = len(buf) if buf else 0
                print(f"[Zoom] onMixedAudioRawDataReceived: first frame (buf_len={n}); fallback stream=mixed.", flush=True)
                log.info("Zoom audio: first frame received — fallback stream=mixed (buf_len=%s, sample_rate=%s)", n, ZOOM_RAW_SAMPLE_RATE)
            _push_audio_buf(buf, time.monotonic(), stream_key="mixed")
        except Exception as e:
            log.debug("Zoom mixed audio callback error: %s", e)

    def on_one_way_audio_raw_data_received_callback(data, node_id):
        """Per-participant audio; node_id is the Zoom SDK user_id. Skipped when mixed already feeds (non-attribution)."""
        if not stream_attribution and use_mixed[0]:
            return
        try:
            buf = data.GetBuffer()
            user_id = int(node_id)
        except Exception as e:
            log.debug("Zoom audio callback error: %s", e)
            return
        try:
            if not _first_frame_logged[0]:
                _first_frame_logged[0] = True
                n = len(buf) if buf else 0
                mode = "stream-attribution" if stream_attribution else "one-way"
                print(
                    f"[Zoom] onOneWayAudioRawDataReceived: first frame (buf_len={n}); primary stream={mode}; Mechus can hear the meeting.",
                    flush=True,
                )
                log.info(
                    "Zoom audio: first frame received — primary stream=%s (buf_len=%s, user_id=%s)",
                    mode,
                    n,
                    node_id,
                )
            # Avoid SDK participant lookups on every frame once this user_id is mapped.
            with _participant_lock:
                need_name_refresh = int(user_id) not in _participants_by_user_id
            if need_name_refresh:
                _refresh_participant_map([user_id], reason="one-way-audio")
            speaker_name = _get_participant_name(user_id)
            _push_audio_buf(
                buf,
                time.monotonic(),
                stream_key=f"user:{user_id}",
                speaker_id=f"zoom:{user_id}",
                speaker_name=speaker_name,
            )
        except Exception as e:
            log.debug("Zoom audio callback error: %s", e)

    def on_user_join_callback(user_list=None, *args):
        user_ids = _iter_user_ids(user_list)
        if user_ids:
            _refresh_participant_map(user_ids, reason="join")

    def on_user_left_callback(user_list=None, *args):
        for user_id in _iter_user_ids(user_list):
            _remove_participant_name(user_id)

    def on_user_names_changed_callback(user_list=None, *args):
        user_ids = _iter_user_ids(user_list)
        if user_ids:
            _refresh_participant_map(user_ids, reason="rename")

    def _build_participant_event_callbacks():
        callbacks_cls = getattr(zoom, "MeetingParticipantsCtrlEventCallbacks", None)
        if callbacks_cls is None:
            return None
        candidate_kwargs = [
            {"onUserJoinCallback": on_user_join_callback, "onUserLeftCallback": on_user_left_callback, "onUserNamesChangedCallback": on_user_names_changed_callback},
            {"onUserJoinCallback": on_user_join_callback, "onUserLeftCallback": on_user_left_callback, "onUserNameChangedCallback": on_user_names_changed_callback},
            {"onUserJoin": on_user_join_callback, "onUserLeft": on_user_left_callback, "onUserNamesChanged": on_user_names_changed_callback},
            {"onUserJoin": on_user_join_callback, "onUserLeft": on_user_left_callback, "onUserNameChanged": on_user_names_changed_callback},
        ]
        for kwargs in candidate_kwargs:
            try:
                return callbacks_cls(**kwargs)
            except TypeError:
                continue
            except Exception as exc:
                log.debug("Zoom participant callbacks init failed: %s", exc)
                continue
        return None

    def on_mic_initialize_callback(sender):
        audio_out.set_sender(sender)

    def on_mic_start_send_callback():
        audio_out.set_can_send(True)  # Zoom says we can send; TTS will now be heard in the meeting

    class ZoomBot:
        def __init__(self):
            self.meeting_service = None
            self.participants_ctrl = None
            self.participants_event = None
            self.setting_service = None
            self.auth_service = None
            self.meeting_service_event = None
            self.auth_event = None
            self.recording_ctrl = None
            self.audio_source = None
            self.audio_helper = None
            self.virtual_audio_mic_event = None
            self.reminder_controller = None
            self.reminder_event = None
            self._main_loop = None
            self._thread = None
            self._cleanup_lock = threading.Lock()
            self._cleaned_up = False

        def _resolve_own_user_id(self) -> Optional[int]:
            """
            Resolve the current bot's in-meeting user id (int) for audio mute/unmute calls.

            Your Zoom SDK binding expects:
              - UnMuteAudio(self, arg: int)
              - MuteAudio(self, userid: int, allowUnmuteBySelf: bool = True)
            """
            # 1) Fast path: find the user id in our participant name map.
            # _participants_snapshot() maps: {user_id_str: display_name}
            try:
                snap = _participants_snapshot()
                if snap:
                    target = (display_name or "").strip().lower()
                    if target:
                        for uid_s, nm in snap.items():
                            if (nm or "").strip().lower() == target:
                                return int(uid_s)
                    # Heuristic fallback for environments where display_name differs.
                    for uid_s, nm in snap.items():
                        if "mechus" in (nm or "").lower():
                            return int(uid_s)
            except Exception:
                pass

            # 2) Best-effort: try to ask Zoom participants controller for "self user id".
            try:
                if getattr(self, "meeting_service", None) is None:
                    return None
                ctrl = self.meeting_service.GetMeetingParticipantsController()
                if ctrl is None:
                    return None
                for m in (
                    "GetMyUserID",
                    "GetMyUserId",
                    "GetSelfUserID",
                    "GetSelfUserId",
                    "GetMeUserID",
                    "GetMeUserId",
                ):
                    fn = getattr(ctrl, m, None)
                    if not callable(fn):
                        continue
                    try:
                        v = fn()
                    except TypeError:
                        continue
                    if v is None:
                        continue
                    try:
                        return int(v)
                    except Exception:
                        continue
            except Exception:
                pass

            return None

        def _set_meeting_mic_muted(self, *, muted: bool) -> bool:
            """Mute/unmute Mechus in the Zoom participant list (virtual mic still needs this on many builds)."""
            if self.meeting_service is None:
                return False
            uid = self._resolve_own_user_id()
            action = "mute" if muted else "unmute"
            try:
                ctrl = self.meeting_service.GetMeetingAudioController()
                if not ctrl:
                    log.warning("Zoom %s_audio: no meeting audio controller", action)
                    return False
                if muted:
                    method_names = ("MuteAudio", "MuteMyAudio")
                    call_variants: list[tuple] = []
                    if uid is not None:
                        call_variants.extend([(uid, True), (uid,)])
                    call_variants.append(())
                else:
                    method_names = ("UnmuteAudio", "UnMuteAudio", "UnMuteMyAudio", "UnmuteMyAudio")
                    call_variants = []
                    if uid is not None:
                        call_variants.append((uid,))
                    call_variants.append(())
                for method_name in method_names:
                    fn = getattr(ctrl, method_name, None)
                    if not callable(fn):
                        continue
                    for args in call_variants:
                        try:
                            r = fn(*args)
                        except TypeError:
                            continue
                        except Exception as exc:
                            log.debug("Zoom %s_audio %s%r: %s", action, method_name, args, exc)
                            break
                        ok = r == zoom.SDKERR_SUCCESS if hasattr(zoom, "SDKERR_SUCCESS") else r == 0
                        if ok:
                            log.info(
                                "Zoom %s_audio: %s (method=%s uid=%s)",
                                action,
                                "ok",
                                method_name,
                                uid,
                            )
                            return True
                log.warning(
                    "Zoom %s_audio: SDK calls failed (uid=%s). "
                    "If participants cannot hear Mechus, ask the host to allow this participant to unmute.",
                    action,
                    uid,
                )
                return False
            except Exception as e:
                log.warning("Zoom %s_audio: %s", action, e)
                return False

        def ensure_meeting_mic_unmuted(self, *, reason: str) -> bool:
            """Join VoIP leaves many SDK bots muted in the UI until explicitly unmuted."""
            ok = self._set_meeting_mic_muted(muted=False)
            if ok:
                log.info("Zoom: meeting mic live (%s)", reason)
            else:
                log.warning(
                    "Zoom: meeting mic still muted after %s (TTS uses virtual mic; "
                    "host may need to allow participants to unmute)",
                    reason,
                )
            return ok

        def get_participants_snapshot(self) -> dict[str, str]:
            return _participants_snapshot()

        def get_speaker_attribution_mode(self) -> str:
            return _speaker_attribution_mode()

        def leave(self):
            if self.meeting_service is None:
                return
            try:
                status = self.meeting_service.GetMeetingStatus()
                if status != zoom.MEETING_STATUS_IDLE:
                    self.meeting_service.Leave(zoom.LEAVE_MEETING)
            except Exception as e:
                log.debug("Zoom leave: %s", e)

        def mute_audio(self) -> bool:
            """Mute our mic. Returns True if successful. For on-demand speak: mute when not talking."""
            return self._set_meeting_mic_muted(muted=True)

        def unmute_audio(self) -> bool:
            """Unmute our mic. Returns True if successful. Call before TTS, then mute after."""
            return self._set_meeting_mic_muted(muted=False)

        def cleanup(self):
            # cleanup() may be invoked from both the SDK thread finally block and external
            # stop paths (worker/api_server). It MUST be idempotent; double-destroying native
            # Zoom SDK objects can trigger heap corruption (e.g. "double free or corruption").
            with self._cleanup_lock:
                if self._cleaned_up:
                    return
                self._cleaned_up = True

            # If called externally while the GLib loop is running, request it to quit.
            try:
                ml = self._main_loop
                if ml is not None and hasattr(ml, "quit"):
                    ml.quit()
            except Exception:
                pass

            # If we're not on the SDK thread, give it a moment to unwind.
            try:
                t = self._thread
                if t is not None and t.is_alive() and threading.current_thread() is not t:
                    t.join(timeout=5.0)
            except Exception:
                pass

            try:
                if self.audio_helper:
                    try:
                        self.audio_helper.unSubscribe()
                    except Exception:
                        pass
                if self.meeting_service:
                    zoom.DestroyMeetingService(self.meeting_service)
                if self.setting_service:
                    zoom.DestroySettingService(self.setting_service)
                if self.auth_service:
                    zoom.DestroyAuthService(self.auth_service)
                # Let native objects release before CleanUPSDK to reduce teardown races
                # (nanobind leak warnings / occasional heap messages on SIGTERM).
                import gc

                gc.collect()
                time.sleep(0.15)
                zoom.CleanUPSDK()
            except Exception as e:
                log.debug("Zoom cleanup: %s", e)
            try:
                audio_in.stop()
            except Exception:
                pass
            try:
                audio_out.set_sender(None)
            except Exception:
                pass

        def meeting_status_changed(self, status, iResult):
            # Log every status change so we can see join progression (CONNECTING -> INMEETING or error)
            try:
                status_name = getattr(status, "name", str(status))
            except Exception:
                status_name = str(status)
            # Print so Docker/gunicorn logs show status even if logging is buffered from this thread
            print(f"[Zoom] meeting_status_changed: status={status_name} iResult={iResult}", flush=True)
            log.info("Zoom meeting_status_changed: status=%s iResult=%s", status_name, iResult)
            _set_join_state(str(status_name))
            # On join failure, unblock the API so it doesn't wait 120s and surface a clear message
            failed_status = getattr(zoom, "MEETING_STATUS_FAILED", None)
            if failed_status is not None and status == failed_status:
                msg = ZOOM_ERROR_MESSAGES.get(iResult) or f"Zoom join failed with error code {iResult}."
                _zoom_fail_message(iResult)
                _signal_join_done(ok=False, error=msg, error_code=int(iResult))
            if status == zoom.MEETING_STATUS_INMEETING:
                _set_join_state("in_meeting")
                log.info("Zoom: entered meeting (INMEETING); starting VoIP and raw recording.")
                _refresh_participant_map(reason="in-meeting")
                self._on_join()
                # Do NOT set joined_event here: _on_join() may return before raw recording
                # (e.g. CanStartRawRecording failed, retry in 2s). Only set it in
                # _start_raw_recording() so the API waits until the virtual mic is ready.

        def _on_join(self):
            try:
                log.info("Zoom _on_join: entered (after INMEETING)")
                # Join VoIP so the bot can send/receive audio
                try:
                    audio_ctrl = self.meeting_service.GetMeetingAudioController()
                    if audio_ctrl:
                        audio_ctrl.JoinVoip()
                except Exception as e:
                    log.debug("Zoom JoinVoip: %s", e)
                # SDK bots often show muted until explicitly unmuted (virtual mic still benefits).
                # Poll briefly after JoinVoip only — returning True from this callback reschedules
                # forever and spams logs every 500ms for the whole meeting.
                _voip_unmute_polls = {"n": 0}

                def _poll_after_join_voip() -> bool:
                    if self._cleaned_up:
                        return False
                    _voip_unmute_polls["n"] += 1
                    self.ensure_meeting_mic_unmuted(reason="after_join_voip")
                    return _voip_unmute_polls["n"] < 6

                GLib.timeout_add(500, _poll_after_join_voip)
                self.recording_ctrl = self.meeting_service.GetMeetingRecordingController()
                if not self.recording_ctrl:
                    log.warning("Zoom: GetMeetingRecordingController() returned None; cannot start raw recording")
                    _signal_join_done(ok=True)
                    return
                # Readiness loop: poll CanStartRawRecording() every 1s until SUCCESS (no fixed delay).
                # With token we may get SUCCESS soon; without token we request privilege and retry until host allows.
                if recording_token and str(recording_token).strip():
                    print("[Zoom] Recording token present; waiting for recording controller ready (readiness loop).", flush=True)
                    log.info("Zoom: recording_token present; starting readiness loop for StartRawRecording")
                else:
                    print(
                        "[Zoom] HOST ACTION: In Zoom, click 'Allow' when prompted to let Mechus record. "
                        "Mechus will re-request until you allow.",
                        flush=True,
                    )
                _set_join_state("recording_permission")

                def request_privilege():
                    try:
                        req = getattr(self.recording_ctrl, "RequestLocalRecordingPrivilege", None)
                        if callable(req):
                            req()
                            log.info("Zoom: RequestLocalRecordingPrivilege() called; host should see prompt.")
                    except Exception as e:
                        log.debug("Zoom RequestLocalRecordingPrivilege: %s", e)

                def try_start_raw_recording():
                    try:
                        err = self.recording_ctrl.CanStartRawRecording()
                        log.info("Zoom: CanStartRawRecording returned %s", err)
                        if err == getattr(zoom, "SDKERR_NO_PERMISSION", -1) or "PERMISSION" in str(err).upper():
                            _set_join_state("recording_permission", "waiting_for_host_allow")
                        if err == zoom.SDKERR_SUCCESS:
                            log.info("Zoom: recording controller ready; starting raw recording and subscription.")
                            self._start_raw_recording()
                            return False  # stop loop
                        if err == getattr(zoom, "SDKERR_NO_PERMISSION", -1) or "PERMISSION" in str(err).upper():
                            request_privilege()
                        return True  # retry in 1s
                    except Exception as e:
                        log.warning("Zoom try_start_raw_recording: %s", e)
                        return True
                # First check after 1s; then every 1s until SUCCESS (readiness loop, no fixed delay)
                GLib.timeout_add_seconds(1, try_start_raw_recording)
            except Exception as e:
                log.warning("Zoom on_join error: %s", e)
                _signal_join_done(ok=True)

        def _start_raw_recording(self):
            try:
                log.info("Zoom _start_raw_recording: entered (raw audio sample rate=%s Hz)", ZOOM_RAW_SAMPLE_RATE)
                start_result = self.recording_ctrl.StartRawRecording()
                if start_result != zoom.SDKERR_SUCCESS:
                    log.warning("Zoom StartRawRecording failed: %s", start_result)
                    _signal_join_done(ok=True)
                    return
                print("[Zoom] raw_recording_started (StartRawRecording SUCCESS); subscribing to raw audio.", flush=True)
                log.info("Zoom: raw_recording_started; subscribe next (order: StartRawRecording then subscribe)")
                self.audio_helper = zoom.GetAudioRawdataHelper()
                if self.audio_helper is None:
                    _signal_join_done(ok=True)
                    return
                # SDK order: StartRawRecording() → setExternalAudioSource() → subscribe(). Do not subscribe before setExternal.
                set_result_holder = [None]
                self.virtual_audio_mic_event = zoom.ZoomSDKVirtualAudioMicEventCallbacks(
                    onMicInitializeCallback=on_mic_initialize_callback,
                    onMicStartSendCallback=on_mic_start_send_callback,
                )
                set_result = self.audio_helper.setExternalAudioSource(self.virtual_audio_mic_event)
                set_result_holder[0] = set_result
                print(f"[Zoom] setExternalAudioSource result={set_result} (0 = success)", flush=True)
                log.info("Zoom: setExternalAudioSource result=%s (0 = success); must run before subscribe", set_result)
                try:
                    self.audio_source = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
                        onMixedAudioRawDataReceivedCallback=on_mixed_audio_raw_data_received_callback,
                        onOneWayAudioRawDataReceivedCallback=on_one_way_audio_raw_data_received_callback,
                        collectPerformanceData=False,
                    )
                    prefer_one_way[0] = stream_attribution
                    use_mixed_fallback[0] = False
                    if stream_attribution:
                        log.info(
                            "Zoom audio: MECHUS_ZOOM_STREAM_ATTRIBUTION — one-way only for STT (mixed ignored); "
                            "display names from participant roster"
                        )
                    else:
                        log.info("Zoom audio: using mixed + one-way; Mechus will hear everyone in the meeting")
                except (TypeError, AttributeError):
                    try:
                        self.audio_source = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
                            onOneWayAudioRawDataReceivedCallback=on_one_way_audio_raw_data_received_callback,
                            collectPerformanceData=False,
                        )
                        prefer_one_way[0] = True
                        use_mixed_fallback[0] = False
                        log.info("Zoom audio: using one-way raw audio only with participant-name attribution")
                    except (TypeError, AttributeError):
                        self.audio_source = zoom.ZoomSDKAudioRawDataDelegateCallbacks(
                            onMixedAudioRawDataReceivedCallback=on_mixed_audio_raw_data_received_callback,
                            collectPerformanceData=False,
                        )
                        prefer_one_way[0] = False
                        use_mixed_fallback[0] = True
                        log.warning(
                            "Zoom SDK binding does not expose one-way raw audio callbacks; using mixed audio fallback without speaker attribution."
                        )
                sub_result = self.audio_helper.subscribe(self.audio_source, False)
                print(f"[Zoom] subscribe_raw_audio_result={sub_result} (0 = success)", flush=True)
                log.info("Zoom: subscribe_raw_audio_result=%s (0 = success); callbacks registered", sub_result)
                audio_in.start()

                def _warn_if_no_audio():
                    # Check first-frame flag, not the rolling counter (which resets every 5s)
                    if not _first_frame_logged[0]:
                        log.warning(
                            "No Zoom audio frames after 30s (setExternal_result=%s subscribe_result=%s). "
                            "Check: recording permission, pipeline order (setExternal then subscribe), ALSA.",
                            set_result_holder[0],
                            sub_result,
                        )
                    return False
                GLib.timeout_add_seconds(30, _warn_if_no_audio)

                _set_join_state("audio_ready")
                self.ensure_meeting_mic_unmuted(reason="virtual_mic_ready")
                _signal_join_done(ok=True)
            except Exception as e:
                log.warning("Zoom _start_raw_recording error: %s", e)
                _signal_join_done(ok=True)

        def join_meeting(self):
            pwd_ok = "yes" if (password and password.strip()) else "no"
            print(f"[Zoom] Joining meeting {meeting_number} (display_name={display_name!r}, password_sent={pwd_ok})", flush=True)
            log.info("Zoom: calling Join(meeting_number=%s, display_name=%s, has_password=%s)", meeting_number, display_name, bool(password))
            join_param = zoom.JoinParam()
            join_param.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN
            param = join_param.param
            param.meetingNumber = meeting_number
            param.userName = display_name
            param.psw = password or ""
            if recording_token and recording_token.strip():
                # Local recording join token: pre-grants recording + bypass waiting room. No host prompt.
                # Set on every known param name (SDK varies: appPrivilegeToken, joinToken, etc.).
                token_val = recording_token.strip()
                known_attrs = ("app_privilege_token", "appPrivilegeToken", "joinToken", "join_token", "privilege_token")
                set_attrs = [a for a in known_attrs if hasattr(param, a)]
                for attr in set_attrs:
                    setattr(param, attr, token_val)
                if set_attrs:
                    log.info("Zoom JoinParam: recording_token set on %s for pre-granted recording", set_attrs)
                else:
                    log.warning("Zoom JoinParam: recording_token provided but no known attribute found on param; host may need to allow recording.")
            if zak_token and zak_token.strip():
                # ZAK token: required (since Mar 2026) to join meetings outside the SDK app's own Zoom account.
                # The host generates their ZAK via GET https://api.zoom.us/v2/users/me/token?type=zak
                zak_val = zak_token.strip()
                zak_attrs = ("userZAK", "user_zak", "userZak", "zak")
                set_zak = [a for a in zak_attrs if hasattr(param, a)]
                for attr in set_zak:
                    setattr(param, attr, zak_val)
                if set_zak:
                    log.info("Zoom JoinParam: zak_token set on %s (cross-account join authorized)", set_zak)
                else:
                    log.warning("Zoom JoinParam: zak_token provided but no known ZAK attribute found on param; cross-account join may fail.")
            param.isVideoOff = True
            param.isAudioOff = bool(join_muted)
            param.isAudioRawDataStereo = False
            param.isMyVoiceInMix = False
            param.eAudioRawdataSamplingRate = zoom.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K
            log.info(
                "Zoom JoinParam: raw audio sampling rate=%s (32K requested)",
                getattr(param.eAudioRawdataSamplingRate, "name", param.eAudioRawdataSamplingRate),
            )
            join_result = self.meeting_service.Join(join_param)
            log.info("Zoom Join() returned: %s (wait for meeting_status_changed(INMEETING) for actual join)", join_result)
            if join_result != zoom.SDKERR_SUCCESS:
                log.error("Zoom Join() failed: %s (check meeting number, password, and that meeting allows SDK joins)", join_result)
                _signal_join_done(ok=False, error=f"Zoom Join() failed: {join_result}")
                return
            if self.setting_service:
                audio_settings = self.setting_service.GetAudioSettings()
                if audio_settings:
                    audio_settings.EnableAutoJoinAudio(True)

        def auth_return(self, result):
            log.info("Zoom auth_return: result=%s", result)
            if result == zoom.AUTHRET_SUCCESS:
                log.info("Zoom: auth succeeded; joining meeting.")
                self.join_meeting()
                return
            log.error("Zoom auth failed: %s (check ZOOM_APP_CLIENT_ID and ZOOM_APP_CLIENT_SECRET)", result)
            _signal_join_done(ok=False, error=f"Zoom auth failed: {result}")
            raise RuntimeError("Zoom auth failed: %s", result)

        def run_sdk_thread(self):
            try:
                init_param = zoom.InitParam()
                init_param.strWebDomain = "https://zoom.us"
                init_param.strSupportUrl = "https://zoom.us"
                init_param.enableGenerateDump = False
                init_param.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
                init_param.enableLogByDefault = False
                if zoom.InitSDK(init_param) != zoom.SDKERR_SUCCESS:
                    log.error("Zoom InitSDK failed")
                    _signal_join_done(ok=False, error="Zoom InitSDK failed")
                    return
                log.info("Zoom InitSDK succeeded; creating meeting and auth services.")
                self.meeting_service = zoom.CreateMeetingService()
                self.setting_service = zoom.CreateSettingService()
                self.meeting_service_event = zoom.MeetingServiceEventCallbacks(
                    onMeetingStatusChangedCallback=self.meeting_status_changed
                )
                self.meeting_service.SetEvent(self.meeting_service_event)
                self.participants_ctrl = _participants_controller()
                if self.participants_ctrl is not None:
                    self.participants_event = _build_participant_event_callbacks()
                    if self.participants_event is not None:
                        bound = False
                        for method_name in ("SetEvent", "SetEventCallback", "SetCallbacks"):
                            method = getattr(self.participants_ctrl, method_name, None)
                            if not callable(method):
                                continue
                            try:
                                method(self.participants_event)
                                log.info("Zoom participants controller events registered via %s", method_name)
                                bound = True
                                break
                            except Exception as exc:
                                log.warning("Zoom participants controller %s failed: %s", method_name, exc)
                        if not bound:
                            log.warning("Zoom participants controller is present but no supported event binding method was found")
                    _refresh_participant_map(reason="controller-ready")
                self.auth_service = zoom.CreateAuthService()
                self.auth_event = zoom.AuthServiceEventCallbacks(
                    onAuthenticationReturnCallback=self.auth_return
                )
                self.auth_service.SetEvent(self.auth_event)
                iat = datetime.utcnow()
                exp = iat + timedelta(hours=24)
                payload = {
                    "iat": iat,
                    "exp": exp,
                    "appKey": client_id,
                    "tokenExp": int(exp.timestamp()),
                }
                token = jwt.encode(payload, client_secret, algorithm="HS256")
                if hasattr(token, "decode"):
                    token = token.decode("utf-8")
                auth_ctx = zoom.AuthContext()
                auth_ctx.jwt_token = token
                auth_result = self.auth_service.SDKAuth(auth_ctx)
                if auth_result != zoom.SDKERR_SUCCESS:
                    log.error("Zoom SDKAuth failed: %s", auth_result)
                    _signal_join_done(ok=False, error=f"Zoom SDKAuth failed: {auth_result}")
                    return
                log.info("Zoom SDKAuth succeeded; main loop starting (auth callback will trigger join).")
                self._main_loop = GLib.MainLoop()
                self._main_loop.run()
            except Exception as e:
                log.error("Zoom SDK thread error: %s", e, exc_info=True)
                _signal_join_done(ok=False, error=f"Zoom SDK thread error: {e}")
            finally:
                self.cleanup()

    bot = ZoomBot()
    bot_holder[0] = bot
    try:
        audio_out.set_zoom_bot(bot)
    except Exception:
        pass
    # Default: join unmuted and stay unmuted (virtual mic only). Opt in to idle mute with
    # MECHUS_ZOOM_MUTE_WHEN_IDLE=1 (unmute before each TTS clip, mute after).
    if _zoom_mute_when_idle():
        audio_out_final: object = OnDemandMuteZoomOutput(audio_out, bot)
        log.info("Zoom audio out: on-demand mute enabled (MECHUS_ZOOM_MUTE_WHEN_IDLE=1)")
    else:
        audio_out_final = audio_out
        log.info("Zoom audio out: stay unmuted (default; set MECHUS_ZOOM_MUTE_WHEN_IDLE=1 to mute between replies)")
    bot._thread = threading.Thread(target=bot.run_sdk_thread, daemon=True)
    bot._thread.start()
    return (audio_in, audio_out_final, bot)
