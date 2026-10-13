"""The live UI's server: token, static whitelist, and the event stream.

Bound to loopback on port 0, so it never touches the LAN and never collides
with a California running on the same machine.
"""

import http.client
import json
import os
import socket
import unittest
from unittest.mock import patch

from core.event_bus import EventBus
from core.live_ui import LiveUI
from tests.config_fixture import config_for_tests

LOCALHOST = "127.0.0.1"  # config-literal: the test server binds loopback, never the LAN


def _config(port=0):
    return config_for_tests(ui={"enabled": True, "host": LOCALHOST, "port": port})


class LiveUITests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        with patch.dict(os.environ, {"CALIFORNIA_UI_TOKEN": "s3cret-token"}):
            self.ui = LiveUI(_config(), self.bus)
        self.assertTrue(self.ui.start())
        self.addCleanup(self.ui.stop)
        self.port = self.ui._server.server_address[1]

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection(LOCALHOST, self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        return conn, conn.getresponse()

    def test_the_page_is_served_under_the_token(self):
        conn, resp = self._get("/s3cret-token/")
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"California Live", body)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_assets_are_served_under_the_token(self):
        for name in ("app.js", "app.css", "halo.js"):
            conn, resp = self._get(f"/s3cret-token/{name}")
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 200, name)

    def test_a_missing_or_wrong_token_is_a_404(self):
        for path in ("/", "/index.html", "/wrong/", "/wrong/events", "/s3cret-tokenX/"):
            conn, resp = self._get(path)
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)

    def test_nothing_outside_the_whitelist_is_served(self):
        for path in ("/s3cret-token/../config.yaml", "/s3cret-token/..%2Fconfig.yaml",
                     "/s3cret-token/ui/app.js", "/s3cret-token/.env"):
            conn, resp = self._get(path)
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)

    def _read_events(self, resp, count):
        events, current = [], {}
        while len(events) < count:
            line = resp.fp.readline().decode("utf-8").rstrip("\n")
            if not line:
                if current:
                    events.append(current)
                    current = {}
                continue
            key, _, value = line.partition(": ")
            current[key] = value
        return events

    def test_the_stream_replays_the_session_then_goes_live(self):
        self.bus.publish("state", {"state": "idle"})
        conn, resp = self._get("/s3cret-token/events")
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        hello, replayed, marker = self._read_events(resp, 3)
        self.assertEqual(hello["event"], "hello")
        self.assertEqual(replayed["event"], "state")
        # The end of the backlog is marked, so the page can tell history from
        # now and never animate a finished tool call as if it were new.
        self.assertEqual(marker["event"], "replayed")
        self.assertIn('"count": 1', marker["data"])
        self.assertNotIn("id", marker)
        self.bus.publish("turn_mark", {"name": "stt_done", "ms": 812})
        (live,) = self._read_events(resp, 1)
        self.assertEqual(live["event"], "turn_mark")
        self.assertIn('"stt_done"', live["data"])
        conn.close()

    def test_last_event_id_resumes_without_duplicates(self):
        for i in range(3):
            self.bus.publish("x", {"i": i})
        last = self.bus.subscribe()[0][1]["id"]
        conn, resp = self._get("/s3cret-token/events", {"Last-Event-ID": str(last)})
        hello, only, marker = self._read_events(resp, 3)
        self.assertEqual(only["event"], "x")
        self.assertIn('"i": 2', only["data"])
        self.assertEqual(marker["event"], "replayed")
        conn.close()

    def test_hello_carries_the_thresholds_the_page_draws(self):
        conn, resp = self._get("/s3cret-token/events")
        (hello,) = self._read_events(resp, 1)
        conn.close()
        self.assertIn('"threshold"', hello["data"])
        self.assertIn('"barge_in_threshold"', hello["data"])

    def test_vendored_three_is_served_under_the_token(self):
        conn, resp = self._get("/s3cret-token/vendor/three.min.js")
        body = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/javascript"))
        self.assertIn(b"@license", body[:200])
        self.assertIn(b"THREE", body[:2000])

    def test_vendored_three_is_404_without_the_token(self):
        for path in ("/vendor/three.min.js", "/wrong/vendor/three.min.js",
                     "/s3cret-token/vendor/../app.js", "/s3cret-token/vendor/",
                     "/s3cret-token/vendor/README.md"):
            conn, resp = self._get(path)
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)

    def test_vendored_three_is_cacheable_and_the_page_is_not(self):
        conn, resp = self._get("/s3cret-token/vendor/three.min.js")
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Cache-Control"), "public, max-age=86400, immutable")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        for name in ("", "app.js", "app.css"):
            conn, resp = self._get(f"/s3cret-token/{name}")
            resp.read()
            conn.close()
            self.assertEqual(resp.getheader("Cache-Control"), "no-store", name)
            self.assertIn("worker-src 'none'", resp.getheader("Content-Security-Policy"))

    def test_hello_carries_voice_offset_palette_mood_hold_and_halo(self):
        conn, resp = self._get("/s3cret-token/events")
        (hello,) = self._read_events(resp, 1)
        conn.close()
        data = json.loads(hello["data"])
        shipped = config_for_tests()["ui"]
        self.assertEqual(data["voice_offset_ms"], shipped["voice_offset_ms"])
        self.assertEqual(data["mood_hold_ms"], shipped["mood_hold_ms"])
        self.assertEqual(data["palette"], "dusk")
        self.assertIs(data["halo"], True)

    def test_hello_defaults_when_the_halo_keys_are_absent(self):
        from core.live_ui import _hello

        data = _hello({"ui": {"halo": False, "voice_offset_ms": "nonsense"}})
        self.assertIs(data["halo"], False)
        self.assertEqual(data["voice_offset_ms"], 120)
        self.assertEqual(data["mood_hold_ms"], 6000)
        self.assertEqual(data["palette"], "dusk")


class LiveUILifecycleTests(unittest.TestCase):
    def test_disabled_does_not_bind(self):
        ui = LiveUI(config_for_tests(), EventBus())  # the fixture turns ui off
        self.assertFalse(ui.start())
        self.assertIsNone(ui._server)

    def test_a_taken_port_disables_the_ui_instead_of_raising(self):
        holder = socket.socket()
        holder.bind((LOCALHOST, 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        ui = LiveUI(_config(port=port), EventBus())
        self.assertFalse(ui.start())
        ui.stop()  # safe on a UI that never started

    def test_without_a_configured_token_one_is_generated(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALIFORNIA_UI_TOKEN", None)
            a = LiveUI(_config(), EventBus()).token
            b = LiveUI(_config(), EventBus()).token
        self.assertGreaterEqual(len(a), 16)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
