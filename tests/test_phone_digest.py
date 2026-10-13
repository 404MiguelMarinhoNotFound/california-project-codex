"""
Parsers for the missed-call / phone-notification digest (services/phone_digest.py).

Pure text in, dataclasses out: no adb, no phone. The notification fixture is
synthetic, laid out like a real `dumpsys notification --noredact` capture
(probed 2026-10-13), with invented names and numbers.
"""

import os
import unittest

from services.phone_digest import (
    CALL_LOG_PROJECTION,
    IGNORED_FLAGS,
    MissedCall,
    parse_call_log,
    parse_notifications,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "phone_notification_dump.txt")


def _dump() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


class ParseNotificationsTests(unittest.TestCase):
    def setUp(self):
        self.items = parse_notifications(_dump())

    def test_keeps_only_real_waiting_notifications(self):
        # Dropped: an ongoing media player, a group summary, a missed call (the
        # call log covers those), a record with no title or text, an exact
        # duplicate, and everything under "Snoozed notifications".
        self.assertEqual(
            [(n.package, n.title) for n in self.items],
            [("com.whatsapp", "Rita"), ("com.whatsapp", "Jantar sábado"), ("com.google.android.gm", "Booking.com")],
        )

    def test_multiline_and_awkward_text_is_kept_whole(self):
        self.assertEqual(self.items[1].text, "Ana: (amanhã) às 20h,\nleva vinho = ok?")

    def test_reads_seen_and_when(self):
        self.assertIs(self.items[0].seen, False)
        self.assertEqual(self.items[0].when_ms, 1791900000000)
        self.assertIs(self.items[1].seen, True)

    def test_public_version_never_overrides(self):
        self.assertEqual(self.items[0].title, "Rita")
        self.assertEqual(self.items[0].text, "Já cheguei")

    def test_empty_or_garbage_dump_is_empty_list(self):
        self.assertEqual(parse_notifications(""), [])
        self.assertEqual(parse_notifications("Can't find service: notification"), [])

    def test_crlf_output_parses_the_same(self):
        self.assertEqual(parse_notifications(_dump().replace("\n", "\r\n")), self.items)

    def test_ignored_flags_are_the_three_documented(self):
        self.assertEqual(IGNORED_FLAGS, ("ONGOING_EVENT", "FOREGROUND_SERVICE", "GROUP_SUMMARY"))


def _rows(*rows: str) -> str:
    return "".join(f"Row: {i} {r}\n" for i, r in enumerate(rows))


class ParseCallLogTests(unittest.TestCase):
    # Projection is number:date:new:name -- the free-text cached name goes last,
    # which is what parse_content_rows needs to keep a comma inside it whole.

    def test_projection_puts_the_name_last(self):
        self.assertEqual(CALL_LOG_PROJECTION, ("number", "date", "new", "name"))

    def test_rows_become_missed_calls(self):
        out = parse_call_log(_rows("number=+351910000001, date=1791900000000, new=1, name=Rita"))
        self.assertEqual(out, [MissedCall("+351910000001", "Rita", 1791900000000, True)])

    def test_name_with_comma_is_one_field(self):
        out = parse_call_log(_rows("number=+351910000002, date=1791900000000, new=0, name=Silva, João"))
        self.assertEqual(out[0].name, "Silva, João")
        self.assertIs(out[0].unseen, False)

    def test_null_name_is_empty(self):
        out = parse_call_log(_rows("number=+351910000003, date=1791900000000, new=1, name=NULL"))
        self.assertEqual(out[0].name, "")

    def test_no_result_is_empty_list(self):
        self.assertEqual(parse_call_log("No result found.\n"), [])

    def test_rows_keep_their_order(self):
        out = parse_call_log(_rows(
            "number=+351910000004, date=1791900000002, new=1, name=NULL",
            "number=+351910000005, date=1791900000001, new=1, name=Rita",
        ))
        self.assertEqual([c.number for c in out], ["+351910000004", "+351910000005"])

    def test_row_with_unreadable_date_is_skipped(self):
        out = parse_call_log(_rows(
            "number=+351910000006, date=NULL, new=1, name=Rita",
            "number=+351910000007, date=1791900000000, new=1, name=NULL",
        ))
        self.assertEqual([c.number for c in out], ["+351910000007"])


if __name__ == "__main__":
    unittest.main()
