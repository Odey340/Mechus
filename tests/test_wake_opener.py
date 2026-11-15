"""Wake opener: config defaults and phrase selection."""

from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

import numpy as np


def _koov_agent_class():
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
        sys.modules["soundfile"] = types.SimpleNamespace(
            read=lambda *args, **kwargs: (_np_zeros(), 16000)
        )

    from koov.agent import KoovAgent

    return KoovAgent


def _np_zeros():
    import numpy as np

    return np.zeros((0,), dtype=np.float32)


class TestWakeOpenerConfig(unittest.TestCase):
    def test_low_latency_default_opener_cap_about_one_second(self) -> None:
        with patch.dict(os.environ, {"KOOV_LOW_LATENCY": "1"}, clear=False):
            from koov.config import KoovConfig

            c = KoovConfig.from_env()
        self.assertGreaterEqual(c.wake_opener_max_audio_s, 1.0)
        self.assertTrue(c.wake_opener_enabled)

    def test_low_latency_default_extend_cap_bridges_llm_wait(self) -> None:
        with patch.dict(os.environ, {"KOOV_LOW_LATENCY": "1"}, clear=False):
            from koov.config import KoovConfig

            c = KoovConfig.from_env()
        self.assertGreaterEqual(c.wake_opener_extend_cap_s, 2.5)

    def test_non_low_latency_extend_cap_moderate_by_default(self) -> None:
        with patch.dict(os.environ, {"KOOV_LOW_LATENCY": "0"}, clear=False):
            from koov.config import KoovConfig

            c = KoovConfig.from_env()
        self.assertGreaterEqual(c.wake_opener_extend_cap_s, 3.0)

    def test_extend_cap_override_via_env(self) -> None:
        with patch.dict(
            os.environ,
            {"KOOV_LOW_LATENCY": "1", "KOOV_WAKE_OPENER_EXTEND_CAP_S": "1.7"},
            clear=False,
        ):
            from koov.config import KoovConfig

            c = KoovConfig.from_env()
        self.assertAlmostEqual(c.wake_opener_extend_cap_s, 1.7, places=2)


class TestWakeOpenerPhrasePick(unittest.TestCase):
    def test_prefers_acknowledgment_phrases(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "On it.",
            "Give me a moment.",
            "Yeah, give me a moment.",
            "Let me pull that up.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(pool, prefer_acknowledgment=True) for _ in range(40)
        }
        self.assertTrue(
            any("moment" in p.lower() or "yeah" in p.lower() for p in picks),
            f"expected acknowledgment bias, got {picks}",
        )

    def test_fact_questions_prefer_lookup_phrases(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "Oh, good question.",
            "Right, from what we said.",
            "Sure, let me check that.",
            "Let's look that up.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(pool, request_text="what's the weather in Solon")
            for _ in range(40)
        }
        self.assertTrue(all("check" in p.lower() or "look" in p.lower() for p in picks))

    def test_opinion_questions_prefer_reflective_phrases(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "Sure, let me check that.",
            "Right, from what we said.",
            "Oh, good question.",
            "Wow, that's interesting.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(pool, request_text="what do you think about this")
            for _ in range(40)
        }
        self.assertTrue(all("question" in p.lower() or "interesting" in p.lower() for p in picks))

    def test_meeting_questions_prefer_recall_phrases(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "Sure, let me check that.",
            "Oh, good question.",
            "Right, from what we said.",
            "As we talked about it.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(pool, request_text="what did we decide in the meeting")
            for _ in range(40)
        }
        self.assertTrue(all("said" in p.lower() or "talked" in p.lower() for p in picks))

    def test_wake_opener_deprioritizes_bare_yeah(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "Yeah, give me a moment.",
            "Hmm, good question.",
            "Let me check.",
            "Walking back through that now.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(
                pool,
                request_text="what do you think about blue",
                for_wake_opener=True,
            )
            for _ in range(30)
        }
        self.assertTrue(all("question" in p.lower() or "think" in p.lower() for p in picks))

    def test_fact_tell_me_about_uses_lookup_phrases(self) -> None:
        KoovAgent = _koov_agent_class()

        pool = [
            "Oh, good question.",
            "Let me check.",
            "Right, from what we said.",
            "Walking back through that now.",
        ]
        agent = object.__new__(KoovAgent)
        picks = {
            agent._pick_thinking_phrase(
                pool,
                request_text="can you tell me about the solar system",
                for_wake_opener=True,
            )
            for _ in range(30)
        }
        self.assertTrue(all("check" in p.lower() for p in picks))


