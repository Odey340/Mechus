from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

import numpy as np
import soundfile as sf

from mechus.brand import for_tts
from mechus.text import normalize_text

log = logging.getLogger(__name__)


def _elevenlabs_error_indicates_invalid_voice(http_code: int, detail: str) -> bool:
    """HTTP 400 from ElevenLabs often means a bad ``voice_id`` (placeholder env, wrong persona JSON)."""
    if http_code != 400 or not detail:
        return False
    low = detail.lower()
    if "invalid_uid" in low:
        return True
    if "invalid id" in low and "voice" in low:
        return True
    if "invalid voice" in low:
        return True
    try:
        payload = json.loads(detail)
        detail_obj = payload.get("detail")
        if isinstance(detail_obj, dict):
            if str(detail_obj.get("status") or "").lower() == "invalid_uid":
                return True
            msg = str(detail_obj.get("message") or "").lower()
            if "invalid id" in msg and "voice" in msg:
                return True
    except Exception:
        pass
    return False


_ELEVEN_PCM_OUTPUT_FORMATS = frozenset(
    {
        "pcm_8000",
        "pcm_16000",
        "pcm_22050",
        "pcm_24000",
        "pcm_32000",
        "pcm_44100",
        "pcm_48000",
    }
)


def _eleven_api_base_url(base_url: str) -> str:
    """Strip trailing slashes and accidental ``.../v1`` so paths are ``/v1/text-to-speech/...``."""
    u = (base_url or "").strip().rstrip("/")
    if len(u) >= 3 and u.lower().endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u or "https://api.elevenlabs.io"


@dataclass(frozen=True)
class SynthesizedAudio:
    audio_f32: np.ndarray
    sample_rate_hz: int


class TTSEngine:
    def synthesize(self, text: str) -> SynthesizedAudio:
        raise NotImplementedError

    def skip_further_requests(self) -> bool:
        """When True, caller should stop issuing synthesis (e.g. invalid API voice configuration)."""
        return False


def _decode_elevenlabs_body(raw: bytes, pcm_sample_rate: int) -> SynthesizedAudio:
    """
    ElevenLabs ``pcm_*`` responses are usually raw s16le PCM, but some clients
    see WAV-wrapped audio or an odd trailing byte — both break naive ``frombuffer``.
    """
    silent = SynthesizedAudio(audio_f32=np.zeros((0,), dtype=np.float32), sample_rate_hz=pcm_sample_rate)
    if not raw:
        return silent

    # WAV / RIFF container (some gateways or format fallbacks return this)
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        try:
            audio, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
            if isinstance(audio, np.ndarray) and audio.ndim == 2:
                audio = np.mean(audio, axis=1)
            return SynthesizedAudio(
                audio_f32=np.asarray(audio, dtype=np.float32),
                sample_rate_hz=int(sr),
            )
        except Exception as exc:
            log.warning("ElevenLabs WAV decode failed: %s", exc)
            return silent

    pcm = raw
    if len(pcm) % 2 == 1:
        # API occasionally appends one stray byte on raw PCM; drop it silently.
        pcm = pcm[:-1]
    if len(pcm) < 2:
        return silent
    try:
        audio_i16 = np.frombuffer(pcm, dtype=np.int16)
        audio = audio_i16.astype(np.float32) / 32768.0
        return SynthesizedAudio(audio_f32=audio, sample_rate_hz=pcm_sample_rate)
    except Exception as exc:
        log.debug("ElevenLabs raw PCM decode failed (%s); trying soundfile", exc)

    try:
        audio, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
        if isinstance(audio, np.ndarray) and audio.ndim == 2:
            audio = np.mean(audio, axis=1)
        return SynthesizedAudio(
            audio_f32=np.asarray(audio, dtype=np.float32),
            sample_rate_hz=int(sr),
        )
    except Exception as exc:
        log.warning("ElevenLabs TTS decode failed: %s", exc)
        return silent


