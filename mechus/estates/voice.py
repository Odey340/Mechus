"""ElevenLabs voice: spoken report summaries (WAV, works as an iMessage voice note)."""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np


def configured() -> bool:
    return bool((os.getenv("MECHUS_ELEVEN_API_KEY") or "").strip() and (os.getenv("MECHUS_ELEVEN_VOICE_ID") or "").strip())


def synthesize_wav(text: str, dest: Path, *, language: str = "en") -> dict[str, float | str]:
    """Render `text` with the configured ElevenLabs voice to a 16-bit mono WAV."""
    from mechus.tts import ElevenLabsTTSEngine

    if not configured():
        raise RuntimeError("Set MECHUS_ELEVEN_API_KEY and MECHUS_ELEVEN_VOICE_ID to record audio summaries")
    model = (os.getenv("MECHUS_ELEVEN_MODEL_ID") or "eleven_multilingual_v2").strip()
    if not language.lower().startswith("en") and "multilingual" not in model and "v3" not in model:
        model = "eleven_multilingual_v2"
    engine = ElevenLabsTTSEngine(
        api_key=os.environ["MECHUS_ELEVEN_API_KEY"].strip(),
        voice_id=os.environ["MECHUS_ELEVEN_VOICE_ID"].strip(),
        model_id=model,
        base_url=os.getenv("MECHUS_ELEVEN_BASE_URL", "https://api.elevenlabs.io"),
        output_format="pcm_16000",
    )
    audio = engine.synthesize(text)
    samples = np.asarray(audio.audio_f32, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError("ElevenLabs returned no audio (check the voice id and model)")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(audio.sample_rate_hz))
        wf.writeframes(pcm.tobytes())
    return {"path": str(dest), "duration_s": round(samples.size / float(audio.sample_rate_hz), 1), "model": model}
