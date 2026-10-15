"""
Answering her question without the wake word.

When California's reply asks Master Miguel something -- a read-back, "which
one?", or any reply ending in a question mark -- the mic stays open for a
short window and his answer is taken without "California" first. Speech in
that window that is not an answer to her (the TV, someone else) is recognised
by the LLM, which replies exactly "[ignore]", and is dropped silently.

Spec: docs/superpowers/specs/2026-10-15-follow-up-listening-design.md

Everything here is faked: no mic, no model, no TTS provider, no network.
"""

import json
import os
import tempfile
import unittest

import numpy as np
import soundfile as sf

from services.llm import ERROR_LINE, LLMService
from tests.config_fixture import config_for_tests


def _llm(history):
    svc = LLMService.__new__(LLMService)
    svc.history = list(history)
    return svc


class DropLastExchangeTests(unittest.TestCase):
    def test_removes_the_last_exchange_and_its_tool_rounds(self):
        svc = _llm([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {"role": "assistant", "content": "y"},
        ])
        self.assertTrue(svc.drop_last_exchange())
        self.assertEqual(svc.history, [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
        ])

    def test_a_partial_ignore_reply_is_removed_with_its_question(self):
        svc = _llm([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "[Heard without the wake word...] tv"},
            {"role": "assistant", "content": "[ignore]"},
        ])
        self.assertTrue(svc.drop_last_exchange())
        self.assertEqual(len(svc.history), 2)
        self.assertEqual(svc.history[-1]["content"], "x")

    def test_no_exchange_is_a_logged_no_op(self):
        svc = _llm([])
        with self.assertLogs("services.llm", level="WARNING"):
            self.assertFalse(svc.drop_last_exchange())
        self.assertEqual(svc.history, [])

    def test_error_line_constant_is_what_she_says(self):
        # It ends in a question mark, which is exactly why a reply carrying it
        # must never open a follow-up window (one outage would loop).
        self.assertTrue(ERROR_LINE.endswith("?"))
        self.assertEqual(
            ERROR_LINE, "Sorry, I had trouble thinking about that. Could you try again?"
        )


TIMEOUT_TEXT = "I'll take that as a no. Say my name if you need me."
CAP_TEXT = "Okay, I'll stop listening now. Say California if you need me."


class StopLinesTests(unittest.TestCase):
    def _write(self, folder, names, manifest=True):
        for name in names:
            sf.write(os.path.join(folder, f"{name}.wav"), np.zeros(2400, dtype=np.float32), 24000)
        if manifest:
            with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"timeout": TIMEOUT_TEXT, "cap": CAP_TEXT}, fh)

    def test_loads_both_clips_with_their_text(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout", "cap"])
            lines = load_stop_lines(folder)
        self.assertEqual(set(lines), {"timeout", "cap"})
        audio, sr, text = lines["cap"]
        self.assertEqual(sr, 24000)
        self.assertEqual(len(audio), 2400)
        self.assertEqual(text, CAP_TEXT)
        self.assertEqual(lines["timeout"][2], TIMEOUT_TEXT)

    def test_a_missing_clip_is_skipped_with_a_warning(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout"])
            with self.assertLogs("services.stop_lines", level="WARNING") as logs:
                lines = load_stop_lines(folder)
        self.assertEqual(set(lines), {"timeout"})
        self.assertIn("generate_stop_lines.py", "\n".join(logs.output))

    def test_a_missing_manifest_leaves_the_text_empty(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout", "cap"], manifest=False)
            lines = load_stop_lines(folder)
        self.assertEqual(lines["cap"][2], "")

    def test_a_missing_directory_returns_empty_and_never_raises(self):
        from services.stop_lines import load_stop_lines

        with self.assertLogs("services.stop_lines", level="WARNING"):
            self.assertEqual(load_stop_lines("/nonexistent/stop_lines"), {})
        with self.assertLogs("services.stop_lines", level="WARNING"):
            self.assertEqual(load_stop_lines(""), {})

    def test_generator_text_matches_the_spec(self):
        import generate_stop_lines

        self.assertEqual(generate_stop_lines.STOP_LINES, {"timeout": TIMEOUT_TEXT, "cap": CAP_TEXT})

    def test_shipped_config_points_at_the_aoede_folder(self):
        self.assertEqual(
            config_for_tests()["sounds"]["stop_lines_dir"],
            "sounds/stop_lines/google_en-US-Chirp3-HD-Aoede",
        )


if __name__ == "__main__":
    unittest.main()
