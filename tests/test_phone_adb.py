"""AdbPhone: the phone over ADB. A fake adb plays the phone's side; nothing real runs."""

import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from services import phone_adb
from services.phone_adb import ACTIVE, ALERTING, DIALING, IDLE, AdbPhone

PHONE = "192.168.1.101:38731"
MDNS_NAME = "adb-4POJ4DWOJBXWIVLB-lmuzi2._adb-tls-connect._tcp"


class FakeAdb:
    """Answers adb argv the way the Xiaomi did on 2026-10-13."""

    def __init__(self, local_hash: str):
        self.devices = f"List of devices attached\n{PHONE}\tdevice\n192.168.1.200:5555\tdevice\n"
        self.mdns = f"List of discovered mdns services\nadb-4POJ4DWOJBXWIVLB-lmuzi2\t_adb-tls-connect._tcp\t{PHONE}\n"
        self.local_hash = local_hash
        self.remote_hash = local_hash
        self.gate = "1"
        self.interceptable = "true"
        self.states = [IDLE]
        self.replies = {"dial": "DIALED", "hangup": "ENDED=false", "check": "INTERCEPTABLE=true\nAUDIO_MODE=0\nTELEPHONY_TX=18/x/"}
        self.swapped = False
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        out = self._answer(argv[1:])
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def _answer(self, args):
        if args[:1] == ["devices"]:
            return self.devices
        if args[:2] == ["mdns", "services"]:
            return self.mdns
        if args[:1] == ["connect"]:
            return f"connected to {args[1]}"
        if len(args) >= 3 and args[0] == "-s" and args[2] == "push":
            self.remote_hash = self.local_hash
            return "1 file pushed"
        if len(args) >= 3 and args[0] == "-s" and args[2] == "forward":
            return ""
        cmd = args[3] if len(args) > 3 and args[2] == "shell" else ""
        if "CallAudio" in cmd:
            if self.swapped or self.local_hash not in cmd or "exec app_process" not in cmd:
                return "HELPER_MISMATCH"
            mode = cmd.split("CallAudio ", 1)[1].split()[0]
            if mode == "check":
                return self.replies["check"].replace("true", self.interceptable)
            return self.replies.get(mode, "")
        if "persist.security.adbinput" in cmd:
            return self.gate + "\n"
        if cmd.startswith("sha256sum"):
            return f"{self.remote_hash}  /data/local/tmp/callaudio.dex\n"
        if "mPreciseCallState" in cmd:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return f"    mPreciseCallState=Ringing call state: 0, Foreground call state: {state}, Background call state: 0\n"
        return ""

    def shell_cmds(self):
        return [a[4] for a in self.calls if len(a) > 4 and a[3] == "shell"]


class FakeProc:
    def __init__(self, first_line: str):
        self.stdout = iter([first_line + "\n"])
        self.stderr = iter([])

    def poll(self):
        return None


