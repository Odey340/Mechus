"""Live TTS streaming: chunks must flow producer -> audio_out without pre-buffering."""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest


def _mechus_agent_class():
    if "sounddevice" not in sys.modules:
        sys.modules["sounddevice"] = types.SimpleNamespace(
            PortAudioError=Exception,
            query_devices=lambda *args, **kwargs: {"default_samplerate": 44100},
        )
    if "webrtcvad" not in sys.modules:
        class _Vad:
            def is_speech(self, *args, **kwargs):
                return False

        sys.modules["webrtcvad"] = types.SimpleNamespace(Vad=lambda *args, **kwargs: _Vad())
    if "soundfile" not in sys.modules:
        sys.modules["soundfile"] = types.SimpleNamespace(read=lambda *a, **k: ([], 16000))

    from mechus.agent import MechusAgent

    return MechusAgent


class _FakeAudioOut:
    """Records the order chunks arrive at play_streaming."""

    def __init__(self) -> None:
        self.played: list[list[float]] = []
        self.sample_rate: int | None = None
        self.gain: float | None = None
        self.streaming_calls = 0
        self.blocking_calls = 0
        self.silence_tail_calls = 0

    def play_streaming(self, chunks_iter, sample_rate_hz: int, gain: float = 1.0, **kwargs) -> None:
        self.streaming_calls += 1
        self.sample_rate = sample_rate_hz
        self.gain = gain
        for chunk in chunks_iter:
            self.played.append(list(chunk))

    def play_blocking(self, audio_f32, sample_rate_hz: int, gain: float = 1.0) -> None:
        vals = list(audio_f32)
        self.sample_rate = sample_rate_hz
        self.gain = gain
        # After streaming, the agent plays a short pure-silence tail so the final
        # phoneme is not clipped (MechusAgent._play_phrase_end_tail). That is padding,
        # not audio content, so it is recorded separately and kept out of `played`.
        if vals and not any(vals):
            self.silence_tail_calls += 1
            return
        self.blocking_calls += 1
        self.played.append(vals)


class _FakeTTSEngine:
    """Pushes a series of chunks through ``on_chunk`` with a small delay."""

    def __init__(self, chunks, sample_rate: int = 16000, delay_s: float = 0.0):
        self._chunks = list(chunks)
        self._sample_rate = sample_rate
        self._delay_s = delay_s
        self.synth_calls = 0

    def synthesize_streaming(self, text, on_chunk, *, first_chunk_ms: float = 80.0):
        import numpy as np

        self.synth_calls += 1
        for c in self._chunks:
            if self._delay_s:
                time.sleep(self._delay_s)
            on_chunk(np.asarray(c, dtype=np.float32), self._sample_rate)
        return types.SimpleNamespace(audio_f32=None, sample_rate_hz=self._sample_rate)


def _make_agent_for_streaming(audio_out, tts_output_gain: float = 1.0):
    MechusAgent = _mechus_agent_class()
    agent = object.__new__(MechusAgent)
    agent._audio_out = audio_out
    agent._wake_opener_played = False
    agent._wake_pipeline_t0 = time.perf_counter()
    agent._last_latency = None
    agent._pending_transcript_wake = None
    agent._config = types.SimpleNamespace(tts_output_gain=tts_output_gain)
    agent._playback_output_sample_rate = lambda: 16000
    agent._resample_audio_for_playback = lambda audio, sr: (audio, 16000)
    agent._tts_audio_for_playback = lambda audio, sr, segment="full": (audio, sr)
    agent._log_time_to_first_audio = lambda *a, **kw: None
    return agent


