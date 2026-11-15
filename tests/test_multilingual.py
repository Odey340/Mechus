"""Multilingual STT detection, config, and instant replies."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from mechus.facetime_env import apply_multilingual_env_defaults, apply_production_latency_env_defaults
from mechus.language import (
    guess_language_from_text,
    is_english_language,
    normalize_language_code,
    pick_instant_reply,
)


class TestLanguageHelpers(unittest.TestCase):
    def test_normalize_language_code(self) -> None:
        self.assertEqual(normalize_language_code("es-MX"), "es")
        self.assertEqual(normalize_language_code("en_US"), "en")
        self.assertIsNone(normalize_language_code("und"))

    def test_guess_language_from_script(self) -> None:
        self.assertEqual(guess_language_from_text("こんにちは、調子はどう"), "ja")
        self.assertEqual(guess_language_from_text("안녕하세요 반갑습니다"), "ko")
        self.assertEqual(guess_language_from_text("你好，今天怎么样"), "zh")
        self.assertEqual(guess_language_from_text("नमस्ते क्या हाल है"), "hi")
        self.assertIsNone(guess_language_from_text("hi"))

    def test_is_english_language(self) -> None:
        self.assertTrue(is_english_language("en"))
        self.assertTrue(is_english_language(None))
        self.assertFalse(is_english_language("fr"))

    def test_pick_instant_reply_localized(self) -> None:
        reply = pick_instant_reply(
            "presence",
            ("Yeah, I can hear you.",),
            user_language="es",
        )
        self.assertNotEqual(reply, "Yeah, I can hear you.")
        self.assertTrue(
            any(w in reply.lower() for w in ("escucho", "oigo", "estoy")),
            reply,
        )


class TestMultilingualConfig(unittest.TestCase):
    def test_multilingual_default_auto_detect(self) -> None:
        with patch.dict(os.environ, {"MECHUS_MULTILINGUAL": "1"}, clear=False):
            os.environ.pop("MECHUS_WHISPER_LANGUAGE", None)
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertTrue(c.multilingual)
        self.assertIsNone(c.whisper_language)

    def test_multilingual_off_defaults_english(self) -> None:
        with patch.dict(os.environ, {"MECHUS_MULTILINGUAL": "0"}, clear=True):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertFalse(c.multilingual)
        self.assertEqual(c.whisper_language, "en")

    def test_apply_multilingual_env_strips_english_lock(self) -> None:
        with patch.dict(os.environ, {"MECHUS_WHISPER_LANGUAGE": "en"}, clear=True):
            apply_multilingual_env_defaults()
            self.assertNotIn("MECHUS_WHISPER_LANGUAGE", os.environ)

    def test_apply_multilingual_env_keeps_explicit_override(self) -> None:
        with patch.dict(os.environ, {"MECHUS_WHISPER_LANGUAGE": "es"}, clear=True):
            apply_multilingual_env_defaults()
            self.assertEqual(os.environ.get("MECHUS_WHISPER_LANGUAGE"), "es")

    def test_production_latency_enables_multilingual(self) -> None:
        with patch.dict(os.environ, {"MECHUS_WHISPER_LANGUAGE": "en"}, clear=True):
            apply_production_latency_env_defaults()
            self.assertEqual(os.environ.get("MECHUS_MULTILINGUAL"), "1")
            self.assertNotIn("MECHUS_WHISPER_LANGUAGE", os.environ)


if __name__ == "__main__":
    unittest.main()
