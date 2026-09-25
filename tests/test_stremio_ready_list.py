"""
"Put on <show>": wait for Stremio's stream list, press OK once, confirm the player.

Measured on the real box 2026-09-25 (Fallout S2E9, cold after the force-stop):
the list is visible ~8s after the deep link, the ExoPlayer views 1.8s after
OK, state=3 5.8s after OK. The old path pressed OK at a fixed ~3s -- onto the
splash screen -- pressed again, then ran a uiautomator scan over a video that
was already on: 27-30s, and once a "which source?" question over a playing show.

The two fixtures are real `dumpsys activity top` captures, trimmed to Stremio's
task: the stream list (cards visible, first selected, loading frame gone) and
the player (exo_* views).
"""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.stremio_service import StremioService
from tests.config_fixture import config_for_tests

FIXTURES = Path(__file__).parent / "fixtures"
STREAM_LIST = (FIXTURES / "activity_top_stremio_streams.txt").read_text(encoding="utf-8")
PLAYER = (FIXTURES / "activity_top_stremio_player.txt").read_text(encoding="utf-8")
LOADING = STREAM_LIST.replace(
    "G.E...... ......ID 0,0-1736,1080 #7f0b0222 app:id/meta_details_loading_frame",
    "V.E...... ......ID 0,0-1736,1080 #7f0b0222 app:id/meta_details_loading_frame",
)
ERROR = STREAM_LIST.replace(
    "G.E...... ......I. 0,0-0,0 #7f0b021e app:id/meta_details_error_frame",
    "V.E...... ......I. 0,0-0,0 #7f0b021e app:id/meta_details_error_frame",
)


def _session(package="com.stremio.one", state=3):
    return (f"  Sessions Stack - have 1 sessions:\n    x package={package}\n"
            f"      state=PlaybackState {{state={state}, position=0, buffered position=0, "
            f"speed=1.0, updated=1, actions=0, custom actions=[], active item id=-1, error=null}}\n")


class _Box:
    """Answers `dumpsys activity top` and `dumpsys media_session` from a script."""

    def __init__(self, tops, sessions):
        self.tops = list(tops)
        self.sessions = list(sessions)
        self.keys = []

    def shell(self, command):
        script = self.tops if "activity top" in command else self.sessions
        answer = script[0] if len(script) == 1 else script.pop(0)
        return (answer is not None, answer or "")


def _service(box):
    cfg = config_for_tests(stremio={
        "stream_list_timeout_ms": 5000,
        "player_start_timeout_ms": 5000,
        "playback_timeout_ms": 5000,
        "autoplay_delay_ms": 1,
        "email": None,
        "password": None,
    })
    svc = StremioService(cfg, media_service=Mock())
    svc._run_shell = Mock(side_effect=box.shell)
    svc._keyevent = Mock(side_effect=box.keys.append)
    return svc


class StremioViewsTests(unittest.TestCase):
    def test_the_stream_list_capture_reads_as_ready(self):
        svc = _service(_Box([STREAM_LIST], [""]))
        views = svc._stremio_views()
        self.assertIn("stream_card_stub_inflated", views)
        self.assertNotIn("meta_details_loading_frame", views)
        self.assertFalse(any(v.startswith("exo_") for v in views))

    def test_the_player_capture_reads_as_a_player(self):
        svc = _service(_Box([PLAYER], [""]))
        self.assertTrue(any(v.startswith("exo_") for v in svc._stremio_views()))

    def test_an_unreadable_dump_is_none(self):
        self.assertIsNone(_service(_Box([None], [""]))._stremio_views())

    def test_the_playback_state_is_stremios_own(self):
        """state=3 from another app is not Stremio playing."""
        svc = _service(_Box([""], [_session("com.spotify.tv.android", 3)]))
        self.assertIsNone(svc._stremio_playback_state())
        svc = _service(_Box([""], [_session(state=6)]))
        self.assertEqual(svc._stremio_playback_state(), 6)


