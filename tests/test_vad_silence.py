"""
The pre-speech grace window, which is the fix for "she takes my silence as input".

Before this, `min_recording` was wall-clock elapsed since start_recording(), so
silence counted toward it: 0.9s of room tone stopped the recording, went to
Whisper, came back as "Thank you." and got answered. The recording now has two
phases — waiting for him to start, then endpointing what he said — and the
silence timer only runs in the second one.
"""

import sys
import unittest
from unittest.mock import patch

import numpy as np

from core.vad import SILERO_WINDOW_16K, VAD

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 640  # 40ms, matching config audio.chunk_duration_ms

QUIET = np.full(CHUNK_SAMPLES, 50, dtype=np.int16)     # room tone, under threshold
LOUD = np.full(CHUNK_SAMPLES, 5000, dtype=np.int16)    # him talking


def _vad(**overrides):
    """A VAD on the energy engine, so no model loads and no torch is needed."""
    cfg = {
        "engine": "energy",
        "energy_threshold": 200,
        "silence_duration": 0.9,
        "max_recording": 30.0,
        "min_recording": 0.6,
        "speech_timeout": 4.0,
        "speech_start_frames": 2,
    }
    cfg.update(overrides)
    return VAD({"vad": cfg, "audio": {"sample_rate": SAMPLE_RATE}})


