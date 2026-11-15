"""Substantive general questions must not use the 12-word wake brevity cap."""
from mechus.responder import _general_wants_substantive_answer


def test_substantive_who_what_how():
    assert _general_wants_substantive_answer("who is wasi khan daker")
    assert _general_wants_substantive_answer("what's the best way to grow a plant")
    assert _general_wants_substantive_answer("how do I reset my password")
    assert _general_wants_substantive_answer("how far is uranus from neptune")


def test_intent_wants_extended_spoken():
    from mechus.responder import intent_wants_extended_spoken
    from mechus.intent import INTENT_RECAP

    assert intent_wants_extended_spoken(INTENT_RECAP, "recap the meeting")
    assert intent_wants_extended_spoken("general", "how far is uranus from neptune")
    assert not intent_wants_extended_spoken("general", "what's the best")


def test_brief_fragment_not_substantive():
    assert not _general_wants_substantive_answer("right")
    assert not _general_wants_substantive_answer("")


def test_superlatives_are_substantive():
    """Short superlative facts (largest planet, fastest car) need a full sentence."""
    assert _general_wants_substantive_answer("what's the largest planet")
    assert _general_wants_substantive_answer("whats the tallest mountain")
    assert _general_wants_substantive_answer("name the fastest animal in the world")
    assert _general_wants_substantive_answer("which is the smallest country")
