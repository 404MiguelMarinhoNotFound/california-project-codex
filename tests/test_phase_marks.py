"""
Phase marks: the in-code half of a benchmark timeline.

`mark()` sits on the real wake and play paths, so outside a bench run it must
cost nothing and change nothing. Inside one it records the FIRST time each
phase happened, in ms since the recording started, from whichever thread got
there.
"""

import threading
import unittest
from unittest.mock import patch

from services import phase_marks


class PhaseMarksTests(unittest.TestCase):
    def test_mark_outside_recording_is_a_noop(self):
        phase_marks.mark("x")
        with phase_marks.recording() as marks:
            pass
        self.assertEqual(marks, {})

    def test_first_occurrence_wins(self):
        with patch("services.phase_marks.time.monotonic", side_effect=[0.0, 1.0, 2.5]):
            with phase_marks.recording() as marks:
                phase_marks.mark("a")
                phase_marks.mark("a")
        self.assertEqual(marks, {"a": 1000.0})

    def test_marks_from_another_thread_are_recorded(self):
        with phase_marks.recording() as marks:
            t = threading.Thread(target=phase_marks.mark, args=("box_ready",))
            t.start()
            t.join()
        self.assertIn("box_ready", marks)

    def test_nested_recording_raises(self):
        with phase_marks.recording():
            with self.assertRaises(RuntimeError):
                with phase_marks.recording():
                    pass

    def test_marks_after_the_recording_ends_are_dropped(self):
        with phase_marks.recording() as marks:
            pass
        phase_marks.mark("late")
        self.assertEqual(marks, {})


if __name__ == "__main__":
    unittest.main()