class _Clock:
    """A fake time.time() the test drives explicitly."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class GraceWindowTests(unittest.TestCase):
    def test_silence_alone_never_stops_as_a_command(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad()
            vad.start_recording()

            # Well past silence_duration (0.9s) and min_recording (0.6s), which
            # is exactly where the old code stopped and shipped room tone.
            clock.now += 2.0
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))

    def test_stops_with_no_speech_once_the_grace_window_expires(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad(speech_timeout=4.0)
            vad.start_recording()

            clock.now += 3.9
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))
            clock.now += 0.2
            self.assertEqual(vad.should_stop_recording(QUIET), (True, "no_speech"))

    def test_a_late_start_still_gets_a_full_turn(self):
        # Call her, think for three seconds, then speak. This is the bug report.
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad()
            vad.start_recording()

            clock.now += 3.0
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))

            # He starts talking. speech_start_frames=2, so it takes two chunks.
            vad.should_stop_recording(LOUD)
            self.assertFalse(vad.saw_speech)
            vad.should_stop_recording(LOUD)
            self.assertTrue(vad.saw_speech)

            # A full second of him talking, then he stops.
            clock.now += 1.0
            vad.should_stop_recording(LOUD)

            # The silence clock starts here, not at the top of the recording,
            # so it is a full silence_duration (0.9s) from now.
            clock.now += 0.1
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))
            clock.now += 0.5
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))
            clock.now += 0.5
            self.assertEqual(vad.should_stop_recording(QUIET), (True, "silence"))

    def test_min_recording_measures_speech_not_wall_clock(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad(min_recording=0.6, silence_duration=0.2)
            vad.start_recording()

            clock.now += 5.0  # a long pause before he says anything
            vad.should_stop_recording(LOUD)
            vad.should_stop_recording(LOUD)  # armed here

            # Only 0.3s of speech so far: under min_recording, so a long silence
            # must not end the turn yet even though 5s have elapsed overall.
            clock.now += 0.3
            self.assertEqual(vad.should_stop_recording(QUIET), (False, "continue"))
            clock.now += 0.5
            self.assertEqual(vad.should_stop_recording(QUIET), (True, "silence"))

    def test_a_single_loud_chunk_does_not_count_as_starting_to_talk(self):
        # A door, a keystroke, a chair. speech_start_frames=2 filters these.
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad(speech_start_frames=2)
            vad.start_recording()

            vad.should_stop_recording(LOUD)
            vad.should_stop_recording(QUIET)   # run broken
            vad.should_stop_recording(LOUD)
            self.assertFalse(vad.saw_speech)

    def test_max_duration_still_preempts_everything(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad(max_recording=30.0, speech_timeout=4.0)
            vad.start_recording()
            clock.now += 31.0
            self.assertEqual(vad.should_stop_recording(LOUD), (True, "max_duration"))

    def test_start_recording_clears_speech_state_between_turns(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad()
            vad.start_recording()
            vad.should_stop_recording(LOUD)
            vad.should_stop_recording(LOUD)
            self.assertTrue(vad.saw_speech)

            vad.start_recording()
            self.assertFalse(vad.saw_speech)

    def test_reset_clears_speech_state(self):
        clock = _Clock()
        with patch("core.vad.time.time", clock):
            vad = _vad()
            vad.start_recording()
            vad.should_stop_recording(LOUD)
            vad.should_stop_recording(LOUD)
            vad.reset()
            self.assertFalse(vad.saw_speech)


class ConfigCompatibilityTests(unittest.TestCase):
    def test_a_config_without_the_new_keys_still_constructs(self):
        vad = VAD({
            "vad": {
                "engine": "energy",
                "energy_threshold": 200,
                "silence_duration": 0.9,
                "max_recording": 30.0,
                "min_recording": 0.6,
            },
            "audio": {"sample_rate": SAMPLE_RATE},
        })
        self.assertEqual(vad.speech_timeout, 4.0)
        self.assertEqual(vad.speech_start_frames, 2)


class _FakeSileroModel:
    """Records the window sizes it is handed, the way real Silero would police them."""

    def __init__(self, confidence=0.9, raises=False):
        self.calls = []
        self.confidence = confidence
        self.raises = raises

    def __call__(self, tensor, sample_rate):
        self.calls.append(len(tensor))
        if self.raises:
            raise RuntimeError("Provided number of samples is 640")
        return _FakeScalar(self.confidence)

    def reset_states(self):
        pass


class _FakeScalar:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class SileroFramingTests(unittest.TestCase):
    """
    Silero rejects any window that is not exactly 512 samples at 16kHz, and the
    pipeline feeds 640. Unframed, the first activation after `uv sync --extra
    silero` would raise straight through should_stop_recording into run().
    """

    def setUp(self):
        # torch only ships in the `silero` extra, which `default` does not
        # install, so stand in for it: _silero_detect only needs from_numpy.
        fake_torch = type("torch", (), {"from_numpy": staticmethod(lambda a: a)})
        self._torch_patch = patch.dict(sys.modules, {"torch": fake_torch})
        self._torch_patch.start()
        self.addCleanup(self._torch_patch.stop)

    def _vad_with(self, model):
        vad = _vad()
        vad.engine = "silero"
        vad._silero_model = model
        return vad

    def test_every_call_gets_exactly_one_silero_window(self):
        model = _FakeSileroModel()
        vad = self._vad_with(model)
        vad.start_recording()

        for _ in range(4):  # 4 x 640 = 2560 samples = 5 x 512
            vad.is_speech(LOUD)

        self.assertEqual(len(model.calls), 5)
        self.assertTrue(all(n == SILERO_WINDOW_16K for n in model.calls))

    def test_a_partial_window_is_carried_into_the_next_chunk(self):
        model = _FakeSileroModel()
        vad = self._vad_with(model)
        vad.start_recording()

        vad.is_speech(LOUD)               # 640 -> one 512 window, 128 left over
        self.assertEqual(len(model.calls), 1)
        vad.is_speech(LOUD)               # 128 + 640 = 768 -> one more window
        self.assertEqual(len(model.calls), 2)

    def test_a_failing_model_falls_back_to_energy_instead_of_crashing(self):
        model = _FakeSileroModel(raises=True)
        vad = self._vad_with(model)
        vad.start_recording()

        with self.assertLogs("core.vad", level="ERROR"):
            self.assertTrue(vad.is_speech(LOUD))  # answered by the energy detector
        self.assertEqual(vad.engine, "energy")
        self.assertFalse(vad.is_speech(QUIET))


if __name__ == "__main__":
    unittest.main()
