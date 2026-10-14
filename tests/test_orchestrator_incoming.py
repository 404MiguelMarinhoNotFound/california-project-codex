"""
Announcing an incoming call from the idle loop.

Built with `__new__` like the other orchestrator tests, so nothing here opens
an audio device, a model, adb or a socket. The watcher is a fake with the
same `pop()` contract as services.incoming_call.IncomingCallWatcher.
"""

import unittest
from unittest import mock

import numpy as np

from core.orchestrator import Orchestrator
from services.incoming_call import IncomingCall, RingOffer
from services.phone_service import IncomingTarget, PhoneCommandResult
from tests.config_fixture import config_for_tests


def _call(kind="incoming", number="+351912000111", name="Mama"):
    # Invented number with the real shape.
    return IncomingCall(kind=kind, number=number, name=name, started=0.0)


class _Watcher:
    def __init__(self, call=None):
        self.call = call
        self.pops = 0

    def pop(self):
        self.pops += 1
        call, self.call = self.call, None
        return call


def _orch(call=None, busy=False):
    orch = Orchestrator.__new__(Orchestrator)
    orch.audio = mock.Mock()
    orch.audio.chunk_samples = 640
    orch.audio.bytes_to_numpy.return_value = np.zeros(640, dtype="int16")
    orch.leds = mock.Mock()
    orch.tts = mock.Mock()
    orch.tts.synthesize.return_value = (np.zeros(2400, dtype="float32"), 24000)
    orch.llm = mock.Mock()
    orch.llm.history = []
    orch._drain_mic = mock.Mock()
    orch._capture_ring = None
    orch.wake_word = mock.Mock(process_audio=mock.Mock(return_value=False))
    orch.phone_service = mock.Mock()
    orch.phone_service.pop_report.return_value = None
    orch.phone_service.busy.return_value = busy
    orch.incoming_calls = _Watcher(call)
    return orch


def _mic():
    return mock.Mock(read=mock.Mock(return_value=(b"\x00" * 1280, False)))


