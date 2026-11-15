"""KOOV_PERSONAS_DEFAULT_ONLY keeps extra JSON personas out of the registry by default."""

from __future__ import annotations

import pytest

from koov.config import KoovConfig


def test_default_only_loads_only_default_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOOV_PERSONAS_ENABLED", raising=False)
    monkeypatch.delenv("KOOV_PERSONAS_DEFAULT_ONLY", raising=False)
    cfg = KoovConfig.from_env()
    assert cfg.personas_enabled == ("default",)


def test_personas_csv_overrides_default_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOOV_PERSONAS_ENABLED", "default,penny")
    monkeypatch.delenv("KOOV_PERSONAS_DEFAULT_ONLY", raising=False)
    cfg = KoovConfig.from_env()
    assert cfg.personas_enabled == ("default", "penny")


def test_default_only_off_without_csv_still_defaults_to_koov_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOOV_PERSONAS_ENABLED", raising=False)
    monkeypatch.setenv("KOOV_PERSONAS_DEFAULT_ONLY", "0")
    cfg = KoovConfig.from_env()
    assert cfg.personas_enabled == ("default",)
