"""
tools/score_wakeword.py: every file is scored inside a bed, deterministically,
and --negatives never counts a real wake as a false fire.

Until 2026-09-23 held-out takes were scored bare, straight after a reset, and
the loop dropped the last partial frame. That measured the reset instead of
the model: 18% recall bare against 82% in a bed, for the same 40 takes. These
tests pin the bed, not the model, so they use a stub detector and never load
openWakeWord or open a microphone.
"""

import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import score_wakeword as sw  # noqa: E402


class _StubModel:
    """Records every frame it is fed. Its reset draws from the global RNG, like openWakeWord's."""

    def __init__(self):
        self.fed = []
        self._state = 0.0

    def reset(self):
        self._state = float(np.random.random())

    def predict(self, frame):
        self.fed.append(np.array(frame, dtype=np.int16))
        return {"california": self._state + float(np.abs(frame).mean()) * 1e-6}


class _StubDetector:
    threshold = 0.81
    consecutive_required = 2

    def __init__(self):
        self._oww_model = _StubModel()
        self._dither_rng = np.random.default_rng()
        self._last_activation_time = 0.0
        self.chunks = []

    def _apply_dither(self, frame):
        return (frame + self._dither_rng.normal(0, 10, len(frame))).astype(np.int16)

    def reset(self):
        self._oww_model.reset()

    def process_audio(self, chunk):
        self.chunks.append(np.array(chunk, dtype=np.int16))
        return False


def _write(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio.astype(np.int16).tobytes())


class BedTests(unittest.TestCase):
    def setUp(self):
        self._saved = sw.BED
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        sw.BED = self._saved
        self.tmp.cleanup()

    def test_bed_adds_two_seconds_before_and_one_after(self):
        clip = np.full(11040, 1000, dtype=np.int16)
        out = sw.Bed().surround(clip, seed=1)
        self.assertEqual(len(out), 32000 + 11040 + 16000)
        np.testing.assert_array_equal(out[32000:32000 + 11040], clip)

    def test_no_pad_leaves_the_clip_bare(self):
        clip = np.arange(5000, dtype=np.int16)
        np.testing.assert_array_equal(sw.Bed(enabled=False).surround(clip, seed=1), clip)

    def test_gaussian_bed_is_at_the_requested_level(self):
        out = sw.Bed(rms=36.0).surround(np.zeros(0, dtype=np.int16), seed=3)
        self.assertAlmostEqual(float(np.sqrt(np.mean(out.astype(float) ** 2))), 36.0, delta=2.0)

    def test_room_tone_bed_is_sliced_from_the_recording(self):
        tone = np.arange(1, 4001, dtype=np.int16)
        out = sw.Bed(tone=tone).surround(np.zeros(0, dtype=np.int16), seed=5)
        self.assertTrue(set(np.unique(out)).issubset(set(tone.tolist())))

    def test_the_last_samples_of_the_word_reach_the_model(self):
        # 5 native frames plus 300 samples: bare, the last 300 were never scored.
        clip = np.full(1280 * 5 + 300, 7000, dtype=np.int16)
        clip[-300:] = 20000
        path = self.dir / "take.wav"
        _write(path, clip)
        sw.BED = sw.Bed(rms=0.0)
        det = _StubDetector()
        det._apply_dither = lambda frame: frame
        sw.score_wav(det, str(path))
        fed = np.concatenate(det._oww_model.fed)
        self.assertEqual(int((fed == 20000).sum()), 300)

    def test_framed_scoring_also_sees_the_bed(self):
        clip = np.full(1280 * 3, 7000, dtype=np.int16)
        path = self.dir / "take.wav"
        _write(path, clip)
        sw.BED = sw.Bed(rms=0.0)
        det = _StubDetector()
        sw.fires_framed(det, str(path))
        fed = np.concatenate(det.chunks)
        self.assertEqual(len(fed), 32000 + len(clip) + 16000)

    def test_a_file_scores_the_same_every_time(self):
        rng = np.random.default_rng(0)
        path = self.dir / "take.wav"
        _write(path, rng.normal(0, 2000, 12000))
        sw.BED = sw.Bed()
        det = _StubDetector()
        first = sw.score_wav(det, str(path))
        np.random.seed(999)  # disturb the global RNG between runs
        det._dither_rng = np.random.default_rng(12345)
        self.assertEqual(first, sw.score_wav(det, str(path)))

    def test_negatives_skip_captures_that_hold_a_real_wake(self):
        for name in ("a_ok.wav", "b_no_speech.wav", "c_ok.wav"):
            _write(self.dir / name, np.zeros(16000, dtype=np.int16))
        sw.BED = sw.Bed()
        rows = sw.run_negatives(_StubDetector(), str(self.dir))
        self.assertEqual([p.name for p, _, _ in rows], ["b_no_speech.wav"])

    def test_recall_mode_still_scores_every_file(self):
        for name in ("a_ok.wav", "b.wav"):
            _write(self.dir / name, np.zeros(16000, dtype=np.int16))
        rows = sw.score_dir(_StubDetector(), str(self.dir))
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
