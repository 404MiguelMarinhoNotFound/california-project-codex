"""
Speaking from inside a long-running tool call.

A CEC wake takes ~25s. `_dispatch_tv` runs synchronously inside the LLM stream,
so without this the assistant goes silent for that whole time and reads as hung.

The mechanism is deliberately not a new thread or a second queue: the turn's TTS
worker is already draining `tts_queue` on its own thread while the tool call
blocks the streaming thread, so an interim line just goes into that queue.
"""

import queue
import unittest
from unittest.mock import Mock

from core.orchestrator import Orchestrator, _ensure_playable


class SayNowTests(unittest.TestCase):
    def _orchestrator(self) -> Orchestrator:
        # __init__ builds audio, LLM and TV services; none of that is needed to
        # exercise the queue handoff, so bypass it.
        return Orchestrator.__new__(Orchestrator)

    def test_a_line_reaches_the_live_turn_queue(self):
        orch = self._orchestrator()
        q: queue.Queue = queue.Queue()
        orch._active_tts_queue = q
        orch._say_now("Hold on, waking everything up.")
        self.assertEqual(q.get_nowait(), "Hold on, waking everything up.")

    def test_speaking_outside_a_turn_is_a_no_op(self):
        """
        The handoff is dropped in _generate_and_speak's finally, before the queue
        is closed. A late say_now must not push into a finished turn.
        """
        orch = self._orchestrator()
        orch._active_tts_queue = None
        orch._say_now("nobody is listening")  # must not raise

    def test_empty_text_is_not_queued(self):
        orch = self._orchestrator()
        q: queue.Queue = queue.Queue()
        orch._active_tts_queue = q
        orch._say_now("")
        self.assertTrue(q.empty())

    def test_the_wake_line_is_spoken_before_the_wake_blocks(self):
        """
        Ordering is the point: the line must be queued BEFORE turn_on() is
        called, not after it returns 25 seconds later.
        """
        order = []
        media = Mock()
        media.ensure_connected.side_effect = [False, True]
        media.is_active_source.return_value = True
        media.turn_on.side_effect = lambda: order.append("wake") or True
        media.last_wake_result = None

        _ensure_playable(media, say_now=lambda text: order.append("speak"))
        self.assertEqual(order, ["speak", "wake"])


if __name__ == "__main__":
    unittest.main()
