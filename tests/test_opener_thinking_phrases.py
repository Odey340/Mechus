"""Wake opener thinking phrases must stay short — mid-word chops sound like garbage."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from mechus.agent import MechusAgent
from mechus.text import strip_preroll_echo_from_response


class TestOpenerThinkingPhrases(unittest.TestCase):
    def _agent(self) -> MechusAgent:
        agent = object.__new__(MechusAgent)
        agent._recent_thinking_phrases = []
        return agent

    def test_opener_never_picks_give_me_lines(self) -> None:
        agent = self._agent()
        pool = [
            "Okay, give me just a sec.",
            "Yeah, give me a moment.",
            "Let me check on that real quick.",
            "One sec.",
            "Hang on.",
            "Okay, one sec.",
        ]
        for _ in range(20):
            phrase = agent._pick_thinking_phrase(
                pool,
                request_text="tell me about the solar system",
                for_wake_opener=True,
            )
            self.assertTrue(phrase)
            self.assertNotIn("give me", phrase.lower())
            self.assertLessEqual(len(phrase.split()), 3)

    def test_opener_falls_back_when_category_pool_is_all_long(self) -> None:
        agent = self._agent()
        pool = [
            "Okay, give me just a sec.",
            "Let me check on that real quick.",
            "Hmm, let me think about that for a second.",
        ]
        phrase = agent._pick_thinking_phrase(
            pool,
            request_text="what's your favorite planet",
            for_wake_opener=True,
        )
        self.assertTrue(phrase)
        self.assertNotIn("give me", phrase.lower())
        self.assertLessEqual(len(phrase.split()), 3)

    def test_strip_truncated_okay_give_me_echo(self) -> None:
        out = strip_preroll_echo_from_response(
            "Okay, give me I am. You like pink because it's cute",
            "Okay, give me just a sec.",
        )
        self.assertTrue(out.lower().startswith("you like pink"))


if __name__ == "__main__":
    unittest.main()
