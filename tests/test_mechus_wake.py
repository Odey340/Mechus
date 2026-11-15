"""The assistant answers to "Mechus" (said "MECK-us"), not "Koov"."""
import pytest

from koov.brand import ASSISTANT_NAME, DEFAULT_WAKE_PHRASE, for_tts
from koov.config import KoovConfig
from koov.facetime_env import PRODUCTION_WAKE_PHRASE
from koov.wake_detector import detect_wake, normalize_for_wake, strip_wake_phrase

PHRASES = tuple(p.strip() for p in PRODUCTION_WAKE_PHRASE.split(","))


def test_default_wake_phrases_are_mechus():
    assert ASSISTANT_NAME == "Mechus"
    assert PHRASES == ("hey mechus", "mechus")
    assert DEFAULT_WAKE_PHRASE == PRODUCTION_WAKE_PHRASE


def test_config_default_wake_phrase_is_mechus(monkeypatch):
    monkeypatch.delenv("KOOV_WAKE_PHRASE", raising=False)
    cfg = KoovConfig.from_env()
    assert cfg.wake_phrases == ("hey mechus", "mechus")


@pytest.mark.parametrize(
    "heard",
    [
        "Mechus, can you show me the kitchen findings?",
        "Hey Mechus, what did you see?",
        "Meckus, stop.",                 # one edit
        "Mekus what do you see",         # alias
        "Mecca's, what do you see?",     # STT writes a real word
        "Mech us, can you look again?",  # STT splits the name
        "hey meck us what is that",
    ],
)
def test_mechus_and_its_stt_mishearings_wake(heard):
    assert detect_wake(normalize_for_wake(heard), PHRASES, max_dist=1)


@pytest.mark.parametrize(
    "heard",
    [
        "Koov, what do you see?",
        "Hey Koov, stop.",
        "coop summarize the meeting",
        "the mechanic fixed the sink",
        "that was too much for us",
    ],
)
def test_koov_and_ordinary_speech_do_not_wake(heard):
    assert not detect_wake(normalize_for_wake(heard), PHRASES, max_dist=1)


def test_wake_word_is_stripped_from_the_command():
    assert strip_wake_phrase(normalize_for_wake("Hey Mech us, show me the wall"), PHRASES, max_dist=1) == "show me the wall"


def test_tts_gets_the_phonetic_spelling():
    assert for_tts("Mechus here. Say Mechus, then ask.") == "Meckus here. Say Meckus, then ask."
    assert for_tts("the mechanism is fine") == "the mechanism is fine"
