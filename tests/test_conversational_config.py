"""MECHUS_CONVERSATIONAL preset bundles latency-friendly defaults (no auto barge-in)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestConversationalEnv(unittest.TestCase):
    def test_conversational_enables_low_latency_lexical(self) -> None:
        env = {"MECHUS_CONVERSATIONAL": "1", "MECHUS_RETRIEVAL_MODE": "hybrid"}
        with patch.dict(os.environ, env, clear=False):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertTrue(c.conversational_mode)
        self.assertEqual(c.retrieval_mode, "lexical")
        self.assertEqual(c.retrieval_embed_weight, 0.0)

    def test_conversational_snappier_transcript_defaults(self) -> None:
        with patch.dict(os.environ, {"MECHUS_CONVERSATIONAL": "1"}, clear=False):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertLessEqual(c.transcript_chunk_seconds, 9.0)
        self.assertLessEqual(c.min_silence_before_speaking_transcript_stt_s, 0.12)
        self.assertLessEqual(c.tts_interrupt_grace_ms, 1200.0)
        # Directed barge-in ("Mechus, stop" / a new "Mechus …" command mid-reply) is
        # now ON by default — it routes through the transcript wake detector so
        # it is echo-safe, and the streaming-TTS watcher keeps latency low.
        self.assertTrue(c.transcript_barge_in_enabled)
        self.assertTrue(c.transcript_barge_in_stop_only)
        # Raw-VAD interrupt on Zoom stays OFF (Zoom mixed audio echoes Mechus's TTS).
        self.assertFalse(c.interrupt_on_speech_zoom)


if __name__ == "__main__":
    unittest.main()
