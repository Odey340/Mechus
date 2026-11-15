"""Edge silence trimming for TTS handoffs."""

from __future__ import annotations

import numpy as np

from mechus.audio import trim_f32_silence_edges


def test_trim_removes_leading_and_trailing_zeros() -> None:
    sr = 16000
    mid = np.sin(np.linspace(0, 1.0, 800)).astype(np.float32) * 0.2
    pad = 2400  # 150 ms each side
    a = np.concatenate([np.zeros(pad, dtype=np.float32), mid, np.zeros(pad, dtype=np.float32)])
    out = trim_f32_silence_edges(a, sr)
    assert out.size < a.size
    assert out.size >= mid.size // 2


def test_trim_noop_when_all_voiced() -> None:
    sr = 48000
    a = np.linspace(-0.05, 0.05, 1200, dtype=np.float32)
    out = trim_f32_silence_edges(a, sr)
    assert out.size == a.size
