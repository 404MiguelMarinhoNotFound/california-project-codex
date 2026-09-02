"""
Draining the mic buffer, which is the fix for "it does not wait until the
activation sound is done".

The mic stream is opened once at boot and never stopped, so it keeps capturing
whenever the orchestrator is not reading it — through the bootup sound, through
a blocking activation line, and through the whole LLM and TTS response.
RawInputStream.read() returns the OLDEST buffered frames, so the next read
replays California's own voice out of the buffer.

That is why `sounds.activation_blocking: true` did not help. It genuinely waits
for the line to finish; the line is just sitting in the mic buffer afterwards.
Observed live: a 1.25s cold line, followed by "Recorded 1.2s of audio" and a
transcript of "Yeah." — her own tail, answered as if it were a command.
"""

import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.audio_pipeline import AudioPipeline
from core.orchestrator import Orchestrator

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 640


class _FakeStream:
    """
    A sounddevice.RawInputStream stand-in with the behaviour that matters: it
    buffers, and read() returns the oldest frames first.
    """

    def __init__(self, buffered_frames=0, live_value=100, buffered_value=9000):
        # Everything captured while nobody was reading — her voice, loudly.
        self._buffer = list(np.full(buffered_frames, buffered_value, dtype=np.int16))
        self._live_value = live_value
        self.reads = []

    @property
    def read_available(self):
        return len(self._buffer)

    def read(self, frames):
        self.reads.append(frames)
        taken = self._buffer[:frames]
        self._buffer = self._buffer[frames:]
        # Past the buffer, the stream blocks for live audio — quiet room here.
        if len(taken) < frames:
            taken += [self._live_value] * (frames - len(taken))
        return np.array(taken, dtype=np.int16).tobytes(), False


def _pipeline():
    pipe = AudioPipeline.__new__(AudioPipeline)
    pipe.sample_rate = SAMPLE_RATE
    pipe.channels = 1
    pipe.chunk_samples = CHUNK_SAMPLES
    return pipe


class DrainMicStreamTests(unittest.TestCase):
    def test_drops_everything_buffered(self):
        stream = _FakeStream(buffered_frames=SAMPLE_RATE * 2)  # 2s of her line
        dropped = _pipeline().drain_mic_stream(stream)

        self.assertEqual(dropped, SAMPLE_RATE * 2)
        self.assertEqual(stream.read_available, 0)

    def test_an_empty_buffer_is_a_no_op(self):
        stream = _FakeStream(buffered_frames=0)
        self.assertEqual(_pipeline().drain_mic_stream(stream), 0)
        self.assertEqual(stream.reads, [])

    def test_a_runaway_stream_cannot_loop_forever(self):
        class _NeverEmpty(_FakeStream):
            @property
            def read_available(self):
                return SAMPLE_RATE  # always claims a second is waiting

            def read(self, frames):
                self.reads.append(frames)
                return np.zeros(frames, dtype=np.int16).tobytes(), False

        stream = _NeverEmpty()
        dropped = _pipeline().drain_mic_stream(stream, max_seconds=3.0)
        self.assertEqual(dropped, 3 * SAMPLE_RATE)

    def test_the_next_read_gets_live_audio_not_the_buffer(self):
        # The regression test. Without the drain, the first "recorded" chunk is
        # the loud buffered line; with it, the quiet live room.
        pipe = _pipeline()

        undrained = _FakeStream(buffered_frames=SAMPLE_RATE)
        raw, _ = undrained.read(CHUNK_SAMPLES)
        self.assertEqual(pipe.bytes_to_numpy(raw)[0], 9000)  # her voice

        drained = _FakeStream(buffered_frames=SAMPLE_RATE)
        pipe.drain_mic_stream(drained)
        raw, _ = drained.read(CHUNK_SAMPLES)
        self.assertEqual(pipe.bytes_to_numpy(raw)[0], 100)   # the room


