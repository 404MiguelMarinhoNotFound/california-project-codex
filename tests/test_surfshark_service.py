import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call
from unittest.mock import Mock
from unittest.mock import patch

from services.surfshark_service import SurfsharkService


CONNECTED_ALBANIA_XML = """
<hierarchy>
  <node text="Connected" bounds="[100,100][600,180]" />
  <node text="Albania" bounds="[100,220][600,300]" />
  <node text="Portugal" bounds="[100,900][600,980]" />
</hierarchy>
"""


class SurfsharkServiceTests(unittest.TestCase):
    def _write_route_table(self, route_path: Path, sequence: list[str] | None = None) -> None:
        route_path.write_text(
            json.dumps(
                {
                    "routes": {
                        "restart_autoconnect": {
                            "launch_mode": "package",
                            "force_stop_before_launch": True,
                            "assumed_country": "albania",
                            "sequence": [],
                        },
                        "quick_connect": {
                            "launch_mode": "package",
                            "wait_for_ready": True,
                            "retry_force_stop_on_failure": True,
                            "assumed_country": "portugal",
                            "sequence": sequence or ["DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    def _config(self, state_path: Path, route_path: Path | None = None) -> dict:
        return {
            "media": {
                "vpn_routing_enabled": True,
                "vpn_state_path": str(state_path),
                "surfshark_route_table_path": str(route_path) if route_path else "surfshark_routes.json",
                "vpn_status_cache_max_age_minutes": 5,
                "vpn_failure_policy": "open_anyway",
                "vpn_route_by_app": {
                    "youtube": "restart_autoconnect",
                    "stremio": "quick_connect",
                },
                "surfshark_launch_delay_ms": 1,
                "surfshark_connect_timeout_ms": 50,
                "surfshark_status_poll_interval_ms": 1,
                "surfshark_pre_sequence_wait_ms": 1,
                "surfshark_key_delay_ms": 1,
                "surfshark_post_sequence_wait_ms": 1,
                "surfshark_restart_autoconnect_wait_ms": 1,
                "surfshark_ready_timeout_ms": 1,
                "surfshark_ready_poll_interval_ms": 1,
                "surfshark_ready_stable_polls": 2,
                "surfshark_ready_settle_ms": 1,
                "surfshark_retry_count": 1,
                "surfshark_debug_capture_enabled": False,
                "surfshark_debug_capture_dir": "debug/surfshark",
                "surfshark_quick_connect_aliases": [
                    "quick_connect",
                    "quick connect",
                    "fastest",
                    "fastest location",
                ],
                "surfshark_quick_connect_sequence": ["DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"],
                "surfshark_country_aliases": {
                    "albania": ["albania"],
                    "portugal": ["portugal"],
                },
                "surfshark_connected_markers": ["connected", "protected"],
                "surfshark_disconnected_markers": ["disconnected", "not connected", "unprotected"],
            }
        }

    def _media_service(self) -> Mock:
        media_service = Mock()
        media_service.launch_app.return_value = (True, "Opening surfshark")
        media_service.launch_package.return_value = (True, "Opening surfshark")
        media_service.force_stop_app.return_value = True
        media_service.keyevent.return_value = True
        media_service.capture_screenshot.return_value = True
        media_service.dump_ui_hierarchy.return_value = CONNECTED_ALBANIA_XML
        media_service.get_current_focus.return_value = (
            "com.surfshark.vpnclient.android/"
            "com.surfshark.vpnclient.android.legacyapp.tv.feature.main.TvMainActivity"
        )
        return media_service

    def test_get_status_uses_fresh_authoritative_cache_without_launching_surfshark(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "connected": True,
                        "country": "albania",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                        "source": "surfshark_ui",
                    }
                ),
                encoding="utf-8",
            )

            media_service = self._media_service()
            svc = SurfsharkService(self._config(state_path), media_service)

            status = svc.get_status()

            self.assertTrue(status.connected)
            self.assertEqual(status.country, "albania")
            self.assertEqual(status.source, "cache")
            self.assertEqual(status.cache_source, "surfshark_ui")
            media_service.launch_app.assert_not_called()

    def test_get_status_ignores_non_authoritative_cache_and_refreshes_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "connected": True,
                        "country": "albania",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                        "source": "surfshark_route_restart_autoconnect",
                    }
                ),
                encoding="utf-8",
            )

            media_service = self._media_service()
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                status = svc.get_status()

            self.assertTrue(status.connected)
            self.assertEqual(status.country, "albania")
            media_service.launch_app.assert_called_once_with("surfshark")

    def test_parse_status_detects_connected_country(self):
        svc = SurfsharkService(self._config(Path("vpn_state.json")), self._media_service())

        status = svc._parse_status_from_xml(CONNECTED_ALBANIA_XML)

        self.assertTrue(status.connected)
        self.assertEqual(status.country, "albania")

    def test_restart_autoconnect_force_stops_and_launches_surfshark(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            media_service = self._media_service()
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json", route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_route("restart_autoconnect")

            self.assertTrue(result.success)
            self.assertEqual(result.current_country, "albania")
            media_service.force_stop_app.assert_called_once_with("surfshark")
            media_service.launch_package.assert_called_once_with("surfshark")
            media_service.keyevent.assert_not_called()

    def test_restart_autoconnect_does_not_skip_when_route_cache_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            state_path.write_text(
                json.dumps(
                    {
                        "connected": True,
                        "country": "albania",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                        "source": "surfshark_route_restart_autoconnect",
                    }
                ),
                encoding="utf-8",
            )
            media_service = self._media_service()
            svc = SurfsharkService(self._config(state_path, route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_route("restart_autoconnect")

            self.assertTrue(result.success)
            media_service.force_stop_app.assert_called_once_with("surfshark")
            media_service.launch_package.assert_called_once_with("surfshark")

    def test_quick_connect_route_sends_configured_dpad_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            media_service = self._media_service()
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json", route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_route("quick_connect")

            self.assertTrue(result.success)
            self.assertEqual(result.current_country, "portugal")
            media_service.force_stop_app.assert_not_called()
            media_service.launch_package.assert_called_once_with("surfshark")
            media_service.keyevent.assert_has_calls(
                [call("DPAD_DOWN"), call("DPAD_DOWN"), call("DPAD_CENTER")]
            )

    def test_quick_connect_route_retries_with_force_stop_after_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            media_service = self._media_service()
            media_service.keyevent.side_effect = [False, True, True, True]
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json", route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_route("quick_connect")

            self.assertTrue(result.success)
            self.assertTrue(result.switched)
            self.assertEqual(media_service.launch_package.call_count, 2)
            media_service.force_stop_app.assert_called_once_with("surfshark")

    def test_unknown_route_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            media_service = self._media_service()
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json"), media_service)

            result = svc.ensure_route("portugal")

            self.assertFalse(result.success)
            self.assertIn("route", result.message.lower())

    def test_debug_route_captures_restart_autoconnect_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            media_service = self._media_service()
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json", route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.debug_route("restart_autoconnect", capture=True)

            self.assertTrue(result["success"])
            self.assertEqual(result["sequence_name"], "restart_autoconnect")
            self.assertEqual(result["sequence"], [])
            self.assertGreaterEqual(len(result["captures"]), 3)

    def test_debug_route_captures_quick_connect_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "surfshark_routes.json"
            self._write_route_table(route_path)
            media_service = self._media_service()
            svc = SurfsharkService(self._config(Path(tmp) / "vpn_state.json", route_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.debug_route("quick_connect", capture=True)

            self.assertTrue(result["success"])
            self.assertEqual(result["sequence_name"], "quick_connect")
            self.assertEqual(result["sequence"], ["DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"])
            self.assertGreaterEqual(len(result["captures"]), 6)


if __name__ == "__main__":
    unittest.main()
