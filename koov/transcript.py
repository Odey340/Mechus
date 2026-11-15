from __future__ import annotations

from koov.openai_compat import chat_completions_url as _koov_chat_url, supports_json_mode as _koov_json_ok

from dataclasses import dataclass
import json
import logging
import re
import ssl
import urllib.request

import webrtcvad

from koov.text import normalize_text
from koov.transcript_noise import INLINE_NOISE_RE

log = logging.getLogger(__name__)

_STAGE_WORDS = r"(laugh|laughter|chuckle|smirk|pause|sigh|cough|clears throat|wink)"
_STAGE_RE = re.compile(
    rf"(\*[^*]*{_STAGE_WORDS}[^*]*\*|\[[^\]]*{_STAGE_WORDS}[^\]]*\]|\([^)]*{_STAGE_WORDS}[^)]*\))",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_TAG_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "not",
    "of",
    "on",
    "or",
    "our",
    "ours",
    "she",
    "so",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "to",
    "we",
    "were",
    "what",
    "with",
    "you",
    "your",
    "speaker",
}
_TAG_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_NAME_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\\-]{1,}")
_NAME_STOPWORDS = {
    "and",
    "are",
    "back",
    "call",
    "calling",
    "cool",
    "dude",
    "everyone",
    "guys",
    "hello",
    "hey",
    "here",
    "i",
    "im",
    "i'm",
    "its",
    "it's",
    "just",
    "me",
    "name",
    "no",
    "ok",
    "okay",
    "right",
    "so",
    "speaking",
    "thanks",
    "that",
    "this",
    "yeah",
    "yes",
    "you",
}
_SELF_NAME_PATTERNS = [
    re.compile(
        r"\b(?:i am|i'm|im|this is|it's|it is|my name is|name is|name's|call me|you can call me)\s+([a-zA-Z][a-zA-Z'\\-]*(?:\s+[a-zA-Z][a-zA-Z'\\-]*){0,2})",
        re.IGNORECASE,
    ),
    re.compile(r"\b([a-zA-Z][a-zA-Z'\\-]{1,})(?:\s+(?:here|speaking|calling))\b", re.IGNORECASE),
]


def normalize_stt_command_text(text: str) -> str:
    """
    Fix common STT/word-normalization artifacts before wake routing and intent.
    Complements wake_detector.normalize_for_wake (contraction merge).
    """
    text = normalize_text(text)
    if not text:
        return ""
    t = text.strip()
    # Residual "what s" / "how s" when apostrophes were lost earlier in the pipeline
    t = re.sub(r"\bwhat\s+s\b", "what's", t, flags=re.IGNORECASE)
    t = re.sub(r"\bhow\s+s\b", "how's", t, flags=re.IGNORECASE)
    t = re.sub(r"\bwho\s+s\b", "who's", t, flags=re.IGNORECASE)
    t = re.sub(r"\bthere\s+s\b", "there's", t, flags=re.IGNORECASE)
    t = _WS_RE.sub(" ", t).strip()
    return t


def clean_transcript_text(text: str) -> str:
    text = normalize_text(text)
    if not text:
        return ""
    text = _STAGE_RE.sub("", text)
    text = INLINE_NOISE_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip(" ,.-")
    return normalize_text(text)


def extract_tags(text: str, max_tags: int = 5, min_len: int = 3) -> list[str]:
    text = normalize_text(text).lower()
    if not text:
        return []
    tokens = _TAG_TOKEN_RE.findall(text)
    if not tokens:
        return []
    counts: dict[str, int] = {}
    order: list[str] = []
    for tok in tokens:
        if len(tok) < min_len:
            continue
        if tok in _TAG_STOPWORDS:
            continue
        if tok not in counts:
            order.append(tok)
            counts[tok] = 1
        else:
            counts[tok] += 1
    if not counts:
        return []
    order.sort(key=lambda t: (-counts[t], t))
    return order[: max(1, int(max_tags))]


def extract_self_name(text: str, wake_phrase: str = "") -> str:
    text = normalize_text(text)
    if not text:
        return ""
    wake_phrase = normalize_text(wake_phrase).lower()
    for pattern in _SELF_NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = match.group(1).strip()
        if not candidate:
            continue
        parts = re.split(r"\s+", candidate)
        chosen: list[str] = []
        for part in parts:
            token = part.strip(".,;:!?\"'()[]{}").replace("’", "'")
            if not token:
                continue
            if not _NAME_TOKEN_RE.fullmatch(token):
                break
            if token.lower() in _NAME_STOPWORDS:
                break
            if wake_phrase and token.lower() == wake_phrase:
                break
            chosen.append(token)
            if len(chosen) >= 3:
                break
        if not chosen:
            continue
        return " ".join(word[:1].upper() + word[1:] for word in chosen)
    return ""


@dataclass(frozen=True)
class TranscriptChunk:
    start_ts: float
    end_ts: float
    pcm16: bytes
    # Zoom one-way stream label (display name) when using SDK attribution; else None
    speaker_id: str | None = None


