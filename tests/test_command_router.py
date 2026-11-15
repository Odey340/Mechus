"""Command router: instant routes and wake-only behavior."""

from __future__ import annotations

from koov.command_router import route
from koov.intent_classifier import INTENT_PRESENCE_CHECK


def test_wake_only_presence_stays_silent() -> None:
    """Bare wake ('koov' / 'hey koov' alone) must not speak until a question follows."""
    r = route(INTENT_PRESENCE_CHECK, "", responder_fn=None)
    assert r.is_instant is True
    assert r.response.strip() == ""


def test_presence_can_you_hear_me_still_replies() -> None:
    r = route(INTENT_PRESENCE_CHECK, "can you hear me", responder_fn=None)
    assert r.is_instant is True
    low = r.response.lower()
    assert len(r.response.strip()) >= 4
    # Pool includes hear/here/yeah-style replies and short acknowledgements (e.g. "Got you.",
    # "Crystal clear."). Check that the reply matches any of the known pool strings.
    assert (
        "hear" in low
        or "here" in low
        or "yeah" in low
        or "got you" in low
        or "loud and clear" in low
        or "clear" in low
        or "still here" in low
        or "fine" in low
    )
