"""MECHUS_LOW_LATENCY preset."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestLowLatencyEnv(unittest.TestCase):
    def test_low_latency_forces_lexical_and_zero_embed(self) -> None:
        env = {
            "MECHUS_LOW_LATENCY": "1",
            "MECHUS_RETRIEVAL_MODE": "hybrid",
            "MECHUS_RETRIEVAL_EMBED_WEIGHT": "0.5",
            "MECHUS_RETRIEVAL_EMBED_RERANK_TOP_K": "28",
            "MECHUS_RETRIEVAL_MAX_CHARS": "8000",
            "MECHUS_RETRIEVAL_GENERAL_MAX_CHARS": "5000",
            "MECHUS_RESPONSE_OPENAI_TIMEOUT_S": "60",
        }
        with patch.dict(os.environ, env, clear=False):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertEqual(c.retrieval_mode, "lexical")
        self.assertEqual(c.retrieval_embed_weight, 0.0)
        self.assertLessEqual(c.retrieval_embed_rerank_top_k, 16)
        self.assertLessEqual(c.retrieval_max_chars, 3500)
        self.assertLessEqual(c.retrieval_general_max_chars, 2400)
        self.assertLessEqual(c.response_openai_timeout_s, 15.0)
        self.assertLessEqual(c.response_prompt_max_chars, 8000)

    def test_low_latency_caps_llm_prompt_chars_even_when_env_high(self) -> None:
        env = {
            "MECHUS_LOW_LATENCY": "1",
            "MECHUS_RESPONSE_PROMPT_MAX_CHARS": "20000",
        }
        with patch.dict(os.environ, env, clear=False):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertEqual(c.response_prompt_max_chars, 8000)

    def test_thinking_preroll_transcript_stt_env_toggle(self) -> None:
        from mechus.config import MechusConfig

        with patch.dict(os.environ, {"MECHUS_THINKING_PREROLL_TRANSCRIPT_STT": "1"}, clear=False):
            self.assertTrue(MechusConfig.from_env().thinking_preroll_transcript_stt)
        with patch.dict(os.environ, {"MECHUS_THINKING_PREROLL_TRANSCRIPT_STT": "0"}, clear=False):
            self.assertFalse(MechusConfig.from_env().thinking_preroll_transcript_stt)

    def test_low_latency_enables_thinking_preroll_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {"MECHUS_LOW_LATENCY": "1", "MECHUS_TTS_SMOOTH": "1"},
            clear=False,
        ):
            for key in (
                "MECHUS_THINKING_PREROLL",
                "MECHUS_THINKING_PREROLL_TRANSCRIPT_STT",
                "MECHUS_WAKE_OPENER",
                "MECHUS_WAKE_OPENER_EXTEND_CAP_S",
            ):
                os.environ.pop(key, None)
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertTrue(c.thinking_preroll_enabled)
        # Production defaults transcript preroll off — gapless opener extend owns the bridge.
        self.assertFalse(c.thinking_preroll_transcript_stt)
        self.assertTrue(c.wake_opener_enabled)
        self.assertGreaterEqual(c.wake_opener_extend_cap_s, 2.5)

    def test_zero_extend_cap_bumped_when_wake_opener_on(self) -> None:
        """Production had EXTEND_CAP=0 which silenced the gapless bridge during LLM."""
        with patch.dict(
            os.environ,
            {
                "MECHUS_LOW_LATENCY": "1",
                "MECHUS_WAKE_OPENER": "1",
                "MECHUS_WAKE_OPENER_EXTEND_CAP_S": "0",
            },
            clear=False,
        ):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertGreaterEqual(c.wake_opener_extend_cap_s, 18.0)


if __name__ == "__main__":
    unittest.main()
