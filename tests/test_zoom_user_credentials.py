"""Tests for Supabase-backed Zoom credential lookup."""

from __future__ import annotations

from unittest.mock import patch

from koov.zoom_user_credentials import ZoomUserCredentials, fetch_zoom_credentials_for_user


def test_fetch_zoom_credentials_skips_default_user() -> None:
    assert fetch_zoom_credentials_for_user("default") is None
    assert fetch_zoom_credentials_for_user("") is None


def test_fetch_zoom_credentials_from_app_metadata() -> None:
    fake_user = {
        "user_metadata": {"zoom_connected": True},
        "app_metadata": {
            "zoom_access_token": "access-abc",
            "zoom_refresh_token": "refresh-xyz",
            "zoom_expires_at": 9_999_999_999_999,
            "zak_token": "zak-123",
            "zak_fetched_at": 1_700_000_000_000,
        },
    }

    with patch("koov.zoom_user_credentials._supabase_admin_config", return_value=("https://x.supabase.co", "key")):
        with patch("koov.zoom_user_credentials._supabase_get_user", return_value=fake_user):
            creds = fetch_zoom_credentials_for_user("user-uuid")

    assert isinstance(creds, ZoomUserCredentials)
    assert creds.access_token == "access-abc"
    assert creds.zak_token == "zak-123"


def test_fetch_zoom_credentials_rejects_encrypted_user_metadata_tokens() -> None:
    fake_user = {
        "user_metadata": {
            "zoom_connected": True,
            "zoom_access_token": "eyJzdiI6IjAwMDAwMiIsImFsZyI6Ik",
        },
        "app_metadata": {},
    }

    with patch("koov.zoom_user_credentials._supabase_admin_config", return_value=("https://x.supabase.co", "key")):
        with patch("koov.zoom_user_credentials._supabase_get_user", return_value=fake_user):
            creds = fetch_zoom_credentials_for_user("user-uuid")

    assert creds is None
