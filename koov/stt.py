from __future__ import annotations

from dataclasses import dataclass
import logging
import io
import json
import threading
import urllib.request

import numpy as np
import soundfile as sf

from koov.audio import pcm16_to_float32

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None


class Transcriber:
    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        raise NotImplementedError


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, model: str, compute_type: str = "int8", language: str | None = None):
        from faster_whisper import WhisperModel  # type: ignore

        self._language = language
        self._model = WhisperModel(model, device="cpu", compute_type=compute_type)

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        audio = pcm16_to_float32(pcm16)
        audio_duration_s = float(audio.size) / float(sample_rate_hz) if sample_rate_hz > 0 else 0.0
        
        # Optimized audio preprocessing
        if audio.size > 0:
            # Remove DC offset and normalize
            audio = audio - np.mean(audio)
            audio_max = abs(audio).max()
            if audio_max > 0:
                audio = audio / audio_max * 0.95  # Simple peak normalization

        # Short conversational chunks benefit from fast greedy decode and no cross-chunk conditioning.
        # For longer chunks, modest beam search improves stability on names/numbers without the latency
        # hit of larger beam sizes.
        beam_size = 1 if audio_duration_s <= 4.0 else 2
        best_of = 1 if beam_size == 1 else 2
        use_vad_filter = audio_duration_s >= 15.0
        # Language: when None, faster-whisper auto-detects per utterance. We
        # intentionally do NOT pass an English-biased initial_prompt in that
        # case — it tanks accuracy on non-English speech.
        initial_prompt = (
            "This is a live meeting conversation. "
            "Transcribe only spoken words. Preserve names, numbers, acronyms, and proper nouns."
            if (self._language or "").lower() in ("", "en")
            else None
        )
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            vad_filter=use_vad_filter,
            beam_size=beam_size,
            best_of=best_of,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
            word_timestamps=False,
            vad_parameters=dict(min_silence_duration_ms=300),
        )
        parts: list[str] = []
        for seg in segments:
            t = (seg.text or "").strip()
            if t:
                parts.append(t)
        return Transcript(
            text=" ".join(parts).strip(),
            language=getattr(info, "language", None) or self._language,
        )


class VoskTranscriber(Transcriber):
    """
    Fast local STT using Vosk — already loaded for wake detection.
    20-50x faster than Whisper on CPU; latency is typically <50ms for short commands.
    Uses full-vocabulary mode (no grammar) so it recognises any English phrase.
    Best for: Low-latency on-device command transcription.
    """

    def __init__(self, model_path: str, sample_rate_hz: int = 16000):
        from vosk import KaldiRecognizer, Model  # type: ignore

        self._sr = int(sample_rate_hz)
        model = Model(model_path)
        self._recognizer = KaldiRecognizer(model, self._sr)
        self._recognizer.SetWords(False)

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        if sample_rate_hz != self._sr:
            from koov.dsp import pcm16_resample_linear  # avoid circular at module level
            pcm16 = pcm16_resample_linear(pcm16, sample_rate_hz, self._sr)

        self._recognizer.AcceptWaveform(pcm16)
        result = json.loads(self._recognizer.FinalResult())
        self._recognizer.Reset()
        return Transcript(text=(result.get("text") or "").strip())


