"""OpenAI thinking vs response model config."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestOpenAIModelConfig(unittest.TestCase):
    def test_response_and_thinking_models_split(self) -> None:
        env = {
            "MECHUS_OPENAI_THINKING_MODEL": "gpt-4.1-nano",
            "MECHUS_OPENAI_RESPONSE_MODEL": "gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=False):
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertEqual(c.openai_response_model, "gpt-4o-mini")
        self.assertEqual(c.openai_thinking_model, "gpt-4.1-nano")
        self.assertEqual(c.openai_chat_model, "gpt-4o-mini")

    def test_legacy_chat_model_fills_both_when_unset(self) -> None:
        with patch.dict(
            os.environ,
            {"MECHUS_OPENAI_CHAT_MODEL": "gpt-4o-mini"},
            clear=False,
        ):
            for key in ("MECHUS_OPENAI_THINKING_MODEL", "MECHUS_OPENAI_RESPONSE_MODEL"):
                os.environ.pop(key, None)
            from mechus.config import MechusConfig

            c = MechusConfig.from_env()
        self.assertEqual(c.openai_response_model, "gpt-4o-mini")
        self.assertEqual(c.openai_thinking_model, "gpt-4o-mini")

    def test_dual_model_responder_flag(self) -> None:
        from mechus.responder import OpenAIResponder

        dual = OpenAIResponder(
            api_key="sk-test",
            model="gpt-4o-mini",
            thinking_model="gpt-4.1-nano",
        )
        single = OpenAIResponder(
            api_key="sk-test",
            model="gpt-4o-mini",
            thinking_model="gpt-4o-mini",
        )
        self.assertTrue(dual._use_dual_model)
        self.assertFalse(single._use_dual_model)


if __name__ == "__main__":
    unittest.main()
