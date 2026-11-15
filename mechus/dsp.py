from __future__ import annotations

import numpy as np


def resample_f32_linear(audio_f32: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    audio_f32 = np.asarray(audio_f32, dtype=np.float32)
    if audio_f32.size == 0:
        return audio_f32
    if src_sr == dst_sr:
        return audio_f32
    if src_sr <= 0 or dst_sr <= 0:
        return audio_f32
    if audio_f32.size == 1:
        return np.repeat(audio_f32, 1).astype(np.float32)

    ratio = float(dst_sr) / float(src_sr)
    dst_n = max(1, int(round(audio_f32.size * ratio)))

    x_old = np.linspace(0.0, 1.0, num=audio_f32.size, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, num=dst_n, dtype=np.float32)
    out = np.interp(x_new, x_old, audio_f32).astype(np.float32)
    return out


def pcm16_resample_linear(pcm16: bytes, src_sr: int, dst_sr: int) -> bytes:
    if not pcm16:
        return b""
    if src_sr == dst_sr:
        return pcm16
    audio_i16 = np.frombuffer(pcm16, dtype=np.int16)
    audio_f32 = audio_i16.astype(np.float32) / 32768.0
    out_f32 = resample_f32_linear(audio_f32, src_sr=src_sr, dst_sr=dst_sr)
    out_i16 = np.clip(out_f32, -1.0, 1.0)
    out_i16 = (out_i16 * 32767.0).astype(np.int16)
    return out_i16.tobytes()
