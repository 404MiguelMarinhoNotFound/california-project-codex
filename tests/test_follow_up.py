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

import unittest

from services.llm import ERROR_LINE, LLMService


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


if __name__ == "__main__":
    unittest.main()