class OpenAIWhisperAPITranscriber(Transcriber):
    """
    OpenAI Whisper API transcriber - CHEAPEST option at $0.006/minute.
    High accuracy (95-97%), supports 99 languages, but no real-time streaming.
    Best for: Cost-effective batch transcription with high accuracy.
    """
    def __init__(self, api_key: str, model: str = "whisper-1", language: str | None = None):
        self._api_key = api_key
        self._model = model
        self._language = language

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        # Convert PCM16 to WAV format for API
        audio_f32 = pcm16_to_float32(pcm16)
        
        # Write to in-memory WAV file
        wav_buffer = io.BytesIO()
        sf.write(wav_buffer, audio_f32, sample_rate_hz, format="WAV")
        wav_buffer.seek(0)
        
        # Prepare multipart form data
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        form_data = []
        
        # Add model
        form_data.append(f"--{boundary}\r\n".encode())
        form_data.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        form_data.append(f"{self._model}\r\n".encode())
        
        # Add file
        form_data.append(f"--{boundary}\r\n".encode())
        form_data.append(b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n')
        form_data.append(b'Content-Type: audio/wav\r\n\r\n')
        form_data.append(wav_buffer.read())
        form_data.append(f"\r\n--{boundary}--\r\n".encode())
        
        body = b"".join(form_data)
        
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = payload.get("text", "").strip()
            language = payload.get("language", None)
            return Transcript(text=text, language=language)
        except Exception as exc:
            log.warning("OpenAI Whisper API transcription failed: %s", exc)
            return Transcript(text="")


class ElevenLabsTranscriber(Transcriber):
    """
    ElevenLabs Speech-to-Text (Scribe) transcriber.
    Uses the same API key as ElevenLabs TTS so STT and TTS are in one place.
    Scribe v2: accurate, 90+ languages, low latency with pcm_s16le_16.
    Best for: Single vendor for both TTS and STT; fast, accurate transcription.
    """
    _BOUNDARY = b"----KoovElevenLabsSTT"
    _TARGET_SR = 16000

    def __init__(
        self,
        api_key: str,
        model_id: str = "scribe_v2",
        language: str | None = None,
        base_url: str = "https://api.elevenlabs.io",
    ):
        if not api_key:
            raise ValueError("ElevenLabs API key is required for STT")
        self._api_key = api_key
        self._model_id = model_id
        self._language = language
        self._base_url = base_url.rstrip("/")
        # Precompute multipart prefix/suffix to avoid per-request allocation and encoding
        self._prefix = self._build_multipart_prefix()
        self._suffix = b"\r\n--" + self._BOUNDARY + b"--\r\n"
        self._content_type = b"multipart/form-data; boundary=" + self._BOUNDARY

    def _build_multipart_prefix(self) -> bytes:
        b = self._BOUNDARY
        parts = [
            b"--" + b + b"\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n",
            self._model_id.encode(),
            b"\r\n--" + b + b"\r\nContent-Disposition: form-data; name=\"file_format\"\r\n\r\npcm_s16le_16\r\n",
        ]
        if self._language:
            parts.append(
                b"--" + b + b"\r\nContent-Disposition: form-data; name=\"language_code\"\r\n\r\n"
                + self._language.encode() + b"\r\n"
            )
        parts.append(
            b"--" + b + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.pcm\"\r\n"
            b"Content-Type: application/octet-stream\r\n\r\n"
        )
        return b"".join(parts)

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        if sample_rate_hz != self._TARGET_SR:
            from koov.dsp import pcm16_resample_linear
            pcm16 = pcm16_resample_linear(pcm16, src_sr=sample_rate_hz, dst_sr=self._TARGET_SR)

        body = self._prefix + pcm16 + self._suffix
        from koov.http_pool import http_request

        try:
            resp = http_request(
                "POST",
                f"{self._base_url}/v1/speech-to-text",
                body=body,
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": self._content_type.decode(),
                },
                timeout_s=20.0,
            )
            payload = json.loads(resp.data.decode("utf-8"))
        except Exception as exc:
            log.warning("ElevenLabs STT request failed: %s", exc)
            return Transcript(text="")

        if "text" in payload:
            return Transcript(
                text=(payload.get("text") or "").strip(),
                language=payload.get("language_code") or self._language,
            )
        transcripts = payload.get("transcripts") or []
        if not transcripts:
            return Transcript(text="", language=self._language)
        text = " ".join((t.get("text") or "").strip() for t in transcripts).strip()
        lang = transcripts[0].get("language_code") or self._language
        return Transcript(text=text, language=lang)