class AnnounceTests(unittest.TestCase):
    def test_a_ringing_call_is_announced_between_mic_reads(self):
        orch = _orch(_call())
        mic = _mic()
        orch._idle_loop(mic)
        orch.tts.synthesize.assert_called_once_with("Mama is calling.")
        orch.audio.open_speaker.assert_called_once()
        orch.audio.close_speaker.assert_called_once()
        self.assertEqual(orch.audio.play_audio.call_args.kwargs.get("kind"), "announce")
        orch._drain_mic.assert_called_with(mic, "announcement")
        mic.read.assert_not_called()

    def test_the_speaker_is_opened_before_and_closed_after_the_clip(self):
        orch = _orch(_call())
        order = []
        orch.audio.open_speaker.side_effect = lambda *a, **k: order.append("open")
        orch.audio.play_audio.side_effect = lambda *a, **k: order.append("play")
        orch.audio.close_speaker.side_effect = lambda *a, **k: order.append("close")
        orch._idle_loop(_mic())
        self.assertEqual(order, ["open", "play", "close"])

    def test_a_stop_still_in_force_is_cleared_before_the_clip(self):
        orch = _orch(_call())
        order = []
        orch.audio.open_speaker.side_effect = lambda *a, **k: order.append("open")
        orch.audio.reset_playback.side_effect = lambda *a, **k: order.append("reset")
        orch.audio.play_audio.side_effect = lambda *a, **k: order.append("play")
        orch._idle_loop(_mic())
        self.assertEqual(order, ["open", "reset", "play"])

    def test_the_announcement_plays_after_an_earlier_stop(self):
        class _AbortableAudio:
            """play_audio is a no-op while a stop is in force, like AudioPipeline."""

            def __init__(self):
                self._abort = False
                self.played = []

            def stop_playback(self):
                self._abort = True

            def reset_playback(self):
                self._abort = False

            def open_speaker(self, *a, **k):
                pass

            def close_speaker(self, *a, **k):
                pass

            def play_audio(self, audio, sr, blocking=True, *, text="", kind="reply"):
                if self._abort:
                    return
                self.played.append((text, kind))

        orch = _orch(_call())
        orch.audio = _AbortableAudio()
        orch.audio.stop_playback()  # e.g. a dropped false wake, or "stop"
        orch._idle_loop(_mic())
        self.assertEqual(orch.audio.played, [("Mama is calling.", "announce")])

    def test_no_announcement_during_her_own_call(self):
        orch = _orch(_call(), busy=True)
        orch._idle_loop(_mic())
        orch.tts.synthesize.assert_not_called()
        orch.audio.play_audio.assert_not_called()
        # Dropped, not kept for later.
        self.assertIsNone(orch.incoming_calls.call)

    def test_nothing_queued_reads_the_mic_as_usual(self):
        orch = _orch(None)
        mic = _mic()
        orch._idle_loop(mic)
        mic.read.assert_called_once()
        orch.tts.synthesize.assert_not_called()
        self.assertEqual(orch.incoming_calls.pops, 1)

    def test_no_watcher_reads_the_mic_as_usual(self):
        orch = _orch(None)
        orch.incoming_calls = None
        mic = _mic()
        orch._idle_loop(mic)
        mic.read.assert_called_once()

    def test_a_phone_report_goes_first(self):
        orch = _orch(_call())
        orch.phone_service.pop_report.return_value = mock.Mock()
        orch._deliver_phone_report = mock.Mock()
        orch._idle_loop(_mic())
        orch._deliver_phone_report.assert_called_once()
        orch.tts.synthesize.assert_not_called()
        # The ring is still there for the next pass.
        self.assertIsNotNone(orch.incoming_calls.call)

    def test_an_announcement_is_not_an_llm_turn(self):
        orch = _orch(_call())
        orch._idle_loop(_mic())
        self.assertEqual(orch.llm.mock_calls, [])
        self.assertEqual(orch.llm.history, [])

    def test_a_tts_failure_does_not_take_the_idle_loop_down(self):
        orch = _orch(_call())
        orch.tts.synthesize.side_effect = RuntimeError("tts down")
        mic = _mic()
        with self.assertLogs("core.orchestrator", level="ERROR"):
            orch._idle_loop(mic)
        orch.audio.close_speaker.assert_called_once()
        orch._drain_mic.assert_called_with(mic, "announcement")
        # The next pass reads the mic as usual.
        orch._idle_loop(mic)
        mic.read.assert_called_once()

    def test_a_playback_failure_does_not_take_the_idle_loop_down(self):
        orch = _orch(_call())
        orch.audio.play_audio.side_effect = OSError("device gone")
        with self.assertLogs("core.orchestrator", level="ERROR"):
            orch._idle_loop(_mic())
        orch.audio.close_speaker.assert_called_once()

    def test_a_call_waiting_says_the_other_line(self):
        orch = _orch(_call(kind="waiting"))
        orch._idle_loop(_mic())
        orch.tts.synthesize.assert_called_once_with("Mama is calling on the other line.")


