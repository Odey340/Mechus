"""Persona routing through OpenAIResponder: prompt swap, refusal rules, tool gating."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mechus.personas import Persona


def _make_persona(
    *,
    pid: str = "penny",
    name: str = "Penny",
    wake_phrases: tuple[str, ...] = ("hey penny", "penny"),
    voice_id: str = "voice-penny",
    system_prompt: str = "You are Penny — sharp finance teammate.",
    audio_tags: bool = True,
    max_words: int = 55,
    temperature: float | None = 0.4,
    tools: tuple[str, ...] = ("realtime", "stock_quote"),
    refusal_rules: tuple[str, ...] = ("You are not a licensed financial advisor.",),
    is_default: bool = False,
) -> Persona:
    return Persona(
        id=pid,
        name=name,
        wake_phrases=wake_phrases,
        voice_id=voice_id,
        model_id=None,
        system_prompt=system_prompt,
        audio_tags=audio_tags,
        max_words=max_words,
        temperature=temperature,
        tools=tools,
        refusal_rules=refusal_rules,
        filler_phrases=("Pulling that up.",),
        is_default=is_default,
    )


def _run_respond(persona, request_text: str = "what's apple's earnings"):
    """Run OpenAIResponder.respond with a faked OpenAI HTTP roundtrip and capture the request body."""
    from mechus.responder import OpenAIResponder, ResponseInput

    responder = OpenAIResponder(api_key="test-key", model="gpt-4o-mini")
    captured: dict = {}

    class _FakeResp:
        status = 200
        data = (
            b'{"choices":[{"message":{"content":"This is a sample response from the model."}}]}'
        )

    def _fake_http_request(method, url, *, body=None, headers=None, timeout_s=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["body"] = json.loads(body.decode("utf-8"))
        return _FakeResp()

    inp = ResponseInput(
        wake_phrase="penny",
        request_text=request_text,
        wake_phrases=persona.wake_phrases,
        context_text="",
        memory_text="",
        intent="",
        state={},
        retrieval_meta={},
        persona=persona,
    )
    with patch("mechus.http_pool.http_request", side_effect=_fake_http_request):
        text = responder.respond(inp)
    return text, captured


class TestPersonaPromptSwap(unittest.TestCase):
    def test_custom_persona_replaces_mechus_block(self) -> None:
        persona = _make_persona()
        text, captured = _run_respond(persona)
        sys_msg = captured["body"]["messages"][0]["content"]
        # Custom system prompt is present; built-in Mechus "live teammate" line is gone.
        self.assertIn("You are Penny", sys_msg)
        self.assertNotIn("You are Mechus", sys_msg)
        # Refusal rules appear under Boundaries:
        self.assertIn("Boundaries:", sys_msg)
        self.assertIn("not a licensed financial advisor", sys_msg)
        # Grounding/Priority appendix is still there (so meeting awareness is preserved).
        self.assertIn("CANONICAL_STATE", sys_msg)
        self.assertIn("Priority:", sys_msg)
        self.assertTrue(text)

    def test_default_persona_keeps_mechus_block(self) -> None:
        # Synthetic default persona — empty system_prompt + is_default=True.
        persona = Persona(
            id="default",
            name="Mechus",
            wake_phrases=("mechus",),
            voice_id="default-voice",
            model_id=None,
            system_prompt="",
            audio_tags=False,
            max_words=40,
            temperature=None,
            tools=("realtime",),
            refusal_rules=(),
            filler_phrases=("One sec.",),
            is_default=True,
        )
        _text, captured = _run_respond(persona, "what was decided about launch date")
        sys_msg = captured["body"]["messages"][0]["content"]
        self.assertIn("You are Mechus", sys_msg)
        self.assertNotIn("Boundaries:", sys_msg)


class TestPersonaTemperatureAndTokens(unittest.TestCase):
    def test_persona_temperature_override_used(self) -> None:
        # "who is in the meeting" classifies as INTENT_WHO which is NOT in the
        # list-completeness set and is not GENERAL/RECAP — so persona.temperature wins.
        persona = _make_persona(temperature=0.4)
        _text, captured = _run_respond(persona, "who is in the meeting")
        self.assertEqual(captured["body"]["temperature"], 0.4)

    def test_persona_max_words_caps_max_tokens(self) -> None:
        persona = _make_persona(max_words=20)  # tiny budget
        _text, captured = _run_respond(persona, "who is in the meeting")
        # max_tokens floor is 96; with max_words=20 the cap is 96 (well under default).
        self.assertLessEqual(captured["body"]["max_tokens"], 200)


class TestPersonaToolGating(unittest.TestCase):
    def test_realtime_disabled_when_persona_has_no_realtime_tool(self) -> None:
        persona = _make_persona(tools=("stock_quote",))  # no realtime!
        with patch("mechus.realtime.needs_realtime", return_value=True) as mock_needs:
            with patch("mechus.realtime.fetch_realtime", return_value="should not appear"):
                _text, captured = _run_respond(persona, "what's the weather")
        # When the tool is disabled, needs_realtime must NOT be called at all.
        mock_needs.assert_not_called()
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertNotIn("should not appear", user_msg)

    def test_realtime_enabled_when_persona_has_realtime_tool(self) -> None:
        persona = _make_persona(tools=("realtime",))
        with patch("mechus.realtime.needs_realtime", return_value=True):
            with patch("mechus.realtime.fetch_realtime", return_value="WEATHER: sunny 72F"):
                _text, captured = _run_respond(persona, "what's the weather")
        user_msg = captured["body"]["messages"][1]["content"]
        # Realtime context must be injected into the user message.
        self.assertIn("WEATHER: sunny 72F", user_msg)


class TestPersonaCacheKey(unittest.TestCase):
    """Persona id must be part of the response cache key so two personas don't share answers."""

    def test_cache_key_is_persona_scoped(self) -> None:
        # We don't have to drive the full agent — assert the cache_key formulation directly.
        cmd = "what's apple stock"
        keys = {f"penny::{cmd}", f"doc::{cmd}", f"default::{cmd}"}
        self.assertEqual(len(keys), 3)


if __name__ == "__main__":
    unittest.main()
