"""AdbPhone: the phone over ADB. A fake adb plays the phone's side; nothing real runs."""

import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from services import phone_adb
from services.phone_adb import ACTIVE, ALERTING, DIALING, IDLE, AdbPhone, parse_content_rows
from tests.config_fixture import config_for_tests

NOTIFICATION_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "phone_notification_dump.txt")
# The phone-missed-digest plan's Global Constraints: nothing this file drives may write.
FORBIDDEN = ("content update", "content delete", "content insert", "cmd notification", "service call")
PHONE = "192.168.1.101:38731"  # config-literal: adb devices/mdns output shape, not the deployment
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
        self.content = "No result found.\n"
        self.call_log = "No result found.\n"
        with open(NOTIFICATION_FIXTURE, encoding="utf-8") as f:
            self.notifications = f.read()
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
        if args[0] == "-s" and args[1] not in self.devices and args[1] not in self.mdns:
            return ""  # "device not found" goes to stderr
        cmd = args[3] if len(args) > 3 and args[2] == "shell" else ""
        if "CallAudio" in cmd:
            if self.swapped or self.local_hash not in cmd or "exec app_process" not in cmd:
                return "HELPER_MISMATCH"
            mode = cmd.split("CallAudio ", 1)[1].split()[0]
            if mode == "check":
                return self.replies["check"].replace("true", self.interceptable)
            return self.replies.get(mode, "")
        if cmd.startswith("content query --uri content://call_log/calls "):
            return self.call_log
        if cmd.startswith("content query"):
            return self.content
        if cmd.startswith("dumpsys notification"):
            return self.notifications
        if "persist.security.adbinput" in cmd:
            return self.gate + "\n"
        if cmd.startswith("sha256sum"):
            return f"{self.remote_hash}  /data/local/tmp/callaudio.dex\n"
        if "mPreciseCallState" in cmd:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            line = "    mPreciseCallState=Ringing call state: 0, Foreground call state: {}, Background call state: 0\n"
            return line.format(IDLE) + line.format(state)  # one block per modem; the SIM in use second
        return ""

    def shell_cmds(self):
        return [a[4] for a in self.calls if len(a) > 4 and a[3] == "shell"]


class FakeProc:
    def __init__(self, first_line: str):
        self.stdout = iter([first_line + "\n"])
        self.stderr = iter([])
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


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

        self.procs = []

        def popen(argv, **kw):
            self.popened.append(argv[-1])
            self.procs.append(FakeProc(self.ring_line))
            return self.procs[-1]

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
        self.assertNotIn(["adb", "mdns", "services"], self.adb.calls)


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
        self.adb.states = [IDLE, DIALING]
        self.assertEqual(self.phone().dial("+351912345678"), "NOT_PLACED")

    def test_only_digits_are_dialled(self):
        phone = self.phone()
        for bad in ("12; reboot", "abc", "", "+35191234567890123"):
            self.assertEqual(phone.dial(bad), "NOT_PLACED")
        self.assertFalse(any("CallAudio dial" in c for c in self.adb.shell_cmds()))


class InCallTests(_Base):
    def test_in_call_is_unknown_until_a_call_was_seen(self):
        self.adb.states = [IDLE, DIALING]
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


