"""
Exercise ZoomAudioOutput threaded playback + stop() without a real Zoom SDK.

Run from repo root:
    python3 scripts/test_zoom_barge_in.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types

import numpy as np

# Stubs so importing mechus.zoom_sdk_join does not require optional native deps in bare CI.
_vad = types.ModuleType("webrtcvad")


class _Vad:
    def __init__(self, _mode: int = 2) -> None:
        pass

    def is_speech(self, *_a, **_k) -> bool:
        return False


_vad.Vad = _Vad
sys.modules.setdefault("webrtcvad", _vad)

_stub = types.ModuleType("zoom_meeting_sdk")
_stub.ZoomSDKAudioChannel_Mono = object()
sys.modules.setdefault("zoom_meeting_sdk", _stub)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mechus.zoom_sdk_join import OnDemandMuteZoomOutput, ZoomAudioOutput


class MockSender:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: list[bytes] = []

    def send(self, chunk, *args, **kwargs) -> None:
        with self.lock:
            self.chunks.append(bytes(chunk))


def run(name: str, ok: bool) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        sys.exit(1)


def main() -> None:
    sr = 48000
    out = ZoomAudioOutput(sr)
    sender = MockSender()
    out.set_sender(sender)
    out.set_can_send(True)

    # ~1.0 s at 48 kHz mono float32 → resampled to 32 kHz PCM16 in _prepare_pcm16
    n = int(sr * 1.0)
    audio = np.zeros(n, dtype=np.float32)

    t0 = time.perf_counter()
    out.play(audio, sample_rate_hz=sr, gain=1.0)
    dt_return = time.perf_counter() - t0
    run("play() returns quickly (non-blocking)", dt_return < 0.15)

    time.sleep(0.05)
    out.stop()
    time.sleep(0.02)
    with sender.lock:
        sent = len(sender.chunks)
    # Rough upper bound: full 1s at 32k ≈ 100 chunks; after ~50ms + stop we should send far fewer
    run("stop() cancels most chunks (barge-in)", sent < 40)

    # Full playback
    sender.chunks.clear()
    out.play(audio, sample_rate_hz=sr, gain=1.0)
    out.wait()
    with sender.lock:
        full = len(sender.chunks)
    run("wait() completes full send", full >= 80)

    # OnDemandMuteZoomOutput: mute only after wait/stop
    class Bot:
        def __init__(self) -> None:
            self.unmute_count = 0
            self.mute_count = 0

        def unmute_audio(self) -> None:
            self.unmute_count += 1

        def mute_audio(self) -> None:
            self.mute_count += 1

    inner = ZoomAudioOutput(sr)
    sender2 = MockSender()
    inner.set_sender(sender2)
    inner.set_can_send(True)
    bot = Bot()
    wrapped = OnDemandMuteZoomOutput(inner, bot)

    short_audio = np.zeros(int(sr * 0.2), dtype=np.float32)
    wrapped.play(short_audio, sample_rate_hz=sr, gain=1.0)
    run("OnDemandMuteZoomOutput.play does not mute immediately", bot.mute_count == 0)
    wrapped.wait()
    run("OnDemandMuteZoomOutput.wait mutes", bot.mute_count == 1 and bot.unmute_count == 1)

    print("\nAll Zoom barge-in tests passed.")


if __name__ == "__main__":
    main()
