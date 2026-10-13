"""ContactSync: the phone's contact book over ADB, folded, snapshotted and diffed.

Every name and number here is invented, with the shape of the real thing
(Portuguese mobiles and landlines). No test touches adb: the phone is a Mock.
"""

import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.contact_sync import (
    PROJECTION,
    URI,
    ContactSync,
    ContactSyncResult,
    contacts_from_rows,
    diff_counts,
    load_snapshot,
    write_snapshot,
)
from services.whatsapp_service import Contact
from tests.config_fixture import config_for_tests

T0 = datetime(2026, 10, 13, 18, 2, 11, tzinfo=timezone(timedelta(hours=1)))


def row(cid, name, number, kind="2", acct="com.google", primary="0"):
    return {
        "contact_id": str(cid),
        "data2": kind,
        "is_super_primary": primary,
        "account_type": acct,
        "data1": number,
        "display_name": name,
    }


THREE = [
    row(1, "Ana Exemplo", "912000001"),
    row(2, "Bruno Teste", "912000002"),
    row(3, "Carla Modelo", "212000003", "1"),
]


class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FoldingTests(unittest.TestCase):
    def test_rows_fold_per_contact_and_drop_account_duplicates(self):
        rows = [row(7, "Ana Exemplo", "+351 912 000 001", "2", acct="com.google"),
                row(7, "Ana Exemplo", "912000001", "2", acct="com.whatsapp"),
                row(7, "Ana Exemplo", "212000003", "1")]
        [c] = contacts_from_rows(rows, "351")
        self.assertEqual(c.name, "Ana Exemplo")
        self.assertEqual(c.phones, ["+351912000001", "+351212000003"])

    def test_super_primary_number_comes_first(self):
        rows = [row(7, "Ana Exemplo", "912000001", "2"),
                row(7, "Ana Exemplo", "212000003", "1", primary="1")]
        [c] = contacts_from_rows(rows, "351")
        self.assertEqual(c.phones[0], "+351212000003")

    def test_pause_suffix_is_cut_before_normalising(self):
        [c] = contacts_from_rows([row(1, "Linha Fixa", "212000000,,12")], "351")
        self.assertEqual(c.phones, ["+351212000000"])
        [c] = contacts_from_rows([row(1, "Linha Fixa", "212000000;34")], "351")
        self.assertEqual(c.phones, ["+351212000000"])

    def test_same_name_two_ids_stays_two_contacts(self):
        rows = [row(1, "Rita", "912000011"), row(2, "Rita", "912000012")]
        self.assertEqual(len(contacts_from_rows(rows, "351")), 2)

    def test_nameless_or_numberless_groups_are_dropped(self):
        rows = [row(1, None, "912000001"), row(2, "Servico", "111"), row(3, "", "912000003")]
        self.assertEqual(contacts_from_rows(rows, "351"), [])

    def test_name_is_first_non_empty_and_order_is_first_seen(self):
        rows = [row(5, None, "912000005"), row(4, "Bruno Teste", "912000004"),
                row(5, "Eva Exemplo", "912000006")]
        names = [c.name for c in contacts_from_rows(rows, "351")]
        self.assertEqual(names, ["Eva Exemplo", "Bruno Teste"])

    def test_projection_keeps_display_name_last(self):
        self.assertEqual(PROJECTION[-1], "display_name")
        self.assertEqual(URI, "content://com.android.contacts/data/phones")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "phone_contacts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_round_trips(self):
        contacts = [Contact("Ana Exemplo", ["+351912000001"]), Contact("Zé 🫰🏽", ["+351212000003", "+351912000004"])]
        write_snapshot(self.path, contacts, T0)
        loaded, synced_at = load_snapshot(self.path)
        self.assertEqual([(c.name, c.phones) for c in loaded], [(c.name, c.phones) for c in contacts])
        self.assertEqual(synced_at, T0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["source"], "adb")

    def test_write_is_atomic(self):
        write_snapshot(self.path, [Contact("Ana Exemplo", ["+351912000001"])], T0)
        before = self.path.read_bytes()
        with mock.patch("services.contact_sync.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                write_snapshot(self.path, [Contact("Outro", ["+351912000009"])], T0 + timedelta(days=1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_snapshot_is_empty(self):
        self.assertEqual(load_snapshot(self.path), ([], None))

    def test_a_corrupt_snapshot_makes_a_sync_due(self):
        self.path.write_text("{", encoding="utf-8")
        with self.assertLogs("services.contact_sync", level="WARNING"):
            self.assertEqual(load_snapshot(self.path), ([], None))
        sync = _make_sync(self.path, mock.Mock())
        with self.assertLogs("services.contact_sync", level="WARNING"):
            self.assertTrue(sync.due())


class DiffTests(unittest.TestCase):
    def test_diff_counts(self):
        old = [Contact("A", ["+351912000001"]), Contact("B", ["+351912000002"]), Contact("C", ["+351912000003"])]
        new = [Contact("a", ["+351912000001"]), Contact("B", ["+351912000022"]), Contact("D", ["+351912000004"])]
        self.assertEqual(diff_counts(old, new), (1, 1, 1))

    def test_duplicate_names_are_a_multiset(self):
        old = [Contact("Rita", ["+351912000011"])]
        new = [Contact("Rita", ["+351912000011"]), Contact("Rita", ["+351912000012"])]
        self.assertEqual(diff_counts(old, new), (0, 0, 1))
        self.assertEqual(diff_counts(new, list(reversed(new))), (0, 0, 0))


def _make_sync(path, phone, now=None, on_synced=None, **cfg):
    config = config_for_tests(contacts_sync={"enabled": True, "snapshot_path": str(path), **cfg})
    with mock.patch("sys.platform", "win32"), mock.patch("shutil.which", return_value="adb"):
        return ContactSync(config, phone=phone, on_synced=on_synced, now=now or _Clock(T0))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "phone_contacts.json"
        self.phone = mock.Mock()
        self.phone.query.return_value = list(THREE)
        self.phone.last_status = "OK"
        self.clock = _Clock(T0)
        self.on_synced = mock.Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **cfg):
        return _make_sync(self.path, self.phone, now=self.clock, on_synced=self.on_synced, **cfg)

    def test_first_sync_writes_and_reports(self):
        result = self.make().sync()
        self.assertIsInstance(result, ContactSyncResult)
        self.assertEqual(result.status, "SYNCED")
        self.assertTrue(result)
        self.assertEqual((result.count, result.added, result.removed, result.changed), (3, 3, 0, 0))
        self.assertEqual(result.message, "Contacts refreshed from your phone: 3, 3 new, 0 gone, 0 changed.")
        self.assertTrue(self.path.exists())
        self.on_synced.assert_called_once_with()
        self.phone.query.assert_called_once_with(URI, PROJECTION)

    def test_unchanged_still_moves_synced_at(self):
        sync = self.make()
        sync.sync()
        self.clock.t = T0 + timedelta(days=7)
        result = sync.sync()
        self.assertEqual(result.status, "UNCHANGED")
        self.assertTrue(result)
        self.assertEqual(result.message, "Your contacts were already up to date: 3 from your phone.")
        self.assertEqual(load_snapshot(self.path)[1], T0 + timedelta(days=7))
        self.assertEqual(self.on_synced.call_count, 2)

    def test_zero_rows_is_unreadable_not_an_empty_book(self):
        write_snapshot(self.path, [Contact("Ana Exemplo", ["+351912000001"])], T0)
        before = self.path.read_bytes()
        self.phone.query.return_value = []
        result = self.make().sync()
        self.assertEqual(result.status, "UNREADABLE")
        self.assertFalse(result)
        self.assertEqual(result.message, "Your phone wouldn't hand over its contacts just now. I kept the ones I had.")
        self.assertEqual(self.path.read_bytes(), before)
        self.on_synced.assert_not_called()

    def test_rows_that_fold_to_nothing_are_unreadable(self):
        self.phone.query.return_value = [row(1, None, "912000001")]
        self.assertEqual(self.make().sync().status, "UNREADABLE")
        self.assertFalse(self.path.exists())

    def test_a_shrunken_book_does_not_overwrite_the_snapshot(self):
        old = [Contact(f"Pessoa {i}", [f"+3519120{i:05d}"]) for i in range(400)]
        write_snapshot(self.path, old, T0 - timedelta(days=8))
        before = self.path.read_bytes()
        self.phone.query.return_value = [row(i, f"Pessoa {i}", f"9120{i:05d}") for i in range(150)]
        with self.assertLogs("services.contact_sync", level="WARNING"):
            result = self.make().sync()
        self.assertEqual(result.status, "SHRUNK")
        self.assertEqual((result.count, result.previous), (150, 400))
        self.assertEqual(result.message,
                         "Your phone only gave me 150 contacts against 400 last time, so I kept the old list.")
        self.assertEqual(self.path.read_bytes(), before)
        self.on_synced.assert_not_called()

    def test_no_phone_keeps_the_book_and_names_its_age(self):
        write_snapshot(self.path, [Contact("Ana Exemplo", ["+351912000001"])], T0 - timedelta(days=3, hours=2))
        self.phone.query.return_value = None
        self.phone.last_status = "NO_PHONE"
        result = self.make().sync()
        self.assertEqual(result.status, "NO_PHONE")
        self.assertEqual(result.message,
                         "I can't reach your phone right now, so I'm keeping the contacts from 3 days ago.")

    def test_no_phone_without_a_snapshot(self):
        self.phone.query.return_value = None
        self.phone.last_status = "NO_PHONE"
        result = self.make().sync()
        self.assertEqual(result.message,
                         "I can't reach your phone right now, so I'm keeping the contact book I have.")

    def test_age_words(self):
        sync = self.make()
        for age, words in ((timedelta(hours=5), "today"), (timedelta(days=1, hours=1), "yesterday"),
                           (timedelta(days=9), "9 days ago")):
            write_snapshot(self.path, [Contact("Ana Exemplo", ["+351912000001"])], T0 - age)
            self.phone.query.return_value = None
            self.phone.last_status = "NO_PHONE"
            self.assertTrue(sync.sync().message.endswith(f"keeping the contacts from {words}."))

    def test_locked_down_has_its_own_line(self):
        self.phone.query.return_value = None
        self.phone.last_status = "LOCKED_DOWN"
        result = self.make().sync()
        self.assertEqual(result.status, "LOCKED_DOWN")
        self.assertEqual(result.message,
                         "The phone has USB debugging (Security settings) switched off, so I can't read its contacts.")

    def test_any_other_failure_is_unreadable(self):
        self.phone.query.return_value = None
        self.phone.last_status = "UNREADABLE"
        self.assertEqual(self.make().sync().status, "UNREADABLE")

    def test_a_second_sync_while_one_runs_is_busy(self):
        started, release = threading.Event(), threading.Event()

        def slow(*_a, **_k):
            started.set()
            release.wait(5)
            return list(THREE)

        self.phone.query.side_effect = slow
        sync = self.make()
        results = []
        t = threading.Thread(target=lambda: results.append(sync.sync()))
        t.start()
        self.assertTrue(started.wait(5))
        busy = sync.sync()
        release.set()
        t.join(5)
        self.assertEqual(busy.status, "BUSY")
        self.assertEqual(busy.message, "I'm already refreshing your contacts.")
        self.assertEqual(results[0].status, "SYNCED")
        self.assertEqual(self.phone.query.call_count, 1)

    def test_on_synced_raising_does_not_fail_the_sync(self):
        self.on_synced.side_effect = RuntimeError("boom")
        with self.assertLogs("services.contact_sync", level="WARNING"):
            result = self.make().sync()
        self.assertEqual(result.status, "SYNCED")

    def test_due_after_seven_days(self):
        sync = self.make()
        self.assertTrue(sync.due())  # never synced
        sync.sync()
        self.clock.t = T0 + timedelta(days=6, hours=23)
        self.assertFalse(sync.due())
        self.clock.t = T0 + timedelta(days=7)
        self.assertTrue(sync.due())
        self.assertEqual(sync.age_s(), 7 * 86400)

    def test_settings_and_floors(self):
        sync = self.make(interval_days=0, check_interval_minutes=30)
        self.assertEqual(sync.interval_days, 1)
        self.assertEqual(sync.check_interval_minutes, 30)
        self.assertEqual(sync.snapshot_path, self.path)

    def test_disabled_off_windows_and_without_a_consumer(self):
        cases = [
            ({"contacts_sync": {"enabled": False, "snapshot_path": str(self.path)}}, "win32"),
            ({"contacts_sync": {"enabled": True, "snapshot_path": str(self.path)}}, "linux"),
            ({"contacts_sync": {"enabled": True, "snapshot_path": str(self.path)},
              "whatsapp": {"enabled": False}, "phone": {"enabled": False}}, "win32"),
        ]
        for overrides, platform in cases:
            config = config_for_tests(**overrides)
            with mock.patch("sys.platform", platform), mock.patch("shutil.which", return_value="adb"):
                sync = ContactSync(config, phone=self.phone, now=self.clock)
            self.assertFalse(sync.enabled)
            self.assertFalse(sync.due())
            result = sync.sync()
            self.assertEqual(result.status, "DISABLED")
            self.assertEqual(result.message, "Contact sync isn't set up.")
        self.phone.query.assert_not_called()

    def test_disabled_without_adb(self):
        config = config_for_tests(contacts_sync={"enabled": True, "snapshot_path": str(self.path)})
        with mock.patch("sys.platform", "win32"), mock.patch("shutil.which", return_value=None), \
                mock.patch("os.path.isfile", return_value=False):
            sync = ContactSync(config, phone=self.phone, now=self.clock)
        self.assertFalse(sync.enabled)

    def test_construction_runs_no_adb(self):
        config = config_for_tests(contacts_sync={"enabled": True, "snapshot_path": str(self.path)})
        with mock.patch("sys.platform", "win32"), mock.patch("shutil.which", return_value="adb"), \
                mock.patch("services.contact_sync.AdbPhone.from_config", side_effect=AssertionError("adb")):
            sync = ContactSync(config)
        self.assertTrue(sync.enabled)

    def test_phone_is_built_lazily_on_first_sync(self):
        config = config_for_tests(contacts_sync={"enabled": True, "snapshot_path": str(self.path)})
        with mock.patch("sys.platform", "win32"), mock.patch("shutil.which", return_value="adb"):
            sync = ContactSync(config, now=self.clock)
        with mock.patch("services.contact_sync.AdbPhone.from_config", return_value=self.phone) as build:
            self.assertEqual(sync.sync().status, "SYNCED")
            sync.sync()
        build.assert_called_once()

    def test_logs_and_messages_carry_no_names_or_numbers(self):
        with self.assertLogs("services.contact_sync", level="DEBUG") as logs:
            result = self.make().sync()
        self.assertEqual(result.status, "SYNCED")
        for text in [*logs.output, result.message]:
            self.assertNotIn("Ana", text)
            self.assertNotIn("912000001", text)
        self.assertTrue(any("Contacts synced from phone: 3 contacts (+3 -0 ~0)" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
