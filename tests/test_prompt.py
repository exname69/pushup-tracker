import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vision import load_prompt


class PromptTests(unittest.TestCase):
    def test_default_prompt_keeps_json_contract(self):
        with patch.dict(os.environ, {"GEMINI_PROMPT_PATH": ""}):
            prompt = load_prompt()
        self.assertIn("Count ALL visible push-up-like movements", prompt)
        self.assertIn('{"count": 0, "completed_at_seconds": [], "uncertain": false}', prompt)

    def test_override_is_read_every_time(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prompt.txt"
            with patch.dict(os.environ, {"GEMINI_PROMPT_PATH": str(path)}):
                path.write_text("Первый промпт\n", encoding="utf-8")
                self.assertEqual(load_prompt(), "Первый промпт")
                path.write_text("Second prompt", encoding="utf-8")
                self.assertEqual(load_prompt(), "Second prompt")
                path.write_text("  \n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_prompt()
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    load_prompt()
