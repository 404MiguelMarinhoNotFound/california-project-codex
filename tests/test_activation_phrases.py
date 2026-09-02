import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.orchestrator import Orchestrator, _chunk_rms
from services.activation_phrases import (
    COLD,
    WARM,
    EchoGate,
    resolve_tier,
    strip_activation_echo,
)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 640  # 40ms, matching config audio.chunk_duration_ms
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE


class ResolveTierTests(unittest.TestCase):
    def test_first_wake_of_the_run_is_cold(self):
        self.assertEqual(resolve_tier(0), COLD)

    def test_every_wake_after_the_first_is_warm(self):
        self.assertEqual(resolve_tier(1), WARM)
        self.assertEqual(resolve_tier(37), WARM)


class StripActivationEchoTests(unittest.TestCase):
    def test_strips_a_full_line_that_bled_into_the_transcript(self):
        self.assertEqual(
            strip_activation_echo("Ready when you are. Lights on", "Ready when you are."),
            "Lights on",
        )

    def test_strips_a_clipped_tail_of_the_line(self):
        # The audio trim usually removes the front of the line, so Whisper only
        # ever hears its last few words.
        self.assertEqual(
            strip_activation_echo("when you are lights on", "Ready when you are."),
            "lights on",
        )

    def test_leaves_a_single_word_overlap_alone(self):
        # "go home" is a real control_tv action. Stripping a one-word match would
        # turn it into "home".
        self.assertEqual(strip_activation_echo("go home", "Go."), "go home")

    def test_returns_empty_when_the_transcript_was_only_echo(self):
        self.assertEqual(
            strip_activation_echo("Missed you. Sort of.", "Missed you. Sort of."), ""
        )

    def test_leaves_an_unrelated_transcript_untouched(self):
        self.assertEqual(
            strip_activation_echo("turn on the lights", "Ready when you are."),
            "turn on the lights",
        )

    def test_preserves_casing_and_punctuation_of_what_it_keeps(self):
        self.assertEqual(
            strip_activation_echo("At your service, Master Miguel. Play Shrinking.",
                                  "At your service, Master Miguel."),
            "Play Shrinking.",
        )

    def test_handles_missing_text_without_a_manifest(self):
        self.assertEqual(strip_activation_echo("lights on", ""), "lights on")
        self.assertEqual(strip_activation_echo("", "Hey."), "")


class EchoGateTests(unittest.TestCase):
    def test_arms_when_the_line_finishes(self):
        gate = EchoGate(window_s=1.0, barge_in_rms=900)
        self.assertFalse(gate.update(0.5, rms=300))
        self.assertTrue(gate.update(1.0, rms=300))
        self.assertFalse(gate.barged_in)

    def test_arms_early_when_the_user_talks_over_the_line(self):
        gate = EchoGate(window_s=2.0, barge_in_rms=900)
        self.assertFalse(gate.update(0.2, rms=100))
        self.assertTrue(gate.update(0.5, rms=4000))
        self.assertTrue(gate.barged_in)

    def test_ignores_the_onset_of_the_line_itself(self):
        gate = EchoGate(window_s=2.0, barge_in_rms=900, onset_guard_s=0.15)
        self.assertFalse(gate.update(0.04, rms=9000))
        self.assertFalse(gate.barged_in)

    def test_blocking_playback_leaves_no_window_to_gate(self):
        gate = EchoGate(window_s=0.0, barge_in_rms=900)
        self.assertTrue(gate.armed)

    def test_stays_armed_once_armed(self):
        gate = EchoGate(window_s=1.0, barge_in_rms=900)
        gate.update(1.0, rms=0)
        self.assertTrue(gate.update(1.04, rms=0))


class ChunkRmsTests(unittest.TestCase):
    def test_empty_chunk_is_silent(self):
        self.assertEqual(_chunk_rms(np.array([], dtype=np.int16)), 0.0)

    def test_matches_rms_of_a_constant_chunk(self):
        self.assertAlmostEqual(_chunk_rms(np.full(64, 500, dtype=np.int16)), 500.0, places=3)


