"""Strip duplicated thinking-preroll filler from spoken replies."""

from __future__ import annotations

from mechus.text import strip_preroll_echo_from_response


def test_strip_shared_boundary_word() -> None:
    out = strip_preroll_echo_from_response(
        "Check the planets are aligned.",
        "Let me check.",
    )
    assert out.lower().startswith("the planets")


def test_strip_leading_let_me_check() -> None:
    out = strip_preroll_echo_from_response(
        "Let me check the weather in Mumbai.",
        "",
    )
    assert "weather in Mumbai" in out
    assert "let me check" not in out.lower()


def test_no_op_when_no_overlap() -> None:
    text = "The planets are aligned today."
    assert strip_preroll_echo_from_response(text, "One sec.") == text
