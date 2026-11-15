"""Tests for inferring join_backend from meeting_link (FaceTime vs Zoom)."""

from __future__ import annotations

import pytest

from koov.api_server import infer_join_backend_from_meeting_link


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://facetime.apple.com/join#v=1&k=abc", "facetime_web"),
        ("facetime://join?k=abc", "facetime_web"),
        ("https://zoom.us/j/1234567890?pwd=foo", "zoom_sdk"),
        ("https://us02web.zoom.us/j/1234567890", "zoom_sdk"),
        ("https://meet.google.com/abc-defg-hij", None),
    ],
)
def test_infer_join_backend_from_meeting_link(url: str, expected: str | None) -> None:
    assert infer_join_backend_from_meeting_link(url) == expected