class BusTests(unittest.TestCase):
    def test_the_bus_carries_the_caller(self):
        orch = _orch(_call())
        orch.bus = mock.Mock()
        orch._include_text = True
        orch._idle_loop(_mic())
        orch.bus.publish.assert_any_call("incoming_call", {"kind": "incoming", "label": "Mama"})

    def test_no_label_without_transcripts(self):
        orch = _orch(_call())
        orch.bus = mock.Mock()
        orch._include_text = False
        orch._idle_loop(_mic())
        calls = [c for c in orch.bus.publish.call_args_list if c.args[0] == "incoming_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], {"kind": "incoming"})


class _Episodes:
    """A watcher whose current() plays a script of offers (the last one repeats)."""

    def __init__(self, *offers):
        self.offers = list(offers)
        self.reads = 0

    def current(self):
        self.reads += 1
        if len(self.offers) > 1:
            return self.offers.pop(0)
        return self.offers[0] if self.offers else None


def _target_orch(watcher, enabled=True):
    orch = Orchestrator.__new__(Orchestrator)
    orch.audio = mock.Mock()
    orch.incoming_calls = watcher
    orch.contact_sync = None
    orch.phone_service = mock.Mock()
    orch.phone_service.native_answer_enabled = enabled
    orch.phone_service.bind_incoming.side_effect = lambda offer: IncomingTarget(offer, "TC@5")
    orch.phone_service.answer_speaker.return_value = PhoneCommandResult(False, "Your phone isn't ringing now.")
    return orch


_RING_7 = RingOffer(7, _call(name="Example Caller"))
_RING_8 = RingOffer(8, _call(number="+351912000222", name="Another Caller"))


class TurnTargetTests(unittest.TestCase):
    def test_a_ring_at_turn_start_is_bound_once(self):
        orch = _target_orch(_Episodes(_RING_7))
        orch._capture_incoming_target()
        self.assertEqual(orch._incoming_target, IncomingTarget(_RING_7, "TC@5"))
        orch.phone_service.bind_incoming.assert_called_once_with(_RING_7)

    def test_no_ring_reads_no_telecom(self):
        # R4: telecom is only read when the watcher has a ring.
        orch = _target_orch(_Episodes(None))
        orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)
        orch.phone_service.bind_incoming.assert_not_called()

    def test_switched_off_reads_nothing(self):
        watcher = _Episodes(_RING_7)
        orch = _target_orch(watcher, enabled=False)
        orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)
        self.assertEqual(watcher.reads, 0)
        orch.phone_service.bind_incoming.assert_not_called()

    def test_a_new_episode_during_the_bind_is_not_kept(self):
        orch = _target_orch(_Episodes(_RING_7, _RING_8))
        orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)
        orch = _target_orch(_Episodes(_RING_7, None))
        orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)

    def test_no_watcher_or_a_raising_one_binds_nothing(self):
        orch = _target_orch(None)
        orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)
        watcher = mock.Mock()
        watcher.current.side_effect = RuntimeError("stream died")
        orch = _target_orch(watcher)
        with self.assertLogs("core.orchestrator", "ERROR"):
            orch._capture_incoming_target()
        self.assertIsNone(orch._incoming_target)

    def test_old_turn_cannot_answer_new_ring(self):
        watcher = _Episodes(_RING_7)
        orch = _target_orch(watcher)

        def turn_one(mic, pre_roll):
            orch._capture_incoming_target()
            self.assertIsNotNone(orch._incoming_target)
            return False

        orch._run_activation = turn_one
        orch._handle_activation(mock.Mock(), pre_roll=[])
        # The target ends with its turn.
        self.assertIsNone(orch._incoming_target)

        # Turn two starts with nothing ringing; ring 8 starts mid-turn. The
        # tool call must not go and find it.
        watcher.offers = [None]

        def turn_two(mic, pre_roll):
            orch._capture_incoming_target()
            watcher.offers = [_RING_8]
            orch._handle_tool_call("control_phone", {"action": "phone_answer_speaker", "ring_id": 8})
            return False

        orch._run_activation = turn_two
        orch._handle_activation(mock.Mock(), pre_roll=[])
        orch.phone_service.bind_incoming.assert_called_once_with(_RING_7)   # turn one only
        orch.phone_service.answer_speaker.assert_called_once_with(None)
        orch.audio.stop_playback.assert_not_called()

    def test_a_chained_turn_captures_afresh(self):
        watcher = _Episodes(_RING_7)
        orch = _target_orch(watcher)
        seen = []

        def turn(mic, pre_roll):
            orch._capture_incoming_target()
            seen.append(orch._incoming_target)
            return False

        orch._run_activation = turn
        orch._handle_activation(mock.Mock(), pre_roll=[])
        watcher.offers = [_RING_8]
        orch._handle_activation(mock.Mock(), pre_roll=None)   # chained after a barge-in
        self.assertEqual([t.offer.ring_id for t in seen], [7, 8])

    def test_the_turn_captures_after_the_transcript_and_before_the_llm(self):
        orch = _target_orch(_Episodes(_RING_7))
        order = []
        orch._capture_incoming_target = lambda: order.append("capture")
        orch._stream_response = lambda text, mic: order.append("llm") or False
        orch._handle_command = lambda text: False
        orch.wake_word = mock.Mock()
        orch.leds = mock.Mock()
        orch._wake_count = 1
        orch._record_speech = mock.Mock(return_value=np.ones(16000, dtype="int16"))
        orch._last_record_outcome = "ok"
        orch._capture_activation = mock.Mock()
        orch.audio.play_activation_sound.return_value = None
        orch.audio.sample_rate = 16000
        orch.stt = mock.Mock()
        orch.stt.transcribe.side_effect = lambda wav: order.append("stt") or "answer it on speaker"
        with mock.patch("builtins.print"):
            orch._run_activation(mock.Mock(), pre_roll=[])
        self.assertEqual(order, ["stt", "capture", "llm"])