class FinalReviewTests(_Base):
    """Found by the whole-branch review, 2026-10-13."""

    def test_a_phone_that_moved_port_is_found_again(self):
        # Wireless debugging takes a new port on every reboot or toggle.
        phone = self.phone()
        self.assertEqual(phone.ready(), "OK")
        moved = "192.168.1.101:40000"  # config-literal: adb devices output shape, not the deployment
        self.adb.devices = f"List of devices attached\n{moved}\tdevice\n"
        self.adb.mdns = f"adb-x\t_adb-tls-connect._tcp\t{moved}\n"
        self.assertEqual(phone.ready(), "OK")
        self.assertEqual(phone.serial, moved)

    def test_a_vanished_phone_is_no_phone_not_locked_down(self):
        phone = self.phone()
        self.assertEqual(phone.ready(), "OK")
        self.adb.devices = "List of devices attached\n"
        self.adb.mdns = ""
        self.assertEqual(phone.ready(), "NO_PHONE")
        self.assertEqual(phone.dial("+351912345678"), "NO_PHONE")

    def test_a_dial_that_never_registers_is_hung_up(self):
        self.adb.states = [IDLE]
        self.assertEqual(self.phone().dial("+351912345678"), "NOT_PLACED")
        self.assertTrue(any("CallAudio hangup" in c for c in self.adb.shell_cmds()))

    def test_call_state_reads_every_modem(self):
        self.adb.states = [ALERTING]
        self.assertEqual(self.phone().call_state(), ALERTING)

    def test_dialing_over_a_call_already_up_is_busy(self):
        self.adb.states = [ACTIVE]
        self.assertEqual(self.phone().dial("+351912345678"), "BUSY")
        self.assertFalse(any("CallAudio dial" in c for c in self.adb.shell_cmds()))

    def test_ring_again_while_ringing_starts_no_second_ring(self):
        phone = self.phone()
        self.assertEqual(phone.play_sound(), "PLAYING")
        self.assertEqual(phone.play_sound(), "ALREADY_PLAYING")
        self.assertEqual(len(self.procs), 1)

    def test_a_failed_ring_is_killed(self):
        self.ring_line = "ERROR NullPointerException"
        self.assertEqual(self.phone().play_sound(), "RING_FAILED")
        self.assertTrue(self.procs[-1].killed)


class ContentRowTests(unittest.TestCase):
    KEYS = ["contact_id", "data2", "is_super_primary", "account_type", "data1", "display_name"]

    def test_rows_parse_with_crlf_and_null(self):
        text = ("Row: 0 contact_id=7, data2=2, is_super_primary=0, account_type=com.google, "
                "data1=+351 912 000 001, display_name=Ana Exemplo\r\n"
                "Row: 1 contact_id=8, data2=NULL, is_super_primary=1, account_type=NULL, "
                "data1=912000002, display_name=Rui \U0001faf0\U0001f3fd\r\n")
        rows, bad = parse_content_rows(text, self.KEYS)
        self.assertEqual(bad, 0)
        self.assertEqual(rows[0]["display_name"], "Ana Exemplo")
        self.assertIsNone(rows[1]["data2"])
        self.assertEqual(rows[1]["display_name"], "Rui \U0001faf0\U0001f3fd")

    def test_a_name_with_commas_and_equals_is_kept_whole(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=1, display_name=Silva, Ana = chefe, data1=9\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["display_name"], "Silva, Ana = chefe, data1=9")

    def test_a_number_with_a_pause_comma_is_kept_whole(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=212000000,,12, display_name=Loja\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["data1"], "212000000,,12")

    def test_a_newline_in_the_last_value_continues_the_row(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=1, display_name=Ana\nda Praia\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["display_name"], "Ana\nda Praia")

    def test_a_malformed_row_is_counted_not_raised(self):
        rows, bad = parse_content_rows("Row: 0 contact_id=1, display_name=x\n", self.KEYS)
        self.assertEqual((rows, bad), ([], 1))

    def test_an_empty_line_inside_the_last_value_is_kept(self):
        text = "Row: 0 contact_id=1, data2=2, is_super_primary=0, account_type=x, data1=1, display_name=a\n\nb\n"
        rows, _ = parse_content_rows(text, self.KEYS)
        self.assertEqual(rows[0]["display_name"], "a\n\nb")

    def test_text_before_the_first_row_is_ignored(self):
        rows, bad = parse_content_rows("noise\nRow: 0 contact_id=1, data2=2, is_super_primary=0, "
                                       "account_type=x, data1=1, display_name=Ana\n", self.KEYS)
        self.assertEqual((len(rows), bad), (1, 0))