def _fake_orchestrator(loud_from=None, stop_after=3, stop_reason="silence",
                       min_recording_s=0.0):
    """
    An Orchestrator with only what _record_speech touches, built without running
    __init__ so the test needs no audio device, model or API key.

    `loud_from` is the chunk index from which the mic goes loud (the user
    talking); None means it stays at speaker-bleed level throughout.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch._barge_in_rms = 900.0
    orch._onset_guard_s = 0.15
    orch._barge_in_guard_s = 0.12
    # The trim tests deal in 3-6 chunks (120-240ms), so the real 0.6s floor
    # would drop every one of them. The floor itself is tested separately.
    orch._min_recording_s = min_recording_s
    orch._last_record_outcome = "ok"

    orch.audio = Mock()
    orch.audio.chunk_samples = CHUNK_SAMPLES
    orch.audio.sample_rate = SAMPLE_RATE
    orch.audio.bytes_to_numpy = lambda b: np.frombuffer(b, dtype=np.int16)
    # Real drain_mic_stream returns a frame count; a bare Mock would not divide.
    orch.audio.drain_mic_stream = Mock(return_value=0)

    counter = {"n": 0}

    def read(_samples):
        index = counter["n"]
        counter["n"] += 1
        loud = loud_from is not None and index >= loud_from
        level = 5000 if loud else 200  # 200 = bleed, below barge_in_rms
        return np.full(CHUNK_SAMPLES, level, dtype=np.int16).tobytes(), False

    mic = Mock()
    mic.read = read

    live = {"n": 0}

    def should_stop(_chunk):
        live["n"] += 1
        return (live["n"] >= stop_after, stop_reason)

    orch.vad = Mock()
    orch.vad.speech_timeout = 4.0
    orch.vad.should_stop_recording = should_stop
    return orch, mic, counter


class RecordSpeechTrimTests(unittest.TestCase):
    """
    The trim is the risky part: cut one chunk too many and the first phoneme of
    every command disappears.
    """

    def _run(self, orch, mic, playback):
        # One monotonic tick per chunk read, so elapsed time tracks chunk index.
        ticks = [i * CHUNK_SECONDS for i in range(500)]
        with patch("core.orchestrator.time.monotonic", side_effect=ticks):
            return orch._record_speech(mic, playback)

    def test_drops_the_whole_line_when_it_plays_out(self):
        playback = Mock(duration=0.4)  # 10 chunks
        orch, mic, _ = _fake_orchestrator(loud_from=None, stop_after=3)
        audio = self._run(orch, mic, playback)

        # Chunks 0-8 are pure bleed and go. Chunk 9 is the one that straddles the
        # end of the line, so it is kept: 40ms of her decaying tail costs Whisper
        # nothing, while dropping it would clip him if he started right on the
        # boundary. Then the 3 chunks the VAD saw. Chunks 9..12 = 4.
        self.assertEqual(len(audio), 4 * CHUNK_SAMPLES)
        orch.audio.stop_playback.assert_not_called()

    def test_keeps_speech_from_before_the_barge_in_chunk(self):
        playback = Mock(duration=2.0)  # 50 chunks, cut short by the user
        orch, mic, _ = _fake_orchestrator(loud_from=10, stop_after=2)
        audio = self._run(orch, mic, playback)

        # Armed on chunk 10; the 120ms guard backs the trim up 3 chunks to 7, so
        # the kept audio runs chunks 7..12 — his first phoneme survives.
        orch.audio.stop_playback.assert_called_once()
        self.assertEqual(len(audio), 6 * CHUNK_SAMPLES)
        self.assertTrue(np.all(audio[-CHUNK_SAMPLES:] == 5000))

    def test_the_barge_in_guard_never_reaches_back_past_the_start(self):
        # Talking over her almost immediately: the guard would back up past chunk
        # 0, which must clamp rather than wrap into a negative slice.
        playback = Mock(duration=2.0)
        orch, mic, _ = _fake_orchestrator(loud_from=3, stop_after=2)
        audio = self._run(orch, mic, playback)

        # Armed on chunk 3, guard of 3 chunks would land on 0 exactly. Nothing is
        # dropped, so the recording still opens on the quiet bleed chunk.
        self.assertEqual(len(audio), 6 * CHUNK_SAMPLES)  # chunks 0..5
        self.assertEqual(audio[0], 200)

    def test_blocking_playback_keeps_every_chunk(self):
        playback = Mock(duration=0.0)
        orch, mic, _ = _fake_orchestrator(loud_from=0, stop_after=3)
        audio = self._run(orch, mic, playback)
        self.assertEqual(len(audio), 3 * CHUNK_SAMPLES)

    def test_no_playback_at_all_records_normally(self):
        orch, mic, _ = _fake_orchestrator(loud_from=0, stop_after=3)
        audio = self._run(orch, mic, None)
        self.assertEqual(len(audio), 3 * CHUNK_SAMPLES)


class RecordSpeechDropsEmptyTurnsTests(unittest.TestCase):
    """
    A wake with nothing said after it must not reach Whisper. It used to: the
    VAD stopped on ~0.9s of room tone, Whisper hallucinated "Thank you." out of
    it, and California answered that.
    """

    def _run(self, orch, mic, playback=None):
        ticks = [i * CHUNK_SECONDS for i in range(500)]
        with patch("core.orchestrator.time.monotonic", side_effect=ticks):
            return orch._record_speech(mic, playback)

    def test_returns_none_when_nothing_was_said(self):
        orch, mic, _ = _fake_orchestrator(stop_after=3, stop_reason="no_speech")
        self.assertIsNone(self._run(orch, mic))
        self.assertEqual(orch._last_record_outcome, "no_speech")

    def test_returns_none_when_the_recording_is_shorter_than_min_recording(self):
        # 3 chunks = 120ms, well under a 0.6s floor.
        orch, mic, _ = _fake_orchestrator(stop_after=3, min_recording_s=0.6)
        self.assertIsNone(self._run(orch, mic))
        self.assertEqual(orch._last_record_outcome, "short")

    def test_keeps_a_recording_that_clears_the_floor(self):
        # 20 chunks = 800ms.
        orch, mic, _ = _fake_orchestrator(stop_after=20, min_recording_s=0.6)
        audio = self._run(orch, mic)
        self.assertEqual(len(audio), 20 * CHUNK_SAMPLES)
        self.assertEqual(orch._last_record_outcome, "ok")

    def test_a_max_duration_stop_is_still_kept(self):
        # Only "no_speech" drops the turn — a 30s monologue is still a command.
        orch, mic, _ = _fake_orchestrator(stop_after=20, stop_reason="max_duration")
        self.assertIsNotNone(self._run(orch, mic))


class HandleActivationAbortTests(unittest.TestCase):
    """A dropped turn must be silent and free: no STT, no LLM, no spoken reply."""

    def _orchestrator(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.audio = Mock()
        orch.stt = Mock()
        orch.leds = Mock()
        orch.wake_word = Mock()
        orch._wake_count = 3
        orch._capture_enabled = False
        orch._capture_ring = None
        orch._last_record_outcome = "no_speech"
        orch._stream_response = Mock()
        orch.audio.play_activation_sound.return_value = Mock(
            duration=0.0, name="warm_01", text="Sup."
        )
        return orch

    def test_no_speech_never_reaches_stt_or_the_llm(self):
        orch = self._orchestrator()
        with patch.object(Orchestrator, "_record_speech", return_value=None):
            orch._handle_activation(Mock())

        orch.stt.transcribe.assert_not_called()
        orch._stream_response.assert_not_called()
        orch.audio.play_error_sound.assert_not_called()

    def test_no_speech_resets_the_detector_and_returns_to_idle(self):
        orch = self._orchestrator()
        with patch.object(Orchestrator, "_record_speech", return_value=None):
            orch._handle_activation(Mock())

        orch.wake_word.reset.assert_called_once()
        orch.leds.set_state.assert_called_with("idle")

    def test_a_false_wake_does_not_burn_the_cold_open_line(self):
        # The long first-of-run line is worth exactly one use. A false positive
        # that consumed it would mean the real first wake gets a curt "Sup."
        orch = self._orchestrator()
        orch._wake_count = 0
        with patch.object(Orchestrator, "_record_speech", return_value=None):
            orch._handle_activation(Mock())
        self.assertEqual(orch._wake_count, 0)


if __name__ == "__main__":
    unittest.main()
