"""Call history on the live UI: the default token, /calls, /calls/<id>, and the turn link.

Bound to loopback on port 0 like test_live_ui.py. The log is a tmpdir file,
never the real logs/calls.jsonl.
"""

import http.client
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core.event_bus import EventBus
from core.live_ui import DEFAULT_TOKEN, LiveUI, call_summary, read_calls
from tests.config_fixture import config_for_tests

LOCALHOST = "127.0.0.1"  # config-literal: the test server binds loopback, never the LAN


def _record(call_id, to="Taberna da Praia", status="booked", turn=14, transcript=True):
    record = {
        "ts": "2026-10-15T21:08:00",
        "to": to,
        "kind": "book_table",
        "status": status,
        "duration_s": 143.0,
        "outcome": {"status": status, "summary": "Table for two at 20:30."},
        "meta": {"call_id": call_id, "turn": turn, "talk_s": 130.0},
    }
    if transcript:
        record["transcript"] = [
            {"who": "them", "at": 4.0, "text": "Boa noite."},
            {"who": "california", "at": 6.0, "text": "Boa noite, a table for two?"},
        ]
    return record


class CallsServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "calls.jsonl")
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_record("20261015-210800")) + "\n")
            fh.write("not json\n")
            fh.write(json.dumps(_record("20261015-214500", to="Clinic", status="no_answer", turn=None, transcript=False)) + "\n")
        config = config_for_tests(
            ui={"enabled": True, "host": LOCALHOST, "port": 0},
            phone={"log_path": self.log},  # path: never the real logs/calls.jsonl
        )
        with patch.dict(os.environ, {"CALIFORNIA_UI_TOKEN": "tok"}):
            self.ui = LiveUI(config, EventBus())
        self.assertTrue(self.ui.start())
        self.addCleanup(self.ui.stop)
        self.port = self.ui._server.server_address[1]

    def _get(self, path):
        conn = http.client.HTTPConnection(LOCALHOST, self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_the_list_is_newest_first_without_transcripts(self):
        resp, body = self._get("/tok/calls")
        self.assertEqual(resp.status, 200)
        rows = json.loads(body)
        self.assertEqual([r["id"] for r in rows], ["20261015-214500", "20261015-210800"])
        self.assertNotIn("transcript", rows[0])
        self.assertTrue(rows[1]["has_transcript"])
        self.assertFalse(rows[0]["has_transcript"])
        self.assertEqual(rows[1]["turn"], 14)

    def test_one_call_carries_its_full_timed_transcript(self):
        resp, body = self._get("/tok/calls/20261015-210800")
        self.assertEqual(resp.status, 200)
        record = json.loads(body)
        self.assertEqual([l["at"] for l in record["transcript"]], [4.0, 6.0])
        self.assertEqual(record["outcome"]["summary"], "Table for two at 20:30.")

    def test_an_unknown_call_is_a_404(self):
        resp, _ = self._get("/tok/calls/nope")
        self.assertEqual(resp.status, 404)

    def test_calls_need_the_token(self):
        for path in ("/calls", "/wrong/calls", "/wrong/calls/20261015-210800"):
            resp, _ = self._get(path)
            self.assertEqual(resp.status, 404, path)

    def test_a_missing_log_is_an_empty_list_not_an_error(self):
        os.remove(self.log)
        resp, body = self._get("/tok/calls")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(body), [])

    def test_a_call_id_cannot_reach_other_files(self):
        resp, _ = self._get("/tok/calls/..%2F..%2Fconfig.yaml")
        self.assertEqual(resp.status, 404)

    def test_read_calls_skips_bad_lines(self):
        self.assertEqual(len(read_calls(self.log)), 2)

    def test_summary_falls_back_to_the_timestamp_for_old_lines(self):
        old = {"ts": "2026-09-01T10:00:00", "to": "Marta", "status": "unknown"}
        self.assertEqual(call_summary(old)["id"], "2026-09-01T10:00:00")


class DefaultTokenTests(unittest.TestCase):
    def test_the_url_is_california_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "CALIFORNIA_UI_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            ui = LiveUI(config_for_tests(ui={"enabled": True, "host": LOCALHOST, "port": 0}), EventBus())
        self.assertEqual(DEFAULT_TOKEN, "california")
        self.assertEqual(ui.token, "california")
        self.assertTrue(ui.url.endswith("/california/"))

    def test_the_env_and_config_still_override_it(self):
        with patch.dict(os.environ, {"CALIFORNIA_UI_TOKEN": "mine"}):
            ui = LiveUI(config_for_tests(ui={"enabled": True, "host": LOCALHOST, "port": 0}), EventBus())
        self.assertEqual(ui.token, "mine")
        env = {k: v for k, v in os.environ.items() if k != "CALIFORNIA_UI_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            ui = LiveUI(config_for_tests(ui={"enabled": True, "host": LOCALHOST, "port": 0, "token": "viaconfig"}), EventBus())
        self.assertEqual(ui.token, "viaconfig")


if __name__ == "__main__":
    unittest.main()