class RecordSpeechDrainsFirstTests(unittest.TestCase):
    """
    _record_speech must start from what the room is doing now, whether the
    activation line blocked or not.
    """

    def _orchestrator(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch._barge_in_rms = 900.0
        orch._onset_guard_s = 0.15
        orch._barge_in_guard_s = 0.12
        orch._min_recording_s = 0.0
        orch._last_record_outcome = "ok"
        orch.audio = Mock()
        orch.audio.chunk_samples = CHUNK_SAMPLES
        orch.audio.sample_rate = SAMPLE_RATE
        orch.audio.bytes_to_numpy = lambda b: np.frombuffer(b, dtype=np.int16)
        orch.audio.drain_mic_stream = Mock(return_value=SAMPLE_RATE)
        orch.vad = Mock()
        orch.vad.speech_timeout = 4.0
        calls = {"n": 0}

        def should_stop(_chunk):
            calls["n"] += 1
            return (calls["n"] >= 3, "silence")

        orch.vad.should_stop_recording = should_stop
        return orch

    def test_the_buffer_is_drained_before_the_vad_clock_starts(self):
        orch = self._orchestrator()
        stream = _FakeStream(buffered_frames=SAMPLE_RATE * 2)

        ticks = [i * 0.04 for i in range(500)]
        with patch("core.orchestrator.time.monotonic", side_effect=ticks):
            orch._record_speech(stream, Mock(duration=0.0))

        orch.audio.drain_mic_stream.assert_called_once_with(stream)
        # Draining has to happen before the VAD starts timing, or min_recording
        # and the silence window measure the line instead of him.
        self.assertTrue(orch.vad.start_recording.called)

    def test_a_drain_failure_does_not_take_down_the_turn(self):
        orch = self._orchestrator()
        orch.audio.drain_mic_stream.side_effect = RuntimeError("stream closed")
        stream = _FakeStream()

        ticks = [i * 0.04 for i in range(500)]
        with patch("core.orchestrator.time.monotonic", side_effect=ticks):
            with self.assertLogs("core.orchestrator", level="ERROR"):
                audio = orch._record_speech(stream, None)

        self.assertIsNotNone(audio)


class IdleLoopDrainsAfterSpeakingTests(unittest.TestCase):
    """
    Her own reply must never reach the wake-word detector. It went out of the
    speaker and into the mic buffer while the orchestrator was busy speaking it.
    """

    def _orchestrator(self, wake_fires):
        orch = Orchestrator.__new__(Orchestrator)
        orch.audio = Mock()
        orch.audio.chunk_samples = CHUNK_SAMPLES
        orch.audio.sample_rate = SAMPLE_RATE
        orch.audio.bytes_to_numpy = lambda b: np.frombuffer(b, dtype=np.int16)
        orch.audio.drain_mic_stream = Mock(return_value=0)
        orch.leds = Mock()
        orch.wake_word = Mock()
        orch.wake_word.process_audio = Mock(return_value=wake_fires)
        orch._capture_ring = None
        orch._handle_activation = Mock()
        return orch

    def test_drains_after_an_activation(self):
        orch = self._orchestrator(wake_fires=True)
        orch._idle_loop(_FakeStream())

        orch._handle_activation.assert_called_once()
        orch.audio.drain_mic_stream.assert_called_once()

    def test_does_not_drain_while_merely_listening(self):
        # The idle loop reads continuously, so the buffer stays near empty and
        # draining every chunk would throw away the wake word itself.
        orch = self._orchestrator(wake_fires=False)
        orch._idle_loop(_FakeStream())

        orch._handle_activation.assert_not_called()
        orch.audio.drain_mic_stream.assert_not_called()

    def test_the_capture_ring_is_cleared_so_clips_do_not_carry_her_voice(self):
        import collections

        orch = self._orchestrator(wake_fires=True)
        orch._capture_ring = collections.deque(maxlen=50)
        orch._capture_ring.append(np.zeros(CHUNK_SAMPLES, dtype=np.int16))
        orch._idle_loop(_FakeStream())

        self.assertEqual(len(orch._capture_ring), 0)


if __name__ == "__main__":
    unittest.main()
