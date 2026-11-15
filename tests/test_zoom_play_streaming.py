"""Zoom virtual mic: play_streaming sends chunks without waiting for full buffer."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

# play_streaming imports zoom_meeting_sdk inside the worker thread.
if "zoom_meeting_sdk" not in sys.modules:
    _zm = types.ModuleType("zoom_meeting_sdk")
    _zm.ZoomSDKAudioChannel_Mono = 0
    sys.modules["zoom_meeting_sdk"] = _zm

from mechus.zoom_sdk_join import OnDemandMuteZoomOutput, ZoomAudioOutput


class MockSender:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.lock = __import__("threading").Lock()

    def send(self, data: bytes, *args) -> None:
        with self.lock:
            self.chunks.append(bytes(data))


class TestZoomPlayStreaming(unittest.TestCase):
    def test_play_streaming_sends_before_iterator_exhausted(self) -> None:
        sr = 32000
        out = ZoomAudioOutput(sr)
        sender = MockSender()
        out.set_sender(sender)
        out.set_can_send(True)

        def _chunks():
            yield np.ones(int(sr * 0.05), dtype=np.float32) * 0.1
            with sender.lock:
                mid = len(sender.chunks)
            self.assertGreater(mid, 0)
            yield np.ones(int(sr * 0.05), dtype=np.float32) * 0.1

        out.play_streaming(_chunks(), sample_rate_hz=sr, gain=1.0)
        with sender.lock:
            self.assertGreater(len(sender.chunks), 0)

    def test_on_demand_mute_delegates_play_streaming(self) -> None:
        sr = 32000
        inner = ZoomAudioOutput(sr)
        sender = MockSender()
        inner.set_sender(sender)
        inner.set_can_send(True)

        class Bot:
            def __init__(self) -> None:
                self.unmute_count = 0
                self.mute_count = 0

            def unmute_audio(self) -> None:
                self.unmute_count += 1

            def mute_audio(self) -> None:
                self.mute_count += 1

        bot = Bot()
        with patch.dict(os.environ, {"MECHUS_ZOOM_MUTE_WHEN_IDLE": "1"}, clear=False):
            wrapped = OnDemandMuteZoomOutput(inner, bot)
            wrapped.play_streaming(
                (np.zeros(int(sr * 0.02), dtype=np.float32) for _ in range(2)),
                sample_rate_hz=sr,
                gain=1.0,
            )
        self.assertEqual(bot.unmute_count, 1)
        self.assertEqual(bot.mute_count, 1)

    def test_paced_send_catches_up_without_permanent_gaps(self) -> None:
        """Late frames must not permanently stretch the send clock (that sounds glitchy)."""
        out = ZoomAudioOutput(32000)
        # 60 ms of audio → three 20 ms frames
        pcm = b"\x00\x01" * int(32000 * 0.06)
        times: list[float] = []
        base = 1000.0

        class Clock:
            def __init__(self) -> None:
                self.t = base

            def perf_counter(self) -> float:
                return self.t

            def sleep(self, dt: float) -> None:
                self.t += max(0.0, dt)

        clock = Clock()

        class SlowSender:
            def send(self, data: bytes, *args) -> None:
                times.append(clock.t)
                # Simulate 5 ms of scheduling delay after each send.
                clock.t += 0.005

        class Zoom:
            ZoomSDKAudioChannel_Mono = 0

        with patch("time.perf_counter", clock.perf_counter), patch("time.sleep", clock.sleep):
            end = out._send_pcm16_paced(
                pcm,
                struct_mod=__import__("struct"),
                sender=SlowSender(),
                zoom=Zoom(),
                next_send=base,
                ramp_in=False,
                ramp_out=False,
            )

        self.assertEqual(len(times), 3)
        # Schedule advances by 20 ms each frame from the original clock — not wall time.
        self.assertAlmostEqual(end - base, 0.06, places=3)
        # Sends happen without inserting permanent >20 ms gaps between schedule ticks.
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        self.assertTrue(all(g < 0.03 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
