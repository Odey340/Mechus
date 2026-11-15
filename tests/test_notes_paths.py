"""Notes + meeting-store path resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from koov.notes_paths import apply_notes_paths_to_env, resolve_meeting_store_dir, resolve_notes_dir


class TestNotesPaths(unittest.TestCase):
    def test_resolve_notes_dir_uses_explicit_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"KOOV_NOTES_DIR": tmp}, clear=False):
                self.assertEqual(resolve_notes_dir(), os.path.abspath(tmp))

    def test_meeting_store_under_notes_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"KOOV_NOTES_DIR": tmp}, clear=False):
                os.environ.pop("KOOV_MEETING_STORE_DIR", None)
                store = resolve_meeting_store_dir()
            self.assertEqual(store, os.path.join(os.path.abspath(tmp), "koov_meetings"))

    def test_apply_notes_paths_creates_store_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"KOOV_NOTES_DIR": tmp}
            notes, store = apply_notes_paths_to_env(env)
            self.assertTrue(os.path.isdir(store))
            self.assertEqual(env["KOOV_MEETING_STORE_DIR"], store)
            self.assertEqual(notes, os.path.abspath(tmp))


if __name__ == "__main__":
    unittest.main()
