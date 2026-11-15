"""Help intent must not fire when the utterance contains a real question."""

from koov.intent import INTENT_GENERAL, INTENT_HELP, classify_intent


def test_help_not_on_substantive_barge_in_line() -> None:
    t = (
        "i didnt even finish my question what are you wanted for "
        "what would happen if i"
    )
    assert classify_intent(t) == INTENT_GENERAL


def test_help_still_on_capabilities_only() -> None:
    assert classify_intent("what can you do") == INTENT_HELP
