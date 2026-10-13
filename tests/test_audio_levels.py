"""
Loudness for the live UI's halo: one 0..1 scale for her voice and the mic.

Pure numpy, no audio device. The scale is dBFS mapped between a floor and a
ceiling, because speech RMS spans ~30 dB and a linear RMS would leave every
quiet syllable at zero.
"""

import json
import math
import time
import unittest

import numpy as np

from core.audio_levels import DB_CEIL, DB_FLOOR, envelope, level


def _tone(seconds, sr=24000, dbfs=0.0, freq=220.0):
    t = np.arange(int(seconds * sr)) / sr
    # A sine's RMS is peak / sqrt(2); scale so the RMS sits at `dbfs`.
    amp = (10 ** (dbfs / 20.0)) * math.sqrt(2)
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


class EnvelopeTests(unittest.TestCase):
    def test_silence_is_all_zeros(self):
        env = envelope(np.zeros(24000, dtype=np.float32), 24000)
        self.assertEqual(len(env), 25)
        self.assertTrue(all(v == 0.0 for v in env))

    def test_full_scale_tone_is_near_one(self):
        env = envelope(_tone(1.0, dbfs=-3.0), 24000)
        self.assertTrue(all(v >= 0.95 for v in env), env)

    def test_never_exceeds_one_even_when_clipping(self):
        loud = np.full(24000, 4.0, dtype=np.float32)  # far past full scale
        env = envelope(loud, 24000)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in env))
        self.assertEqual(max(env), 1.0)

    def test_length_is_duration_times_hz_rounded_up(self):
        for samples, hz in ((24000, 25), (24001, 25), (12345, 25), (1, 25), (24000, 10)):
            env = envelope(np.zeros(samples, dtype=np.float32), 24000, hz=hz)
            self.assertEqual(len(env), math.ceil(samples / 24000 * hz), (samples, hz))

    def test_int16_and_float32_agree(self):
        f = _tone(0.5, dbfs=-30.0)
        i = np.round(f * 32767).astype(np.int16)
        a, b = envelope(f, 24000), envelope(i, 24000)
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, delta=0.02)

    def test_stereo_is_mixed_to_mono(self):
        mono = _tone(0.4, dbfs=-20.0)
        stereo = np.stack([mono, mono], axis=1)
        self.assertEqual(envelope(stereo, 24000), envelope(mono, 24000))

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(envelope(np.zeros(0, dtype=np.float32), 24000), [])
        self.assertEqual(envelope(None, 24000), [])
        self.assertEqual(envelope(np.zeros(10, dtype=np.float32), 0), [])

    def test_quiet_speech_is_visible(self):
        env = envelope(_tone(0.4, dbfs=-40.0), 24000)
        self.assertTrue(all(v > 0.3 for v in env), env)

    def test_ten_second_clip_is_fast(self):
        clip = _tone(10.0, dbfs=-20.0)
        envelope(clip, 24000)  # warm numpy
        start = time.perf_counter()
        envelope(clip, 24000)
        self.assertLess(time.perf_counter() - start, 0.005)

    def test_values_are_rounded_and_a_sentence_payload_is_small(self):
        env = envelope(_tone(4.0, dbfs=-25.0), 24000)
        self.assertTrue(all(round(v, 2) == v for v in env))
        self.assertLess(len(json.dumps({"env": env})), 1024)


class LevelTests(unittest.TestCase):
    def test_level_matches_envelope_scale(self):
        chunk = np.round(_tone(0.04, sr=16000, dbfs=-32.0) * 32767).astype(np.int16)
        env = envelope(chunk, 16000, hz=25)
        self.assertEqual(len(env), 1)
        self.assertAlmostEqual(level(chunk), env[0], delta=0.01)

    def test_floor_and_ceiling_bound_the_scale(self):
        self.assertEqual(level(_tone(0.04, sr=16000, dbfs=DB_FLOOR - 10)), 0.0)
        self.assertEqual(level(_tone(0.04, sr=16000, dbfs=DB_CEIL + 5)), 1.0)

    def test_level_is_fast_and_never_raises(self):
        chunk = np.zeros(640, dtype=np.int16)
        start = time.perf_counter()
        for _ in range(100):
            level(chunk)
        self.assertLess((time.perf_counter() - start) / 100, 0.0001)
        self.assertEqual(level(None), 0.0)
        self.assertEqual(level(np.zeros(0, dtype=np.int16)), 0.0)


if __name__ == "__main__":
    unittest.main()
