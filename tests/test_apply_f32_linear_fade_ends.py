"""Linear fade helper for thinking preroll."""

from __future__ import annotations

import numpy as np

from koov.audio import apply_f32_linear_fade_ends


def test_fade_in_and_out_shapes_energy() -> None:
    a = np.ones(4800, dtype=np.float32)  # 100 ms @ 48 kHz
    out = apply_f32_linear_fade_ends(a, 48000, fade_in_ms=10.0, fade_out_ms=10.0)
    assert out.shape == a.shape
    assert out[0] < 0.05
    assert out[-1] < 0.05
    mid = out[len(out) // 2]
    assert mid > 0.95


def test_short_clip_avoids_overlapping_fades() -> None:
    a = np.ones(120, dtype=np.float32)
    out = apply_f32_linear_fade_ends(a, 48000, fade_in_ms=50.0, fade_out_ms=50.0)
    assert np.max(np.abs(out)) <= 1.001
    assert np.isfinite(out).all()
