"""
The per-turn speaker session.

`sd.play` opened and closed a fresh PortAudio stream per clip. On the dev
laptop (MME, `latency='high'`) that is ~180ms of primed silence plus the device
open in front of every sentence, and in front of an activation clip trimmed to
a 20ms lead-in — the missing first syllable of "Sup.". `AudioPipeline` now opens
one `OutputStream` at the wake and holds it for the whole exchange, writing
clips into it in small blocks so a stop lands within one block.

Everything here runs against a fake `sounddevice.OutputStream`; no PortAudio.
"""

import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from core.audio_pipeline import AudioPipeline


class _FakeStream:
    """Records writes; `abort()` makes the next write raise, like PortAudio."""

    instances: list["_FakeStream"] = []

    def __init__(self, samplerate, channels, dtype, write_delay=0.0):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.writes: list[np.ndarray] = []
        self.started = 0
        self.stopped = True
        self.closed = False
        self.aborted = False
        self._write_delay = write_delay
        _FakeStream.instances.append(self)

    def start(self):
        self.started += 1
        self.stopped = False

    def write(self, data):
        if self.stopped:
            raise RuntimeError("Stream is stopped")
        if self._write_delay:
            time.sleep(self._write_delay)
        self.writes.append(np.array(data, copy=True))

    def abort(self):
        self.aborted = True
        self.stopped = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    @property
    def written_frames(self) -> int:
        return sum(len(w) for w in self.writes)


def _pipeline(tail_ms=500) -> AudioPipeline:
    pipe = AudioPipeline.__new__(AudioPipeline)
    pipe.sample_rate = 16000
    pipe.channels = 1
    pipe.chunk_samples = 640
    pipe._speaker = None
    pipe._speaker_sr = 0
    pipe._speaker_dead = False
    pipe._speaker_tail_s = tail_ms / 1000.0
    pipe._abort = threading.Event()
    pipe._speaker_lock = threading.Lock()
    return pipe


