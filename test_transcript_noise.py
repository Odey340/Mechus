"""Tests for STT non-speech / noise-only transcript filtering."""
from __future__ import annotations

import re
import unittest

from koov.text import normalize_text
from koov.transcript_noise import INLINE_NOISE_RE, is_noise_only_transcript


class TestNoiseOnly(unittest.TestCase):
    def test_drop_single_labels(self) -> None:
        for s in ("silence", "Silence.", "typing", "clicking", "hmm", "background noise"):
            with self.subTest(s=s):
                self.assertTrue(is_noise_only_transcript(s), f"expected noise: {s!r}")

    def test_drop_multi_token_noise(self) -> None:
        self.assertTrue(is_noise_only_transcript("typing clicking"))
        self.assertTrue(is_noise_only_transcript("um um um"))

    def test_keep_backchannels(self) -> None:
        for s in ("yeah", "okay", "yeah okay", "right", "thanks"):
            with self.subTest(s=s):
                self.assertFalse(is_noise_only_transcript(s), f"expected keep: {s!r}")

    def test_keep_substantive_short(self) -> None:
        self.assertFalse(is_noise_only_transcript("yeah silence"))
        self.assertFalse(is_noise_only_transcript("I think we should"))
        self.assertFalse(is_noise_only_transcript("computer"))

    def test_long_not_noise_only(self) -> None:
        s = " ".join(["typing"] * 8)
        self.assertFalse(is_noise_only_transcript(s))

    def test_extra_noise(self) -> None:
        self.assertFalse(is_noise_only_transcript("beep"))
        self.assertTrue(is_noise_only_transcript("beep", extra_noise=frozenset({"beep"})))

    def test_extra_phrase(self) -> None:
        self.assertTrue(
            is_noise_only_transcript("custom label", extra_noise=frozenset({"custom label"}))
        )


class TestCleanTranscriptInline(unittest.TestCase):
    def test_strips_bracketed_noise(self) -> None:
        raw = "Hello [typing] world"
        t = normalize_text(INLINE_NOISE_RE.sub("", raw))
        t = re.sub(r"\s+", " ", t).strip()
        self.assertIn("Hello", t)
        self.assertIn("world", t)
        self.assertNotIn("typing", t.lower())


if __name__ == "__main__":
    unittest.main()