class TestWakeOpenerExtend(unittest.TestCase):
    """Opener plays once, then holds the stream until reply TTS (no filler loop)."""

    def _make_agent(self, *, extend_cap_s: float, max_audio_cap_s: float = 0.5):
        KoovAgent = _koov_agent_class()
        agent = object.__new__(KoovAgent)

        agent._tts_playing = False
        agent._wake_opener_played = False
        agent._wake_pipeline_t0 = time.perf_counter()
        agent._last_latency = None
        agent._config = types.SimpleNamespace(
            wake_opener_max_audio_s=max_audio_cap_s,
            wake_opener_extend_cap_s=extend_cap_s,
            thinking_preroll_enabled=False,
            thinking_preroll_transcript_stt=False,
            thinking_preroll_max_audio_s=0.45,
        )
        agent._bridge_finale_preroll_played = False
        agent._wake_opener_bridge_active = False
        agent._zoom_virtual_mic_output = lambda: True
        agent._reassert_facetime_tts_routing = lambda: None
        agent._wake_opener_command_text = lambda trigger_text, *, source: trigger_text
        agent._log_time_to_first_audio = lambda *a, **kw: None
        agent._audio_out = types.SimpleNamespace(play_blocking=lambda *a, **k: None)

        plays: list[float] = []

        def _fake_play(
            persona,
            *,
            request_text="",
            max_audio_cap_s=None,
            prefer_short_fillers=False,
            prefer_acknowledgment=False,
            bridge_segment="full",
            **kwargs,
        ):
            plays.append(time.monotonic())
            time.sleep(0.05)  # simulate a tiny audio clip
            return True

        agent._play_cached_thinking_preroll = _fake_play
        return agent, plays

    def test_opener_holds_until_stop_without_extra_fillers(self) -> None:
        agent, plays = self._make_agent(extend_cap_s=2.0)
        stop_evt = threading.Event()

        th = threading.Thread(
            target=agent._run_wake_opener,
            args=(object(),),
            kwargs={"source": "transcript_stt", "trigger_text": "computer hello",
                    "stop_evt": stop_evt},
            daemon=True,
        )
        th.start()
        time.sleep(0.30)
        stop_evt.set()
        th.join(timeout=2.0)

        self.assertFalse(th.is_alive(), "opener thread should exit after stop_evt set")
        self.assertEqual(len(plays), 1, "expected a single opener clip, not repeated fillers")

    def test_opener_respects_extend_cap(self) -> None:
        agent, plays = self._make_agent(extend_cap_s=0.25)
        stop_evt = threading.Event()  # never set; rely on cap

        t0 = time.monotonic()
        agent._run_wake_opener(
            object(), source="transcript_stt", trigger_text="computer hello",
            stop_evt=stop_evt,
        )
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 1.5, f"extend cap should bound runtime; got {elapsed:.2f}s")
        self.assertGreaterEqual(len(plays), 1)

    def test_opener_sets_handoff_after_extend_bridge(self) -> None:
        agent, plays = self._make_agent(extend_cap_s=1.0)
        stop_evt = threading.Event()

        th = threading.Thread(
            target=agent._run_wake_opener,
            args=(object(),),
            kwargs={
                "source": "transcript_stt",
                "trigger_text": "koov weather",
                "stop_evt": stop_evt,
            },
            daemon=True,
        )
        th.start()
        time.sleep(0.12)
        stop_evt.set()
        th.join(timeout=2.0)

        self.assertEqual(len(plays), 1)
        self.assertTrue(agent._bridge_finale_preroll_played)

    def test_opener_plays_finale_preroll_when_extend_disabled(self) -> None:
        agent, plays = self._make_agent(extend_cap_s=0.0)
        agent._config.thinking_preroll_enabled = True
        agent._config.thinking_preroll_transcript_stt = True
        stop_evt = threading.Event()
        stop_evt.set()

        agent._run_wake_opener(
            object(),
            source="transcript_stt",
            trigger_text="koov weather",
            stop_evt=stop_evt,
        )

        self.assertEqual(len(plays), 2, "expected opener ack + one full finale preroll")
        self.assertTrue(agent._bridge_finale_preroll_played)

    def test_gapless_empty_cache_falls_back_to_per_clip_blocking(self) -> None:
        KoovAgent = _koov_agent_class()
        agent = object.__new__(KoovAgent)
        agent._tts_playing = False
        agent._wake_opener_played = False
        agent._bridge_finale_preroll_played = False
        agent._wake_pipeline_t0 = time.perf_counter()
        agent._last_latency = None
        agent._wake_opener_bridge_active = False
        agent._config = types.SimpleNamespace(
            wake_opener_max_audio_s=1.0,
            wake_opener_extend_cap_s=2.0,
            thinking_preroll_enabled=False,
            thinking_preroll_transcript_stt=False,
            thinking_preroll_max_audio_s=0.45,
            tts_output_gain=1.0,
        )
        agent._zoom_virtual_mic_output = lambda: False
        agent._playback_output_sample_rate = lambda: 32000
        agent._reassert_facetime_tts_routing = lambda: None
        agent._wake_opener_command_text = lambda trigger_text, *, source: trigger_text
        agent._log_time_to_first_audio = lambda *a, **kw: None
        agent._thinking_cache_has_audio = lambda persona: True
        agent._wait_thinking_cache_min = lambda persona, *, timeout_s=4.0: True
        agent._play_wake_bridge_streaming = MagicMock(return_value=0)

        blocking_calls: list[str] = []

        def _blocking_play(
            persona,
            *,
            request_text="",
            max_audio_cap_s=None,
            prefer_short_fillers=False,
            prefer_acknowledgment=False,
            bridge_segment="full",
            **kwargs,
        ):
            blocking_calls.append(bridge_segment)
            return True

        agent._play_cached_thinking_preroll = _blocking_play

        agent._audio_out = types.SimpleNamespace(play_streaming=lambda *a, **k: None)
        stop_evt = threading.Event()
        stop_evt.set()
        agent._run_wake_opener(
            object(),
            source="transcript_stt",
            trigger_text="koov weather",
            stop_evt=stop_evt,
        )
        self.assertIn("opener", blocking_calls, "empty gapless bridge should fall back to blocking opener")

    def test_gapless_bridge_uses_single_play_streaming_on_zoom(self) -> None:
        KoovAgent = _koov_agent_class()
        agent = object.__new__(KoovAgent)
        agent._tts_playing = False
        agent._wake_opener_played = False
        agent._bridge_finale_preroll_played = False
        agent._wake_pipeline_t0 = time.perf_counter()
        agent._last_latency = None
        agent._wake_opener_bridge_active = False
        agent._config = types.SimpleNamespace(
            wake_opener_max_audio_s=1.0,
            wake_opener_extend_cap_s=2.0,
            thinking_preroll_enabled=False,
            thinking_preroll_transcript_stt=False,
            thinking_preroll_max_audio_s=0.45,
            tts_output_gain=1.0,
        )
        agent._zoom_virtual_mic_output = lambda: True
        agent._playback_output_sample_rate = lambda: 32000
        agent._reassert_facetime_tts_routing = lambda: None
        agent._wake_opener_command_text = lambda trigger_text, *, source: trigger_text
        agent._log_time_to_first_audio = lambda *a, **kw: None
        agent._thinking_cache_has_audio = lambda persona: True
        agent._wait_thinking_cache_min = lambda persona, *, timeout_s=4.0: True
        chunk = np.ones(400, dtype=np.float32) * 0.1
        agent._prepare_bridge_chunk_f32 = MagicMock(return_value=(chunk, 32000))
        stream_calls: list[dict] = []

        class _ZoomOut:
            def play_streaming(self, chunks_iter, **kwargs):
                stream_calls.append(kwargs)
                n = 0
                for c in chunks_iter:
                    if np.asarray(c).size:
                        n += 1
                stream_calls[-1]["_chunks"] = n

            def play_blocking(self, *args, **kwargs):
                raise AssertionError("Zoom bridge must not use per-clip play_blocking")

        agent._audio_out = _ZoomOut()
        stop_evt = threading.Event()
        stop_evt.set()
        agent._run_wake_opener(
            object(),
            source="vosk",
            trigger_text="koov weather",
            stop_evt=stop_evt,
        )
        self.assertEqual(len(stream_calls), 1, "Zoom bridge should use one play_streaming call")
        self.assertEqual(stream_calls[0].get("sample_rate_hz"), 32000)
        self.assertGreaterEqual(stream_calls[0].get("_chunks", 0), 1)
        agent._prepare_bridge_chunk_f32.assert_called()
        self.assertTrue(agent._bridge_finale_preroll_played)

    def test_gapless_bridge_uses_single_play_streaming_on_pulse(self) -> None:
        KoovAgent = _koov_agent_class()
        agent = object.__new__(KoovAgent)
        agent._tts_playing = False
        agent._wake_opener_played = False
        agent._bridge_finale_preroll_played = False
        agent._wake_pipeline_t0 = time.perf_counter()
        agent._last_latency = None
        agent._wake_opener_bridge_active = False
        agent._config = types.SimpleNamespace(
            wake_opener_max_audio_s=1.0,
            wake_opener_extend_cap_s=2.0,
            thinking_preroll_enabled=False,
            thinking_preroll_transcript_stt=False,
            thinking_preroll_max_audio_s=0.45,
            tts_output_gain=1.0,
        )
        agent._zoom_virtual_mic_output = lambda: False
        agent._playback_output_sample_rate = lambda: 32000
        agent._reassert_facetime_tts_routing = lambda: None
        agent._wake_opener_command_text = lambda trigger_text, *, source: trigger_text
        agent._log_time_to_first_audio = lambda *a, **kw: None
        agent._prepare_bridge_chunk_f32 = MagicMock(
            return_value=(np.ones(300, dtype=np.float32) * 0.1, 32000),
        )
        stream_calls: list[int] = []

        class _PulseOut:
            def play_streaming(self, chunks_iter, **kwargs):
                stream_calls.append(sum(1 for c in chunks_iter if np.asarray(c).size))

        agent._audio_out = _PulseOut()
        stop_evt = threading.Event()
        stop_evt.set()
        agent._run_wake_opener(
            object(),
            source="transcript_stt",
            trigger_text="koov weather",
            stop_evt=stop_evt,
        )
        self.assertEqual(len(stream_calls), 1, "Pulse bridge should use one play_streaming call")
        self.assertGreaterEqual(
            stream_calls[0],
            1,
            "pre-set stop_evt should yield opener audio (may split for crossfade tail)",
        )
        self.assertTrue(agent._bridge_finale_preroll_played)

    def test_opener_no_extend_when_cap_zero(self) -> None:
        agent, plays = self._make_agent(extend_cap_s=0.0)
        stop_evt = threading.Event()

        agent._run_wake_opener(
            object(), source="transcript_stt", trigger_text="computer hello",
            stop_evt=stop_evt,
        )

        self.assertEqual(len(plays), 1, "opener should only play the initial clip")


if __name__ == "__main__":
    unittest.main()