class ElevenLabsRealtimeTranscriber(Transcriber):
    """Scribe v2 Realtime over a persistent WebSocket (server-side streaming).

    In segment-isolated mode (default for meetings), each local VAD utterance is sent
    and committed as its own WebSocket segment — no continuous feed that races manual
    commits. On empty/timeout, falls back to batch HTTP Scribe.
    """

    _TARGET_SR = 16000

    def __init__(
        self,
        api_key: str,
        model_id: str = "scribe_v2_realtime",
        language: str | None = None,
        base_url: str = "https://api.elevenlabs.io",
    ) -> None:
        import os

        from koov.elevenlabs_realtime_stt import ElevenLabsRealtimeSTTSession

        self._api_key = api_key
        self._model_id = model_id or "scribe_v2_realtime"
        self._base_url = base_url
        self._language = language
        self._segment_isolated = os.getenv("KOOV_ELEVEN_STT_SEGMENT_ISOLATED", "1").lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        self._session = ElevenLabsRealtimeSTTSession(
            api_key,
            model_id=self._model_id,
            base_url=base_url,
            language=language,
        )
        self._session.start()
        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._batch_fallback: ElevenLabsTranscriber | None = None
        self._batch_first = os.getenv("KOOV_ELEVEN_STT_BATCH_FIRST", "0").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self._realtime_fail_streak = 0

    @property
    def streams_continuously(self) -> bool:
        return not self._segment_isolated

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def _batch_transcribe(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if self._batch_fallback is None:
            self._batch_fallback = ElevenLabsTranscriber(
                api_key=self._api_key,
                model_id="scribe_v2",
                language=self._language,
                base_url=self._base_url,
            )
        return self._batch_fallback.transcribe_pcm16(pcm16, sample_rate_hz)

    def feed_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> None:
        """Stream audio into the open realtime session (legacy continuous mode only)."""
        if self._segment_isolated or not pcm16:
            return
        if sample_rate_hz != self._TARGET_SR:
            from koov.dsp import pcm16_resample_linear

            pcm16 = pcm16_resample_linear(pcm16, src_sr=sample_rate_hz, dst_sr=self._TARGET_SR)
        with self._buf_lock:
            self._buf.extend(pcm16)
            if len(self._buf) >= 6400:
                self._flush_buf(commit=False)

    def _flush_buf(self, *, commit: bool) -> None:
        if not self._buf:
            return
        chunk = bytes(self._buf)
        self._buf.clear()
        self._session.send_pcm16(chunk, self._TARGET_SR, commit=commit)

    def commit_segment(
        self,
        pcm16: bytes | None = None,
        sample_rate_hz: int | None = None,
        *,
        timeout_s: float = 2.0,
    ) -> Transcript:
        """Transcribe one VAD utterance and return the finalized transcript."""
        sr = int(sample_rate_hz or self._TARGET_SR)
        if pcm16 and (self._batch_first or self._realtime_fail_streak >= 2):
            return self._batch_transcribe(pcm16, sr)
        if pcm16 and self._segment_isolated:
            text = self._session.commit_pcm_segment(pcm16, sr, timeout_s=timeout_s)
        else:
            with self._buf_lock:
                self._flush_buf(commit=False)
            text = self._session.commit(timeout_s=timeout_s)
        if not (text or "").strip():
            self._realtime_fail_streak += 1
            if pcm16:
                log.info(
                    "ElevenLabs realtime STT empty for %d-byte chunk — batch Scribe fallback",
                    len(pcm16),
                )
                return self._batch_transcribe(pcm16, sr)
        else:
            self._realtime_fail_streak = 0
        lang = self._session.last_committed_language or self._language
        return Transcript(text=(text or "").strip(), language=lang)

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")
        if self._batch_first or self._realtime_fail_streak >= 2:
            return self._batch_transcribe(pcm16, sample_rate_hz)
        text = self._session.commit_pcm_segment(pcm16, sample_rate_hz, timeout_s=2.0)
        if not (text or "").strip():
            self._realtime_fail_streak += 1
            return self._batch_transcribe(pcm16, sample_rate_hz)
        self._realtime_fail_streak = 0
        lang = self._session.last_committed_language or self._language
        return Transcript(text=(text or "").strip(), language=lang)


class DeepgramTranscriber(Transcriber):
    """
    Deepgram transcriber - Cost-effective with real-time streaming support.
    Good accuracy, low latency, supports real-time transcription.
    Best for: Real-time transcription with good cost/performance balance.
    Pricing: Typically $0.0043-$0.0159 per minute depending on model.
    """
    def __init__(self, api_key: str, model: str = "nova-2", language: str | None = None):
        self._api_key = api_key
        self._model = model
        self._language = language

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        # Deepgram accepts raw PCM16 audio via multipart form
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        form_data = []
        
        # Add audio file
        form_data.append(f"--{boundary}\r\n".encode())
        form_data.append(b'Content-Disposition: form-data; name="audio"; filename="audio.pcm"\r\n')
        form_data.append(b'Content-Type: application/octet-stream\r\n\r\n')
        form_data.append(pcm16)
        form_data.append(f"\r\n--{boundary}--\r\n".encode())
        
        body_data = b"".join(form_data)
        
        # Build query parameters
        params = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate_hz),
            "channels": "1",
        }
        if self._language:
            params["language"] = self._language
        
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"https://api.deepgram.com/v1/listen?{query_string}"
        
        try:
            req = urllib.request.Request(
                url,
                data=body_data,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )
            
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            
            # Extract text from Deepgram response
            results = payload.get("results", {})
            channels = results.get("channels", [])
            if channels:
                alternatives = channels[0].get("alternatives", [])
                if alternatives:
                    text = alternatives[0].get("transcript", "").strip()
                    return Transcript(text=text, language=self._language)
            
            return Transcript(text="")
        except Exception as exc:
            log.warning("Deepgram transcription failed: %s", exc)
            return Transcript(text="")


