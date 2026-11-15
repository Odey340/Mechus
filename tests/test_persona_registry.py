"""Persona registry: load JSON files, fall back to default, resolve by wake phrase."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass


@dataclass
class _StubConfig:
    """Minimal stand-in for MechusConfig — only the fields PersonaRegistry reads."""

    wake_phrase: str = "mechus"
    wake_phrases: tuple[str, ...] = ("mechus",)
    eleven_voice_id: str | None = "default-voice"
    eleven_model_id: str | None = "eleven_v3"
    eleven_audio_tags: bool = False
    max_response_words: int = 40
    response_temperature: float | None = None
    bot_persona_id: str | None = None


def _write_persona(directory: str, data: dict) -> str:
    pid = data.get("id") or "x"
    path = os.path.join(directory, f"{pid}.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp)
    return path


class TestPersonaRegistry(unittest.TestCase):
    def test_missing_dir_yields_default_only(self) -> None:
        from mechus.personas import PersonaRegistry

        # Point at a path that does not exist; registry must still produce default.
        registry = PersonaRegistry.load(_StubConfig(), personas_dir="/tmp/__nope_does_not_exist__")
        self.assertEqual(len(registry.personas()), 1)
        self.assertTrue(registry.default().is_default)
        self.assertEqual(registry.default().id, "default")
        self.assertEqual(registry.default().wake_phrases, ("mechus",))

    def test_loads_named_persona_and_resolves_by_wake(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["hey penny", "penny"],
                "voice_id": "voice-penny",
                "system_prompt": "You are Penny.",
                "audio_tags": True,
                "max_words": 55,
                "temperature": 0.4,
                "tools": ["realtime", "stock_quote"],
                "refusal_rules": ["Not advice."],
                "filler_phrases": ["Pulling that up."],
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)

        # default + penny
        ids = [p.id for p in registry.personas()]
        self.assertIn("default", ids)
        self.assertIn("penny", ids)

        penny = registry.get("penny")
        self.assertIsNotNone(penny)
        assert penny is not None  # for type checkers
        self.assertEqual(penny.name, "Penny")
        self.assertEqual(penny.voice_id, "voice-penny")
        self.assertTrue(penny.audio_tags)
        self.assertEqual(penny.temperature, 0.4)
        self.assertIn("realtime", penny.tools)
        self.assertIn("stock_quote", penny.tools)
        self.assertFalse(penny.is_default)

        # wake-phrase resolution
        self.assertEqual(registry.resolve_by_wake("hey penny").id, "penny")
        self.assertEqual(registry.resolve_by_wake("penny").id, "penny")
        # unknown wake → default
        self.assertEqual(registry.resolve_by_wake("mechus").id, "default")
        self.assertEqual(registry.resolve_by_wake("").id, "default")

    def test_longest_wake_phrase_wins(self) -> None:
        """If two personas claim overlapping phrases (rare), the longer one wins."""
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["penny"],
                "voice_id": "voice-penny",
                "system_prompt": "You are Penny.",
            })
            _write_persona(d, {
                "id": "hey-penny-too",
                "name": "HeyPennyToo",
                "wake_phrases": ["hey penny too"],
                "voice_id": "voice-htn",
                "system_prompt": "You are HeyPennyToo.",
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)

        phrases = registry.all_wake_phrases()
        # Multi-word phrase must come before single-word ones in the flat list.
        self.assertLess(phrases.index("hey penny too"), phrases.index("penny"))

    def test_env_var_substitution_for_voice_id(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["penny"],
                "voice_id": "${MECHUS_TEST_PENNY_VOICE_ID}",
                "system_prompt": "You are Penny.",
            })
            os.environ["MECHUS_TEST_PENNY_VOICE_ID"] = "expanded-voice"
            try:
                registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)
            finally:
                del os.environ["MECHUS_TEST_PENNY_VOICE_ID"]
        penny = registry.get("penny")
        self.assertIsNotNone(penny)
        assert penny is not None
        self.assertEqual(penny.voice_id, "expanded-voice")

    def test_voice_id_falls_back_to_config_when_var_unset(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["penny"],
                "voice_id": "${MECHUS_NEVER_SET_VAR_XYZ}",
                "system_prompt": "You are Penny.",
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)
        penny = registry.get("penny")
        self.assertIsNotNone(penny)
        assert penny is not None
        self.assertEqual(penny.voice_id, "default-voice")  # from _StubConfig

    def test_enabled_filter_keeps_default_even_when_excluded(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny", "name": "Penny",
                "wake_phrases": ["penny"], "system_prompt": "x",
            })
            _write_persona(d, {
                "id": "doc", "name": "Doc",
                "wake_phrases": ["doc"], "system_prompt": "x",
            })
            # Only "penny" is enabled. Default persona is always present.
            registry = PersonaRegistry.load(
                _StubConfig(), personas_dir=d, enabled_ids=["penny"]
            )
        ids = sorted(p.id for p in registry.personas())
        self.assertEqual(ids, ["default", "penny"])

    def test_skips_persona_without_wake_phrases(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "broken", "name": "Broken", "system_prompt": "x",
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)
        self.assertEqual([p.id for p in registry.personas()], ["default"])

    def test_default_persona_reflects_config_wake_phrases(self) -> None:
        from mechus.personas import PersonaRegistry

        cfg = _StubConfig(wake_phrases=("hey computer", "computer", "mechus"), wake_phrase="hey computer")
        registry = PersonaRegistry.load(cfg, personas_dir="/tmp/__nope__")
        d = registry.default()
        self.assertEqual(d.wake_phrases, ("hey computer", "computer", "mechus"))

    def test_locked_persona_id_single_registry(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["hey penny", "penny"],
                "voice_id": "voice-penny",
                "system_prompt": "You are Penny.",
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d, locked_persona_id="penny")
        ids = [p.id for p in registry.personas()]
        self.assertEqual(ids, ["penny"])
        self.assertEqual(registry.default().id, "penny")
        self.assertEqual(registry.resolve_by_wake("penny").id, "penny")

    def test_locked_unknown_raises(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["penny"],
                "system_prompt": "You are Penny.",
            })
            with self.assertRaises(ValueError):
                PersonaRegistry.load(_StubConfig(), personas_dir=d, locked_persona_id="nope")


class TestPersonaResolveFromText(unittest.TestCase):
    def test_resolve_from_text_uses_fuzzy_wake_match(self) -> None:
        from mechus.personas import PersonaRegistry

        with tempfile.TemporaryDirectory() as d:
            _write_persona(d, {
                "id": "penny",
                "name": "Penny",
                "wake_phrases": ["hey penny", "penny"],
                "system_prompt": "You are Penny.",
            })
            registry = PersonaRegistry.load(_StubConfig(), personas_dir=d)

        self.assertEqual(registry.resolve_from_text("hey penny what's the price").id, "penny")
        self.assertEqual(registry.resolve_from_text("can you hear me mechus").id, "default")
        # No wake phrase at all → default
        self.assertEqual(registry.resolve_from_text("just chatting here").id, "default")


if __name__ == "__main__":
    unittest.main()
