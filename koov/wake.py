from __future__ import annotations

import json
import logging
import re

import numpy as np

from koov.audio import pcm16_to_float32
from koov.wake_detector import (
    detect_wake,
    normalize_for_wake,
    remainder_qualifies_as_wake_command,
    strip_wake_phrase,
)

log = logging.getLogger(__name__)


class WakeDetector:
    # Subclasses set this to the raw transcript that triggered the last wake.
    last_trigger_text: str = ""

    def reset(self) -> None:  # noqa: D401
        """Reset any internal state."""

    def process(self, pcm16: bytes, timestamp_s: float) -> bool:
        raise NotImplementedError


class NoOpWakeDetector(WakeDetector):
    """Placeholder when meeting audio uses transcript-STT wake only (Zoom / FaceTime).

    Avoids loading the Vosk model (~20–60s, hundreds of MB) in worker containers that
  never call ``process()`` for wake detection.
    """

    def reset(self) -> None:
        self.last_trigger_text = ""

    def process(self, pcm16: bytes, timestamp_s: float) -> bool:
        return False


class CooldownGate:
    def __init__(self, cooldown_s: float):
        self._cooldown_s = float(cooldown_s)
        self._last_fire_s = -1e9

    def ready(self, now_s: float) -> bool:
        if (now_s - self._last_fire_s) < self._cooldown_s:
            return False
        return True

    def fired(self, now_s: float) -> None:
        self._last_fire_s = now_s


class FallbackWakeDetector(WakeDetector):
    def __init__(self, primary: WakeDetector, fallback: WakeDetector):
        self._primary = primary
        self._fallback = fallback

    def reset(self) -> None:
        self._primary.reset()
        self._fallback.reset()
        self.last_trigger_text = ""

    @property
    def last_trigger_text(self) -> str:  # type: ignore[override]
        return self._primary.last_trigger_text or self._fallback.last_trigger_text

    @last_trigger_text.setter
    def last_trigger_text(self, value: str) -> None:
        pass  # read-only; delegates to children

    def process(self, pcm16: bytes, timestamp_s: float) -> bool:
        if self._primary.process(pcm16, timestamp_s):
            return True
        return self._fallback.process(pcm16, timestamp_s)


