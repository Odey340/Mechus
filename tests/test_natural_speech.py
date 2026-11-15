"""Natural TTS chunking for conversational pacing."""

from __future__ import annotations

from koov.text import phrases_for_natural_speech


def test_phrases_split_on_sentences_and_commas() -> None:
    text = (
        "The solar system has eight planets. The four inner planets are rocky, "
        "while the outer planets are gas giants."
    )
    parts = phrases_for_natural_speech(text)
    assert len(parts) == 2
    assert parts[0].endswith(".")
    assert parts[1].endswith(".")
    assert "eight planets" in parts[0]
    assert "gas giants" in parts[1]


def test_short_reply_stays_single_chunk() -> None:
    parts = phrases_for_natural_speech("Loud and clear.")
    assert parts == ["Loud and clear."]


def test_humanize_strips_bot_openers_and_contracts() -> None:
    from koov.text import clean_spoken_text, humanize_spoken_text, strip_tts_disfluencies

    assert humanize_spoken_text("Certainly! The launch is on July 25.") == "The launch is on July 25."
    assert clean_spoken_text("We do not have that yet.") == "We don't have that yet."
    assert strip_tts_disfluencies("Yep, I'm here. uh uh") == "Yep, I'm here."
    assert clean_spoken_text("Yep, I'm here. uh uh") == "Yep, I'm here."
