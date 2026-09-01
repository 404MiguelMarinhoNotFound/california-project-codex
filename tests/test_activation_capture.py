"""
Activation clip capture — the diagnostics that turn false positives from
anecdotes into a scoreable corpus.

The outcome label in the filename is the whole point: a `no_speech` or `short`
clip is by construction a wake nobody spoke into, so the directory doubles as
the negatives set for `tools/score_wakeword.py --negatives`.
"""

import os
import tempfile
import unittest

from core.orchestrator import _activation_clip_name, _prune_clips


class ClipNameTests(unittest.TestCase):
    def test_the_name_carries_the_outcome(self):
        self.assertTrue(_activation_clip_name("no_speech").endswith("_no_speech.wav"))
        self.assertTrue(_activation_clip_name("short").endswith("_short.wav"))
        self.assertTrue(_activation_clip_name("ok").endswith("_ok.wav"))

    def test_names_sort_chronologically(self):
        # _prune_clips deletes by sorted order, so the name must sort by time.
        early = _activation_clip_name("ok", when=1_700_000_000.0)
        late = _activation_clip_name("ok", when=1_700_000_060.0)
        self.assertLess(early, late)

    def test_two_clips_in_the_same_second_do_not_collide(self):
        first = _activation_clip_name("ok", when=1_700_000_000.100)
        second = _activation_clip_name("ok", when=1_700_000_000.900)
        self.assertNotEqual(first, second)


class PruneTests(unittest.TestCase):
    def _make(self, directory, names):
        for name in names:
            with open(os.path.join(directory, name), "wb") as fh:
                fh.write(b"")

    def test_prunes_the_oldest_beyond_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, [f"2026090{i}_000000_000_ok.wav" for i in range(1, 6)])
            removed = _prune_clips(tmp, max_files=2)

            self.assertEqual(len(removed), 3)
            left = sorted(os.listdir(tmp))
            self.assertEqual(left, ["20260904_000000_000_ok.wav", "20260905_000000_000_ok.wav"])

    def test_keeps_everything_when_under_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, ["20260901_000000_000_ok.wav", "20260902_000000_000_ok.wav"])
            self.assertEqual(_prune_clips(tmp, max_files=10), [])
            self.assertEqual(len(os.listdir(tmp)), 2)

    def test_a_cap_of_zero_disables_pruning_rather_than_deleting_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, ["20260901_000000_000_ok.wav"])
            self.assertEqual(_prune_clips(tmp, max_files=0), [])
            self.assertEqual(len(os.listdir(tmp)), 1)

    def test_leaves_non_wav_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, ["a_ok.wav", "b_ok.wav", "notes.txt"])
            _prune_clips(tmp, max_files=1)
            self.assertIn("notes.txt", os.listdir(tmp))


if __name__ == "__main__":
    unittest.main()
