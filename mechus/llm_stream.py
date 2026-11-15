"""Streaming helpers for OpenAI Chat Completions + sentence-level TTS dispatch.

Two pieces:

1.  ``SpokenJsonStreamer`` — an incremental parser that pulls the value of the
    JSON ``spoken`` field out of a streaming OpenAI JSON-mode response. We only
    emit characters that belong to the spoken field, so ``private_thoughts`` /
    ``spoken_continuation`` are never spoken aloud.
2.  ``SentenceBoundaryEmitter`` — buffers incoming characters and yields full
    sentences as soon as it sees a terminator (``.``, ``?``, ``!``) followed by a
    word break, with a small minimum word floor for the *first* sentence so the
    spoken response starts ASAP.

Both helpers are pure Python and easy to test.
"""

from __future__ import annotations

import re


class SpokenJsonStreamer:
    """Extract the ``spoken`` field value from a streaming JSON object.

    Usage::

        s = SpokenJsonStreamer()
        text_so_far = ""
        for delta in stream_of_token_strings():
            new_chars = s.feed(delta)
            text_so_far += new_chars
            if s.done:
                break

    Robust to:
        * The ``spoken`` field appearing anywhere in the object (not just first).
        * Whitespace between ``"spoken"``, ``:`` and the opening ``"``.
        * Standard JSON escapes (``\\"``, ``\\n``, ``\\t``, ``\\r``, ``\\\\``,
          ``\\u00xx``).
        * Tokens splitting across boundaries (e.g. escape char in one delta,
          escaped char in the next).
    """

    _STATE_WAITING = 0
    _STATE_AFTER_KEY = 1
    _STATE_IN_VALUE = 2
    _STATE_DONE = 3

    def __init__(self, key_name: str = "spoken") -> None:
        self._key_pattern = f'"{key_name}"'
        self._buf = ""
        self._state = self._STATE_WAITING
        self._escape = False
        self._unicode_buffer = ""

    @property
    def done(self) -> bool:
        return self._state == self._STATE_DONE

    def feed(self, text: str) -> str:
        """Feed ``text`` to the streamer; return any newly extracted spoken chars."""
        if not text or self._state == self._STATE_DONE:
            return ""
        self._buf += text
        out: list[str] = []
        i = 0
        n = len(self._buf)
        while i < n:
            if self._state == self._STATE_WAITING:
                idx = self._buf.find(self._key_pattern, i)
                if idx < 0:
                    # Keep just enough tail to detect a key split across feeds.
                    keep = max(0, n - len(self._key_pattern))
                    self._buf = self._buf[keep:]
                    return "".join(out)
                i = idx + len(self._key_pattern)
                self._state = self._STATE_AFTER_KEY
                continue
            if self._state == self._STATE_AFTER_KEY:
                c = self._buf[i]
                if c == '"':
                    self._state = self._STATE_IN_VALUE
                    i += 1
                    continue
                if c.isspace() or c == ":":
                    i += 1
                    continue
                # Unexpected — value is not a string. Abort to avoid mis-speaking.
                self._state = self._STATE_DONE
                self._buf = ""
                return "".join(out)
            if self._state == self._STATE_IN_VALUE:
                c = self._buf[i]
                if self._unicode_buffer:
                    self._unicode_buffer += c
                    i += 1
                    if len(self._unicode_buffer) == 4:
                        try:
                            out.append(chr(int(self._unicode_buffer, 16)))
                        except ValueError:
                            pass
                        self._unicode_buffer = ""
                    continue
                if self._escape:
                    if c == "n":
                        out.append("\n")
                    elif c == "t":
                        out.append("\t")
                    elif c == "r":
                        out.append("\r")
                    elif c in ('"', "\\", "/"):
                        out.append(c)
                    elif c == "u":
                        self._unicode_buffer = ""
                        self._escape = False
                        i += 1
                        # Need 4 hex chars; if not available yet, save state.
                        if i + 4 > n:
                            self._buf = "\\u" + self._buf[i:]
                            return "".join(out)
                        hex_chars = self._buf[i : i + 4]
                        try:
                            out.append(chr(int(hex_chars, 16)))
                        except ValueError:
                            pass
                        i += 4
                        continue
                    else:
                        out.append(c)
                    self._escape = False
                    i += 1
                    continue
                if c == "\\":
                    # Hold the escape; if next char is not available yet we'll
                    # come back next feed().
                    if i + 1 >= n:
                        self._buf = self._buf[i:]
                        return "".join(out)
                    self._escape = True
                    i += 1
                    continue
                if c == '"':
                    self._state = self._STATE_DONE
                    self._buf = ""
                    return "".join(out)
                out.append(c)
                i += 1
                continue
            # STATE_DONE
            break
        self._buf = ""
        return "".join(out)


