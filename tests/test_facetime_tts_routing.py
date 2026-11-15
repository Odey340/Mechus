"""FaceTime TTS routing must not assume KoovConfig.join_backend (env-only)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from koov.agent import KoovAgent
from koov.config import KoovConfig


class TestFacetimeTtsRouting(unittest.TestCase):
    def test_reassert_facetime_tts_routing_uses_env_not_config_field(self) -> None:
        agent = object.__new__(KoovAgent)
        agent._is_facetime_web = True
        agent._config = KoovConfig.from_env()
        with patch.dict(os.environ, {"KOOV_JOIN_BACKEND": "facetime_web"}, clear=False):
            with patch("subprocess.run") as mock_run:
                KoovAgent._reassert_facetime_tts_routing(agent)
        self.assertTrue(mock_run.called)

    def test_reassert_skips_when_not_facetime(self) -> None:
        agent = object.__new__(KoovAgent)
        agent._is_facetime_web = False
        agent._config = KoovConfig.from_env()
        with patch.dict(os.environ, {"KOOV_JOIN_BACKEND": "zoom_sdk"}, clear=False):
            with patch("subprocess.run") as mock_run:
                KoovAgent._reassert_facetime_tts_routing(agent)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