class _Base(unittest.TestCase):
    def setUp(self):
        handle, self.dex = tempfile.mkstemp(suffix=".dex")
        os.write(handle, b"dex\n035 fake")
        os.close(handle)
        self.addCleanup(os.remove, self.dex)
        with open(self.dex, "rb") as f:
            self.local_hash = hashlib.sha256(f.read()).hexdigest()
        self.adb = FakeAdb(self.local_hash)
        self.now = [0.0]
        self.popened: list[str] = []
        self.ring_line = "RINGING"

        def popen(argv, **kw):
            self.popened.append(argv[-1])
            return FakeProc(self.ring_line)

        self.popen = popen
        # The whole file must never reach a real adb.
        for target in ("subprocess.run", "subprocess.Popen"):
            patcher = mock.patch(target, side_effect=AssertionError(f"real {target}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def phone(self, **kw):
        return AdbPhone(
            helper_dex=self.dex, runner=self.adb, popen=self.popen,
            sleep=lambda s: self.now.__setitem__(0, self.now[0] + s), clock=lambda: self.now[0], **kw,
        )


class ConstructionTests(_Base):
    def test_construction_runs_no_adb(self):
        self.phone()
        self.assertEqual(self.adb.calls, [])


class DiscoveryTests(_Base):
    def test_discovery_finds_the_mdns_name(self):
        self.adb.devices = f"List of devices attached\n{MDNS_NAME}\tdevice\n"
        phone = self.phone()
        self.assertEqual(phone.ready(), "OK")
        self.assertEqual(phone.serial, MDNS_NAME)

    def test_discovery_accepts_an_attached_ip_port_when_mdns_is_silent(self):
        self.adb.mdns = "List of discovered mdns services\n"
        phone = self.phone()
        self.assertEqual(phone.ready(), "OK")
        self.assertEqual(phone.serial, PHONE)

    def test_discovery_connects_when_only_mdns_knows_the_phone(self):
        self.adb.devices = "List of devices attached\n192.168.1.200:5555\tdevice\n"
        phone = self.phone()
        self.assertEqual(phone.ready(), "OK")
        self.assertEqual(phone.serial, PHONE)
        self.assertIn(["adb", "connect", PHONE], self.adb.calls)

    def test_discovery_never_picks_the_mi_box(self):
        self.adb.devices = "List of devices attached\n192.168.1.200:5555\tdevice\n"
        self.adb.mdns = "List of discovered mdns services\n"
        self.assertEqual(self.phone().ready(), "NO_PHONE")

    def test_no_phone_is_a_status_not_an_exception(self):
        self.adb.devices = "List of devices attached\n"
        self.adb.mdns = ""
        phone = self.phone()
        self.assertEqual(phone.ready(), "NO_PHONE")
        self.assertEqual(phone.dial("+351912345678"), "NO_PHONE")
        self.assertEqual(phone.play_sound(), "NO_PHONE")
        self.assertEqual(phone.hang_up(), "")
        self.assertIsNone(phone.call_state())

    def test_a_configured_serial_skips_discovery(self):
        phone = self.phone(serial=PHONE)
        self.assertEqual(phone.ready(), "OK")
        self.assertNotIn(["adb", "devices"], self.adb.calls)


class ReadyTests(_Base):
    def test_locked_down_dials_nothing(self):
        self.adb.gate = ""
        phone = self.phone()
        self.assertEqual(phone.dial("+351912345678"), "LOCKED_DOWN")
        self.assertFalse(any("CallAudio dial" in c for c in self.adb.shell_cmds()))

    def test_no_interception_blocks_calls_but_not_ringing(self):
        self.adb.interceptable = "false"
        phone = self.phone()
        self.assertEqual(phone.dial("+351912345678"), "NO_INTERCEPTION")
        self.assertEqual(phone.play_sound(), "PLAYING")

    def test_helper_is_pushed_only_when_its_hash_differs(self):
        phone = self.phone()
        phone.ready()
        pushes = [a for a in self.adb.calls if "push" in a]
        self.assertEqual(pushes, [])
        self.assertIn("chmod 444 /data/local/tmp/callaudio.dex", self.adb.shell_cmds())

        self.adb.remote_hash = "0" * 64
        phone2 = self.phone()
        phone2.ready()
        pushes = [a for a in self.adb.calls if "push" in a]
        self.assertEqual(len(pushes), 1)

    def test_a_swapped_helper_is_never_run(self):
        self.adb.swapped = True
        phone = self.phone()
        self.assertEqual(phone.dial("+351912345678"), "HELPER_MISMATCH")
        self.ring_line = "HELPER_MISMATCH"
        self.assertEqual(phone.play_sound(), "RING_FAILED")
        for cmd in self.adb.shell_cmds() + self.popened:
            if "CallAudio" in cmd:
                self.assertIn(self.local_hash, cmd)
                self.assertIn("exec app_process", cmd)


class CallStateTests(_Base):
    def test_call_state_parses_precise_state(self):
        self.adb.states = [ALERTING]
        self.assertEqual(self.phone().call_state(), ALERTING)

    def test_garbage_is_unknown(self):
        phone = self.phone()
        phone.ready()
        with mock.patch.object(self.adb, "_answer", return_value="nonsense"):
            self.assertIsNone(phone.call_state())


class DialTests(_Base):
    def test_dial_waits_for_the_call_to_register(self):
        self.adb.states = [IDLE, IDLE, DIALING]
        self.assertEqual(self.phone().dial("+351912345678"), "DIALED")

    def test_a_call_that_never_registers_is_not_placed(self):
        self.adb.states = [IDLE]
        self.assertEqual(self.phone().dial("+351912345678"), "NOT_PLACED")
        self.assertGreaterEqual(self.now[0], 10)

    def test_a_helper_error_is_not_placed(self):
        self.adb.replies["dial"] = "ERROR SecurityException: CALL_PHONE permission required"
        self.adb.states = [DIALING]
        self.assertEqual(self.phone().dial("+351912345678"), "NOT_PLACED")

    def test_only_digits_are_dialled(self):
        phone = self.phone()
        for bad in ("12; reboot", "abc", "", "+35191234567890123"):
            self.assertEqual(phone.dial(bad), "NOT_PLACED")
        self.assertFalse(any("CallAudio dial" in c for c in self.adb.shell_cmds()))


class InCallTests(_Base):
    def test_in_call_is_unknown_until_a_call_was_seen(self):
        self.adb.states = [DIALING]
        phone = self.phone()
        phone.dial("+351912345678")
        self.adb.states = [IDLE]
        phone._seen = False
        self.assertIsNone(phone.in_call())
        self.adb.states = [ACTIVE]
        self.assertTrue(phone.in_call())
        self.adb.states = [IDLE]
        self.assertFalse(phone.in_call())


class HangUpTests(_Base):
    def test_hang_up_without_a_call_is_no_call(self):
        self.assertEqual(self.phone().hang_up(), "NO_CALL")

    def test_hang_up_of_a_live_call_is_ended(self):
        self.adb.replies["hangup"] = "ENDED=true"
        self.assertEqual(self.phone().hang_up(), "ENDED")


class PlaySoundTests(_Base):
    def test_play_sound_reports_playing_once_the_tone_starts(self):
        phone = self.phone()
        self.assertEqual(phone.play_sound(), "PLAYING")
        self.assertTrue(any("CallAudio ring 20" in c for c in self.popened))

    def test_play_sound_ring_failed(self):
        self.ring_line = "ERROR NullPointerException: no context"
        self.assertEqual(self.phone().play_sound(), "RING_FAILED")


if __name__ == "__main__":
    unittest.main()