class VoskWakeDetector(WakeDetector):
    def __init__(
        self,
        wake_phrase: str,
        sample_rate_hz: int,
        model_path: str | None,
        cooldown_s: float,
        wake_phrases: tuple[str, ...] | None = None,
    ):
        from vosk import KaldiRecognizer, Model  # type: ignore

        if model_path is None:
            raise ValueError("KOOV_VOSK_MODEL_PATH is required for vosk wake backend")

        self._wake_phrases = wake_phrases if wake_phrases else (wake_phrase.lower().strip(),)
        self._wake_phrase = self._wake_phrases[0]
        self._sr = int(sample_rate_hz)
        self._model = Model(model_path)
        # No grammar: use full vocabulary so we get transcripts like "computer can you hear me"
        # and detect wake phrase + command in text. Grammar mode only matched exact phrases, so
        # "Computer, can you hear me?" never matched and wake rarely fired.
        self._recognizer = KaldiRecognizer(self._model, self._sr)
        self._recognizer.SetWords(True)
        self._gate = CooldownGate(cooldown_s)
        self._recent_hits: list[float] = []
        # Stores the Vosk transcript that triggered the last wake fire.
        # Agent reads this to use as the command source instead of re-recording.
        self.last_trigger_text: str = ""

    def reset(self) -> None:
        self._recognizer.Reset()
        self._recent_hits.clear()
        self.last_trigger_text = ""

    def _hit(self, now_s: float, window_s: float = 1.2, required: int = 1) -> bool:
        self._recent_hits.append(now_s)
        self._recent_hits = [t for t in self._recent_hits if (now_s - t) <= window_s]
        if len(self._recent_hits) >= required:
            self._recent_hits.clear()
            return True
        return False

    @staticmethod
    def _json_text(raw_json: str, key: str) -> str:
        try:
            data = json.loads(raw_json)
            return (data.get(key) or "").lower().strip()
        except Exception as e:
            log.debug("Vosk _json_text parse failed: %s", e)
            return ""

    def _contains_phrase(self, text: str) -> bool:
        """Fuzzy match the wake phrase in Vosk's transcript.

        Vosk's small-en model has a fixed English vocabulary, so it CANNOT
        produce "koov" verbatim — it will hallucinate the closest English word
        ("kuv", "coov", "koop", "coup", "could", etc.). We therefore use the
        same fuzzy matcher as the transcript wake path (Levenshtein-bounded,
        with `_FUZZY_FALSE_FRIENDS` blocking common English homophones like
        "too" / "cool"). The companion `_has_command_or_question` check guards
        against bare-noun false fires.
        """
        if not text:
            return False
        return detect_wake(
            normalize_for_wake(text),
            self._wake_phrases,
            max_dist=2,
        )

    def _has_command_or_question(self, text: str) -> bool:
        """Wake fires only when the wake word is followed by a real command/question.

        Bare wake ("Koov." / "Computer.") with nothing after is intentionally
        ignored — users get no answer until they say what they actually want.
        Idle praise ("Koov is cool", "the computer is dead") is also rejected
        by ``remainder_qualifies_as_wake_command``.
        """
        if not text:
            return False
        normalized = normalize_for_wake(text)
        # Strip any wake-token occurrence (fuzzy) — handles Vosk mishearings like
        # "kuv summarize meeting" where regex against the literal "koov" misses.
        stripped = strip_wake_phrase(normalized, self._wake_phrases, max_dist=2)
        if not stripped:
            # Bare wake — caller said nothing actionable. Stay silent.
            return False
        return remainder_qualifies_as_wake_command(stripped)

    def process(self, pcm16: bytes, timestamp_s: float) -> bool:
        if not pcm16:
            return False

        is_final = self._recognizer.AcceptWaveform(pcm16)
        # Strict: only fire on final hypothesis (wake tied to this segment)
        if not is_final:
            return False

        # Final hypothesis – cooldown, phrase, and command co-occurrence
        text = self._json_text(self._recognizer.Result(), key="text")
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Vosk final: %s", text)

        if not self._contains_phrase(text):
            return False
        if not self._has_command_or_question(text):
            log.debug("Vosk wake phrase present but no command/question in segment; not activating")
            return False
        if not self._gate.ready(timestamp_s):
            return False
        self._gate.fired(timestamp_s)
        # Store trigger text so the agent can use it as the primary command source.
        self.last_trigger_text = text
        self._recognizer.Reset()
        self._recent_hits.clear()
        return True


class OpenWakeWordDetector(WakeDetector):
    def __init__(
        self,
        model_path: str,
        sample_rate_hz: int,
        threshold: float,
        cooldown_s: float,
    ):
        if sample_rate_hz != 16000:
            raise ValueError("openWakeWord backend requires KOOV_SAMPLE_RATE_HZ=16000")
        self._threshold = float(threshold)
        self._gate = CooldownGate(cooldown_s)
        self._recent_hits: list[float] = []

        try:
            from openwakeword.model import Model  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("openwakeword is not installed") from exc

        self._model = Model(wakeword_models=[model_path])

    def _hit(self, now_s: float, window_s: float = 0.75, required: int = 2) -> bool:
        self._recent_hits.append(now_s)
        self._recent_hits = [t for t in self._recent_hits if (now_s - t) <= window_s]
        if len(self._recent_hits) >= required:
            self._recent_hits.clear()
            return True
        return False

    def reset(self) -> None:
        if hasattr(self._model, "reset"):
            self._model.reset()
        self._recent_hits.clear()

    def process(self, pcm16: bytes, timestamp_s: float) -> bool:
        if not pcm16:
            return False

        audio = pcm16_to_float32(pcm16)
        scores = self._model.predict(audio)

        best = 0.0
        if isinstance(scores, dict):
            for _, value in scores.items():
                try:
                    if isinstance(value, (list, tuple, np.ndarray)):
                        v = float(np.max(value))
                    else:
                        v = float(value)
                except Exception:
                    continue
                best = max(best, v)
        else:
            try:
                best = float(scores)
            except Exception:
                best = 0.0

        if best < self._threshold:
            return False
        if not self._hit(timestamp_s):
            return False
        if not self._gate.ready(timestamp_s):
            return False
        self._gate.fired(timestamp_s)
        return True
