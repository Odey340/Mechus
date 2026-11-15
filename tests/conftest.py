"""Shared pytest hooks and lightweight stubs for optional native deps."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

# webrtcvad imports pkg_resources at import time; Python 3.12+ may not ship setuptools.
if "pkg_resources" not in sys.modules:
    _pkg = ModuleType("pkg_resources")

    def _get_distribution(_name: str):
        dist = MagicMock()
        dist.version = "0.0.0"
        return dist

    _pkg.require = MagicMock()  # type: ignore[attr-defined]
    _pkg.get_distribution = _get_distribution  # type: ignore[attr-defined]
    _pkg.working_set = MagicMock()  # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = _pkg
