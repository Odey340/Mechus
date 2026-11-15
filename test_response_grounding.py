"""Tests for production response grounding: retrieval + prompt packing."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from koov.intent import INTENT_GENERAL, INTENT_RECAP, classify_intent
from koov.responder import (
    HybridResponder,
    OpenAIResponder,
    ResponseInput,
    RuleResponder,
    pack_grounding_prompt_blocks,
)
from koov.retrieval import retrieve_for_question


class TestPackGroundingPromptBlocks(unittest.TestCase):
    def test_canonical_state_preserved_first(self) -> None:
        state = {
            "decisions": [{"id": "d1", "value": "Ship in June"}],
            "action_items": [{"owner": "Alice", "task": "Write doc", "deadline": "Friday"}],
            "open_issues": [],
        }
        snip = "x" * 5000
        packed, stats = pack_grounding_prompt_blocks(
            state=state,
            snippets_text=snip,
            memory_aux="User asked about lunch",
            budget_chars=2000,
        )
        self.assertIn("CANONICAL_STATE", packed)
        self.assertIn("Alice", packed)
        self.assertTrue(stats.get("truncated_snippets") or stats.get("dropped_memory"))

    def test_empty_state_message(self) -> None:
        packed, _ = pack_grounding_prompt_blocks(
            state={},
            snippets_text="Speaker A: hello",
            memory_aux="",
            budget_chars=8000,
        )
        self.assertIn("CANONICAL_STATE", packed)
        self.assertIn("MEETING_SNIPPETS", packed)


class TestRetrievalGeneralPath(unittest.TestCase):
    def test_general_still_loads_state_from_engine(self) -> None:
        engine = MagicMock()
        engine.get_canonical_state_for_qa.return_value = {
            "decisions": [],
            "action_items": [{"owner": "Bob", "task": "Call legal", "deadline": None}],
            "open_issues": [],
        }
        engine.get_relevant_chunks.return_value = ("Tagged chunk text about nothing.", [1], [])

        mem = MagicMock()
        mem.retrieve.return_value = ""

        bundle = retrieve_for_question(
            "What is the capital of France?",
            context_engine=engine,
            memory=mem,
            max_chars=4000,
            general_max_chars=2000,
        )
        self.assertEqual(bundle.intent, INTENT_GENERAL)
        self.assertTrue(bundle.meta.get("used_light_path"))
        self.assertEqual(len(bundle.state.get("action_items", [])), 1)
        engine.get_relevant_chunks.assert_called_once()


class TestIntentRecapPrecedence(unittest.TestCase):
    def test_recap_wins_over_decided_substring(self) -> None:
        self.assertEqual(
            classify_intent("recap what we decided today"),
            INTENT_RECAP,
        )


class TestOpenAIGreetingUsesUtteranceIntent(unittest.TestCase):
    def test_greeting_short_circuit_ignores_wrong_bundle_intent(self) -> None:
        oa = OpenAIResponder(api_key="sk-test", max_response_words=1)
        inp = ResponseInput(
            wake_phrase="koov",
            wake_phrases=("koov",),
            request_text="Wait sorry, Koov, are you there?",
            intent=INTENT_GENERAL,
        )
        out = oa.respond(inp)
        # Greeting short-circuit returns canned lines (no OpenAI); wording may vary.
        self.assertTrue(
            any(x in out.lower() for x in ("what", "yeah", "listening", "here", "go ahead")),
            msg=out,
        )
        self.assertGreater(len(out.split()), 1)


class TestHybridPlaceholderFallback(unittest.TestCase):
    def test_hybrid_uses_rules_when_primary_returns_placeholder(self) -> None:
        class FakePrimary:
            def respond(self, inp: ResponseInput) -> str:
                return "I don't have enough information yet."

        rules = RuleResponder(max_response_words=80)
        hybrid = HybridResponder(primary=FakePrimary(), fallback=rules)
        inp = ResponseInput(
            wake_phrase="koov",
            wake_phrases=("koov",),
            request_text="recap",
            context_text="Alex: We chose Tuesday.",
            memory_text="",
            intent=INTENT_RECAP,
        )
        out = hybrid.respond(inp)
        self.assertNotEqual(out.strip(), "I don't have enough information yet.")
        self.assertIn("Tuesday", out)


if __name__ == "__main__":
    unittest.main()
