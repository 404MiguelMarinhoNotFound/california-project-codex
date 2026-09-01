"""
openWakeWord framing, which is the fix for "she activates when I don't even speak".

`consecutive_frames: 2` was meant to require two sustained detections before
waking. It required one. config.yaml feeds 40ms/640-sample chunks, but
openWakeWord only computes a new score every 1280 samples: on a short chunk
Model.predict takes its `n_prepared_samples < 1280` branch and returns the
PREVIOUS prediction verbatim. So the score stream was 0, S1, S1, S2, S2, ...
and one real detection always produced two identical frames over threshold.

core/wake_word.py now buffers to 1280 before calling predict, so a counted
frame is one inference and the knob means what it says.
"""

import unittest
from unittest.mock import patch

import numpy as np

from core.wake_word import WakeWordDetector

NATIVE_FRAME = 1280  # openWakeWord's step
CHUNK_SAMPLES = 640  # what the orchestrator actually reads per loop


class _FakeOWW:
    """
    Stands in for openwakeword.model.Model, and polices the contract that
    matters: it refuses anything that is not exactly one native frame.
    """

    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []
        self.resets = 0

    def predict(self, chunk):
        assert len(chunk) == NATIVE_FRAME, (
            f"openWakeWord was handed {len(chunk)} samples, not {NATIVE_FRAME} — "
            "it would return a stale duplicate score"
        )
        self.calls.append(len(chunk))
        score = self.scores.pop(0) if self.scores else 0.0
        return {"california_v2": score}

    def reset(self):
        self.resets += 1


def _detector(scores, threshold=0.81, consecutive=2, debounce=1.2):
    """A detector with no model file, audio device or onnxruntime involved."""
    det = WakeWordDetector.__new__(WakeWordDetector)
    det._backend = "oww"
    det._enabled = True
    det.threshold = threshold
    det.consecutive_required = consecutive
    det.debounce_seconds = debounce
    det._consecutive_count = 0
    det._last_activation_time = 0.0
    det.primary_key = "california_v2"
    det._oww_frame_length = NATIVE_FRAME
    det._oww_buffer = np.array([], dtype=np.int16)
    det._oww_model = _FakeOWW(scores)
    return det


def _chunk(value=1000):
    return np.full(CHUNK_SAMPLES, value, dtype=np.int16)


class NativeFramingTests(unittest.TestCase):
    def test_two_pipeline_chunks_make_exactly_one_inference(self):
        det = _detector([0.0] * 10)
        det.process_audio(_chunk())
        self.assertEqual(det._oww_model.calls, [])  # 640 buffered, nothing scored
        det.process_audio(_chunk())
        self.assertEqual(det._oww_model.calls, [NATIVE_FRAME])

    def test_a_remainder_carries_across_calls(self):
        det = _detector([0.0] * 10)
        for _ in range(10):  # 10 x 640 = 6400 = 5 native frames exactly
            det.process_audio(_chunk())
        self.assertEqual(len(det._oww_model.calls), 5)

    def test_odd_chunk_sizes_are_buffered_not_dropped(self):
        det = _detector([0.0] * 10)
        odd = np.full(300, 1000, dtype=np.int16)
        for _ in range(9):  # 2700 samples = 2 native frames, 140 left over
            det.process_audio(odd)
        self.assertEqual(len(det._oww_model.calls), 2)
        self.assertEqual(len(det._oww_buffer), 2700 - 2 * NATIVE_FRAME)


class ConsecutiveFrameTests(unittest.TestCase):
    """
    The regression tests for the false-positive bug. Under the old code every
    one of these single-spike cases woke her up.
    """

    def _feed(self, det, count):
        """count native frames' worth of audio, in the pipeline's chunk size."""
        fired = False
        for _ in range(count * 2):
            fired = det.process_audio(_chunk()) or fired
        return fired

    def test_one_high_frame_does_not_fire(self):
        det = _detector([0.95, 0.10, 0.05])
        self.assertFalse(self._feed(det, 3))

    def test_two_consecutive_high_frames_fire(self):
        det = _detector([0.95, 0.93, 0.05])
        self.assertTrue(self._feed(det, 3))

    def test_a_gap_between_high_frames_does_not_fire(self):
        # high, low, high: a flicker, not a spoken word.
        det = _detector([0.95, 0.10, 0.93])
        self.assertFalse(self._feed(det, 3))

    def test_three_frames_can_be_required_instead(self):
        det = _detector([0.95, 0.93, 0.91], consecutive=3)
        self.assertTrue(self._feed(det, 3))

        det = _detector([0.95, 0.93, 0.10], consecutive=3)
        self.assertFalse(self._feed(det, 3))

    def test_a_score_exactly_at_the_threshold_counts(self):
        det = _detector([0.81, 0.81])
        self.assertTrue(self._feed(det, 2))


class DetectionSideEffectTests(unittest.TestCase):
    def test_detection_clears_the_buffer_and_resets_the_model(self):
        det = _detector([0.95, 0.93])
        det.process_audio(np.full(NATIVE_FRAME * 2 + 500, 1000, dtype=np.int16))

        # Whatever was left over is the start of his command, not wake word.
        self.assertEqual(len(det._oww_buffer), 0)
        self.assertEqual(det._oww_model.resets, 1)
        self.assertEqual(det._consecutive_count, 0)

    def test_debounce_still_suppresses_an_immediate_second_fire(self):
        det = _detector([0.95, 0.93, 0.95, 0.93], debounce=1.2)
        with patch("core.wake_word.time.time", side_effect=[1000.0, 1000.5]):
            self.assertTrue(
                det.process_audio(np.full(NATIVE_FRAME * 2, 1000, dtype=np.int16))
            )
            self.assertFalse(
                det.process_audio(np.full(NATIVE_FRAME * 2, 1000, dtype=np.int16))
            )

    def test_reset_clears_the_buffer(self):
        det = _detector([0.0])
        det.process_audio(_chunk())
        self.assertEqual(len(det._oww_buffer), CHUNK_SAMPLES)
        det.reset()
        self.assertEqual(len(det._oww_buffer), 0)

    def test_disable_short_circuits_before_the_buffer_is_touched(self):
        det = _detector([0.95, 0.93])
        det.disable()
        det.process_audio(_chunk())
        det.process_audio(_chunk())
        self.assertEqual(det._oww_model.calls, [])
        self.assertEqual(len(det._oww_buffer), 0)


if __name__ == "__main__":
    unittest.main()
