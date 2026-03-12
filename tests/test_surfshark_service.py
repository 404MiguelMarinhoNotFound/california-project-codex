import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from services.surfshark_service import SurfsharkService


CONNECTED_ALBANIA_XML = """
<hierarchy>
  <node text="Connected" bounds="[100,100][600,180]" />
  <node text="Albania" bounds="[100,220][600,300]" />
  <node text="Portugal" bounds="[100,900][600,980]" />
  <node text="Albania" bounds="[100,1050][600,1130]" />
</hierarchy>
"""

DISCONNECTED_XML = """
<hierarchy>
  <node text="Not connected" bounds="[100,100][600,180]" />
  <node text="Portugal" bounds="[100,900][600,980]" />
  <node text="Albania" bounds="[100,1050][600,1130]" />
</hierarchy>
"""

CONNECTED_PORTUGAL_XML = """
<hierarchy>
  <node text="Protected" bounds="[100,100][600,180]" />
  <node text="Portugal" bounds="[100,220][600,300]" />
  <node text="Portugal" bounds="[100,900][600,980]" />
  <node text="Albania" bounds="[100,1050][600,1130]" />
</hierarchy>
"""


class SurfsharkServiceTests(unittest.TestCase):
    def _config(self, state_path: Path) -> dict:
        return {
            "media": {
                "vpn_routing_enabled": True,
                "vpn_state_path": str(state_path),
                "vpn_status_cache_max_age_minutes": 5,
                "vpn_failure_policy": "open_anyway",
                "surfshark_launch_delay_ms": 1,
                "surfshark_connect_timeout_ms": 50,
                "surfshark_status_poll_interval_ms": 1,
                "surfshark_favorite_scan_pages": 2,
                "surfshark_favorite_scroll_delay_ms": 1,
                "surfshark_post_selection_delay_ms": 1,
                "surfshark_quick_connect_aliases": [
                    "quick_connect",
                    "quick connect",
                    "fastest",
                    "fastest location",
                ],
                "surfshark_quick_connect_tap": {"x": 320, "y": 210},
                "surfshark_first_favorite_country": "albania",
                "surfshark_first_favorite_tap": {"x": 320, "y": 440},
                "surfshark_country_aliases": {
                    "albania": ["albania"],
                    "portugal": ["portugal"],
                },
                "surfshark_connected_markers": ["connected", "protected"],
                "surfshark_connecting_markers": ["connecting", "reconnecting"],
                "surfshark_disconnected_markers": ["disconnected", "not connected", "unprotected"],
            }
        }

    def test_get_status_uses_fresh_cache_without_launching_surfshark(self):
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

            media_service = Mock()
            svc = SurfsharkService(self._config(state_path), media_service)

            status = svc.get_status()

            self.assertTrue(status.connected)
            self.assertEqual(status.country, "albania")
            self.assertEqual(status.source, "cache")
            media_service.launch_app.assert_not_called()

    def test_get_status_refreshes_stale_cache_from_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "connected": True,
                        "country": "portugal",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                        "source": "surfshark_ui",
                    }
                ),
                encoding="utf-8",
            )

            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.dump_ui_hierarchy.return_value = CONNECTED_ALBANIA_XML
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                status = svc.get_status()

            self.assertTrue(status.connected)
            self.assertEqual(status.country, "albania")
            media_service.launch_app.assert_called_once_with("surfshark")

    def test_parse_status_detects_connected_country(self):
        svc = SurfsharkService(self._config(Path("vpn_state.json")), Mock())

        status = svc._parse_status_from_xml(CONNECTED_ALBANIA_XML)

        self.assertTrue(status.connected)
        self.assertEqual(status.country, "albania")

    def test_ensure_country_selects_favorite_and_waits_for_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.dump_ui_hierarchy.side_effect = [
                DISCONNECTED_XML,
                CONNECTED_PORTUGAL_XML,
            ]
            media_service.tap.return_value = True
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("portugal")

            self.assertTrue(result.success)
            self.assertEqual(result.current_country, "portugal")
            self.assertTrue(result.switched)
            media_service.tap.assert_called_once()

    def test_ensure_country_fails_when_favorite_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.dump_ui_hierarchy.return_value = DISCONNECTED_XML
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("iceland")

            self.assertFalse(result.success)
            self.assertIn("Iceland", result.message)

    def test_ensure_country_uses_quick_connect_tap_for_stremio_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.tap.return_value = True
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("quick_connect")

            self.assertTrue(result.success)
            self.assertTrue(result.switched)
            self.assertIsNone(result.current_country)
            media_service.launch_app.assert_called_once_with("surfshark")
            media_service.tap.assert_called_once_with(320, 210)

    def test_ensure_country_uses_first_favorite_tap_for_albania(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.tap.return_value = True
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("albania")

            self.assertTrue(result.success)
            self.assertEqual(result.current_country, "albania")
            media_service.launch_app.assert_called_once_with("surfshark")
            media_service.tap.assert_called_once_with(320, 440)

    def test_non_authoritative_cache_does_not_skip_first_favorite_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "vpn_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "connected": True,
                        "country": "albania",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                        "source": "surfshark_first_favorite",
                    }
                ),
                encoding="utf-8",
            )
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.tap.return_value = True
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("albania")

            self.assertTrue(result.success)
            self.assertTrue(result.switched)
            media_service.launch_app.assert_called_once_with("surfshark")
            media_service.tap.assert_called_once_with(320, 440)

    def test_authoritative_ui_cache_can_skip_matching_country(self):
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
            media_service = Mock()
            svc = SurfsharkService(self._config(state_path), media_service)

            result = svc.ensure_country("albania")

            self.assertTrue(result.success)
            self.assertFalse(result.switched)
            media_service.launch_app.assert_not_called()

    def test_force_switch_bypasses_matching_cache(self):
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
            media_service = Mock()
            media_service.launch_app.return_value = (True, "Opening surfshark")
            media_service.tap.return_value = True
            svc = SurfsharkService(self._config(state_path), media_service)

            with patch("services.surfshark_service.time.sleep"):
                result = svc.ensure_country("albania", force_switch=True)

            self.assertTrue(result.success)
            self.assertTrue(result.switched)
            media_service.launch_app.assert_called_once_with("surfshark")
            media_service.tap.assert_called_once_with(320, 440)


if __name__ == "__main__":
    unittest.main()
