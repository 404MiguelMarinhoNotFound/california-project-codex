"""
tools/wakeword_dataset.py: the checks that keep junk out of training, and the
guard that keeps holdout out of train.

Synthetic audio only: tones stand in for a word, noise for the room. No mic,
no Whisper, no model, no network.
"""

import argparse
import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import wakeword_dataset as wd  # noqa: E402

SR = 16000


def window(word_at=(1.5, 2.1), level=3000, floor=30, seconds=4.0, extra=None, seed=0):
    """A 4s take: room noise, a tone for the word, optionally a second sound."""
    rng = np.random.default_rng(seed)
    audio = rng.normal(0, floor, int(seconds * SR))
    for start, end in [word_at] + ([extra] if extra else []):
        a, b = int(start * SR), int(end * SR)
        t = np.arange(b - a) / SR
        audio[a:b] += level * np.sin(2 * np.pi * 220 * t)
    return np.clip(audio, -32768, 32767).astype(np.int16)


def checked(audio, floor=30.0, noisy=False, check_edges=True):
    metrics = wd.analyze(audio, floor)
    return metrics, wd.check(metrics, noisy=noisy, check_edges=check_edges)


class CheckTests(unittest.TestCase):
    def test_a_clean_take_passes(self):
        metrics, (rejects, flags) = checked(window())
        self.assertEqual((rejects, flags), ([], []))
        self.assertAlmostEqual(metrics["word"][0], 1.5, delta=0.03)
        self.assertAlmostEqual(metrics["word"][1], 2.1, delta=0.03)

    def test_silence_is_rejected(self):
        _, (rejects, _) = checked(window(level=0))
        self.assertEqual(rejects, ["no_sound"])

    def test_a_blip_is_too_short(self):
        _, (rejects, _) = checked(window(word_at=(1.5, 1.7)))
        self.assertIn("too_short", rejects)

    def test_a_word_cut_off_by_the_window_is_rejected(self):
        _, (rejects, _) = checked(window(word_at=(3.5, 4.0)))
        self.assertIn("cut_off_end", rejects)
        _, (rejects, _) = checked(window(word_at=(0.0, 0.6)))
        self.assertIn("cut_off_start", rejects)

    def test_edges_are_not_checked_on_pretrimmed_clips(self):
        _, (rejects, _) = checked(window(word_at=(0.0, 0.6), seconds=0.6), check_edges=False)
        self.assertEqual(rejects, [])

    def test_clipping_is_rejected(self):
        _, (rejects, _) = checked(window(level=40000))
        self.assertIn("clipping", rejects)

    def test_too_long_rejects_in_a_quiet_room_but_only_flags_with_the_tv_on(self):
        take = window(word_at=(1.0, 2.8))
        self.assertIn("too_long", checked(take)[1][0])
        rejects, flags = checked(take, noisy=True)[1]
        self.assertNotIn("too_long", rejects)
        self.assertIn("too_long", flags)

    def test_a_second_sound_is_flagged_not_merged_into_the_word(self):
        metrics, (rejects, flags) = checked(window(extra=(3.0, 3.3)))
        self.assertEqual(rejects, [])
        self.assertIn("extra_sounds", flags)
        self.assertAlmostEqual(metrics["word"][1], 2.1, delta=0.03)

    def test_a_short_gap_inside_the_word_does_not_split_it(self):
        take = window(word_at=(1.5, 1.8), extra=(1.95, 2.3))
        metrics, (_, flags) = checked(take)
        self.assertNotIn("extra_sounds", flags)
        self.assertEqual(len(metrics["segments"]), 1)

    def test_a_word_barely_above_the_room_is_flagged(self):
        _, (_, flags) = checked(window(level=250, floor=90), floor=90.0)
        self.assertIn("low_snr", flags)


class TranscriptTests(unittest.TestCase):
    def test_renderings_of_the_wake_word_match(self):
        for text in ("California.", "Califórnia!", "california?", "Cali fornia",
                     "Hey California", "Califórnia", "Kalifornia"):
            self.assertTrue(wd.transcript_matches(text), text)

    def test_other_words_do_not(self):
        for text in ("Carolina", "Thank you.", "", None, "cauliflower", "caroline"):
            self.assertFalse(wd.transcript_matches(text), text)


def row(take_id, session, split, status=wd.OK, h=None, source="session", **extra):
    return {
        "id": take_id, "session": session, "split": split, "status": status,
        "sha1": h or take_id, "source": source, "lang": "en", "distance": "couch",
        "background": "tv", "metrics": {"word": [1.0, 1.5]}, **extra,
    }


