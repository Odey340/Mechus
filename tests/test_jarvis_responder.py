"""Jarvis-style realtime + web lookup through OpenAIResponder."""

from __future__ import annotations

import json
from unittest.mock import patch

from koov.personas import Persona
from koov.responder import OpenAIResponder, ResponseInput


def _default_persona() -> Persona:
    return Persona(
        id="default",
        name="Koov",
        wake_phrases=("koov",),
        voice_id="v",
        model_id=None,
        system_prompt="",
        audio_tags=False,
        max_words=55,
        temperature=None,
        tools=("realtime",),
        refusal_rules=(),
        filler_phrases=(),
        is_default=True,
    )


def test_weather_returns_directly_without_llm() -> None:
    captured: dict = {}

    class _FakeResp:
        status = 200
        data = b'{"choices":[{"message":{"content":"Should not reach LLM for weather."}}]}'

    def _fake_http_request(method, url, *, body=None, headers=None, timeout_s=None, **kwargs):
        captured["body"] = json.loads(body.decode("utf-8"))
        return _FakeResp()

    responder = OpenAIResponder(api_key="test-key", model="gpt-4o-mini")
    inp = ResponseInput(
        wake_phrase="koov",
        request_text="what's the weather in Solon Ohio",
        wake_phrases=("koov",),
        context_text="",
        memory_text="",
        intent="",
        state={},
        retrieval_meta={},
        persona=_default_persona(),
    )
    with (
        patch("koov.realtime.needs_realtime", return_value=True),
        patch("koov.realtime.needs_web_lookup", return_value=False),
        patch(
            "koov.realtime.fetch_realtime",
            return_value="CURRENT_WEATHER: Currently in Solon: 53°F and clear.",
        ),
        patch("koov.http_pool.http_request", side_effect=_fake_http_request),
    ):
        text = responder.respond(inp)
    assert "53" in text
    assert "body" not in captured


def test_web_lookup_injects_into_llm_prompt() -> None:
    captured: dict = {}

    class _FakeResp:
        status = 200
        data = b'{"choices":[{"message":{"content":"Try the ASUS PG27AQDM."}}]}'

    def _fake_http_request(method, url, *, body=None, headers=None, timeout_s=None, **kwargs):
        captured["body"] = json.loads(body.decode("utf-8"))
        return _FakeResp()

    responder = OpenAIResponder(api_key="test-key", model="gpt-4o-mini")
    inp = ResponseInput(
        wake_phrase="koov",
        request_text="find me a good 4k monitor",
        wake_phrases=("koov",),
        context_text="",
        memory_text="",
        intent="",
        state={},
        retrieval_meta={},
        persona=_default_persona(),
    )
    with (
        patch("koov.realtime.needs_web_lookup", return_value=True),
        patch("koov.realtime.needs_realtime", return_value=False),
        patch(
            "koov.realtime.fetch_realtime",
            return_value="ASUS PG27AQDM is a top 240Hz OLED (Source: rtings.com)",
        ),
        patch("koov.http_pool.http_request", side_effect=_fake_http_request),
    ):
        text = responder.respond(inp)
    user_msg = captured["body"]["messages"][1]["content"]
    assert "Web lookup" in user_msg
    assert "ASUS PG27AQDM" in user_msg
    assert text