_SENTENCE_TERMINATORS = ".!?"


class SentenceBoundaryEmitter:
    """Buffer characters; emit full sentences as soon as boundaries are seen.

    The *first* sentence is allowed to be shorter (``first_min_words``) so the
    spoken reply starts ASAP. Subsequent sentences require ``min_words``.
    The ``flush`` method drains whatever is buffered (used at end-of-stream).
    """

    _WHITESPACE = re.compile(r"\s+")

    def __init__(
        self,
        *,
        first_min_words: int = 4,
        min_words: int = 1,
        soft_break_min_words: int = 12,
    ) -> None:
        self._buf = ""
        self._sentences_emitted = 0
        self._first_min_words = max(1, int(first_min_words))
        self._min_words = max(1, int(min_words))
        self._soft_break_min_words = max(self._first_min_words, int(soft_break_min_words))

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        self._buf += text
        return self._extract_ready_sentences()

    def flush(self) -> list[str]:
        sentences = self._extract_ready_sentences()
        leftover = self._buf.strip()
        self._buf = ""
        if leftover:
            sentences.append(leftover)
            self._sentences_emitted += 1
        return sentences

    def _extract_ready_sentences(self) -> list[str]:
        sentences: list[str] = []
        while True:
            cut = self._find_sentence_end()
            if cut is None:
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip()
            if sentence:
                sentences.append(sentence)
                self._sentences_emitted += 1
        return sentences

    def _word_count(self, s: str) -> int:
        return len([w for w in self._WHITESPACE.split(s.strip()) if w])

    def _required_words(self) -> int:
        return self._first_min_words if self._sentences_emitted == 0 else self._min_words

    def _find_sentence_end(self) -> int | None:
        """Index just past the sentence terminator, or None if not ready."""
        for i, ch in enumerate(self._buf):
            if ch in _SENTENCE_TERMINATORS:
                # Look ahead for a word-break (space / newline / end-of-buf).
                # If buffer ends right at the terminator, wait for more chars to
                # disambiguate ("e.g." style abbreviations vs. true sentence end).
                if i + 1 >= len(self._buf):
                    return None
                next_ch = self._buf[i + 1]
                if not (next_ch.isspace() or next_ch in "\"')]"):
                    continue
                candidate = self._buf[: i + 1].strip()
                if self._word_count(candidate) >= self._required_words():
                    return i + 1
        # Soft break on long unterminated chunks so we don't sit on a half-paragraph.
        if self._word_count(self._buf) >= self._soft_break_min_words:
            # Break on the last comma followed by space.
            comma = self._buf.rfind(", ")
            if comma > 0 and self._word_count(self._buf[: comma + 1]) >= self._required_words():
                return comma + 2
        return None


def parse_openai_sse_deltas(line: str) -> str | None:
    """Extract the ``content`` delta from one SSE ``data:`` line.

    Returns the delta string, or ``None`` if the line should be skipped
    (heartbeat, ``[DONE]`` terminator, parse failure, or no content).
    """
    import json

    if not line:
        return None
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    try:
        choices = obj.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content is None:
            return None
        return str(content)
    except Exception:
        return None
