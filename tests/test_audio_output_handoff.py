"""Gapless bridge → reply handoff on PulseAudio AudioOutput."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from koov.audio import AudioOutput


class TestAudioOutputHandoff(unittest.TestCase):
    def test_keep_mic_live_then_handoff_reuses_stream(self) -> None:
        out = AudioOutput("pulse")
        mock_stream = MagicMock()
        mock_stream.start = MagicMock()
        mock_stream.write = MagicMock()
        mock_stream.stop = MagicMock()
        mock_stream.close = MagicMock()

        with (
            patch.object(out, "_resolve_device", return_value="pulse"),
            patch.object(out, "_select_channels", return_value=1),
            patch("koov.audio.sd.OutputStream", return_value=mock_stream) as mock_ctor,
        ):
            bridge = np.ones(160, dtype=np.float32) * 0.2
            out.play_streaming(
                iter([bridge]),
                16000,
                keep_mic_live=True,
            )
            self.assertIs(out._handoff_stream, mock_stream)
            reply = np.ones(320, dtype=np.float32) * 0.5
            out.play_streaming(
                iter([reply]),
                16000,
                handoff_from_bridge=True,
                skip_edge_ramp_in=True,
            )

        self.assertEqual(mock_ctor.call_count, 1)
        self.assertIsNone(out._handoff_stream)
        self.assertEqual(mock_stream.write.call_count, 2)

    def test_handoff_from_bridge_skips_stop_on_fresh_stream(self) -> None:
        out = AudioOutput("pulse")
        with patch.object(out, "stop") as mock_stop:
            with (
                patch.object(out, "_resolve_device", return_value="pulse"),
                patch.object(out, "_select_channels", return_value=1),
                patch("koov.audio.sd.OutputStream") as mock_ctor,
            ):
                inst = MagicMock()
                mock_ctor.return_value = inst
                out.play_streaming(
                    iter([np.zeros(80, dtype=np.float32)]),
                    16000,
                    handoff_from_bridge=True,
                )
        mock_stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
