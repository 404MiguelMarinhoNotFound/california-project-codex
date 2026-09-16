"""
Interrupting a reply with the wake word.

Nothing used to read the microphone while California was talking: the main
thread was inside the LLM stream, the mic buffer filled, and `_idle_loop` threw
it away afterwards. `_interrupted` was assigned `False` in two places and
`True` nowhere, so the three barge-in guards were dead code. And the 30s joins
on the TTS threads abandoned any reply with more than 30s of speech still
queued, which is how a long answer got cut mid-word by the next activation.

Now `_stream_response` runs a listener thread over the mic for the length of
the reply, scoring it with the wake-word detector. A hit stops the speaker,
flags the turn, and the idle loop chains straight into a new activation.

Everything here is faked: no sounddevice, no model, no TTS provider.
"""

import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.orchestrator import Orchestrator

CHUNK = 640


class _FakeMic:
    """Yields quiet chunks forever; each read takes a little real time."""

    def __init__(self, delay=0.002):
        self.reads = 0
        self._delay = delay

    def read(self, frames):
        self.reads += 1
        time.sleep(self._delay)
        return np.zeros(frames, dtype=np.int16).tobytes(), False


class _FakeWake:
    """Fires on the Nth chunk it is given, then never again."""

    def __init__(self, fire_on: int | None):
        self.fire_on = fire_on
        self.calls = 0
        self.thresholds = []

    def process_audio(self, chunk, threshold=None):
        self.calls += 1
        self.thresholds.append(threshold)
        return self.fire_on is not None and self.calls == self.fire_on

    def reset(self):
        pass


class _FakeAudio:
    """Plays by sleeping; stop_playback cuts the current clip short."""

    def __init__(self):
        self.sample_rate = 16000
        self.chunk_samples = CHUNK
        self.played: list[str] = []
        self.stopped = threading.Event()
        self.stop_calls = 0
        self.speaker_opens = 0
        self.speaker_closes = 0
        self.resets = 0

    def bytes_to_numpy(self, b):
        return np.frombuffer(b, dtype=np.int16)

    def numpy_to_wav_bytes(self, audio):
        return audio.tobytes()

    def play_audio(self, audio, sr, blocking=True):
        # `audio` is the sentence encoded as bytes by the fake TTS.
        text = audio.tobytes().decode()
        self.played.append(text)
        # ~50ms per sentence unless stopped.
        self.stopped.wait(0.05)

    def stop_playback(self):
        self.stop_calls += 1
        self.stopped.set()

    def reset_playback(self):
        self.resets += 1
        self.stopped.clear()

    def open_speaker(self, sr=24000):
        self.speaker_opens += 1

    def close_speaker(self):
        self.speaker_closes += 1

    def drain_mic_stream(self, mic, max_seconds=30.0):
        return 0


class _FakeLLM:
    """Streams sentences slowly enough that a barge-in lands mid-reply."""

    def __init__(self, sentences, delay=0.03):
        self.sentences = sentences
        self.delay = delay
        self.closed = False
        self.yielded = 0

    def stream_response(self, text):
        try:
            for s in self.sentences:
                time.sleep(self.delay)
                self.yielded += 1
                yield s + " "
        except GeneratorExit:
            self.closed = True
            raise


def _tts_synthesize(sentence):
    return np.frombuffer(sentence.encode(), dtype=np.uint8), 24000


def _orchestrator(fire_on, sentences, barge_in_threshold=0.9):
    orch = Orchestrator.__new__(Orchestrator)
    orch.audio = _FakeAudio()
    orch.leds = Mock()
    orch.wake_word = _FakeWake(fire_on)
    orch.llm = _FakeLLM(sentences)
    orch.tts = Mock()
    orch.tts.synthesize = _tts_synthesize
    orch._interrupted = threading.Event()
    orch._speaking_text = ""
    orch._active_tts_queue = None
    orch._barge_in_threshold = barge_in_threshold
    orch._capture_ring = None
    orch._capture_enabled = False
    return orch


# Four full sentences so the chunker ships them one per call.
SENTENCES = [
    "The first sentence is long enough to be a chunk on its own, honestly.",
    "The second sentence is also long enough to stand alone as a chunk here.",
    "The third one keeps going for a while so it also clears the minimum length.",
    "And the fourth wraps it up with enough words to be its own chunk as well.",
]