class PiperTTSEngine(TTSEngine):
    def __init__(self, model_path: str):
        if not os.path.exists(model_path):
            raise FileNotFoundError(model_path)
        self._model_path = model_path
        self._piper_bin = shutil.which("piper")
        if self._piper_bin is None:
            raise RuntimeError("piper binary not found in PATH")

    def synthesize(self, text: str) -> SynthesizedAudio:
        text = for_tts(normalize_text(text))
        if not text:
            return SynthesizedAudio(audio_f32=np.zeros((0,), dtype=np.float32), sample_rate_hz=16000)

        with tempfile.TemporaryDirectory(prefix="mechus_tts_") as td:
            wav_path = os.path.join(td, "out.wav")
            cmd = [
                self._piper_bin,
                "--model",
                self._model_path,
                "--output_file",
                wav_path,
            ]
            proc = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"piper failed: {proc.stderr.decode('utf-8', 'ignore')}")

            audio, sr = sf.read(wav_path, dtype="float32", always_2d=False)
            if isinstance(audio, np.ndarray) and audio.ndim == 2:
                audio = np.mean(audio, axis=1)
            audio = np.asarray(audio, dtype=np.float32)
            return SynthesizedAudio(audio_f32=audio, sample_rate_hz=int(sr))


class ElevenLabsTTSEngine(TTSEngine):
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model_id: str | None = None,
        base_url: str = "https://api.elevenlabs.io",
        output_format: str | None = None,
    ):
        if not api_key:
            raise ValueError("ElevenLabs API key is required")
        if not voice_id:
            raise ValueError("ElevenLabs voice ID is required")
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id or "eleven_v3"
        self._base_url = _eleven_api_base_url(base_url)
        _fmt = (output_format or os.getenv("MECHUS_ELEVEN_OUTPUT_FORMAT") or "pcm_16000").strip()
        self._output_format = _fmt if _fmt in _ELEVEN_PCM_OUTPUT_FORMATS else "pcm_16000"
        # Optional query param (0–4). Omit by default: some accounts/models return HTTP 400
        # when ``optimize_streaming_latency`` is set on the non-streaming REST endpoint.
        _lat = (os.getenv("MECHUS_ELEVEN_TTS_LATENCY_OPT") or "").strip()
        # eleven_v3 rejects optimize_streaming_latency (HTTP 400); skip to avoid a wasted round-trip.
        if "v3" in (self._model_id or "").lower():
            self._latency_query = {}
        else:
            self._latency_query = (
                {"optimize_streaming_latency": _lat}
                if _lat in {"0", "1", "2", "3", "4"}
                else {}
            )
        self._voice_invalid_skip_network = False
        self._voice_invalid_logged = False

    def skip_further_requests(self) -> bool:
        return self._voice_invalid_skip_network

    def _voice_settings(self) -> dict:
        def _tts_float(key: str, default: float) -> float:
            raw = (os.getenv(key) or "").strip()
            if not raw:
                return default
            try:
                return max(0.0, min(1.0, float(raw)))
            except ValueError:
                return default

        speed_raw = (os.getenv("MECHUS_ELEVEN_TTS_SPEED") or "").strip()
        speed: float | None = None
        if speed_raw:
            try:
                candidate = float(speed_raw)
                if 0.7 <= candidate <= 1.2:
                    speed = candidate
            except ValueError:
                pass
        if "v3" in (self._model_id or "").lower():
            settings = {
                "stability": 0.28,
                "similarity_boost": 0.85,
                "use_speaker_boost": True,
            }
        else:
            _mid = (self._model_id or "").lower()
            if "flash" in _mid or "turbo" in _mid:
                settings = {
                    "stability": _tts_float("MECHUS_ELEVEN_TTS_STABILITY", 0.12),
                    "similarity_boost": _tts_float("MECHUS_ELEVEN_TTS_SIMILARITY_BOOST", 0.72),
                    "style": _tts_float("MECHUS_ELEVEN_TTS_STYLE", 0.86),
                    "use_speaker_boost": True,
                }
            else:
                settings = {
                    "stability": _tts_float("MECHUS_ELEVEN_TTS_STABILITY", 0.30),
                    "similarity_boost": _tts_float("MECHUS_ELEVEN_TTS_SIMILARITY_BOOST", 0.72),
                    "style": _tts_float("MECHUS_ELEVEN_TTS_STYLE", 0.62),
                    "use_speaker_boost": True,
                }
        if speed is not None:
            settings["speed"] = speed
        return settings

    def _sample_rate_for_format(self, output_format: str) -> tuple[str, int]:
        try:
            return output_format, int(output_format.split("_", 1)[1])
        except (IndexError, ValueError):
            return "pcm_16000", 16000

    def _synthesize_once(
        self,
        text: str,
        *,
        output_format: str,
        extra_query: dict[str, str] | None = None,
    ) -> SynthesizedAudio:
        output_format, sample_rate = self._sample_rate_for_format(output_format)
        query_params: dict[str, str] = {"output_format": output_format}
        if extra_query:
            query_params.update(extra_query)
        query = urlencode(query_params)
        voice_seg = quote(self._voice_id.strip(), safe="")
        url = f"{self._base_url}/v1/text-to-speech/{voice_seg}?{query}"
        body = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": self._voice_settings(),
        }
        from mechus.http_pool import http_request

        try:
            resp = http_request(
                "POST",
                url,
                body=json.dumps(body).encode("utf-8"),
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout_s=20.0,
            )
            raw = resp.data
            status = resp.status
            if status >= 400:
                detail = ""
                try:
                    detail = raw.decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                if _elevenlabs_error_indicates_invalid_voice(status, detail):
                    self._voice_invalid_skip_network = True
                    if not self._voice_invalid_logged:
                        self._voice_invalid_logged = True
                        log.warning(
                            "ElevenLabs TTS: invalid voice_id %r — skipping further HTTP requests for this engine "
                            "(set MECHUS_ELEVEN_VOICE_ID / persona voice env vars to a real voice id).",
                            self._voice_id,
                        )
                else:
                    log.warning(
                        "ElevenLabs TTS HTTP %s (model=%s format=%s latency=%s): %s",
                        status,
                        self._model_id,
                        output_format,
                        (extra_query or {}).get("optimize_streaming_latency", "-"),
                        detail or "",
                    )
                return SynthesizedAudio(
                    audio_f32=np.zeros((0,), dtype=np.float32),
                    sample_rate_hz=sample_rate,
                )
        except Exception as exc:
            log.warning("ElevenLabs TTS failed: %s", exc)
            return SynthesizedAudio(
                audio_f32=np.zeros((0,), dtype=np.float32),
                sample_rate_hz=sample_rate,
            )

        head = raw.lstrip()[:1]
        if head in (b"{", b"["):
            try:
                payload = json.loads(raw.decode("utf-8"))
                log.warning("ElevenLabs TTS error response: %s", payload)
            except Exception:
                log.warning("ElevenLabs TTS returned non-audio data")
            return SynthesizedAudio(
                audio_f32=np.zeros((0,), dtype=np.float32),
                sample_rate_hz=sample_rate,
            )
        return _decode_elevenlabs_body(raw, sample_rate)

    def synthesize(self, text: str) -> SynthesizedAudio:
        text = for_tts(normalize_text(text))
        if not text:
            return SynthesizedAudio(audio_f32=np.zeros((0,), dtype=np.float32), sample_rate_hz=16000)
        if self._voice_invalid_skip_network:
            return SynthesizedAudio(audio_f32=np.zeros((0,), dtype=np.float32), sample_rate_hz=16000)

        attempts: list[tuple[str, dict[str, str]]] = []
        fmt = self._output_format
        if self._latency_query:
            attempts.append((fmt, dict(self._latency_query)))
        attempts.append((fmt, {}))
        if fmt != "pcm_16000":
            attempts.append(("pcm_16000", {}))

        last: SynthesizedAudio | None = None
        for output_format, extra_query in attempts:
            last = self._synthesize_once(text, output_format=output_format, extra_query=extra_query)
            if np.asarray(last.audio_f32).size > 0:
                if extra_query or output_format != fmt:
                    log.info(
                        "ElevenLabs TTS succeeded on retry (format=%s latency=%s)",
                        output_format,
                        extra_query.get("optimize_streaming_latency", "off"),
                    )
                return last
            if self._voice_invalid_skip_network:
                break

        log.error(
            "ElevenLabs TTS returned no audio for %d chars (model=%s voice=%r); "
            "check API key, voice id, and model compatibility",
            len(text),
            self._model_id,
            self._voice_id,
        )
        return last or SynthesizedAudio(
            audio_f32=np.zeros((0,), dtype=np.float32),
            sample_rate_hz=16000,
        )

    def synthesize_streaming(
        self,
        text: str,
        on_chunk: Callable[[np.ndarray, int], None],
        *,
        first_chunk_ms: float = 80.0,
    ) -> SynthesizedAudio:
        """Stream PCM from ElevenLabs and invoke ``on_chunk`` as audio arrives.

        Returns the full synthesized audio (concatenated chunks) for logging/metrics.
        """
        text = for_tts(normalize_text(text))
        empty = SynthesizedAudio(audio_f32=np.zeros((0,), dtype=np.float32), sample_rate_hz=16000)
        if not text or self._voice_invalid_skip_network:
            return empty

        output_format, sample_rate = self._sample_rate_for_format(self._output_format)
        if first_chunk_ms == 80.0 and self._latency_query:
            first_chunk_ms = 65.0
        min_bytes = max(
            4,
            int(sample_rate * (max(20.0, first_chunk_ms) / 1000.0)) * 2,
        )
        query_params: dict[str, str] = {"output_format": output_format}
        if self._latency_query:
            query_params.update(self._latency_query)
        query = urlencode(query_params)
        voice_seg = quote(self._voice_id.strip(), safe="")
        url = f"{self._base_url}/v1/text-to-speech/{voice_seg}/stream?{query}"
        body = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": self._voice_settings(),
        }
        all_parts: list[np.ndarray] = []
        buf = b""

        def _flush_playable(*, force: bool = False) -> None:
            nonlocal buf
            usable = len(buf) - (len(buf) % 2)
            if usable < 2:
                return
            if not force and usable < min_bytes:
                return
            pcm = buf[:usable]
            buf = buf[usable:]
            audio_i16 = np.frombuffer(pcm, dtype=np.int16)
            part = audio_i16.astype(np.float32) / 32768.0
            if part.size:
                all_parts.append(part)
                on_chunk(part, sample_rate)

        from mechus.http_pool import http_request

        resp = None
        try:
            resp = http_request(
                "POST",
                url,
                body=json.dumps(body).encode("utf-8"),
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/pcm",
                },
                timeout_s=60.0,
                preload_content=False,
            )
            status = resp.status
            if status >= 400:
                detail = ""
                try:
                    detail = resp.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                if _elevenlabs_error_indicates_invalid_voice(status, detail):
                    self._voice_invalid_skip_network = True
                else:
                    log.warning(
                        "ElevenLabs stream HTTP %s (model=%s): %s",
                        status,
                        self._model_id,
                        detail or "",
                    )
                return self.synthesize(text)
            for raw in resp.stream(8192, decode_content=False):
                if not raw:
                    continue
                buf += raw
                _flush_playable()
            _flush_playable(force=True)
            if buf.lstrip()[:1] == b"{" and len(buf) < 4096:
                try:
                    log.warning("ElevenLabs stream error: %s", buf[:400])
                except Exception:
                    pass
                return self.synthesize(text)
        except Exception as exc:
            log.warning("ElevenLabs stream failed: %s — falling back to blocking synth", exc)
            return self.synthesize(text)
        finally:
            if resp is not None:
                try:
                    resp.release_conn()
                except Exception:
                    pass

        if not all_parts:
            return self.synthesize(text)
        full = np.concatenate(all_parts)
        return SynthesizedAudio(audio_f32=full, sample_rate_hz=sample_rate)