class QueryTests(_Base):
    URI = "content://com.android.contacts/data/phones"
    KEYS = ["contact_id", "data1", "display_name"]
    TWO_ROWS = ("Row: 0 contact_id=1, data1=912000001, display_name=Ana Exemplo\n"
                "Row: 1 contact_id=2, data1=912000002, display_name=Rui Exemplo\n")

    def test_query_returns_rows(self):
        self.adb.content = self.TWO_ROWS
        phone = self.phone()
        rows = phone.query(self.URI, self.KEYS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["display_name"], "Rui Exemplo")
        self.assertEqual(phone.last_status, "OK")
        self.assertIn(f"content query --uri {self.URI} --projection contact_id:data1:display_name",
                      self.adb.shell_cmds())

    def test_no_result_is_an_empty_list(self):
        self.adb.content = "No result found.\n"
        phone = self.phone()
        self.assertEqual(phone.query(self.URI, self.KEYS), [])
        self.assertEqual(phone.last_status, "OK")

    def test_provider_error_is_none(self):
        self.adb.content = ("Error while accessing provider:com.android.contacts\n"
                            "java.lang.SecurityException: Permission Denial\n")
        phone = self.phone()
        with self.assertLogs("services.phone_adb", "WARNING") as logs:
            self.assertIsNone(phone.query(self.URI, self.KEYS))
        self.assertEqual(phone.last_status, "UNREADABLE")
        self.assertNotIn("SecurityException", "".join(logs.output))  # a length, not the phone's text

    def test_no_phone_is_none(self):
        self.adb.devices = "List of devices attached\n192.168.1.200:5555\tdevice\n"
        self.adb.mdns = "List of discovered mdns services\n"
        phone = self.phone()
        self.assertIsNone(phone.query(self.URI, self.KEYS))
        self.assertEqual(phone.last_status, "NO_PHONE")
        self.assertFalse(any(c.startswith("content") for c in self.adb.shell_cmds()))

    def test_locked_down_is_none(self):
        self.adb.gate = "0"
        phone = self.phone()
        self.assertIsNone(phone.query(self.URI, self.KEYS))
        self.assertEqual(phone.last_status, "LOCKED_DOWN")
        self.assertFalse(any(c.startswith("content") for c in self.adb.shell_cmds()))

    def test_query_never_pushes_or_runs_the_helper(self):
        self.adb.remote_hash = "0" * 64  # a stale helper must not be pushed by a read either
        self.adb.content = self.TWO_ROWS
        self.phone().query(self.URI, self.KEYS)
        self.assertFalse(any("push" in a for a in self.adb.calls))
        for cmd in self.adb.shell_cmds():
            self.assertNotIn("CallAudio", cmd)
            self.assertNotIn("sha256sum", cmd)

    def test_query_is_read_only(self):
        self.adb.content = self.TWO_ROWS
        self.phone().query(self.URI, self.KEYS)
        cmds = self.adb.shell_cmds()
        self.assertTrue(cmds)
        for cmd in cmds:
            self.assertTrue(cmd.startswith(("getprop", "content query")), cmd)
            for verb in ("insert", "update", "delete"):
                self.assertNotIn(verb, cmd)

    def test_query_after_a_port_change_rediscovers(self):
        self.adb.content = self.TWO_ROWS
        phone = self.phone()
        phone.serial = "192.168.1.101:40000"  # config-literal: adb devices output shape, not the deployment
        self.assertEqual(len(phone.query(self.URI, self.KEYS)), 2)
        self.assertEqual(phone.serial, PHONE)
        shells = [a for a in self.adb.calls if len(a) > 3 and a[3] == "shell"]
        self.assertTrue(shells)
        self.assertTrue(all(a[2] == PHONE for a in shells))

    def test_a_uri_or_key_that_could_reach_the_shell_is_refused(self):
        phone = self.phone()
        for uri, keys in ((self.URI + "; reboot", self.KEYS), ("content://x y", self.KEYS),
                          (self.URI, ["data1", "a;b"]), (self.URI, ["$(id)"]), (self.URI, [])):
            with self.assertRaises(ValueError):
                phone.query(uri, keys)
        self.assertEqual(self.adb.calls, [])


