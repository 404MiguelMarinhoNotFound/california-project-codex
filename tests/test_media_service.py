import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from unittest.mock import patch

from services.media_service import MediaService


class MediaServiceYouTubeTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "media": {
                "mibox_ip": "192.168.1.26",
                "adb_port": 5555,
                "adb_path": "adb",
                "adb_timeout_ms": 12000,
                "volume_max_steps": 15,
                "apps": {
                    "youtube": "com.google.android.youtube.tv",
                    "stremio": "com.stremio.one",
                    "surfshark": "com.surfshark.vpnclient.android",
                },
                "app_launch_components": {
                    "surfshark": "com.surfshark.vpnclient.android/.StartActivity",
                },
                "app_launch_categories": {
                    "surfshark": "android.intent.category.LEANBACK_LAUNCHER",
                },
            }
        }

    def test_youtube_playlist_launches_expected_url(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="youtube"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")) as launch_app:
                    with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                        ok = svc.youtube_playlist("PL12345")

        self.assertTrue(ok)
        launch_app.assert_not_called()
        command = adb.call_args[0][0]
        self.assertIn("https://www.youtube.com/playlist?list=PL12345", command)
        self.assertIn("com.google.android.youtube.tv", command)

    def test_youtube_playlist_warm_launches_from_other_app(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")) as launch_app:
                    with patch("services.media_service.time.sleep") as sleep:
                        with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                            ok = svc.youtube_playlist("PL12345")

        self.assertTrue(ok)
        launch_app.assert_called_once_with("youtube")
        sleep.assert_any_call(svc.youtube_warm_launch_delay_s)
        sleep.assert_any_call(svc.youtube_profile_select_delay_s)
        self.assertEqual(adb.call_count, 2)
        self.assertEqual(adb.call_args_list[0][0][0], "shell input keyevent KEYCODE_DPAD_CENTER")
        self.assertIn("https://www.youtube.com/playlist?list=PL12345", adb.call_args_list[1][0][0])

    def test_youtube_playlist_returns_false_when_warm_launch_fails(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(False, "Couldn't open youtube")):
                    with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                        ok = svc.youtube_playlist("PL12345")

        self.assertFalse(ok)
        adb.assert_not_called()

    def test_youtube_search_encodes_query(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="youtube"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")) as launch_app:
                    with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                        ok = svc.youtube_search("jazz fusion")

        self.assertTrue(ok)
        launch_app.assert_not_called()
        command = adb.call_args[0][0]
        self.assertIn("https://www.youtube.com/results?search_query=jazz+fusion", command)

    def test_youtube_search_skips_profile_confirm_when_disabled(self):
        config = {
            "media": {
                **self.config["media"],
                "youtube_profile_select_on_cold_start": False,
            }
        }
        svc = MediaService(config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")) as launch_app:
                    with patch("services.media_service.time.sleep") as sleep:
                        with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                            ok = svc.youtube_search("jazz fusion")

        self.assertTrue(ok)
        launch_app.assert_called_once_with("youtube")
        sleep.assert_called_once_with(svc.youtube_warm_launch_delay_s)
        self.assertEqual(adb.call_count, 1)
        command = adb.call_args[0][0]
        self.assertIn("https://www.youtube.com/results?search_query=jazz+fusion", command)

    def test_youtube_playlist_fails_fast_when_disconnected(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=False):
            with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                ok = svc.youtube_playlist("PL12345")

        self.assertFalse(ok)
        adb.assert_not_called()

    def test_adb_handles_missing_text_streams_without_crashing(self):
        svc = MediaService(self.config)
        completed = Mock(returncode=0, stdout=None, stderr=None)
        with patch("services.media_service.subprocess.run", return_value=completed):
            ok, output = svc._adb("shell echo ping")

        self.assertTrue(ok)
        self.assertEqual(output, "")

    def test_adb_uses_configured_timeout(self):
        svc = MediaService(self.config)
        completed = Mock(returncode=0, stdout="ok", stderr="")
        with patch("services.media_service.subprocess.run", return_value=completed) as run:
            svc._adb("shell echo ping")

        self.assertEqual(run.call_args.kwargs["timeout"], svc.adb_timeout_s)

    def test_force_stop_app_uses_package_name(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                ok = svc.force_stop_app("youtube")

        self.assertTrue(ok)
        self.assertEqual(adb.call_args[0][0], "shell am force-stop com.google.android.youtube.tv")

    def test_dump_ui_hierarchy_reads_dump_file(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(
                svc,
                "_adb",
                side_effect=[(True, "UI hierchary dumped"), (True, "<hierarchy />")],
            ) as adb:
                xml = svc.dump_ui_hierarchy()

        self.assertEqual(xml, "<hierarchy />")
        self.assertEqual(adb.call_count, 2)

    def test_dump_ui_hierarchy_returns_empty_when_all_dump_attempts_fail(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(
                svc,
                "_adb",
                return_value=(True, "ERROR: could not get idle state."),
            ) as adb:
                with patch("services.media_service.time.sleep"):
                    xml = svc.dump_ui_hierarchy()

        self.assertEqual(xml, "")
        self.assertEqual(adb.call_count, svc.ui_dump_retry_count)

    def test_is_app_foreground_matches_known_package(self):
        svc = MediaService(self.config)
        with patch.object(svc, "get_current_app", return_value="com.google.android.youtube.tv"):
            self.assertTrue(svc.is_app_foreground("youtube"))
            self.assertFalse(svc.is_app_foreground("stremio"))

    def test_launch_app_uses_explicit_component_when_configured(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "Status: ok")) as adb:
                ok, message = svc.launch_app("surfshark")

        self.assertTrue(ok)
        self.assertEqual(message, "Opening surfshark")
        command = adb.call_args[0][0]
        self.assertIn("shell am start", command)
        self.assertNotIn("-W", command)
        self.assertIn("-n com.surfshark.vpnclient.android/.StartActivity", command)
        self.assertIn("-c android.intent.category.LEANBACK_LAUNCHER", command)

    def test_launch_app_falls_back_to_package_launch_when_explicit_start_fails(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "start_activity", return_value=(False, "Error type 3")):
                with patch.object(svc, "launch_package", return_value=(True, "Opening surfshark")) as launch_package:
                    ok, message = svc.launch_app("surfshark")

        self.assertTrue(ok)
        self.assertEqual(message, "Opening surfshark")
        launch_package.assert_called_once_with("surfshark")

    def test_launch_package_uses_monkey_launcher(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "Events injected: 1")) as adb:
                ok, message = svc.launch_package("surfshark")

        self.assertTrue(ok)
        self.assertEqual(message, "Opening surfshark")
        self.assertEqual(
            adb.call_args[0][0],
            "shell monkey -p com.surfshark.vpnclient.android -c android.intent.category.LAUNCHER 1",
        )

    def test_get_current_app_parses_focus_without_host_grep(self):
        svc = MediaService(self.config)
        dumpsys_output = (
            "Window #1\n"
            "  mCurrentFocus=Window{135764f u0 com.surfshark.vpnclient.android/"
            "com.surfshark.vpnclient.android.legacyapp.tv.feature.main.TvMainActivity}\n"
        )
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, dumpsys_output)) as adb:
                current = svc.get_current_app()

        self.assertEqual(current, "surfshark")
        self.assertEqual(adb.call_args[0][0], "shell dumpsys window displays")

    def test_get_current_focus_returns_package_and_activity_token(self):
        svc = MediaService(self.config)
        dumpsys_output = (
            "Window #1\n"
            "  mCurrentFocus=Window{135764f u0 com.surfshark.vpnclient.android/"
            "com.surfshark.vpnclient.android.legacyapp.tv.feature.main.TvMainActivity}\n"
        )
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, dumpsys_output)):
                focus = svc.get_current_focus()

        self.assertEqual(
            focus,
            "com.surfshark.vpnclient.android/com.surfshark.vpnclient.android.legacyapp.tv.feature.main.TvMainActivity",
        )

    def test_capture_screenshot_runs_screencap_cat_and_cleanup(self):
        svc = MediaService(self.config)
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "captures" / "screen.png"
            with patch.object(svc, "ensure_connected", return_value=True):
                with patch.object(
                    svc,
                    "_adb",
                    side_effect=[(True, "ok"), (True, "ok"), (True, "ok")],
                ) as adb:
                    ok = svc.capture_screenshot(destination)

        self.assertTrue(ok)
        self.assertEqual(adb.call_count, 3)
        self.assertEqual(adb.call_args_list[0][0][0], "shell screencap -p /sdcard/california_capture.png")
        self.assertIn("shell cat /sdcard/california_capture.png >", adb.call_args_list[1][0][0])
        self.assertEqual(adb.call_args_list[2][0][0], "shell rm /sdcard/california_capture.png")

    def test_capture_screenshot_bytes_uses_exec_out(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb_exec", return_value=(True, b"png-data")) as adb_exec:
                screenshot = svc.capture_screenshot_bytes()

        self.assertEqual(screenshot, b"png-data")
        self.assertEqual(adb_exec.call_args[0], ("exec-out", "screencap", "-p"))


if __name__ == "__main__":
    unittest.main()
