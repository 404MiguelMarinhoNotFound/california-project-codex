import unittest
from unittest.mock import Mock

from core.orchestrator import _dispatch_tv
from core.orchestrator import _route_target_for_action
from services.stremio_service import StremioPlayResult
from services.surfshark_service import EnsureVpnResult


class OrchestratorVpnRoutingTests(unittest.TestCase):
    def test_route_target_for_action_maps_launch_app_targets(self):
        self.assertEqual(_route_target_for_action("launch_app", {"app_name": "youtube"}), ("youtube", "albania"))
        self.assertEqual(_route_target_for_action("launch_app", {"app_name": "stremio"}), ("stremio", "quick_connect"))
        self.assertEqual(_route_target_for_action("launch_app", {"app_name": "spotify"}), (None, None))

    def test_dispatch_tv_skips_vpn_when_requested_app_is_already_foreground(self):
        media_svc = Mock()
        media_svc.ensure_connected.return_value = True
        media_svc.is_app_foreground.return_value = True
        media_svc.youtube_playlist.return_value = True
        surfshark_svc = Mock()
        surfshark_svc.enabled = True
        surfshark_svc.route_by_app = {"youtube": "albania", "stremio": "quick_connect"}

        response = _dispatch_tv(
            {"action": "youtube_playlist", "playlist_id": "PL123"},
            media_svc,
            Mock(),
            surfshark_svc,
            {},
        )

        self.assertEqual(response, "Opening that YouTube playlist.")
        surfshark_svc.ensure_country.assert_not_called()
        media_svc.force_stop_app.assert_not_called()

    def test_dispatch_tv_routes_cross_app_youtube_and_force_stops_before_opening(self):
        media_svc = Mock()
        media_svc.ensure_connected.return_value = True
        media_svc.is_app_foreground.return_value = False
        media_svc.youtube_playlist.return_value = True
        media_svc.force_stop_app.return_value = True
        surfshark_svc = Mock()
        surfshark_svc.enabled = True
        surfshark_svc.route_by_app = {"youtube": "albania", "stremio": "quick_connect"}
        surfshark_svc.ensure_country.return_value = EnsureVpnResult(
            success=True,
            target_country="albania",
            current_country="albania",
            switched=True,
        )

        response = _dispatch_tv(
            {"action": "youtube_playlist", "playlist_id": "PL123"},
            media_svc,
            Mock(),
            surfshark_svc,
            {},
        )

        self.assertEqual(response, "Opening that YouTube playlist.")
        surfshark_svc.ensure_country.assert_called_once_with("albania")
        media_svc.force_stop_app.assert_called_once_with("youtube")

    def test_dispatch_tv_routes_cross_app_stremio_without_force_stop(self):
        media_svc = Mock()
        media_svc.ensure_connected.return_value = True
        media_svc.is_app_foreground.return_value = False
        surfshark_svc = Mock()
        surfshark_svc.enabled = True
        surfshark_svc.route_by_app = {"youtube": "albania", "stremio": "quick_connect"}
        surfshark_svc.ensure_country.return_value = EnsureVpnResult(
            success=True,
            target_country="quick_connect",
            current_country=None,
            switched=True,
        )
        stremio_svc = Mock()
        stremio_svc.play.return_value = StremioPlayResult(success=True)

        response = _dispatch_tv(
            {"action": "stremio_play", "title": "Shrinking", "media_type": "series"},
            media_svc,
            stremio_svc,
            surfshark_svc,
            {},
        )

        self.assertEqual(response, "Opening Shrinking on Stremio.")
        surfshark_svc.ensure_country.assert_called_once_with("quick_connect")
        media_svc.force_stop_app.assert_not_called()

    def test_dispatch_tv_appends_warning_when_vpn_check_fails_but_opening_continues(self):
        media_svc = Mock()
        media_svc.ensure_connected.return_value = True
        media_svc.is_app_foreground.return_value = False
        media_svc.youtube_search.return_value = True
        media_svc.force_stop_app.return_value = True
        surfshark_svc = Mock()
        surfshark_svc.enabled = True
        surfshark_svc.route_by_app = {"youtube": "albania", "stremio": "quick_connect"}
        surfshark_svc.ensure_country.return_value = EnsureVpnResult(
            success=False,
            target_country="albania",
            current_country="portugal",
            switched=False,
            message="I couldn't confirm Surfshark was connected to Albania.",
        )

        response = _dispatch_tv(
            {"action": "youtube_search", "query": "pagode praia"},
            media_svc,
            Mock(),
            surfshark_svc,
            {},
        )

        self.assertEqual(
            response,
            "Searching YouTube for pagode praia but I couldn't confirm Surfshark was on Albania.",
        )


if __name__ == "__main__":
    unittest.main()
