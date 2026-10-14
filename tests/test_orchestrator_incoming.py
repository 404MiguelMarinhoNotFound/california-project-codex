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
from services.incoming_call import IncomingCall
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

    def test_the_shipped_config_enables_it(self):
        from tests.config_fixture import real_config

        self.assertIs(real_config()["phone"]["incoming"]["enabled"], True)


if __name__ == "__main__":
    unittest.main()