class LeakTests(unittest.TestCase):
    def test_a_clean_split_has_no_leaks(self):
        rows = [row("a/1", "a", "train"), row("b/1", "b", "holdout")]
        self.assertEqual(wd.leaks(rows), [])

    def test_a_session_on_both_sides_is_a_leak(self):
        rows = [row("a/1", "a", "train"), row("a/2", "a", "holdout")]
        self.assertTrue(any("session a" in p for p in wd.leaks(rows)))

    def test_the_same_audio_on_both_sides_is_a_leak(self):
        rows = [row("a/1", "a", "train", h="x"), row("b/1", "b", "holdout", h="x")]
        self.assertTrue(any("identical audio" in p for p in wd.leaks(rows)))

    def test_only_kept_takes_are_exportable(self):
        rows = [row("a/1", "a", "train", wd.OK), row("a/2", "a", "train", wd.ACCEPTED),
                row("a/3", "a", "train", wd.FLAGGED), row("a/4", "a", "train", wd.REJECTED),
                row("a/5", "a", "train", wd.DROPPED)]
        self.assertEqual([r["id"] for r in wd.exportable(rows)], ["a/1", "a/2"])

    def test_cells_without_a_holdout_are_reported(self):
        rows = [row("a/1", "a", "train"), row("b/1", "b", "train", lang="pt"),
                row("c/1", "c", "holdout")]
        self.assertEqual(wd.holdout_gaps(rows), ["pt-couch-tv"])

    def test_train_clips_are_cut_to_the_word_with_a_tight_pad(self):
        audio = np.arange(4 * SR, dtype=np.int16)
        clip = wd.train_clip(row("a/1", "a", "train"), audio)
        self.assertEqual(len(clip), int(0.5 * SR) + 2 * int(wd.EXPORT_PAD_S * SR))
        legacy = wd.train_clip(row("l/1", "l", "train", source="legacy_v1"), audio)
        self.assertEqual(len(legacy), len(audio))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(wd, "ROOT", root),
            mock.patch.object(wd, "MANIFEST_PATH", root / "manifest.jsonl"),
            mock.patch.object(wd, "SESSIONS_DIR", root / "sessions"),
            mock.patch.object(wd, "EXPORT_DIR", root / "export"),
        ]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _take(self, take_id, session, split, status=wd.OK, **extra):
        path = self.root / "sessions" / session / f"{take_id.split('/')[1]}.wav"
        audio = window(seed=zlib.crc32(take_id.encode()))
        wd.write_wav(path, audio)
        r = row(take_id, session, split, status, h=wd.sha1(audio), **extra)
        r["path"] = path.relative_to(self.root).as_posix()
        return r

    def _export(self, rows):
        m = wd.Manifest()
        m.rows = rows
        m.save()
        with mock.patch("builtins.print"):
            wd.cmd_export(argparse.Namespace(name="v3", force=False))
        return self.root / "export" / "v3"

    def test_export_writes_each_split_and_leaves_out_what_was_not_kept(self):
        wd.write_wav(self.root / "sessions" / "a" / "roomtone.wav", window(level=0))
        wd.write_wav(self.root / "sessions" / "b" / "roomtone.wav", window(level=0))
        out = self._export([
            self._take("a/001", "a", "train"),
            self._take("a/002", "a", "train", wd.FLAGGED),
            self._take("b/001", "b", "holdout"),
        ])
        self.assertEqual([p.name for p in (out / "train").glob("*.wav")], ["a__001.wav"])
        self.assertEqual(len(list((out / "holdout" / "en-couch-tv").glob("*.wav"))), 1)
        # Room tone from the train session only.
        self.assertEqual([p.name for p in (out / "backgrounds").glob("*.wav")], ["a.wav"])
        manifest = json.loads((out / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["train"], ["a/001"])

    def test_export_refuses_a_leak(self):
        rows = [self._take("a/001", "a", "train"), self._take("a/002", "a", "holdout")]
        with self.assertRaises(SystemExit):
            self._export(rows)
        self.assertFalse((self.root / "export" / "v3").exists())

    def test_export_will_not_overwrite_without_force(self):
        rows = [self._take("a/001", "a", "train")]
        self._export(rows)
        with self.assertRaises(SystemExit):
            self._export(rows)


class CaptureModeTests(unittest.TestCase):
    def test_push_to_talk_drops_both_key_clicks(self):
        audio = np.zeros(2 * SR, dtype=np.int16)
        audio[: int(0.2 * SR)] = 9000     # start key coming back up
        audio[-int(0.08 * SR):] = 9000    # stop key going down
        trimmed = wd.trim_key_clicks(audio)
        self.assertEqual(int(np.abs(trimmed).max()), 0)
        self.assertEqual(len(trimmed), 2 * SR - int(0.25 * SR) - int(0.10 * SR))

    def test_a_take_shorter_than_the_guards_is_empty_not_negative(self):
        self.assertEqual(len(wd.trim_key_clicks(np.ones(1000, dtype=np.int16))), 0)

    def _cut(self, audio, floor=30.0, chunk=640):
        cutter = wd.UtteranceCutter(floor, chunk)
        takes = []
        for i in range(0, len(audio) - chunk + 1, chunk):
            take = cutter.feed(audio[i:i + chunk])
            if take is not None:
                takes.append(take)
        return takes

    def test_hands_free_cuts_one_take_per_utterance_with_room_either_side(self):
        stream = np.concatenate([
            window(word_at=(1.0, 1.6), seconds=3.0, seed=1),
            window(word_at=(1.0, 1.5), seconds=3.0, seed=2),
        ])
        takes = self._cut(stream)
        self.assertEqual(len(takes), 2)
        for take in takes:
            _, (rejects, flags) = checked(take)
            self.assertEqual(rejects, [], "the word must not touch either edge")
            self.assertNotIn("extra_sounds", flags)

    def test_hands_free_ignores_a_single_loud_chunk(self):
        stream = window(level=0, seconds=3.0)
        stream[SR:SR + 640] = 9000
        self.assertEqual(self._cut(stream), [])

    def test_a_take_that_never_goes_quiet_is_closed_at_the_limit(self):
        stream = window(word_at=(0.5, 11.5), seconds=12.0)
        takes = self._cut(stream)
        self.assertEqual(len(takes), 1)
        self.assertLessEqual(len(takes[0]) / SR, wd.MAX_TAKE_S + 0.1)


class ManifestTests(unittest.TestCase):
    def test_rows_survive_a_round_trip_and_ids_stay_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.jsonl"
            m = wd.Manifest(path)
            m.add({"id": "a/1", "path": "x.wav", "note": "Califórnia"})
            self.assertEqual(wd.Manifest(path).rows[0]["note"], "Califórnia")
            with self.assertRaises(ValueError):
                m.add({"id": "a/1", "path": "y.wav"})


if __name__ == "__main__":
    unittest.main()
