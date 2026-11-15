"""Repeat requests should rephrase the prior Mechus line, not replay verbatim."""

from __future__ import annotations

from mechus.dialog_memory import is_repeat_request
from mechus.responder import rephrase_repeat_spoken


def test_is_repeat_request_matches_common_phrases() -> None:
    assert is_repeat_request("can you repeat that")
    assert is_repeat_request("say that again please")
    assert is_repeat_request("what did you say")
    assert is_repeat_request("what did you just say")
    assert is_repeat_request("could you say that again")


def test_rephrase_repeat_spoken_changes_wording() -> None:
    last = (
        "Sahasra has a physics assignment due tomorrow, June 18. "
        "That's the only action item from this meeting."
    )
    out = rephrase_repeat_spoken(last)
    assert out
    assert out != last
    assert "physics" in out.lower()
    assert "june" in out.lower() or "18" in out