class ReadOnlyReadTests(_Base):
    """reachable(), missed_calls_raw() and notifications_raw(): reads that need no helper."""

    CALL_ROWS = ("Row: 0 number=912000001, date=1791800100000, new=1, name=Ana Exemplo\n"
                 "Row: 1 number=-2, date=1791800200000, new=1, name=NULL\n")

    def test_reachable_is_ok_and_finds_the_phone(self):
        phone = self.phone()
        self.assertEqual(phone.reachable(), "OK")
        self.assertEqual(phone.serial, PHONE)

    def test_reads_return_the_raw_text(self):
        self.adb.call_log = self.CALL_ROWS
        phone = self.phone()
        self.assertEqual(phone.missed_calls_raw(1791800000000), self.CALL_ROWS)
        self.assertIn("Notification List:", phone.notifications_raw())

    def test_missed_calls_raw_feeds_the_digest_parser(self):
        from services.phone_digest import parse_call_log

        self.adb.call_log = self.CALL_ROWS
        calls = parse_call_log(self.phone().missed_calls_raw(0))
        self.assertEqual([c.name for c in calls], ["Ana Exemplo", ""])

    def test_no_missed_calls_is_the_no_result_text_not_none(self):
        self.assertEqual(self.phone().missed_calls_raw(0), "No result found.\n")

    def test_reads_need_no_helper(self):
        self.adb.remote_hash = "0" * 64  # a stale helper must not be pushed by a read either
        phone = self.phone()
        phone.reachable()
        phone.missed_calls_raw(1791800000000)
        phone.notifications_raw()
        self.assertFalse(any("push" in a for a in self.adb.calls))
        for cmd in self.adb.shell_cmds():
            self.assertNotIn("CallAudio", cmd)
            self.assertNotIn("adbinput", cmd)

    def test_failed_reads_are_none_not_empty(self):
        self.adb.devices = "List of devices attached\n192.168.1.200:5555\tdevice\n"
        self.adb.mdns = "List of discovered mdns services\n"
        phone = self.phone()
        self.assertEqual(phone.reachable(), "NO_PHONE")
        self.assertIsNone(phone.missed_calls_raw(0))
        self.assertIsNone(phone.notifications_raw())

        # A serial set but the shell answers "" (adb failed): never "nothing new".
        self.adb.devices = f"List of devices attached\n{PHONE}\tdevice\n"
        phone = self.phone(serial=PHONE)
        with mock.patch.object(self.adb, "_answer",
                               side_effect=lambda args: self.adb.devices if args[:1] == ["devices"] else ""):
            self.assertEqual(phone.reachable(), "OK")
            with self.assertLogs("services.phone_adb", "WARNING"):
                self.assertIsNone(phone.missed_calls_raw(0))
                self.assertIsNone(phone.notifications_raw())

    def test_unreadable_output_is_none(self):
        self.adb.call_log = "Error while accessing provider:call_log\njava.lang.SecurityException\n"
        self.adb.notifications = "Can't find service: notification\n"
        phone = self.phone()
        with self.assertLogs("services.phone_adb", "WARNING") as logs:
            self.assertIsNone(phone.missed_calls_raw(0))
            self.assertIsNone(phone.notifications_raw())
        self.assertNotIn("SecurityException", "".join(logs.output))  # a length, not the phone's text

    def test_missed_query_filters_missed_and_window(self):
        self.phone().missed_calls_raw(1791800000000)
        queries = [c for c in self.adb.shell_cmds() if c.startswith("content query")]
        self.assertEqual(len(queries), 1)
        self.assertIn("--uri content://call_log/calls", queries[0])
        self.assertIn("--projection number:date:new:name", queries[0])
        self.assertIn("type=3 AND date>=1791800000000", queries[0])
        self.assertIn('--sort "date DESC"', queries[0])

    def test_notifications_raw_uses_noredact_with_a_20s_timeout(self):
        seen = []

        def runner(argv, timeout):
            seen.append((argv, timeout))
            return self.adb(argv, timeout)

        AdbPhone(helper_dex=self.dex, runner=runner, popen=self.popen).notifications_raw()
        dumps = [(a, t) for a, t in seen if len(a) > 4 and a[4].startswith("dumpsys notification")]
        self.assertEqual([(a[4], t) for a, t in dumps], [("dumpsys notification --noredact", 20.0)])

    def test_reads_never_write_to_the_phone(self):
        self.adb.states = [IDLE, DIALING]
        self.adb.call_log = self.CALL_ROWS
        phone = self.phone()
        phone.reachable()
        phone.missed_calls_raw(1791800000000)
        phone.notifications_raw()
        phone.dial("+351912345678")
        phone.hang_up()
        phone.play_sound()
        issued = [" ".join(a) for a in self.adb.calls] + self.popened
        self.assertTrue(issued)
        for cmd in issued:
            for fragment in FORBIDDEN:
                self.assertNotIn(fragment, cmd)

    def test_ready_takes_its_discovery_from_reachable(self):
        phone = self.phone()
        with mock.patch.object(phone, "reachable", return_value="NO_PHONE") as reach:
            self.assertEqual(phone.ready(), "NO_PHONE")
        reach.assert_called_once_with()
        self.assertEqual(self.adb.calls, [])


