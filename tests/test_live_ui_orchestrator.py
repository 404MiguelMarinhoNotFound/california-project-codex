"""
The orchestrator's side of the live UI's halo: her voice and the mic level.

Built with `__new__` like the other orchestrator tests, so nothing here opens
an audio device, a model or a socket. The audio pipeline is faked with the same
hook contract `AudioPipeline` honours (pinned in tests/test_speaker_session.py).
"""

import queue
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.event_bus import NULL_BUS, EventBus
from core.orchestrator import Orchestrator

CHUNK = 640


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _tone(seconds, sr=24000, amp=0.3):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class _HookedAudio:
    """Plays instantly; fires on_clip_start / on_clip_end like the real pipeline."""

    def __init__(self):
        self.sample_rate = 16000
        self.chunk_samples = CHUNK
        self.on_clip_start = None
        self.on_clip_end = None
        self.calls = []

    def play_audio(self, audio, sr, blocking=True, *, text="", kind="reply"):
        self.calls.append({"text": text, "kind": kind, "blocking": blocking})
        clip = self.on_clip_start(audio, sr, text, kind) if self.on_clip_start else None
        if self.on_clip_end and clip is not None:
            self.on_clip_end(clip, False)

    def play_activation_sound(self, tier="warm", blocking=None):
        data = _tone(0.5)
        self.play_audio(data, 24000, blocking=True, text="Sup.", kind="ack")
        return Mock(duration=0.0, name="sup", text="Sup.", blocking=True)

    def bytes_to_numpy(self, b):
        return np.frombuffer(b, dtype=np.int16)

    def drain_mic_stream(self, mic, max_seconds=30.0):
        return 0

    def reset_playback(self):
        pass

    def stop_playback(self):
        pass

    def open_speaker(self, sr=24000):
        pass

    def close_speaker(self):
        pass


class _Mic:
    def __init__(self, amp=3000):
        self.amp = amp
        self.reads = 0

    def read(self, frames):
        self.reads += 1
        chunk = np.full(frames, self.amp, dtype=np.int16)
        return chunk.tobytes(), False


def _orch(include_text=True, ui=True):
    orch = Orchestrator.__new__(Orchestrator)
    orch.audio = _HookedAudio()
    orch.leds = Mock()
    orch.bus = EventBus()
    orch.live_ui = Mock() if ui else None
    orch._attach_live_ui({"ui": {"mic_publish_hz": 0},
                          "logging": {"include_transcripts": include_text}})
    return orch


