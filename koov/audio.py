from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Deque, Iterable, Optional

import numpy as np
import sounddevice as sd
import webrtcvad

try:
    PortAudioError = sd.PortAudioError
except AttributeError:
    PortAudioError = Exception  # older sounddevice

log = logging.getLogger(__name__)


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    audio_i16 = np.frombuffer(pcm, dtype=np.int16)
    audio_f32 = audio_i16.astype(np.float32) / 32768.0
    return audio_f32


def float32_to_pcm16(audio: np.ndarray) -> bytes:
    audio = np.clip(audio, -1.0, 1.0)
    audio_i16 = (audio * 32767.0).astype(np.int16)
    return audio_i16.tobytes()


def trim_silence_pcm16(
    pcm16: bytes,
    sample_rate_hz: int,
    frame_ms: int,
    vad_mode: int = 2,
    padding_ms: int = 200,
) -> bytes:
    if not pcm16:
        return b""
    if frame_ms not in {10, 20, 30}:
        return pcm16
    if sample_rate_hz not in {8000, 16000, 32000, 48000}:
        return pcm16
    frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
    if frame_bytes <= 0:
        return pcm16
    total_frames = len(pcm16) // frame_bytes
    if total_frames <= 0:
        return b""

    vad = webrtcvad.Vad(vad_mode)
    speech_flags: list[bool] = []
    for i in range(total_frames):
        start = i * frame_bytes
        end = start + frame_bytes
        frame = pcm16[start:end]
        if len(frame) != frame_bytes:
            break
        speech_flags.append(vad.is_speech(frame, sample_rate_hz))

    if not any(speech_flags):
        return b""

    first = next(i for i, flag in enumerate(speech_flags) if flag)
    last = len(speech_flags) - 1 - next(i for i, flag in enumerate(reversed(speech_flags)) if flag)
    pad_frames = int(round(max(0.0, float(padding_ms)) / float(frame_ms)))
    start = max(0, first - pad_frames)
    end = min(len(speech_flags), last + pad_frames + 1)
    return pcm16[start * frame_bytes : end * frame_bytes]


def trim_f32_silence_edges(
    audio: np.ndarray,
    sample_rate_hz: int,
    *,
    frame_ms: float = 10.0,
    abs_threshold: float = 0.00015,
    pad_ms: float = 14.0,
) -> np.ndarray:
    """
    Drop leading/trailing near-silence from mono float32 audio (e.g. TTS).

    Makes handoffs between thinking preroll and main answer tighter and removes
    long dead air at the start/end of a single ElevenLabs clip without changing
    the voiced middle.
    """
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    n = int(a.shape[0])
    if n <= 0:
        return a
    fs = max(1, int(sample_rate_hz))
    frame_samples = max(1, int(round(fs * max(0.5, frame_ms) / 1000.0)))
    pad = max(0, int(round(fs * max(0.0, pad_ms) / 1000.0)))
    n_frames = (n + frame_samples - 1) // frame_samples
    padded_len = n_frames * frame_samples
    if padded_len > n:
        padded = np.zeros(padded_len, dtype=np.float32)
        padded[:n] = a
    else:
        padded = a[:padded_len].copy()
    frames = padded.reshape(n_frames, frame_samples)
    peak = np.max(np.abs(frames), axis=1)
    voiced = peak > float(abs_threshold)
    if not np.any(voiced):
        return a
    idx = np.flatnonzero(voiced)
    first_f, last_f = int(idx[0]), int(idx[-1])
    start = max(0, first_f * frame_samples - pad)
    end = min(n, (last_f + 1) * frame_samples + pad)
    if end <= start:
        return a
    return np.ascontiguousarray(a[start:end])