class ReplyBargeInTests(unittest.TestCase):
    def test_a_full_reply_is_spoken_to_the_end_and_not_interrupted(self):
        orch = _orchestrator(fire_on=None, sentences=SENTENCES)
        mic = _FakeMic()

        barged = orch._stream_response("hi", mic)

        self.assertFalse(barged)
        # The chunker may travel short sentences together; every word plays.
        spoken = " ".join(orch.audio.played)
        for sentence in SENTENCES:
            self.assertIn(sentence, spoken)
        self.assertEqual(orch.audio.stop_calls, 0)
        self.assertFalse(orch.llm.closed)
        # The listener was reading the mic the whole time.
        self.assertGreater(mic.reads, 5)

    def test_the_wake_word_mid_reply_stops_her_and_ends_the_turn(self):
        orch = _orchestrator(fire_on=8, sentences=SENTENCES)
        mic = _FakeMic()

        barged = orch._stream_response("hi", mic)

        self.assertTrue(barged)
        self.assertEqual(orch.audio.stop_calls, 1)
        self.assertNotIn(SENTENCES[-1], " ".join(orch.audio.played))
        self.assertTrue(orch.llm.closed, "the LLM stream must be closed, not run out")
        self.assertIsNone(orch._active_tts_queue)
        # No stray reply-listener thread survives the turn.
        self.assertFalse(any(t.name == "reply-listener" for t in threading.enumerate()))

    def test_the_listener_scores_with_the_barge_in_threshold(self):
        orch = _orchestrator(fire_on=None, sentences=SENTENCES[:1], barge_in_threshold=0.93)
        orch._stream_response("hi", _FakeMic())
        self.assertTrue(orch.wake_word.thresholds)
        self.assertTrue(all(t == 0.93 for t in orch.wake_word.thresholds))

    def test_she_does_not_wake_herself_by_saying_her_name(self):
        """A hit while the playing chunk contains her name is ignored."""
        sentences = ["I'm California, nice to meet you, and this line is long enough."]
        orch = _orchestrator(fire_on=None, sentences=sentences)
        # Fire the moment the player is actually saying something, whatever
        # chunk index that turns out to be.
        fired = []

        def fire_while_speaking(chunk, threshold=None):
            if orch._speaking_text and not fired:
                fired.append(orch._speaking_text)
                return True
            return False

        orch.wake_word.process_audio = fire_while_speaking
        barged = orch._stream_response("hi", _FakeMic())

        self.assertTrue(fired, "the detector never scored during playback")
        self.assertIn("California", fired[0])
        self.assertFalse(barged)
        self.assertEqual(orch.audio.stop_calls, 0)

    def test_a_hit_while_she_says_anything_else_does_interrupt(self):
        orch = _orchestrator(fire_on=None, sentences=SENTENCES)
        fired = []

        def fire_while_speaking(chunk, threshold=None):
            if orch._speaking_text and not fired:
                fired.append(orch._speaking_text)
                return True
            return False

        orch.wake_word.process_audio = fire_while_speaking
        barged = orch._stream_response("hi", _FakeMic())

        self.assertTrue(fired)
        self.assertTrue(barged)
        self.assertEqual(orch.audio.stop_calls, 1)

    def test_no_mic_means_no_listener_and_a_normal_reply(self):
        orch = _orchestrator(fire_on=1, sentences=SENTENCES[:2])
        barged = orch._stream_response("hi", None)
        self.assertFalse(barged)
        self.assertEqual(orch.wake_word.calls, 0)
        self.assertIn(SENTENCES[1], " ".join(orch.audio.played))

    def test_a_listener_error_does_not_take_the_turn_down(self):
        orch = _orchestrator(fire_on=None, sentences=SENTENCES[:1])
        orch.wake_word.process_audio = Mock(side_effect=RuntimeError("model died"))
        barged = orch._stream_response("hi", _FakeMic())
        self.assertFalse(barged)
        self.assertIn(SENTENCES[0], " ".join(orch.audio.played))


class IdleLoopChainingTests(unittest.TestCase):
    """A barge-in is a new activation, not a return to idle."""

    def _orch(self):
        orch = _orchestrator(fire_on=1, sentences=[])
        orch._capture_ring = None
        return orch

    def test_a_barged_in_turn_chains_into_another_activation(self):
        orch = self._orch()
        outcomes = iter([True, False])
        calls = []

        def fake_activation(mic, pre_roll=None):
            calls.append(pre_roll)
            return next(outcomes)

        orch._handle_activation = fake_activation
        orch._idle_loop(_FakeMic())

        self.assertEqual(len(calls), 2)
        self.assertEqual(orch.audio.speaker_opens, 1, "one session for the whole exchange")
        self.assertEqual(orch.audio.speaker_closes, 1)

    def test_the_speaker_is_closed_even_when_the_turn_raises(self):
        orch = self._orch()
        orch._handle_activation = Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            orch._idle_loop(_FakeMic())
        self.assertEqual(orch.audio.speaker_closes, 1)

    def test_a_quiet_turn_does_not_chain(self):
        orch = self._orch()
        orch._handle_activation = Mock(return_value=False)
        orch._idle_loop(_FakeMic())
        self.assertEqual(orch._handle_activation.call_count, 1)


class HandleActivationReturnTests(unittest.TestCase):
    """Every exit path reports whether to chain, and arms the speaker first."""

    def _orch(self):
        orch = _orchestrator(fire_on=None, sentences=[])
        orch.stt = Mock()
        orch._wake_count = 3
        orch._last_record_outcome = "no_speech"
        orch.audio.play_activation_sound = Mock(
            return_value=Mock(duration=0.0, name="warm", text="Sup.")
        )
        return orch

    def test_no_speech_returns_false_after_arming_the_speaker(self):
        orch = self._orch()
        with patch.object(Orchestrator, "_record_speech", return_value=None):
            self.assertFalse(orch._handle_activation(Mock()))
        self.assertEqual(orch.audio.resets, 1)

    def test_a_reply_returns_what_stream_response_said(self):
        orch = self._orch()
        orch.stt.transcribe.return_value = "what time is it"
        orch._stream_response = Mock(return_value=True)
        mic = Mock()
        with patch.object(
            Orchestrator, "_record_speech", return_value=np.ones(16000, dtype=np.int16)
        ):
            self.assertTrue(orch._handle_activation(mic))
        orch._stream_response.assert_called_once_with("what time is it", mic)

    def test_the_stop_command_sets_the_interrupt(self):
        orch = self._orch()
        self.assertTrue(orch._handle_command("stop"))
        self.assertTrue(orch._interrupted.is_set())
        self.assertEqual(orch.audio.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
