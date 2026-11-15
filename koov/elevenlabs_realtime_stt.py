"""ElevenLabs Scribe v2 Realtime — persistent WebSocket STT session.

Server-side streaming docs:
https://elevenlabs.io/docs/cookbooks/speech-to-text/server-side-streaming

Uses a single long-lived WebSocket so Koov avoids a fresh HTTP POST per VAD
chunk (~150–250 ms TLS handshake each time with batch Scribe).
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Callable
from urllib.parse import urlencode

from koov.brand import ASSISTANT_NAME

log = logging.getLogger(__name__)

_TARGET_SR = 16000
# ~200 ms @ 16 kHz mono s16le — balances latency vs WebSocket message count.
_SEND_CHUNK_BYTES = 6400


class ElevenLabsRealtimeSTTSession:
    """Thread-safe Scribe v2 Realtime WebSocket client."""

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "scribe_v2_realtime",
        base_url: str = "https://api.elevenlabs.io",
        language: str | None = None,
        keyterms: tuple[str, ...] = (ASSISTANT_NAME, ASSISTANT_NAME.lower()),
        on_partial: Callable[[str], None] | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        if not self._api_key:
            raise ValueError("ElevenLabs API key is required for realtime STT")
        self._model_id = (model_id or "scribe_v2_realtime").strip()
        host = base_url.rstrip("/").replace("https://", "").replace("http://", "")
        params: dict[str, str] = {
            "model_id": self._model_id,
            # Timestamps enable committed_transcript_with_timestamps + language_code.
            "include_timestamps": "true",
            "no_verbatim": "true",
        }
        if language:
            params["language_code"] = language
        qs = urlencode(params)
        for term in keyterms:
            if term:
                qs += f"&keyterms={term}"
        self._ws_url = f"wss://{host}/v1/speech-to-text/realtime?{qs}"

        self._on_partial = on_partial
        self._send_lock = threading.Lock()
        self._commit_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._commit_event = threading.Event()
        self._last_committed = ""
        self._last_committed_language: str | None = None
        self._last_error = ""
        self._ws_app = None
        self._thread: threading.Thread | None = None

    def start(self, *, timeout_s: float = 12.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        import websocket  # type: ignore

        def _on_open(ws) -> None:  # noqa: ANN001
            log.info("ElevenLabs realtime STT connected (model=%s)", self._model_id)

        def _on_message(_ws, message: str) -> None:  # noqa: ANN001
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            mt = str(data.get("message_type") or "")
            if mt == "session_started":
                self._ready.set()
            elif mt == "partial_transcript":
                text = str(data.get("text") or "").strip()
                if text and self._on_partial:
                    try:
                        self._on_partial(text)
                    except Exception:  # noqa: BLE001
                        pass
            elif mt in ("committed_transcript", "committed_transcript_with_timestamps"):
                self._last_committed = str(data.get("text") or "").strip()
                lang = data.get("language_code")
                if lang:
                    self._last_committed_language = str(lang).strip() or None
                self._commit_event.set()
            elif mt == "error":
                self._last_error = str(data.get("error") or data.get("message") or "error")
                log.warning("ElevenLabs realtime STT error: %s", self._last_error)
                self._commit_event.set()

        def _on_error(_ws, error) -> None:  # noqa: ANN001
            self._last_error = str(error)
            log.warning("ElevenLabs realtime STT websocket error: %s", error)
            self._ready.set()
            self._commit_event.set()

        def _on_close(_ws, _status, _msg) -> None:  # noqa: ANN001
            self._closed.set()
            self._ready.set()
            self._commit_event.set()

        self._ws_app = websocket.WebSocketApp(
            self._ws_url,
            header={"xi-api-key": self._api_key},
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        self._thread = threading.Thread(
            target=lambda: self._ws_app.run_forever(ping_interval=20, ping_timeout=10),
            name="ElevenLabsRealtimeSTT",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=max(1.0, timeout_s)):
            raise TimeoutError("ElevenLabs realtime STT session did not start in time")

    def close(self) -> None:
        app = self._ws_app
        if app is not None:
            try:
                app.close()
            except Exception:  # noqa: BLE001
                pass
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)

    def _send_json(self, payload: dict) -> None:
        if self._closed.is_set():
            return
        app = self._ws_app
        if app is None:
            return
        raw = json.dumps(payload)
        with self._send_lock:
            try:
                app.send(raw)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                log.warning("ElevenLabs realtime STT send failed: %s", exc)

    def send_pcm16(self, pcm16: bytes, sample_rate_hz: int = _TARGET_SR, *, commit: bool = False) -> None:
        if not pcm16 and not commit:
            return
        b64 = base64.b64encode(pcm16).decode("ascii") if pcm16 else ""
        self._send_json(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": b64,
                "commit": bool(commit),
                "sample_rate": int(sample_rate_hz),
            }
        )

    @property
    def last_committed_language(self) -> str | None:
        return self._last_committed_language

    def _wait_for_commit(self, *, timeout_s: float) -> str:
        if not self._commit_event.wait(timeout=max(0.5, timeout_s)):
            log.warning("ElevenLabs realtime STT commit timed out")
            return ""
        return self._last_committed

    def _restart_session(self) -> None:
        """Reconnect after a stuck commit (best-effort)."""
        log.info("ElevenLabs realtime STT: reconnecting after commit failure")
        old_app = self._ws_app
        old_thread = self._thread
        self._ws_app = None
        self._thread = None
        self._closed.clear()
        self._ready.clear()
        self._commit_event.clear()
        if old_app is not None:
            try:
                old_app.close()
            except Exception:  # noqa: BLE001
                pass
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
        try:
            self.start(timeout_s=10.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("ElevenLabs realtime STT reconnect failed: %s", exc)

    def commit_pcm_segment(
        self,
        pcm16: bytes,
        sample_rate_hz: int = _TARGET_SR,
        *,
        timeout_s: float = 2.0,
    ) -> str:
        """Send one VAD utterance and commit it (isolated segment, no prior stream buffer)."""
        with self._commit_lock:
            self._commit_event.clear()
            self._last_committed = ""
            self._last_committed_language = None
            if pcm16 and sample_rate_hz != _TARGET_SR:
                from koov.dsp import pcm16_resample_linear

                pcm16 = pcm16_resample_linear(pcm16, src_sr=sample_rate_hz, dst_sr=_TARGET_SR)
                sample_rate_hz = _TARGET_SR
            if pcm16:
                step = _SEND_CHUNK_BYTES
                for off in range(0, len(pcm16), step):
                    chunk = pcm16[off : off + step]
                    is_last = (off + step) >= len(pcm16)
                    self.send_pcm16(chunk, sample_rate_hz, commit=is_last)
            else:
                self.send_pcm16(b"", sample_rate_hz, commit=True)
            text = self._wait_for_commit(timeout_s=timeout_s)
            if text:
                return text
            # Fallback: explicit commit message (older manual-commit path).
            self._commit_event.clear()
            self._last_committed = ""
            self._send_json({"message_type": "commit"})
            text = self._wait_for_commit(timeout_s=min(4.0, timeout_s))
            if not text and not self._closed.is_set():
                self._restart_session()
            return text

    def commit(self, *, timeout_s: float = 2.0) -> str:
        """Finalize the current streamed segment and wait for committed text."""
        with self._commit_lock:
            self._commit_event.clear()
            self._last_committed = ""
            self._last_committed_language = None
            self._send_json({"message_type": "commit"})
            text = self._wait_for_commit(timeout_s=timeout_s)
            if not text and not self._closed.is_set():
                self._restart_session()
            return text
