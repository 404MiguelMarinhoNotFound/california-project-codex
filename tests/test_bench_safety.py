"""
Bench runner safety: the parts of tools/bench_tv_power.py that touch the room.

A bench run turns CEC settings and the television's power over on purpose,
unattended. Whatever happens inside a run -- a failure, an exception, Ctrl-C --
the box's HDMI-CEC settings must come back as they were, or the run must say
loudly that they did not: a box left at hdmi_control_enabled=0 stops the Xiaomi
remote turning the TV on, and that already happened once (2026-09-25).

No real adb, sockets or files outside a tmpdir: the fake box below answers
`settings get/put` from a dict.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.cec_wake import WakeResult
from services.media_service import HdmiState
from tools import bench_tv_power as bench


class _FakeBox:
    def __init__(self, reachable=True, **settings):
        self.reachable = reachable
        self.settings = {k: "1" for k in bench.SETTING_KEYS}
        self.settings.update(settings)
        self.commands = []

    def adb(self, command, **_kw):
        self.commands.append(command)
        if not self.reachable:
            return False, "error: closed"
        parts = command.split()
        if parts[:4] == ["shell", "settings", "get", "global"]:
            return True, self.settings.get(parts[4], "null")
        if parts[:4] == ["shell", "settings", "put", "global"]:
            self.settings[parts[4]] = parts[5]
            return True, ""
        return True, ""


def _svc(box):
    svc = Mock()
    svc._adb = Mock(side_effect=box.adb)
    svc.ip = "192.168.1.200"  # config-literal: never contacted, PortObserver is stubbed
    svc.port = 5555
    return svc


class _NoObserver:
    def __init__(self, *a, **kw):
        self.phases = {}

    def start(self):
        pass

    def stop(self):
        pass


class SettingsRestoreTests(unittest.TestCase):
    def test_restore_puts_back_only_changed_keys_and_verifies(self):
        box = _FakeBox()
        svc = _svc(box)
        before = bench.capture_settings(svc)
        box.settings["hdmi_control_enabled"] = "0"
        box.commands.clear()

        self.assertTrue(bench.restore_settings(svc, before))

        self.assertEqual(box.settings["hdmi_control_enabled"], "1")
        puts = [c for c in box.commands if " put " in c]
        self.assertEqual(puts, ["shell settings put global hdmi_control_enabled 1"])

    def test_restore_reports_false_when_the_box_is_unreachable(self):
        box = _FakeBox()
        svc = _svc(box)
        before = bench.capture_settings(svc)
        box.reachable = False
        with self.assertLogs("tools.bench_tv_power", level="WARNING"):
            self.assertFalse(bench.restore_settings(svc, before))


class RunRecordedTests(unittest.TestCase):
    def _run(self, action):
        box = _FakeBox()
        svc = _svc(box)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "r.jsonl"
        with patch.object(bench, "PortObserver", _NoObserver), \
                patch.object(bench, "_tv_ip", return_value=""), \
                patch.object(bench, "_git_sha", return_value="abc1234"):
            try:
                record = bench.run_recorded(svc, "S1", "baseline", 1, action, out)
            except KeyboardInterrupt:
                record = None
        return box, out, record

    def test_a_run_writes_one_record_with_its_total(self):
        _, out, record = self._run(lambda: True)
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        saved = json.loads(lines[0])
        self.assertEqual(saved, record)
        self.assertEqual(saved["outcome"], "ok")
        self.assertIsNotNone(saved["total_ms"])
        self.assertTrue(saved["settings_restored"])
        self.assertEqual(saved["git_sha"], "abc1234")

    def test_restore_runs_on_keyboard_interrupt(self):
        def action():
            box_ref.settings["hdmi_control_enabled"] = "0"
            raise KeyboardInterrupt

        box_ref = None
        box = _FakeBox()
        box_ref = box
        svc = _svc(box)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.jsonl"
            with patch.object(bench, "PortObserver", _NoObserver), \
                    patch.object(bench, "_tv_ip", return_value=""), \
                    patch.object(bench, "_git_sha", return_value="abc1234"):
                with self.assertRaises(KeyboardInterrupt):
                    bench.run_recorded(svc, "S1", "baseline", 1, action, out)
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(box.settings["hdmi_control_enabled"], "1")
        self.assertEqual(saved["outcome"], "interrupted")
        self.assertIsNone(saved["total_ms"])

    def test_settings_captured_before_deep_standby_are_the_ones_restored(self):
        """
        A deep-standby run starts with the box off the LAN, so capturing at the
        start of the run reads nothing and the restore silently does nothing
        (first live baseline, 2026-10-03). The caller captures while the box is
        awake and hands that in.
        """
        box = _FakeBox()
        svc = _svc(box)
        before = bench.capture_settings(svc)
        box.reachable = False  # deep standby: nothing answers at the start

        def action():
            box.reachable = True  # the wake brought it back...
            box.settings["hdmi_control_enabled"] = "0"  # ...and something changed this
            return True

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bench, "PortObserver", _NoObserver), \
                    patch.object(bench, "_tv_ip", return_value=""), \
                    patch.object(bench, "_git_sha", return_value="abc1234"):
                record = bench.run_recorded(svc, "S1", "baseline", 1, action,
                                            Path(tmp) / "r.jsonl", before=before)
        self.assertEqual(record["settings_before"], before)
        self.assertEqual(box.settings["hdmi_control_enabled"], "1")
        self.assertTrue(record["settings_restored"])

    def test_a_failed_action_is_recorded_as_failed(self):
        _, _, record = self._run(lambda: False)
        self.assertEqual(record["outcome"], "failed")


class BoxHealthTests(unittest.TestCase):
    """
    The box's own load decides the numbers: Button Mapper leaks media players
    until every ADB call times out (2026-09-25, and again 2026-10-03 mid-bench).
    Each record carries the count read before the run, so a leak is visible.
    """

    def test_media_player_clients_are_counted(self):
        svc = Mock()
        svc._adb.return_value = (True, " Client\n  pid(13125), connId(6)\n Client\n  pid(13125), connId(7)\n")
        self.assertEqual(bench.box_health(svc), {"media_player_clients": 2})

    def test_an_unreadable_box_reports_none_not_zero(self):
        svc = Mock()
        svc._adb.return_value = (False, "error: closed")
        self.assertEqual(bench.box_health(svc), {"media_player_clients": None})

    def test_the_record_carries_the_health_it_was_given(self):
        box = _FakeBox()
        svc = _svc(box)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bench, "PortObserver", _NoObserver), \
                    patch.object(bench, "_tv_ip", return_value=""), \
                    patch.object(bench, "_git_sha", return_value="abc1234"):
                record = bench.run_recorded(svc, "S1", "base", 1, lambda: True, Path(tmp) / "r.jsonl",
                                            health={"media_player_clients": 0})
        self.assertEqual(record["box_health"], {"media_player_clients": 0})


class DeepStandbyStartTests(unittest.TestCase):
    def test_deep_standby_refuses_without_an_on_reading(self):
        svc = _svc(_FakeBox())
        svc.is_awake.return_value = True
        svc.hdmi_state.return_value = HdmiState(True, "standby", "x")
        self.assertFalse(bench.put_room_in_deep_standby(svc, idle_s=0))
        svc.cec_waker._send_keys.assert_not_called()

    def test_deep_standby_sends_one_key_power_and_waits_for_the_box_to_drop(self):
        svc = _svc(_FakeBox())
        svc.is_awake.return_value = True
        svc.hdmi_state.return_value = HdmiState(True, "on", "x")
        svc.cec_waker._send_keys.return_value = WakeResult(True, "sent")
        with patch.object(bench, "port_open", side_effect=[True, True, False]), \
                patch.object(bench.time, "sleep"):
            self.assertTrue(bench.put_room_in_deep_standby(svc, idle_s=0))
        svc.cec_waker._send_keys.assert_called_once_with(["KEY_POWER"])

    def test_the_tv_address_is_resolved_before_the_key(self):
        """_send_keys builds its websocket from _tv_ip; unresolved, it fails as
        'Can't build URL with port but without host' (first live baseline run)."""
        svc = _svc(_FakeBox())
        svc.is_awake.return_value = True
        svc.hdmi_state.return_value = HdmiState(True, "on", "x")
        order = []
        svc.cec_waker.resolve_tv_ip.side_effect = lambda *a, **kw: order.append("resolve") or "192.168.1.201"  # config-literal: stub
        svc.cec_waker._send_keys.side_effect = lambda keys: order.append("key") or WakeResult(True, "sent")
        with patch.object(bench, "port_open", return_value=False), patch.object(bench.time, "sleep"):
            self.assertTrue(bench.put_room_in_deep_standby(svc, idle_s=0))
        self.assertEqual(order, ["resolve", "key"])

    def test_an_unresolvable_tv_sends_no_key(self):
        svc = _svc(_FakeBox())
        svc.is_awake.return_value = True
        svc.hdmi_state.return_value = HdmiState(True, "on", "x")
        svc.cec_waker.resolve_tv_ip.return_value = ""
        with patch.object(bench, "port_open", return_value=False), patch.object(bench.time, "sleep"):
            self.assertFalse(bench.put_room_in_deep_standby(svc, idle_s=0))
        svc.cec_waker._send_keys.assert_not_called()

    def test_bench_never_sends_keycode_power(self):
        box = _FakeBox()
        svc = _svc(box)
        svc.is_awake.return_value = True
        svc.hdmi_state.return_value = HdmiState(True, "on", "x")
        svc.cec_waker._send_keys.return_value = WakeResult(True, "sent")
        with patch.object(bench, "port_open", return_value=False), patch.object(bench.time, "sleep"):
            bench.put_room_in_deep_standby(svc, idle_s=0)
        before = bench.capture_settings(svc)
        bench.restore_settings(svc, before)
        self.assertFalse([c for c in box.commands if "KEYCODE_POWER" in c])


class OverrideTests(unittest.TestCase):
    def test_set_override_deep_merges(self):
        config = {"media": {"cec_wake": {"poll_interval_ms": 2000, "settle_ms": 45000}, "x": 1}}
        out = bench.apply_overrides(config, ["media.cec_wake.poll_interval_ms=500",
                                             "stremio.prepare_during_wake=true"])
        self.assertEqual(out["media"]["cec_wake"], {"poll_interval_ms": 500, "settle_ms": 45000})
        self.assertEqual(out["media"]["x"], 1)
        self.assertIs(out["stremio"]["prepare_during_wake"], True)
        self.assertEqual(config["media"]["cec_wake"]["poll_interval_ms"], 2000, "input not mutated")


if __name__ == "__main__":
    unittest.main()
