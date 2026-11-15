"""Zoom OAuth join token helpers."""

from __future__ import annotations

from unittest.mock import patch

from koov.zoom_tokens import (
    fetch_local_recording_token,
    fetch_zak_token,
    is_likely_external_zoom_link,
    resolve_zoom_join_tokens,
    resolve_zoom_oauth_access_token,
)


def test_is_likely_external_zoom_link() -> None:
    assert is_likely_external_zoom_link("https://osu.zoom.us/j/12345678901")
    assert is_likely_external_zoom_link("https://company.zoom.us/j/999")
    assert not is_likely_external_zoom_link("https://zoom.us/j/12345678901")
    assert not is_likely_external_zoom_link("https://us06web.zoom.us/j/12345678901")


def test_resolve_zoom_oauth_access_token_from_body() -> None:
    tok = resolve_zoom_oauth_access_token({"zoom_access_token": "abc123"})
    assert tok == "abc123"


def test_resolve_zoom_join_tokens_fetches_missing(monkeypatch) -> None:
  def _fake_rec(mid: int, oauth: str) -> str | None:
      assert mid == 94310946611
      assert oauth == "oauth"
      return "rec-tok"

  def _fake_zak(oauth: str) -> str | None:
      assert oauth == "oauth"
      return "zak-tok"

  monkeypatch.setattr("koov.zoom_tokens.fetch_local_recording_token", _fake_rec)
  monkeypatch.setattr("koov.zoom_tokens.fetch_zak_token", _fake_zak)
  rec, zak, warnings, errors = resolve_zoom_join_tokens(
      meeting_number=94310946611,
      oauth_access_token="oauth",
  )
  assert rec == "rec-tok"
  assert zak == "zak-tok"
  assert not warnings
  assert not errors


def test_resolve_zoom_join_tokens_zak_failure_is_error(monkeypatch) -> None:
  monkeypatch.setattr("koov.zoom_tokens.fetch_local_recording_token", lambda *a, **k: None)
  monkeypatch.setattr("koov.zoom_tokens.fetch_zak_token", lambda *a, **k: None)
  rec, zak, warnings, errors = resolve_zoom_join_tokens(
      meeting_number=123,
      oauth_access_token="oauth",
  )
  assert rec is None
  assert zak is None
  assert errors
  assert any("zak" in e.lower() for e in errors)


def test_fetch_zak_token_parses_response() -> None:
    class _Resp:
        def read(self) -> bytes:
            return b'{"token":"zak-xyz"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        assert fetch_zak_token("oauth") == "zak-xyz"


def test_fetch_recording_token_parses_response() -> None:
    class _Resp:
        def read(self) -> bytes:
            return b'{"token":"rec-xyz"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        assert fetch_local_recording_token(123, "oauth") == "rec-xyz"
