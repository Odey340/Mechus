"""Natural truncation and smooth segment fades."""

from __future__ import annotations

import unittest

import numpy as np

from mechus.audio import apply_f32_linear_fade_ends, truncate_f32_to_max_duration


class TestTruncateNatural(unittest.TestCase):
    def test_prefers_quiet_tail_over_hard_cut(self) -> None:
        sr = 16000
        speech = np.sin(2 * np.pi * 220 * np.arange(int(0.5 * sr)) / sr).astype(np.float32) * 0.2
        silence = np.zeros(int(0.08 * sr), dtype=np.float32)
        audio = np.concatenate([speech, silence])
        capped = truncate_f32_to_max_duration(audio, sr, 0.55)
        self.assertLess(capped.shape[0], audio.shape[0])
        self.assertGreater(capped.shape[0], int(0.4 * sr))


class TestStreamSegmentFades(unittest.TestCase):
    def test_mid_chunk_has_no_fade_dip(self) -> None:
        sr = 32000
        n = int(0.08 * sr)
        mid = np.ones(n, dtype=np.float32) * 0.25
        out = apply_f32_linear_fade_ends(mid, sr, fade_in_ms=0.0, fade_out_ms=0.0)
        np.testing.assert_allclose(out, mid, rtol=1e-5)


if __name__ == "__main__":
    unittest.main()