class SpeakerSessionTests(unittest.TestCase):
    def setUp(self):
        _FakeStream.instances.clear()
        patcher = patch("core.audio_pipeline.sd.OutputStream", _FakeStream)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sd_play = patch("core.audio_pipeline.sd.play").start()
        self.addCleanup(patch.stopall)

    def test_open_is_idempotent_at_the_same_rate(self):
        pipe = _pipeline()
        pipe.open_speaker(24000)
        pipe.open_speaker(24000)
        self.assertEqual(len(_FakeStream.instances), 1)
        self.assertTrue(pipe.speaker_open)

    def test_a_clip_is_written_in_blocks_and_never_touches_sd_play(self):
        pipe = _pipeline()
        pipe.open_speaker(24000)
        clip = np.linspace(-0.5, 0.5, 24000, dtype=np.float32)  # 1s

        pipe.play_audio(clip, 24000, blocking=True)

        stream = _FakeStream.instances[0]
        block = 24000 * AudioPipeline.SPEAKER_BLOCK_MS // 1000
        self.assertEqual(stream.written_frames, len(clip))
        self.assertTrue(all(len(w) <= block for w in stream.writes))
        np.testing.assert_array_equal(np.concatenate(stream.writes), clip)
        self.sd_play.assert_not_called()

    def test_no_session_falls_back_to_sd_play(self):
        pipe = _pipeline()
        clip = np.zeros(100, dtype=np.float32)
        pipe.play_audio(clip, 24000, blocking=True)
        self.sd_play.assert_called_once()
        self.assertEqual(_FakeStream.instances, [])

    def test_non_blocking_playback_stays_on_sd_play(self):
        """The `activation_blocking: false` path records over the line and
        relies on sd.play returning immediately."""
        pipe = _pipeline()
        pipe.open_speaker(24000)
        pipe.play_audio(np.zeros(100, dtype=np.float32), 24000, blocking=False)
        self.sd_play.assert_called_once()
        self.assertEqual(_FakeStream.instances[0].writes, [])

    def test_a_different_rate_reopens_the_session(self):
        pipe = _pipeline()
        pipe.open_speaker(24000)
        pipe.play_audio(np.zeros(2205, dtype=np.float32), 22050, blocking=True)
        self.assertEqual(len(_FakeStream.instances), 2)
        self.assertTrue(_FakeStream.instances[0].closed)
        self.assertEqual(_FakeStream.instances[1].samplerate, 22050)
        self.assertEqual(_FakeStream.instances[1].written_frames, 2205)

    def test_int16_and_stereo_are_converted_for_the_stream(self):
        pipe = _pipeline()
        pipe.open_speaker(24000)
        stereo_int16 = np.full((480, 2), 16384, dtype=np.int16)
        pipe.play_audio(stereo_int16, 24000, blocking=True)
        written = np.concatenate(_FakeStream.instances[0].writes)
        self.assertEqual(written.dtype, np.float32)
        self.assertEqual(written.ndim, 1)
        self.assertAlmostEqual(float(written[0]), 0.5, places=4)

    def test_close_writes_the_tail_then_stops_and_closes(self):
        pipe = _pipeline(tail_ms=500)
        pipe.open_speaker(24000)
        pipe.close_speaker()
        stream = _FakeStream.instances[0]
        self.assertEqual(stream.written_frames, 12000)  # 0.5s at 24kHz
        self.assertTrue(np.all(np.concatenate(stream.writes) == 0))
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertFalse(pipe.speaker_open)

    def test_close_without_a_session_is_a_no_op(self):
        _pipeline().close_speaker()  # must not raise

    def test_stop_lands_within_one_block(self):
        """
        A stop from another thread (the reply listener) must cut the clip
        short, not wait for it to finish.
        """
        pipe = _pipeline()
        # 5ms per 40ms block: a 2s clip would take ~250ms of wall clock.
        with patch(
            "core.audio_pipeline.sd.OutputStream",
            lambda **kw: _FakeStream(write_delay=0.005, **kw),
        ):
            pipe.open_speaker(24000)
        stream = _FakeStream.instances[0]
        clip = np.ones(48000, dtype=np.float32)  # 2s

        def stop_soon():
            time.sleep(0.03)
            pipe.stop_playback()

        threading.Thread(target=stop_soon).start()
        pipe.play_audio(clip, 24000, blocking=True)

        block = 24000 * AudioPipeline.SPEAKER_BLOCK_MS // 1000
        self.assertTrue(stream.aborted)
        self.assertLess(stream.written_frames, len(clip))
        # The stopper had ~6 blocks of head start, plus one in flight.
        self.assertLessEqual(stream.written_frames, block * 12)

    def test_a_stop_stays_in_force_until_the_next_turn(self):
        """
        The player thread may have already dequeued the next sentence when
        the stop lands. It must be refused, not played in full — that race is
        why the abort flag is cleared by reset_playback, never by play_audio.
        """
        pipe = _pipeline()
        pipe.open_speaker(24000)
        stream = _FakeStream.instances[0]

        pipe.stop_playback()
        pipe.play_audio(np.ones(24000, dtype=np.float32), 24000, blocking=True)
        self.assertEqual(stream.written_frames, 0)
        self.assertEqual(len(_FakeStream.instances), 1, "a refused clip opens nothing")

        pipe.reset_playback()
        pipe.play_audio(np.ones(24000, dtype=np.float32), 24000, blocking=True)
        # MME will not restart an aborted stream, so the next clip gets a
        # fresh one and the dead one is closed without a tail.
        self.assertEqual(len(_FakeStream.instances), 2)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.written_frames, 0)
        self.assertEqual(_FakeStream.instances[1].written_frames, 24000)

    def test_close_after_a_stop_skips_the_tail(self):
        """Nothing is left in an aborted stream to flush, and writing to it
        raises on MME."""
        pipe = _pipeline(tail_ms=100)
        pipe.open_speaker(24000)
        pipe.stop_playback()
        pipe.close_speaker()
        stream = _FakeStream.instances[0]
        self.assertTrue(stream.closed)
        self.assertEqual(stream.written_frames, 0)
        self.assertFalse(pipe.speaker_open)

    def test_open_after_a_stop_replaces_the_dead_stream(self):
        pipe = _pipeline()
        pipe.open_speaker(24000)
        pipe.stop_playback()
        pipe.open_speaker(24000)
        self.assertEqual(len(_FakeStream.instances), 2)
        self.assertTrue(_FakeStream.instances[0].closed)
        self.assertTrue(pipe.speaker_open)

    def test_the_activation_clip_goes_through_the_session(self):
        pipe = _pipeline()
        pipe._activation_pools = {"warm": [("sup", "Sup.", np.ones(2400, dtype=np.float32), 24000)]}
        pipe._chime_data = None
        pipe._activation_blocking = True
        pipe.open_speaker(24000)

        playback = pipe.play_activation_sound("warm")

        self.assertEqual(playback.name, "sup")
        self.assertEqual(playback.duration, 0.0)
        self.assertEqual(_FakeStream.instances[0].written_frames, 2400)
        self.sd_play.assert_not_called()

    def test_a_failed_open_leaves_playback_on_the_fallback(self):
        pipe = _pipeline()

        def boom(**kw):
            raise RuntimeError("no device")

        with patch("core.audio_pipeline.sd.OutputStream", boom):
            pipe.open_speaker(24000)  # must not raise
        self.assertFalse(pipe.speaker_open)
        pipe.play_audio(np.zeros(10, dtype=np.float32), 24000, blocking=True)
        self.sd_play.assert_called_once()


if __name__ == "__main__":
    unittest.main()
