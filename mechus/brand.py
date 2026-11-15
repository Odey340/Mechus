"""The assistant's public name: what callers say to wake it, hear it call itself, and see.

The Python package, env vars and logs keep the ``mechus`` / ``MECHUS_`` prefix; only the
caller-facing identity lives here.

Env:
  MECHUS_ASSISTANT_NAME         written name (default "Mechus")
  MECHUS_ASSISTANT_SPOKEN_NAME  spelling handed to TTS so it is pronounced "MECK-us"
                              (default "Meckus"; set equal to the written name to disable)
"""
from __future__ import annotations

import os
import re

ASSISTANT_NAME = (os.environ.get("MECHUS_ASSISTANT_NAME") or "").strip() or "Mechus"
ASSISTANT_SPOKEN_NAME = (os.environ.get("MECHUS_ASSISTANT_SPOKEN_NAME") or "").strip() or "Meckus"
WAKE_WORD = ASSISTANT_NAME.lower()

# Default wake phrases: "hey mechus" and bare "mechus" (longer phrase matched first).
DEFAULT_WAKE_PHRASE = f"hey {WAKE_WORD},{WAKE_WORD}"

# What speech-to-text tends to write when someone says "MECK-us". These are more than one
# edit from "mechus", so fuzzy matching at MECHUS_WAKE_TRANSCRIPT_MAX_DIST=1 would miss them.
MECHUS_STT_ALIASES: frozenset[str] = frozenset(
    {
        "meckus", "mekus", "mekkus", "meccus", "mecus", "mechis", "mekis", "meckis",
        "mechas", "meccas", "mekas", "meckas", "mechos", "mecos", "meckes", "mechuss",
        "mackus", "makus", "mexus", "mekhus", "meckous", "mechous",
    }
)

# STT also splits the name in two ("mech us", "meck us"); joined back before wake matching.
_SPLIT_NAME_RE = re.compile(r"\b(?:mech|meck|mek|mec|mack)\s+(?:us|is|as)\b")

_WRITTEN_NAME_RE = re.compile(rf"\b{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)


def join_split_wake_word(normalized: str) -> str:
    """"hey mech us show me" -> "hey mechus show me" (input is lower-case, punctuation-free)."""
    if WAKE_WORD != "mechus" or not normalized:
        return normalized
    return _SPLIT_NAME_RE.sub("mechus", normalized)


def for_tts(text: str) -> str:
    """Swap the written name for its phonetic spelling just before speech synthesis."""
    if not text or ASSISTANT_SPOKEN_NAME == ASSISTANT_NAME:
        return text
    return _WRITTEN_NAME_RE.sub(ASSISTANT_SPOKEN_NAME, text)