class TranscriptChunker:
    def __init__(
        self,
        sample_rate_hz: int,
        frame_ms: int,
        min_seconds: float,
        max_seconds: float,
        end_silence_seconds: float,
        vad_mode: int = 2,
        *,
        default_speaker_id: str | None = None,
        pre_roll_seconds: float = 0.0,
    ) -> None:
        self._default_speaker_id = default_speaker_id
        self._sr = sample_rate_hz
        self._frame_ms = frame_ms
        self._frame_bytes = int(sample_rate_hz * (frame_ms / 1000.0) * 2)
        self._vad = webrtcvad.Vad(vad_mode)
        self._min_frames = int(max(1, min_seconds / (frame_ms / 1000.0)))
        self._max_frames = int(max_seconds / (frame_ms / 1000.0))
        self._end_silence_frames = int(end_silence_seconds / (frame_ms / 1000.0))
        self._pre_roll_frames = max(0, int(pre_roll_seconds / (frame_ms / 1000.0)))

        self._buf: list[bytes] = []
        self._pre_roll_buf: list[bytes] = []
        self._in_speech = False
        self._silence_frames = 0
        self._total_frames = 0
        self._start_ts = 0.0
        self._last_ts = 0.0

    def reset(self) -> None:
        self._buf = []
        self._pre_roll_buf = []
        self._in_speech = False
        self._silence_frames = 0
        self._total_frames = 0
        self._start_ts = 0.0
        self._last_ts = 0.0

    def push(self, pcm16: bytes, timestamp_s: float) -> TranscriptChunk | None:
        if len(pcm16) != self._frame_bytes:
            return None

        is_speech = self._vad.is_speech(pcm16, self._sr)
        if not self._in_speech:
            if self._pre_roll_frames > 0:
                self._pre_roll_buf.append(pcm16)
                if len(self._pre_roll_buf) > self._pre_roll_frames:
                    self._pre_roll_buf = self._pre_roll_buf[-self._pre_roll_frames:]
            if not is_speech:
                return None
            self._in_speech = True
            self._buf = [*self._pre_roll_buf, pcm16]
            self._silence_frames = 0
            self._total_frames = len(self._buf)
            pre_roll_s = len(self._pre_roll_buf) * (self._frame_ms / 1000.0)
            self._start_ts = max(0.0, timestamp_s - pre_roll_s)
            self._last_ts = timestamp_s
            self._pre_roll_buf = []
            return None

        self._buf.append(pcm16)
        self._total_frames += 1
        self._last_ts = timestamp_s
        if is_speech:
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        if self._total_frames >= self._max_frames:
            return self._flush()
        if self._silence_frames >= self._end_silence_frames and self._total_frames >= self._min_frames:
            return self._flush()
        return None

    def flush(self) -> TranscriptChunk | None:
        return self._flush()

    def _flush(self) -> TranscriptChunk | None:
        if not self._in_speech:
            return None
        if self._total_frames < self._min_frames:
            self.reset()
            return None
        pcm16 = b"".join(self._buf)
        start_ts = self._start_ts or self._last_ts
        end_ts = self._last_ts or self._start_ts
        self.reset()
        return TranscriptChunk(
            start_ts=start_ts,
            end_ts=end_ts,
            pcm16=pcm16,
            speaker_id=self._default_speaker_id,
        )


class TranscriptRefiner:
    """Uses OpenAI Chat to clean/rewrite live meeting transcript text."""

    def __init__(self, api_key: str, model: str, max_words: int = 120):
        self._api_key = api_key.strip()
        self._model = (model or "gpt-4o-mini").strip()
        self._max_words = max(40, int(max_words))

    def refine(self, text: str) -> str:
        text = clean_transcript_text(text)
        if not text:
            return ""
        if len(text.split()) < 5:
            return text

        system = (
            "You are cleaning a live meeting transcript.\n"
            "CRITICAL RULES:\n"
            "- Fix obvious transcription errors and missing short words.\n"
            "- Do NOT add facts, names, numbers, or content that isn't clearly implied.\n"
            "- Keep the meaning identical and preserve the speaker's tone.\n"
            "- Remove greetings, filler words, and stage directions (e.g., laughs, pauses).\n"
            "- Return ONLY the cleaned transcript text. Do NOT add any prefix like 'Here is the cleaned transcript:' or 'Sure!' or any commentary.\n"
            "- Output ONLY the cleaned text, nothing else.\n"
        )
        user = f"Raw transcript:\n{text}\n\nCleaned transcript (output only the text, no commentary):"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_words * 2,
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            _koov_chat_url(),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.debug("Transcript refine failed: %s", exc)
            return text
        choices = payload.get("choices") or []
        if not choices:
            return text
        raw_cleaned = str(choices[0].get("message", {}).get("content", ""))
        cleaned = clean_transcript_text(raw_cleaned)
        cleaned = re.sub(
            r"^(Sure!?\s*)?(Here is|Here's)\s+(the\s+)?(cleaned\s+)?(transcript|version):\s*",
            "",
            cleaned,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        cleaned = cleaned.strip('"').strip()
        return cleaned or text
