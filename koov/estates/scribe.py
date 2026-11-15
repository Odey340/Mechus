"""ElevenLabs Scribe (batch speech-to-text) for walkthrough narration and voice notes.

POST {base}/v1/speech-to-text  (multipart: file, model_id, diarize, timestamps_granularity)
Response: {text, language_code, words: [{text, start, end, type, speaker_id}]}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def configured() -> bool:
    return bool((os.getenv("KOOV_ELEVEN_API_KEY") or "").strip())


def _base_url() -> str:
    base = (os.getenv("KOOV_ELEVEN_BASE_URL") or "https://api.elevenlabs.io").strip().rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----koov{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8")
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    out += data + b"\r\n" + f"--{boundary}--\r\n".encode("ascii")
    return bytes(out), boundary


def transcribe(path: Path, *, language: str = "", diarize: bool = True, timeout_s: float = 300.0) -> dict[str, Any]:
    key = (os.getenv("KOOV_ELEVEN_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("KOOV_ELEVEN_API_KEY is not set")
    model = (os.getenv("KOOV_ELEVEN_BATCH_STT_MODEL") or "scribe_v2").strip()
    fields = {
        "model_id": model,
        "diarize": "true" if diarize else "false",
        "timestamps_granularity": "word",
        "tag_audio_events": "false",
    }
    if language:
        fields["language_code"] = language
    body, boundary = _multipart(fields, "file", path.name, path.read_bytes(), "audio/wav")
    req = urllib.request.Request(
        f"{_base_url()}/v1/speech-to-text",
        data=body,
        method="POST",
        headers={"xi-api-key": key, "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs STT HTTP {exc.code}: {detail[:300]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"ElevenLabs STT unreachable: {type(exc).__name__}") from None
    return parse_response(raw)


def parse_response(raw: dict[str, Any], *, pause_s: float = 0.9) -> dict[str, Any]:
    """Group word timestamps into speaker-attributed segments."""
    words = [w for w in (raw.get("words") or []) if isinstance(w, dict)]
    segments: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for w in words:
        if w.get("type") not in (None, "word", "spacing"):
            continue
        text = str(w.get("text") or "")
        start = w.get("start")
        end = w.get("end")
        speaker = str(w.get("speaker_id") or w.get("speaker") or "")
        if w.get("type") == "spacing":
            if cur is not None:
                cur["text"] += text
            continue
        new_seg = (
            cur is None
            or speaker != cur["speaker"]
            or (isinstance(start, (int, float)) and start - cur["end"] > pause_s)
            or cur["text"].rstrip().endswith((".", "?", "!")) and len(cur["text"]) > 60
        )
        if new_seg:
            if cur is not None:
                cur["text"] = " ".join(cur["text"].split())
                segments.append(cur)
            cur = {"start": float(start or 0.0), "end": float(end or start or 0.0), "speaker": speaker, "text": text}
        else:
            cur["text"] += text if text.startswith(" ") else (" " + text if not cur["text"].endswith(" ") else text)
            cur["end"] = float(end or cur["end"])
    if cur is not None:
        cur["text"] = " ".join(cur["text"].split())
        segments.append(cur)
    speakers = sorted({s["speaker"] for s in segments if s["speaker"]})
    labels = {sp: f"Speaker {i + 1}" for i, sp in enumerate(speakers)}
    for s in segments:
        s["speaker_label"] = labels.get(s["speaker"], "")
    return {
        "text": str(raw.get("text") or " ".join(s["text"] for s in segments)).strip(),
        "language": str(raw.get("language_code") or ""),
        "segments": [s for s in segments if s["text"]],
    }


def narration_near(transcript: dict[str, Any] | None, media_id: str, t: float | None, window_s: float = 6.0) -> str:
    """Narration spoken around time t of a given video (for room/defect hints)."""
    if not transcript or t is None:
        return ""
    parts = []
    for seg in (transcript.get("by_media") or {}).get(media_id, []):
        if seg["end"] >= t - window_s and seg["start"] <= t + window_s / 2:
            parts.append(seg["text"])
    return " ".join(parts)[:400]
