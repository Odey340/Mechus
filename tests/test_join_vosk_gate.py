"""Join API should not require Vosk for transcript-only meeting backends."""

from __future__ import annotations

import pytest


def _join_requires_vosk_wake_model(join_backend: str) -> bool:
    """Mirror koov.api_server.join_requires_vosk_wake_model (avoid Flask import in unit tests)."""
    return (join_backend or "").strip().lower() in ("browser", "external")


@pytest.mark.parametrize(
    "backend,expected",
    [
        ("browser", True),
        ("external", True),
        ("zoom_sdk", False),
        ("facetime_web", False),
    ],
)
def test_join_requires_vosk_wake_model(backend: str, expected: bool) -> None:
    assert _join_requires_vosk_wake_model(backend) is expected