class TestLiveStreamingPlayback(unittest.TestCase):
    def test_chunks_flow_in_order_to_play_streaming(self) -> None:
        import numpy as np

        audio_out = _FakeAudioOut()
        agent = _make_agent_for_streaming(audio_out)
        tts = _FakeTTSEngine(
            [np.array([0.1, 0.2]), np.array([0.3, 0.4]), np.array([0.5, 0.6])]
        )

        agent._play_streaming_phrase_smooth(tts, "hello world")

        self.assertEqual(audio_out.streaming_calls, 1)
        self.assertEqual(audio_out.blocking_calls, 0)
        flattened = [v for chunk in audio_out.played for v in chunk]
        self.assertEqual(flattened, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        # The streamed reply is followed by a silence tail so the last phoneme
        # is not clipped; content must still go out via play_streaming only.
        self.assertEqual(audio_out.silence_tail_calls, 1)

    def test_empty_stream_does_not_call_play(self) -> None:
        audio_out = _FakeAudioOut()
        agent = _make_agent_for_streaming(audio_out)
        tts = _FakeTTSEngine([])

        agent._play_streaming_phrase_smooth(tts, "nothing")

        self.assertEqual(audio_out.streaming_calls, 0)
        self.assertEqual(audio_out.blocking_calls, 0)

    def test_prefetched_chunks_skip_live_streaming(self) -> None:
        import numpy as np

        audio_out = _FakeAudioOut()
        agent = _make_agent_for_streaming(audio_out)
        tts = _FakeTTSEngine([np.array([1.0])])

        prefetched = [(np.array([0.9, 0.8], dtype=np.float32), 16000)]
        agent._play_streaming_phrase_smooth(tts, "buffered", prefetched=prefetched)

        self.assertEqual(audio_out.streaming_calls, 0)
        self.assertEqual(audio_out.blocking_calls, 1)
        self.assertEqual(tts.synth_calls, 0, "prefetched path must not call synthesize_streaming")

    def test_falls_back_when_audio_out_has_no_play_streaming(self) -> None:
        import numpy as np

        class _LegacyOut:
            def __init__(self) -> None:
                self.played: list[list[float]] = []

            def play_blocking(self, audio_f32, sample_rate_hz: int, gain: float = 1.0) -> None:
                self.played.append(list(audio_f32))

        audio_out = _LegacyOut()
        agent = _make_agent_for_streaming(audio_out)
        tts = _FakeTTSEngine(
            [np.array([0.1]), np.array([0.2])]
        )

        agent._play_streaming_phrase_smooth(tts, "fallback")

        self.assertEqual(len(audio_out.played), 1, "legacy path concatenates into one play call")
        self.assertEqual(audio_out.played[0], [0.1, 0.2])

    def test_consumer_does_not_wait_for_full_synthesis(self) -> None:
        """Producer slow → consumer should start playing as soon as the first chunk lands."""
        import numpy as np

        audio_out = _FakeAudioOut()
        first_chunk_play_t: list[float] = []

        class _RecordingOut(_FakeAudioOut):
            def play_streaming(self, chunks_iter, sample_rate_hz: int, gain: float = 1.0, **kwargs):
                self.streaming_calls += 1
                for chunk in chunks_iter:
                    if not first_chunk_play_t:
                        first_chunk_play_t.append(time.monotonic())
                    self.played.append(list(chunk))

        audio_out = _RecordingOut()
        agent = _make_agent_for_streaming(audio_out)
        tts = _FakeTTSEngine(
            [np.array([0.1]), np.array([0.2]), np.array([0.3])],
            delay_s=0.10,
        )

        t0 = time.monotonic()
        agent._play_streaming_phrase_smooth(tts, "slow producer")
        elapsed_to_first_play = (first_chunk_play_t[0] - t0) if first_chunk_play_t else 999.0

        self.assertLess(
            elapsed_to_first_play,
            0.25,
            f"first chunk should reach audio_out well before full synthesis "
            f"(got {elapsed_to_first_play * 1000:.0f}ms)",
        )
        self.assertEqual(len(audio_out.played), 3)


class TestAudioOutputPlayStreaming(unittest.TestCase):
    """Smoke test that AudioOutput exposes play_streaming with expected signature."""

    def test_audio_output_has_play_streaming(self) -> None:
        from mechus.audio import AudioOutput

        self.assertTrue(callable(getattr(AudioOutput, "play_streaming", None)))


if __name__ == "__main__":
    unittest.main()
