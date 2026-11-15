"""
Wake phrase + command: ensures e.g. "mechus" then a request is accepted.

Production sets MECHUS_WAKE_PHRASE=mechus (see docker-compose.zoom.yml).
"""

from __future__ import annotations

import pytest

from mechus.command_router import route
from mechus.intent_classifier import classify_pipeline_intent
from mechus.wake_detector import (
    normalize_for_wake,
    qualifies_as_wake_command,
    remainder_qualifies_as_wake_command,
    strip_wake_phrase,
)


# Production wake list (see mechus/facetime_env.PRODUCTION_WAKE_PHRASE)
_WAKE_PHRASES_CLOUD = ("hey mechus", "mechus")


@pytest.mark.parametrize(
    "raw,expect_qualifies",
    [
        ("Mechus, recap the meeting", True),
        ("mechus what did we decide", True),
        ("Mechus can you hear me", True),
        (
            "so anyway today i went to the mall and mechus can you hear me",
            True,
        ),
        ("mechus summarize the action items", True),
        ("meckus recap the meeting", True),  # 1-edit STT mishear
        ("Mechus", False),  # wake-only: no command after wake
        ("hey mechus", False),
        ("Hey Mechus", False),
    ],
)
def test_qualifies_as_wake_command_with_mechus_phrase_list(raw: str, expect_qualifies: bool) -> None:
    n = normalize_for_wake(raw)
    got = qualifies_as_wake_command(n, _WAKE_PHRASES_CLOUD)
    assert got is expect_qualifies, (raw, n, got)


def test_require_wake_at_start_rejects_mid_utterance_wake() -> None:
    n = normalize_for_wake(
        "so anyway today i went to the mall and mechus can you hear me"
    )
    assert qualifies_as_wake_command(n, _WAKE_PHRASES_CLOUD) is True
    assert (
        qualifies_as_wake_command(
            n, _WAKE_PHRASES_CLOUD, require_wake_at_start=True
        )
        is False
    )
    assert (
        qualifies_as_wake_command(
            n, _WAKE_PHRASES_CLOUD, max_leading_words_before_wake=2
        )
        is False
    )
    n2 = normalize_for_wake("mechus can you hear me")
    assert qualifies_as_wake_command(
        n2, _WAKE_PHRASES_CLOUD, require_wake_at_start=True
    )
    n3 = normalize_for_wake("okay mechus can you hear me")
    assert qualifies_as_wake_command(
        n3, _WAKE_PHRASES_CLOUD, max_leading_words_before_wake=2
    )


def test_strip_wake_last_uses_final_wake_in_long_line() -> None:
    from mechus.wake_detector import matched_wake_phrase_last, strip_wake_phrase_last

    n = normalize_for_wake(
        "girl i will die okay mechus what would happen if i stood on jupiter"
    )
    assert matched_wake_phrase_last(n, _WAKE_PHRASES_CLOUD) == "mechus"
    assert strip_wake_phrase_last(n, _WAKE_PHRASES_CLOUD) == (
        "what would happen if i stood on jupiter"
    )


def test_strip_mechus_leaves_recap_command() -> None:
    n = normalize_for_wake("Mechus recap the last five minutes")
    cmd = strip_wake_phrase(n, _WAKE_PHRASES_CLOUD)
    assert "recap" in cmd
    assert "mechus" not in cmd.split()


def test_remainder_after_mechus_is_valid_command() -> None:
    n = normalize_for_wake("mechus tell me about the budget")
    rem = strip_wake_phrase(n, _WAKE_PHRASES_CLOUD)
    assert remainder_qualifies_as_wake_command(rem)


def test_pipeline_intent_after_strip_mechus_recap() -> None:
    n = normalize_for_wake("Mechus, give me a recap of the meeting")
    cmd = strip_wake_phrase(n, _WAKE_PHRASES_CLOUD)
    intent = classify_pipeline_intent(cmd)
    assert intent == "recap_query"


def test_route_recap_goes_to_responder_when_fn_provided() -> None:
    n = normalize_for_wake("mechus what were the action items")
    cmd = strip_wake_phrase(n, _WAKE_PHRASES_CLOUD)
    intent = classify_pipeline_intent(cmd)
    called: list[str] = []

    def _fn(c: str) -> str:
        called.append(c)
        return "ok"

    r = route(intent, cmd, responder_fn=_fn)
    assert called == [cmd]
    assert r.response == "ok"