class ClipEventTests(unittest.TestCase):
    def test_player_worker_passes_text_to_play_audio(self):
        orch = _orch()
        orch._interrupted = threading.Event()
        orch._speaking_text = ""
        orch._turn_writer = None
        q = queue.Queue()
        q.put((_tone(0.2), 24000, "Fallout is up on the TV."))
        q.put(None)
        orch._audio_player_worker(q)
        self.assertEqual(orch.audio.calls, [
            {"text": "Fallout is up on the TV.", "kind": "reply", "blocking": True},
        ])

    def test_clip_start_carries_envelope_duration_and_kind(self):
        orch = _orch()
        _, q = orch.bus.subscribe()
        orch.audio.play_audio(_tone(2.0), 24000, text="Hello there.", kind="reply")
        start, end = _drain(q)
        self.assertEqual(start["kind"], "clip_start")
        data = start["data"]
        self.assertEqual(data["kind"], "reply")
        self.assertEqual(data["hz"], 25)
        self.assertEqual(data["ms"], 2000)
        self.assertEqual(len(data["env"]), 50)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in data["env"]))
        self.assertGreater(min(data["env"]), 0.5)
        self.assertEqual(data["text"], "Hello there.")
        self.assertEqual(end["kind"], "clip_end")
        self.assertEqual(end["data"], {"clip": data["clip"], "stopped": False})

    def test_clip_ids_increase(self):
        orch = _orch()
        _, q = orch.bus.subscribe()
        orch.audio.play_audio(_tone(0.1), 24000)
        orch.audio.play_audio(_tone(0.1), 24000)
        ids = [e["data"]["clip"] for e in _drain(q) if e["kind"] == "clip_start"]
        self.assertEqual(len(ids), 2)
        self.assertLess(ids[0], ids[1])

    def test_include_transcripts_false_omits_clip_text(self):
        orch = _orch(include_text=False)
        _, q = orch.bus.subscribe()
        orch.audio.play_audio(_tone(0.3), 24000, text="tell mum I'm late")
        start = _drain(q)[0]
        self.assertNotIn("text", start["data"])
        self.assertNotIn("mum", repr(start))

    def test_activation_line_publishes_an_ack_clip_before_state_listening(self):
        """wake_hit -> clip_start(kind=ack) -> state: listening, the order the page relies on.

        With activation_blocking the line plays out while the published state is
        still idle, so the halo wakes on the hit and the ack clip, not on state.
        """
        orch = _orch()
        _, q = orch.bus.subscribe()
        orch.wake_word = Mock(threshold=0.81, last_score=0.9)
        orch.wake_word.process_audio = Mock(return_value=True)
        orch._capture_ring = None
        orch._capture_enabled = False
        orch._wake_count = 1
        orch._turn_writer = None
        orch._last_record_outcome = "no_speech"
        with patch.object(Orchestrator, "_record_speech", return_value=None):
            orch._idle_loop(_Mic())
        order = [
            (e["kind"], e["data"].get("kind") or e["data"].get("state") or e["data"].get("phase"))
            for e in _drain(q)
            if e["kind"] in ("wake_hit", "clip_start", "state")
        ]
        self.assertEqual(order[:4], [
            ("state", "idle"),
            ("wake_hit", "idle"),
            ("clip_start", "ack"),
            ("state", "listening"),
        ])


class MicLevelTests(unittest.TestCase):
    def test_mic_levels_offered_from_idle_recording_and_reply(self):
        orch = _orch()
        _, q = orch.bus.subscribe()

        # Idle: one chunk, no wake.
        orch.wake_word = Mock(threshold=0.81, last_score=0.0)
        orch.wake_word.process_audio = Mock(return_value=False)
        orch._capture_ring = None
        orch._idle_loop(_Mic())

        # Recording: the VAD stops on the first chunk.
        orch.vad = Mock(speech_timeout=4.0)
        orch.vad.should_stop_recording = Mock(return_value=(True, "no_speech"))
        orch._barge_in_rms = 900
        orch._onset_guard_s = 0.0
        orch._record_speech(_Mic())

        # Reply: the listener reads one chunk, then is told to stop.
        stop = threading.Event()
        orch._barge_in_threshold = 0.5
        orch._speaking_text = ""

        def once(chunk, threshold=None):
            stop.set()
            return False

        orch.wake_word.process_audio = once
        orch._reply_listener(_Mic(), stop)

        mics = [e["data"] for e in _drain(q) if e["kind"] == "mic"]
        self.assertEqual([m["phase"] for m in mics], ["idle", "recording", "reply"])
        self.assertTrue(all(0.0 < m["level"] <= 1.0 for m in mics), mics)


class WithoutUITests(unittest.TestCase):
    def test_orchestrator_without_ui_publishes_nothing_and_never_raises(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.audio = _HookedAudio()
        orch.live_ui = None
        orch._attach_live_ui({"ui": {"enabled": False}})
        self.assertIs(orch.bus, NULL_BUS)
        self.assertIsNone(orch._mic_meter)
        self.assertIsNone(orch.audio.on_clip_start)
        self.assertIsNone(orch.audio.on_clip_end)
        orch._offer_mic(np.zeros(CHUNK, dtype=np.int16), "idle")  # no-op
        orch.audio.play_audio(_tone(0.1), 24000, text="Hi.")  # plays, publishes nothing


if __name__ == "__main__":
    unittest.main()