class AssemblyAITranscriber(Transcriber):
    """
    AssemblyAI transcriber - Good balance of features and cost.
    Real-time streaming, speaker diarization, sentiment analysis.
    Best for: Feature-rich transcription with good accuracy.
    Pricing: ~$0.00025/second = ~$0.015/minute
    """
    def __init__(self, api_key: str, language_code: str | None = None):
        self._api_key = api_key
        self._language_code = language_code

    def transcribe_pcm16(self, pcm16: bytes, sample_rate_hz: int) -> Transcript:
        if not pcm16:
            return Transcript(text="")

        # Convert to WAV for AssemblyAI
        audio_f32 = pcm16_to_float32(pcm16)
        wav_buffer = io.BytesIO()
        sf.write(wav_buffer, audio_f32, sample_rate_hz, format="WAV")
        wav_buffer.seek(0)
        
        # Upload audio file
        upload_req = urllib.request.Request(
            "https://api.assemblyai.com/v2/upload",
            data=wav_buffer.read(),
            headers={
                "authorization": self._api_key,
                "content-type": "application/octet-stream",
            },
            method="POST",
        )
        
        try:
            with urllib.request.urlopen(upload_req, timeout=30) as resp:
                upload_response = json.loads(resp.read().decode("utf-8"))
            upload_url = upload_response.get("upload_url")
            
            if not upload_url:
                return Transcript(text="")
            
            # Submit transcription request
            transcript_body = {
                "audio_url": upload_url,
                "language_detection": True,
            }
            if self._language_code:
                transcript_body["language_code"] = self._language_code
            
            transcript_req = urllib.request.Request(
                "https://api.assemblyai.com/v2/transcript",
                data=json.dumps(transcript_body).encode("utf-8"),
                headers={
                    "authorization": self._api_key,
                    "content-type": "application/json",
                },
                method="POST",
            )
            
            with urllib.request.urlopen(transcript_req, timeout=30) as resp:
                transcript_response = json.loads(resp.read().decode("utf-8"))
            transcript_id = transcript_response.get("id")
            
            if not transcript_id:
                return Transcript(text="")
            
            # Poll for completion (simple polling, can be improved)
            import time
            max_attempts = 30
            for _ in range(max_attempts):
                status_req = urllib.request.Request(
                    f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                    headers={"authorization": self._api_key},
                    method="GET",
                )
                
                with urllib.request.urlopen(status_req, timeout=30) as resp:
                    status_response = json.loads(resp.read().decode("utf-8"))
                
                status = status_response.get("status")
                if status == "completed":
                    text = status_response.get("text", "").strip()
                    language = status_response.get("language_code")
                    return Transcript(text=text, language=language)
                elif status == "error":
                    error = status_response.get("error", "Unknown error")
                    log.warning("AssemblyAI transcription error: %s", error)
                    return Transcript(text="")
                
                time.sleep(0.5)
            
            log.warning("AssemblyAI transcription timeout")
            return Transcript(text="")
            
        except Exception as exc:
            log.warning("AssemblyAI transcription failed: %s", exc)
            return Transcript(text="")
