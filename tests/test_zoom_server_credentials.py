"""Tests for server-level Zoom credential resolution."""

from __future__ import annotations

from unittest.mock import patch

from koov.zoom_server_credentials import (
    ZoomServerCredentials,
    fetch_server_zoom_credentials,
    resolve_zoom_credentials_for_join,
)


def test_resolve_zoom_credentials_falls_back_to_server() -> None:
    server = ZoomServerCredentials(access_token="srv-access", zak_token="srv-zak", source="server_s2s")
    with patch("koov.zoom_user_credentials.fetch_zoom_credentials_for_user", return_value=None):
        with patch("koov.zoom_server_credentials.fetch_server_zoom_credentials", return_value=server):
            creds = resolve_zoom_credentials_for_join("user-uuid")
    assert creds is not None
    assert creds.access_token == "srv-access"
    assert creds.zak_token == "srv-zak"


def test_fetch_server_zoom_credentials_none_without_config() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert fetch_server_zoom_credentials() is None