class FromConfigTests(unittest.TestCase):
    def test_from_config_reads_phone_adb(self):
        config = config_for_tests()
        phone = AdbPhone.from_config(config)
        self.assertEqual(phone.adb_path, config["phone"]["adb"]["path"])
        self.assertEqual(phone.port, 27183)
        self.assertEqual(phone.configured_serial, config["phone"]["adb"]["serial"])
        self.assertIs(phone.mute_phone_mic, True)

    def test_from_config_defaults_when_the_block_is_missing(self):
        phone = AdbPhone.from_config({})
        self.assertEqual((phone.adb_path, phone.serial, phone.port, phone.mute_phone_mic), ("adb", "", 27183, True))

    def test_from_config_runs_nothing(self):
        def runner(*args, **kw):
            raise AssertionError("ran adb at construction")

        phone = AdbPhone.from_config(config_for_tests(), runner=runner, popen=runner)
        self.assertIs(phone._runner, runner)


if __name__ == "__main__":
    unittest.main()


class StreamTests(_Base):
    """stream(): one long-running read-only shell, for the incoming-call watcher."""

    def test_stream_is_read_only_and_needs_no_helper(self):
        from services.incoming_call import RING_LOOP

        self.adb.remote_hash = "0" * 64  # a stale helper must not be pushed by a stream either
        proc = self.phone().stream(RING_LOOP)
        self.assertIs(proc, self.procs[-1])
        self.assertEqual(self.popened, [RING_LOOP])  # the Popen argv ends with the command
        self.assertFalse(any("push" in a for a in self.adb.calls))
        for cmd in self.adb.shell_cmds() + self.popened:
            self.assertNotIn("CallAudio", cmd)
            self.assertNotIn("adbinput", cmd)
            for verb in FORBIDDEN:
                self.assertNotIn(verb, cmd)

    def test_stream_targets_the_phone(self):
        argv = []
        phone = self.phone()
        phone._popen = lambda a, **kw: argv.append(a) or FakeProc("x")
        phone.stream("true")
        self.assertEqual(argv[0][1:4], ["-s", PHONE, "shell"])
        self.assertEqual(argv[0][-1], "true")

    def test_no_phone_streams_nothing(self):
        self.adb.devices = "List of devices attached\n192.168.1.200:5555\tdevice\n"
        self.adb.mdns = "List of discovered mdns services\n"
        self.assertIsNone(self.phone().stream("true"))
        self.assertEqual(self.popened, [])

    def test_a_failing_popen_is_none_not_a_raise(self):
        phone = self.phone()
        phone._popen = mock.Mock(side_effect=OSError("adb missing"))
        with self.assertLogs("services.phone_adb", "WARNING"):
            self.assertIsNone(phone.stream("true"))