class BuildTests(unittest.TestCase):
    def test_disabled_builds_no_watcher(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = mock.Mock()
        with mock.patch("core.orchestrator.AdbPhone") as adb:
            watcher = orch._build_incoming_calls(config_for_tests())
        self.assertIsNone(watcher)
        adb.from_config.assert_not_called()

    def test_enabled_builds_a_watcher_with_its_own_phone_and_runs_no_adb(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = mock.Mock(enabled=True)
        cfg = config_for_tests(phone={"incoming": {"enabled": True}})
        with mock.patch("core.orchestrator.AdbPhone") as adb:
            watcher = orch._build_incoming_calls(cfg)
        self.assertIsNotNone(watcher)
        adb.from_config.assert_called_once_with(cfg)
        self.assertIs(watcher._phone, adb.from_config.return_value)
        # Construction ran nothing on the phone.
        self.assertEqual(adb.from_config.return_value.mock_calls, [])
        self.assertIsNone(watcher.thread)
        watcher._resolve_name("+351912000111")
        orch.whatsapp_service.name_for_number.assert_called_once_with("+351912000111")

    def test_without_whatsapp_names_resolve_to_nothing(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = mock.Mock(enabled=False)
        cfg = config_for_tests(phone={"incoming": {"enabled": True}})
        with mock.patch("core.orchestrator.AdbPhone"):
            watcher = orch._build_incoming_calls(cfg)
        self.assertEqual(watcher._resolve_name("+351912000111"), "")
        orch.whatsapp_service.name_for_number.assert_not_called()

    def test_a_transport_that_cannot_be_built_means_no_watcher(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = mock.Mock(enabled=True)
        cfg = config_for_tests(phone={"incoming": {"enabled": True}})
        with mock.patch("core.orchestrator.AdbPhone") as adb:
            adb.from_config.side_effect = ValueError("bad port")
            with self.assertLogs("core.orchestrator", level="ERROR"):
                watcher = orch._build_incoming_calls(cfg)
        self.assertIsNone(watcher)

    def test_phone_service_reads_the_watchers_current_ring(self):
        orch = Orchestrator.__new__(Orchestrator)
        watcher = mock.Mock()
        watcher.current.return_value = "offer"
        orch.incoming_calls = watcher
        self.assertEqual(orch._current_ring(), "offer")
        orch.incoming_calls = None
        self.assertIsNone(orch._current_ring())

    def test_the_shipped_config_enables_it(self):
        from tests.config_fixture import real_config

        self.assertIs(real_config()["phone"]["incoming"]["enabled"], True)


if __name__ == "__main__":
    unittest.main()
