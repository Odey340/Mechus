"""Production latency env defaults (Zoom + FaceTime workers)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from koov.facetime_env import (
    PRODUCTION_WAKE_PHRASE,
    apply_production_latency_env_defaults,
    production_latency_env_dict,
)


class TestProductionLatencyEnv(unittest.TestCase):
    def test_low_latency_default_eleven_flash_tts(self) -> None:
        with patch.dict(os.environ, {"KOOV_LOW_LATENCY": "1"}, clear=False):
            for key in ("KOOV_ELEVEN_MODEL_ID",):
                os.environ.pop(key, None)
            from koov.config import KoovConfig

            c = KoovConfig.from_env()
        self.assertEqual(c.eleven_model_id, "eleven_flash_v2_5")
        self.assertFalse(c.eleven_audio_tags)

    def test_apply_production_latency_sets_eleven_agent_models(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            apply_production_latency_env_defaults()
            self.assertEqual(os.environ.get("KOOV_ELEVEN_MODEL_ID"), "eleven_flash_v2_5")
            self.assertEqual(os.environ.get("KOOV_ELEVEN_STT_MODEL"), "scribe_v2_realtime")
            self.assertEqual(os.environ["KOOV_WAKE_PHRASE"], "hey mechus,mechus")
            self.assertEqual(PRODUCTION_WAKE_PHRASE, "hey mechus,mechus")
            self.assertEqual(os.environ["KOOV_WAKE_OPENER"], "1")
            self.assertEqual(os.environ["KOOV_THINKING_PREROLL"], "1")
            self.assertEqual(os.environ["KOOV_THINKING_PREROLL_TRANSCRIPT_STT"], "0")
            self.assertEqual(os.environ["KOOV_WAKE_OPENER_EXTEND_CAP_S"], "18")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_STYLE"], "0.86")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_STABILITY"], "0.12")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_SPEED"], "1.09")

    def test_production_latency_dict_includes_opener_and_preroll(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            d = production_latency_env_dict()
        self.assertEqual(d["KOOV_WAKE_OPENER"], "1")
        self.assertEqual(d["KOOV_THINKING_PREROLL_TRANSCRIPT_STT"], "0")
        self.assertEqual(d["KOOV_ELEVEN_TTS_STYLE"], "0.86")

    def test_facetime_eleven_overrides_include_expressiveness(self) -> None:
        from koov.facetime_env import facetime_worker_eleven_overrides

        with patch.dict(os.environ, {}, clear=True):
            out = facetime_worker_eleven_overrides()
        self.assertEqual(out["KOOV_ELEVEN_TTS_STYLE"], "0.86")
        self.assertEqual(out["KOOV_ELEVEN_TTS_STABILITY"], "0.12")

    def test_facetime_worker_env_enables_opener_not_parallel_transcript_preroll(self) -> None:
        from koov.facetime_env import apply_facetime_worker_env_defaults

        with patch.dict(os.environ, {}, clear=True):
            apply_facetime_worker_env_defaults()
            self.assertEqual(os.environ["KOOV_WAKE_OPENER"], "1")
            self.assertEqual(os.environ["KOOV_THINKING_PREROLL"], "1")
            self.assertEqual(os.environ["KOOV_THINKING_PREROLL_TRANSCRIPT_STT"], "0")
            self.assertEqual(os.environ["KOOV_WAKE_OPENER_EXTEND_CAP_S"], "18")

    def test_zoom_worker_env_uses_human_tts_middle_ground(self) -> None:
        from koov.facetime_env import apply_zoom_worker_env_defaults

        with patch.dict(os.environ, {}, clear=True):
            apply_zoom_worker_env_defaults()
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_SPEED"], "1.0")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_STYLE"], "0.50")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_STABILITY"], "0.40")
            self.assertEqual(os.environ["KOOV_ELEVEN_TTS_LATENCY_OPT"], "1")
            self.assertEqual(os.environ["KOOV_TRANSCRIPT_BARGE_IN_STOP_ONLY"], "0")
            self.assertEqual(os.environ["KOOV_TTS_PHRASE_PAUSE_SCALE"], "1.0")
            self.assertEqual(os.environ["KOOV_TTS_PHRASE_MAX_CHARS"], "280")
            self.assertEqual(os.environ["KOOV_WAKE_SPOKEN_MAX_WORDS"], "28")
            self.assertEqual(os.environ["KOOV_WAKE_SPOKEN_MAX_CHARS"], "180")
            self.assertEqual(os.environ["KOOV_WAKE_TTS_CONTINUATION"], "1")
            self.assertEqual(os.environ["KOOV_TTS_FADE_IN_MS"], "0")
            self.assertEqual(os.environ["KOOV_MIN_SILENCE_BEFORE_SPEAKING_INSTANT_S"], "0")
            self.assertEqual(os.environ["KOOV_POST_TTS_TRANSCRIPT_WAKE_MUTE_S"], "0.8")


if __name__ == "__main__":
    unittest.main()
