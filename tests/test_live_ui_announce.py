"""
The boot announcement of the live page: WhatsApp the link, open it in Firefox.

Nothing here starts Firefox, runs tasklist or sends a message: subprocess and
the WhatsApp service are always stubbed, and the guard test proves it.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import live_ui_announce as a
from services.whatsapp_service import _MSG_BUSY, ContactMatch, WhatsAppCommandResult

URL = "http://california.test:8765/tok/"


def _mozlz4(payload: bytes) -> bytes:
    """A valid .jsonlz4 holding `payload` as literals only (no matches)."""
    n = len(payload)
    if n < 15:
        block = bytes([n << 4]) + payload
    else:
        extra, rest = bytearray(), n - 15
        while rest >= 255:
            extra.append(255)
            rest -= 255
        extra.append(rest)
        block = bytes([0xF0]) + bytes(extra) + payload
    return a._MOZLZ4_MAGIC + n.to_bytes(4, "little") + block


def _profiles(urls) -> Path:
    root = Path(tempfile.mkdtemp())
    store = root / "abc.default-release" / "sessionstore-backups"
    store.mkdir(parents=True)
    session = {"windows": [{"tabs": [{"entries": [{"url": u}]} for u in urls]}]}
    (store / "recovery.jsonlz4").write_bytes(_mozlz4(json.dumps(session).encode()))
    return root


class Lz4Tests(unittest.TestCase):
    def test_literals_and_an_overlapping_match(self):
        # "abcd", then copy 8 bytes from 4 back (overlapping), then "!".
        block = bytes([0x44]) + b"abcd" + (4).to_bytes(2, "little") + bytes([0x10]) + b"!"
        self.assertEqual(a.lz4_block_decompress(block, 13), b"abcdabcdabcd!")

    def test_long_literal_run_uses_the_extension_bytes(self):
        payload = bytes(range(256)) * 2
        self.assertEqual(a.lz4_block_decompress(_mozlz4(payload)[12:], len(payload)), payload)

    def test_a_wrong_size_is_an_error_not_a_bad_read(self):
        with self.assertRaises(ValueError):
            a.lz4_block_decompress(bytes([0x30]) + b"abc", 5)


class OpenTabsTests(unittest.TestCase):
    def test_every_open_tab_url_is_read_from_the_session_file(self):
        self.assertEqual(a.open_tab_urls(_profiles([URL, "https://x.test/"])), {URL, "https://x.test/"})

    def test_an_unreadable_session_file_is_skipped(self):
        root = _profiles([URL])
        next(root.glob("*/sessionstore-backups/recovery.jsonlz4")).write_bytes(b"junk")
        self.assertEqual(a.open_tab_urls(root), set())


class OpenInFirefoxTests(unittest.TestCase):
    def setUp(self):
        self.firefox = Path(tempfile.mkdtemp()) / "firefox.exe"
        self.firefox.write_bytes(b"")

    def _open(self, urls, running):
        with patch.object(a, "firefox_running", return_value=running), \
             patch.object(a.subprocess, "Popen") as popen:
            result = a.open_in_firefox(URL, str(self.firefox), _profiles(urls))
        return result, popen

    def test_a_tab_already_showing_the_page_is_left_alone(self):
        result, popen = self._open([URL], running=True)
        self.assertEqual(result, "already_open")
        popen.assert_not_called()

    def test_otherwise_a_new_tab_is_opened(self):
        result, popen = self._open(["http://california.test:8765/old-token/"], running=True)
        self.assertEqual(result, "opened")
        self.assertEqual(popen.call_args.args[0], [str(self.firefox), "-new-tab", URL])

    def test_a_closed_firefox_opens_even_if_the_last_session_had_it(self):
        # Firefox keeps the session file after a quit; it only counts while running.
        result, popen = self._open([URL], running=False)
        self.assertEqual(result, "opened")
        popen.assert_called_once()

    def test_a_missing_firefox_is_a_log_line(self):
        with patch.object(a, "firefox_running", return_value=False), \
             patch.object(a.subprocess, "Popen") as popen:
            self.assertEqual(a.open_in_firefox(URL, str(self.firefox) + ".gone", _profiles([])), "failed")
        popen.assert_not_called()


def _wa(backend="playwright", results=None):
    svc = Mock(enabled=True, backend=backend)
    svc.resolve_contact.return_value = ContactMatch(key="+351933852719", phone="+351933852719", certain=True)
    svc.send.side_effect = results or [WhatsAppCommandResult(True, "sent")]
    return svc


class SendLinkTests(unittest.TestCase):
    def test_the_link_goes_to_the_number_once(self):
        svc = _wa()
        self.assertTrue(a.send_link(svc, "933852719", URL))
        svc.resolve_contact.assert_called_once_with("933852719")
        # Alone on its own line, so WhatsApp makes it a link.
        self.assertEqual(svc.send.call_args.args[1].splitlines()[-1], URL)

    def test_the_keyboard_backend_never_sends_at_boot(self):
        svc = _wa(backend="keyboard")
        self.assertFalse(a.send_link(svc, "933852719", URL))
        svc.send.assert_not_called()

    def test_a_busy_refusal_is_retried(self):
        svc = _wa(results=[WhatsAppCommandResult(False, _MSG_BUSY), WhatsAppCommandResult(True, "sent")])
        with patch.object(a.time, "sleep"):
            self.assertTrue(a.send_link(svc, "933852719", URL))
        self.assertEqual(svc.send.call_count, 2)

    def test_any_other_failure_is_not_retried(self):
        # A timeout may still send later; a retry would send the link twice.
        svc = _wa(results=[WhatsAppCommandResult(False, "I couldn't reach WhatsApp.")])
        self.assertFalse(a.send_link(svc, "933852719", URL))
        svc.send.assert_called_once()

    def test_a_disabled_whatsapp_sends_nothing(self):
        svc = _wa()
        svc.enabled = False
        self.assertFalse(a.send_link(svc, "933852719", URL))
        svc.send.assert_not_called()


class AnnounceTests(unittest.TestCase):
    def test_no_number_means_no_message(self):
        svc = _wa()
        with patch.dict(os.environ, {"CALIFORNIA_UI_NOTIFY_NUMBER": ""}), \
             patch.object(a, "open_in_firefox"):
            a.announce(URL, svc, {"ui": {}})
        svc.send.assert_not_called()

    def test_a_firefox_failure_does_not_stop_the_message(self):
        svc = _wa()
        with patch.dict(os.environ, {"CALIFORNIA_UI_NOTIFY_NUMBER": "933852719"}), \
             patch.object(a, "open_in_firefox", side_effect=RuntimeError("boom")):
            a.announce(URL, svc, {"ui": {}})
        svc.send.assert_called_once()

    def test_both_steps_can_be_switched_off(self):
        svc = _wa()
        with patch.dict(os.environ, {"CALIFORNIA_UI_NOTIFY_NUMBER": "933852719"}), \
             patch.object(a, "open_in_firefox") as ff:
            a.announce(URL, svc, {"ui": {"announce": {"open_firefox": False, "whatsapp": False}}})
        ff.assert_not_called()
        svc.send.assert_not_called()

    def test_this_file_never_starts_a_process(self):
        with patch.object(a.subprocess, "Popen", side_effect=AssertionError("Popen")), \
             patch.object(a.subprocess, "run", side_effect=AssertionError("run")):
            self.test_a_firefox_failure_does_not_stop_the_message()
            self.test_no_number_means_no_message()


if __name__ == "__main__":
    unittest.main()