def truncate_f32_to_max_duration(
    audio: np.ndarray,
    sample_rate_hz: int,
    max_seconds: float,
    *,
    search_tail_ms: float = 200.0,
) -> np.ndarray:
    """
    Cap duration without chopping mid-word when possible: prefer the last quiet
  frame in the final ``search_tail_ms`` before ``max_seconds``.
    """
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    fs = max(1, int(sample_rate_hz))
    max_n = int(round(max(0.05, float(max_seconds)) * fs))
    if a.shape[0] <= max_n:
        return np.ascontiguousarray(a)
    head = a[:max_n]
    search_n = min(int(head.shape[0]), int(round(fs * max(0.04, search_tail_ms) / 1000.0)))
    if search_n < int(0.04 * fs):
        return np.ascontiguousarray(head)
    tail = head[-search_n:]
    frame = max(1, int(round(fs * 0.012)))
    n_frames = max(1, (int(tail.shape[0]) + frame - 1) // frame)
    padded_len = n_frames * frame
    if padded_len > int(tail.shape[0]):
        pad = np.zeros(padded_len, dtype=np.float32)
        pad[: int(tail.shape[0])] = tail
        frames = pad.reshape(n_frames, frame)
    else:
        frames = tail[:padded_len].reshape(n_frames, frame)
    peak = np.max(np.abs(frames), axis=1)
    quiet = peak < 0.0002
    cut = max_n
    if np.any(quiet):
        last_q = int(np.flatnonzero(quiet)[-1])
        cut = max_n - search_n + (last_q + 1) * frame
        cut = max(int(0.12 * fs), min(max_n, cut))
    return np.ascontiguousarray(head[:cut])


def apply_f32_linear_fade_ends(
    audio: np.ndarray,
    sample_rate_hz: int,
    *,
    fade_in_ms: float = 0.0,
    fade_out_ms: float = 0.0,
) -> np.ndarray:
    """
    Gentle cosine fades on mono float32 audio (e.g. TTS / thinking preroll) to avoid
    clicks at clip boundaries and when truncating to a max duration.
    """
    a = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
    n = int(a.shape[0])
    if n <= 0:
        return a
    fs = max(1, int(sample_rate_hz))
    fi = min(n, int(round(fs * max(0.0, fade_in_ms) / 1000.0)))
    fo = min(n, int(round(fs * max(0.0, fade_out_ms) / 1000.0)))
    if fi + fo > n:
        fi = min(fi, n // 2)
        fo = min(fo, max(0, n - fi))
    if fi > 0:
        # Half-cosine ramp: softer than linear at the very start of speech.
        t = np.linspace(0.0, np.pi, fi, dtype=np.float32)
        a[:fi] *= 0.5 - 0.5 * np.cos(t)
    if fo > 0:
        t = np.linspace(0.0, np.pi, fo, dtype=np.float32)
        a[-fo:] *= 0.5 + 0.5 * np.cos(t)
    return np.ascontiguousarray(a)


def append_f32_tail_silence(
    audio: np.ndarray,
    sample_rate_hz: int,
    *,
    tail_ms: float = 55.0,
) -> np.ndarray:
    """Room after the last phoneme so endings sound finished, not chopped."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    if a.size == 0 or tail_ms <= 0:
        return a
    n = max(1, int(round(max(1, int(sample_rate_hz)) * tail_ms / 1000.0)))
    return np.ascontiguousarray(np.concatenate([a, np.zeros(n, dtype=np.float32)]))


@dataclass
class AudioFrame:
    pcm16: bytes
    timestamp_s: float
    # Zoom one-way raw audio: stream id (e.g. zoom:<user_id>); optional display name from roster
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None


class RollingPCMBuffer:
    def __init__(self, max_seconds: float, sample_rate_hz: int, frame_ms: int):
        self._max_bytes = int(max_seconds * sample_rate_hz * 2)
        self._buf: Deque[bytes] = deque()
        self._buf_bytes = 0
        self._lock = threading.Lock()
        self._sample_rate_hz = sample_rate_hz
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)

    def push(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        if len(pcm16) % 2 != 0:
            return
        with self._lock:
            self._buf.append(pcm16)
            self._buf_bytes += len(pcm16)
            while self._buf_bytes > self._max_bytes and self._buf:
                dropped = self._buf.popleft()
                self._buf_bytes -= len(dropped)

    def snapshot_last(self, seconds: float) -> bytes:
        if seconds <= 0:
            return b""
        target_bytes = int(seconds * self._sample_rate_hz * 2)
        if target_bytes <= 0:
            return b""
        with self._lock:
            chunks = list(self._buf)
        if not chunks:
            return b""
        joined = b"".join(chunks)
        return joined[-target_bytes:]


class SilenceMonitor:
    def __init__(self, sample_rate_hz: int, frame_ms: int, vad_mode: int = 2):
        self._vad = webrtcvad.Vad(vad_mode)
        self._frame_ms = frame_ms
        self._sample_rate_hz = sample_rate_hz
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
        self._lock = threading.Lock()
        self._last_voice_time_s = 0.0

    def update(self, pcm16: bytes, timestamp_s: float) -> None:
        if len(pcm16) != self._frame_bytes:
            return
        is_voice = self._vad.is_speech(pcm16, self._sample_rate_hz)
        if is_voice:
            with self._lock:
                self._last_voice_time_s = timestamp_s

    def silent_for(self, seconds: float, now_s: float | None = None) -> bool:
        if now_s is None:
            now_s = time.monotonic()
        with self._lock:
            last_voice = self._last_voice_time_s
        return (now_s - last_voice) >= seconds


class AudioInput:
    def __init__(self, device: str | int | None, sample_rate_hz: int, frame_ms: int):
        self._device = device
        self._sample_rate_hz = sample_rate_hz
        self._frame_ms = frame_ms
        self._blocksize = int(sample_rate_hz * (frame_ms / 1000.0))
        self._queue: "queue.Queue[AudioFrame]" = queue.Queue(maxsize=200)
        self._stream: sd.InputStream | None = None
        self._running = False
        self._no_device = False  # True when running in Docker/no audio; read() returns None

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def frame_bytes(self) -> int:
        return self._blocksize * 2

    def _resolve_device(self) -> str | int | None:
        device = self._device
        if device is None:
            return None
        if isinstance(device, int):
            return device
        if isinstance(device, str):
            if device.isdigit():
                return int(device)
            needle = device.lower()
            devices = sd.query_devices()
            matches_exact = [
                i
                for i, d in enumerate(devices)
                if d.get("max_input_channels", 0) > 0 and str(d.get("name", "")).lower() == needle
            ]
            if matches_exact:
                return matches_exact[0]
            matches = [
                i
                for i, d in enumerate(devices)
                if d.get("max_input_channels", 0) > 0 and needle in str(d.get("name", "")).lower()
            ]
            if matches:
                return matches[0]
        return device

    def _select_channels(self, device: str | int | None) -> int:
        info = None
        try:
            info = sd.query_devices(device, "input")
        except Exception:
            info = None

        max_in = None
        if isinstance(info, dict):
            max_in = int(info.get("max_input_channels", 0))

        if max_in == 0:
            raise ValueError("Selected input device has no input channels.")

        candidates = [1, 2]
        if max_in and max_in not in candidates:
            candidates.append(max_in)

        for channels in candidates:
            if max_in and channels > max_in:
                continue
            try:
                sd.check_input_settings(
                    device=device,
                    channels=channels,
                    samplerate=self._sample_rate_hz,
                    dtype="int16",
                )
                if channels != 1:
                    log.info("Audio input device requires %s channels; downmixing to mono.", channels)
                return channels
            except Exception:
                continue

        return 1

    def _to_mono_pcm16(self, indata: np.ndarray, channels: int) -> bytes:
        if channels == 1:
            return indata.tobytes()
        if indata.ndim == 2:
            # Downmix to mono when the device cannot open a 1-channel stream.
            mono = indata.mean(axis=1)
        else:
            mono = indata
        return mono.astype(np.int16).tobytes()

    def start(self) -> None:
        if self._running:
            return

        resolved = self._resolve_device()
        if resolved is not None and resolved != self._device:
            log.info("Audio input device resolved to %s", resolved)

        try:
            channels = self._select_channels(resolved)
        except (PortAudioError, ValueError) as e:
            log.warning(
                "No audio input device available (e.g. Docker with no device): %s. "
                "Session will run without listening. For audio, run Koov on the host or use Zoom SDK image.",
                e,
            )
            self._no_device = True
            self._running = True
            return

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if not self._running:
                return
            if status:
                log.debug("Audio input status: %s", status)
            pcm16 = self._to_mono_pcm16(indata, channels)
            try:
                self._queue.put_nowait(AudioFrame(pcm16=pcm16, timestamp_s=time.monotonic()))
            except queue.Full:
                _ = self._queue.get_nowait()
                self._queue.put_nowait(AudioFrame(pcm16=pcm16, timestamp_s=time.monotonic()))

        self._running = True
        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate_hz,
                channels=channels,
                dtype="int16",
                blocksize=self._blocksize,
                device=resolved,
                callback=callback,
            )
            self._stream.start()
        except PortAudioError as e:
            log.warning(
                "Could not open audio input (e.g. Docker with no device): %s. "
                "Session will run without listening. For audio, run Koov on the host or use Zoom SDK image.",
                e,
            )
            self._no_device = True
            self._stream = None

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def read(self, timeout_s: float = 1.0) -> AudioFrame | None:
        if self._no_device:
            time.sleep(min(0.5, timeout_s))
            return None
        try:
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None


class AudioOutput:
    def __init__(self, device: str | int | None):
        self._device = device
        self._lock = threading.RLock()
        self._stream: sd.OutputStream | None = None
        self._play_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._finished = threading.Event()
        self._finished.set()
        # Open stream kept alive across bridge → reply handoff (FaceTime PulseAudio).
        self._handoff_stream: sd.OutputStream | None = None
        self._handoff_device: str | int | None = None
        self._handoff_channels: int = 1
        self._handoff_sample_rate_hz: int = 0
        try:
            self._blocksize = max(512, int((os.getenv("KOOV_AUDIO_OUTPUT_BLOCKSIZE") or "2048").strip()))
        except Exception:
            self._blocksize = 2048
        _latency_raw = (os.getenv("KOOV_AUDIO_OUTPUT_LATENCY") or "high").strip().lower()
        if _latency_raw in {"low", "high"}:
            self._latency: str | float = _latency_raw
        else:
            try:
                self._latency = max(0.02, float(_latency_raw))
            except Exception:
                self._latency = "high"

    def _ensure_pulse_sink_route(self) -> None:
        """Pin PulseAudio output before opening a PortAudio stream.

        The sounddevice ``pulse`` device binds to PulseAudio's default sink when
        the stream is opened. FaceTime workers need every short TTS stream to
        open on ``koov_sink`` so it flows through ``koov_mic_source`` into the
        call. Worker-side polling is too slow for sub-second presence replies,
        so enforce the route synchronously at stream-open time.
        """
        sink = (os.getenv("KOOV_AUDIO_OUTPUT_PULSE_SINK") or "").strip()
        if not sink:
            return
        os.environ["PULSE_SINK"] = sink
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.4,
            )
        except Exception:
            pass

    def _resolve_device(self) -> str | int | None:
        self._ensure_pulse_sink_route()
        device = self._device
        if device is None:
            return None
        if isinstance(device, int):
            return device
        if isinstance(device, str):
            if device.isdigit():
                return int(device)
            needle = device.lower()
            devices = sd.query_devices()
            matches_exact = [
                i
                for i, d in enumerate(devices)
                if d.get("max_output_channels", 0) > 0 and str(d.get("name", "")).lower() == needle
            ]
            if matches_exact:
                return matches_exact[0]
            matches = [
                i
                for i, d in enumerate(devices)
                if d.get("max_output_channels", 0) > 0 and needle in str(d.get("name", "")).lower()
            ]
            if matches:
                return matches[0]
        return device

    def _select_channels(self, device: str | int | None, sample_rate_hz: int) -> int:
        info = None
        try:
            info = sd.query_devices(device, "output")
        except Exception:
            info = None

        max_out = None
        if isinstance(info, dict):
            max_out = int(info.get("max_output_channels", 0))

        if max_out == 0:
            raise ValueError("Selected output device has no output channels.")

        candidates = [1, 2]
        if max_out and max_out not in candidates:
            candidates.append(max_out)

        for channels in candidates:
            if max_out and channels > max_out:
                continue
            try:
                sd.check_output_settings(
                    device=device,
                    channels=channels,
                    samplerate=sample_rate_hz,
                    dtype="float32",
                )
                if channels != 1:
                    log.info("Audio output device requires %s channels; duplicating mono TTS.", channels)
                return channels
            except Exception:
                continue

        return 1

    def _prepare_output(
        self,
        audio_f32: np.ndarray,
        sample_rate_hz: int,
        gain: float,
    ) -> tuple[np.ndarray, str | int | None, int]:
        resolved = self._resolve_device()
        channels = self._select_channels(resolved, sample_rate_hz)
        audio_f32 = np.asarray(audio_f32, dtype=np.float32)
        audio_f32 = np.clip(audio_f32 * float(gain), -1.0, 1.0)

        if audio_f32.ndim == 1:
            base = audio_f32.reshape(-1, 1)
        elif audio_f32.ndim == 2:
            base = audio_f32
        else:
            base = audio_f32.reshape(-1, 1)

        if base.shape[1] == channels:
            return base, resolved, channels
        if base.shape[1] == 1 and channels > 1:
            return np.repeat(base, channels, axis=1), resolved, channels
        if base.shape[1] > channels:
            return base[:, :channels], resolved, channels
        if base.shape[1] < channels:
            reps = int(np.ceil(channels / float(base.shape[1])))
            expanded = np.tile(base, (1, reps))
            return expanded[:, :channels], resolved, channels
        return base, resolved, channels

    def _play_stream_blocking(self, audio_f32: np.ndarray, sample_rate_hz: int, gain: float = 1.0) -> None:
        if audio_f32.size == 0:
            return

        frames, resolved, channels = self._prepare_output(audio_f32, sample_rate_hz, gain)
        chunk_frames = min(max(256, self._blocksize), max(1, int(frames.shape[0])))

        self._stop_event.clear()
        self._finished.clear()
        try:
            stream = sd.OutputStream(
                samplerate=sample_rate_hz,
                channels=channels,
                dtype="float32",
                blocksize=chunk_frames,
                device=resolved,
                latency=self._latency,
            )
            with self._lock:
                self._stream = stream
            stream.start()
            idx = 0
            while idx < frames.shape[0] and not self._stop_event.is_set():
                end = min(idx + chunk_frames, int(frames.shape[0]))
                stream.write(frames[idx:end])
                idx = end
        finally:
            with self._lock:
                stream = self._stream
                self._stream = None
                if threading.current_thread() is self._play_thread:
                    self._play_thread = None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            self._stop_event.clear()
            self._finished.set()

    def play_blocking(self, audio_f32: np.ndarray, sample_rate_hz: int, gain: float = 1.0) -> None:
        self.stop()
        self._play_stream_blocking(audio_f32, sample_rate_hz, gain)

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
        """Open one OutputStream and write chunks as they arrive from ``chunks_iter``.

        When ``keep_mic_live`` is True the PortAudio stream stays open for a later
        ``handoff_from_bridge`` call (wake opener → main reply). Returns when the
        iterator is exhausted or ``stop()`` is called.
        """
        with self._lock:
            reuse = bool(handoff_from_bridge and self._handoff_stream is not None)
            if reuse:
                stream = self._handoff_stream
                resolved = self._handoff_device
                channels = self._handoff_channels
                play_sr = int(self._handoff_sample_rate_hz or sample_rate_hz)
                self._handoff_stream = None
                self._handoff_device = None
                self._handoff_channels = 1
                self._handoff_sample_rate_hz = 0
                self._stream = stream
            else:
                if not handoff_from_bridge:
                    self.stop()
                resolved = self._resolve_device()
                channels = self._select_channels(resolved, sample_rate_hz)
                play_sr = int(sample_rate_hz)
                blocksize = min(max(256, self._blocksize), 2048)
                stream = sd.OutputStream(
                    samplerate=play_sr,
                    channels=channels,
                    dtype="float32",
                    blocksize=blocksize,
                    device=resolved,
                    latency=self._latency,
                )
                self._stream = stream
                stream.start()

            self._stop_event.clear()
            self._finished.clear()

        try:
            first_chunk = True
            for raw_chunk in chunks_iter:
                if self._stop_event.is_set():
                    break
                arr = np.asarray(raw_chunk, dtype=np.float32).reshape(-1)
                if arr.size == 0:
                    continue
                if first_chunk and not skip_edge_ramp_in:
                    arr = apply_f32_linear_fade_ends(
                        arr,
                        play_sr,
                        fade_in_ms=10.0,
                        fade_out_ms=0.0,
                    )
                first_chunk = False
                arr = np.clip(arr * float(gain), -1.0, 1.0)
                base = arr.reshape(-1, 1) if arr.ndim == 1 else arr
                if base.shape[1] != channels:
                    if base.shape[1] == 1 and channels > 1:
                        base = np.repeat(base, channels, axis=1)
                    elif base.shape[1] > channels:
                        base = base[:, :channels]
                    else:
                        reps = int(np.ceil(channels / float(base.shape[1])))
                        base = np.tile(base, (1, reps))[:, :channels]
                stream.write(base)
        finally:
            with self._lock:
                active = stream
                if keep_mic_live and active is not None and not self._stop_event.is_set():
                    self._handoff_stream = active
                    self._handoff_device = resolved
                    self._handoff_channels = channels
                    self._handoff_sample_rate_hz = play_sr
                    self._stream = None
                else:
                    self._handoff_stream = None
                    self._stream = None
                    if active is not None:
                        try:
                            active.stop()
                        except Exception:
                            pass
                        try:
                            active.close()
                        except Exception:
                            pass
                if threading.current_thread() is self._play_thread:
                    self._play_thread = None
            self._stop_event.clear()
            self._finished.set()

    def play(self, audio_f32: np.ndarray, sample_rate_hz: int, gain: float = 1.0) -> None:
        if audio_f32.size == 0:
            return
        self.stop()
        thread = threading.Thread(
            target=self._play_stream_blocking,
            args=(np.asarray(audio_f32, dtype=np.float32).copy(), sample_rate_hz, gain),
            daemon=True,
            name="koov-audio-out",
        )
        with self._lock:
            self._play_thread = thread
        thread.start()

    def wait(self) -> None:
        with self._lock:
            thread = self._play_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        else:
            self._finished.wait()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            stream = self._stream
            handoff = self._handoff_stream
            thread = self._play_thread
            self._handoff_stream = None
            self._handoff_device = None
            self._handoff_channels = 1
            self._handoff_sample_rate_hz = 0
        if handoff is not None:
            try:
                handoff.abort()
            except Exception:
                pass
            try:
                handoff.close()
            except Exception:
                pass
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                try:
                    stream.stop()
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if thread is self._play_thread and (thread is None or not thread.is_alive()):
                self._play_thread = None
            if stream is self._stream:
                self._stream = None
        self._finished.set()


class UtteranceRecorder:
    def __init__(
        self,
        sample_rate_hz: int,
        frame_ms: int,
        min_seconds: float,
        max_seconds: float,
        end_silence_seconds: float,
        pre_roll_seconds: float,
        vad_mode: int = 2,
    ):
        self._sr = sample_rate_hz
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
        self._vad = webrtcvad.Vad(vad_mode)
        self._min_frames = int(max(1, min_seconds / (frame_ms / 1000.0)))
        self._max_frames = int(max_seconds / (frame_ms / 1000.0))
        self._end_silence_frames = int(end_silence_seconds / (frame_ms / 1000.0))
        self._pre_roll_frames = int(pre_roll_seconds / (frame_ms / 1000.0))

    def record_next(
        self,
        frames: Iterable[AudioFrame],
        wait_for_speech_s: float,
        now_s: float | None = None,
    ) -> bytes:
        if now_s is None:
            now_s = time.monotonic()
        deadline = now_s + wait_for_speech_s

        pre_roll: Deque[bytes] = deque(maxlen=self._pre_roll_frames)
        voiced_run = 0

        started = False
        collected: list[bytes] = []
        trailing_silence = 0
        total_frames = 0

        for frame in frames:
            if not started and frame.timestamp_s > deadline:
                return b""

            if len(frame.pcm16) != self._frame_bytes:
                continue

            is_speech = self._vad.is_speech(frame.pcm16, self._sr)

            if not started:
                pre_roll.append(frame.pcm16)
                if is_speech:
                    voiced_run += 1
                else:
                    voiced_run = 0
                if voiced_run >= 3:
                    started = True
                    collected.extend(list(pre_roll))
                    pre_roll.clear()
                    collected.append(frame.pcm16)
                    total_frames = len(collected)
                    trailing_silence = 0
                continue

            collected.append(frame.pcm16)
            total_frames += 1

            if is_speech:
                trailing_silence = 0
            else:
                trailing_silence += 1

            if total_frames >= self._max_frames:
                break

            if total_frames >= self._min_frames and trailing_silence >= self._end_silence_frames:
                break

        if total_frames < self._min_frames:
            return b""
        return b"".join(collected)

    def record_from_seed(
        self,
        seed_pcm16: bytes,
        frames: Iterable[AudioFrame],
        require_voice: bool = True,
    ) -> bytes:
        """Record audio starting from the seed (pre-roll) and continuing with live frames.

        When ``require_voice=False`` (wake already confirmed by an external detector), silence
        is counted even before any VAD-detected speech so the function terminates on trailing
        silence without needing webrtcvad to flag the audio as voiced.  This handles low-
        amplitude Zoom audio that webrtcvad misses but a cloud STT can still transcribe.
        """
        if not seed_pcm16:
            return b""

        usable = (len(seed_pcm16) // self._frame_bytes) * self._frame_bytes
        if usable <= 0:
            return b""
        seed_pcm16 = seed_pcm16[-usable:]

        collected: list[bytes] = [
            seed_pcm16[i : i + self._frame_bytes] for i in range(0, len(seed_pcm16), self._frame_bytes)
        ]
        if not collected:
            return b""

        total_frames = len(collected)
        trailing_silence = 0
        seen_voice = False

        for chunk in collected:
            is_speech = self._vad.is_speech(chunk, self._sr)
            if is_speech:
                seen_voice = True
                trailing_silence = 0
            else:
                if seen_voice or not require_voice:
                    trailing_silence += 1

        for frame in frames:
            if len(frame.pcm16) != self._frame_bytes:
                continue
            collected.append(frame.pcm16)
            total_frames += 1

            is_speech = self._vad.is_speech(frame.pcm16, self._sr)
            if is_speech:
                seen_voice = True
                trailing_silence = 0
            else:
                if seen_voice or not require_voice:
                    trailing_silence += 1

            if total_frames >= self._max_frames:
                break
            if (seen_voice or not require_voice) and total_frames >= self._min_frames and trailing_silence >= self._end_silence_frames:
                break

        if total_frames < self._min_frames:
            return b""
        if require_voice and not seen_voice:
            return b""
        return b"".join(collected)