@patch("services.stremio_service.time.sleep")
class PlayWhenReadyTests(unittest.TestCase):
    def test_one_ok_on_a_ready_list_then_playing(self, _sleep):
        box = _Box([LOADING, LOADING, STREAM_LIST, PLAYER], ["", "", _session(state=6), _session(state=3)])
        svc = _service(box)
        result, pressed = svc._play_when_ready("episode", "Fallout")
        self.assertTrue(result.success)
        self.assertTrue(pressed)
        self.assertEqual(box.keys, [23], "exactly one OK, and only once the list was ready")

    def test_no_key_while_the_list_is_still_loading(self, _sleep):
        box = _Box([LOADING], [""])
        svc = _service(box)
        svc.stream_list_timeout_s = 0.01
        self.assertEqual(svc._play_when_ready("episode", "Fallout"), (None, False))
        self.assertEqual(box.keys, [])

    def test_the_error_frame_ends_the_wait_without_a_key(self, _sleep):
        box = _Box([ERROR], [""])
        svc = _service(box)
        self.assertEqual(svc._play_when_ready("episode", "Fallout"), (None, False))
        self.assertEqual(box.keys, [])

    def test_a_player_still_buffering_is_said_as_loading_not_hit_ok(self, _sleep):
        box = _Box([STREAM_LIST, PLAYER], ["", _session(state=6)])
        svc = _service(box)
        svc.playback_timeout_s = 0.01
        result, pressed = svc._play_when_ready("episode", "Fallout")
        self.assertFalse(result.success)
        self.assertIn("loading", result.message)
        self.assertNotIn("hit OK", result.message)
        self.assertEqual(box.keys, [23])

    def test_ok_that_opened_no_player_reports_pressed(self, _sleep):
        box = _Box([STREAM_LIST], [""])
        svc = _service(box)
        svc.player_start_timeout_s = 0.01
        self.assertEqual(svc._play_when_ready("episode", "Fallout"), (None, True))


@patch("services.stremio_service.time.sleep")
class PlayDeepLinkTests(unittest.TestCase):
    def _svc(self, box):
        svc = _service(box)
        svc._launch_uri = Mock()
        svc._wait_for_stremio_foreground = Mock(return_value=True)
        return svc

    def test_the_link_clears_stremios_task_instead_of_killing_it(self, _sleep):
        """
        A plain link into a warm Stremio opened S01E01 instead of S2E9; a
        force-stop fixed that with a ~10s cold start. Clearing the task routes
        correctly in 2.2-2.7s (2026-09-25).
        """
        box = _Box([STREAM_LIST, PLAYER], ["", _session(state=3)])
        svc = _service(box)
        svc._wait_for_stremio_foreground = Mock(return_value=True)
        result = svc._play_deep_link("tt12637874", "series", season=2, episode=9, title_label="Fallout")
        self.assertTrue(result.success)
        svc.media_service.force_stop_app.assert_not_called()
        launch = [c[0][0] for c in svc._run_shell.call_args_list if c[0][0].startswith("am start")]
        self.assertEqual(len(launch), 1)
        self.assertIn("-f 0x10008000", launch[0])
        self.assertIn("tt12637874:2:9", launch[0])

    def test_no_second_ok_after_the_first_opened_nothing(self, _sleep):
        box = _Box([STREAM_LIST], [""])
        svc = self._svc(box)
        svc.player_start_timeout_s = 0.01
        with patch.object(svc, "_try_stremio_autoplay") as blind_ok:
            with patch.object(svc, "_attempt_provider", return_value=None):
                with patch.object(svc, "_is_playing", return_value=False):
                    svc._play_deep_link("tt12637874", "series", season=2, episode=9, title_label="Fallout")
        blind_ok.assert_not_called()
        self.assertEqual(box.keys, [23])

    def test_a_series_without_progress_opens_its_page_and_presses_nothing(self, _sleep):
        box = _Box([STREAM_LIST], [""])
        svc = self._svc(box)
        result = svc._play_deep_link("tt12637874", "series", title_label="Fallout")
        self.assertTrue(result.success)
        self.assertEqual(result.target_mode, "series_detail")
        self.assertEqual(box.keys, [])

    def test_a_list_that_never_shows_falls_back_to_the_old_path(self, _sleep):
        box = _Box([LOADING], [""])
        svc = self._svc(box)
        svc.stream_list_timeout_s = 0.01
        with patch.object(svc, "_try_stremio_autoplay", return_value=True) as old_path:
            result = svc._play_deep_link("tt12637874", "series", season=2, episode=9, title_label="Fallout")
        old_path.assert_called_once()
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
