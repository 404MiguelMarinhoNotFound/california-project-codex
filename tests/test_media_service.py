import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from unittest.mock import patch

from services.media_service import MediaService
from tests.config_fixture import config_for_tests


class MediaServiceYouTubeTests(unittest.TestCase):
    def setUp(self):
        # The real config.yaml. This fixture used to hardcode an IP from two
        # moves ago plus its own copy of the app table, and kept passing --
        # it was asserting agreement with itself, not with what ships.
        # Every ADB call below is mocked, so no deployment value needs pinning.
        # discovery off too: with it on, construction reads the real
        # device_state.json out of the CWD and connect() socket-probes the LAN.
        self.config = config_for_tests(
            media={"cec_wake": {"enabled": False}, "discovery": {"enabled": False}},
        )

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
        picker_focus = "com.google.android.youtube.tv/com.google.android.apps.youtube.tv.profile.ProfileSwitcherActivity"
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")) as launch_app:
                    with patch("services.media_service.time.sleep") as sleep:
                        with patch.object(svc, "get_current_focus", return_value=picker_focus):
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


    def test_youtube_profile_picker_skipped_when_main_activity_focused(self):
        svc = MediaService(self.config)
        main_focus = "com.google.android.youtube.tv/com.google.android.apps.youtube.tv.activity.ShellActivity"
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")):
                    with patch("services.media_service.time.sleep"):
                        with patch.object(svc, "get_current_focus", return_value=main_focus):
                            with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                                ok = svc.youtube_playlist("PL12345")

        self.assertTrue(ok)
        # Only the deep-link ADB call, no DPAD_CENTER
        self.assertEqual(adb.call_count, 1)
        self.assertIn("https://www.youtube.com/playlist?list=PL12345", adb.call_args_list[0][0][0])

    def test_youtube_profile_picker_pressed_on_empty_focus(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")):
                    with patch("services.media_service.time.sleep"):
                        with patch.object(svc, "get_current_focus", return_value=""):
                            with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                                ok = svc.youtube_playlist("PL12345")

        self.assertTrue(ok)
        # Fallback: DPAD_CENTER + deep-link = 2 calls
        self.assertEqual(adb.call_count, 2)
        self.assertEqual(adb.call_args_list[0][0][0], "shell input keyevent KEYCODE_DPAD_CENTER")

    def test_detect_youtube_profile_picker_fast_main_activity(self):
        svc = MediaService(self.config)
        focus = "com.google.android.youtube.tv/com.google.android.apps.youtube.tv.activity.ShellActivity"
        with patch.object(svc, "get_current_focus", return_value=focus):
            should_press, source = svc._detect_youtube_profile_picker_fast()
        self.assertFalse(should_press)
        self.assertTrue(source.startswith("focus_main:"))

    def test_detect_youtube_profile_picker_fast_picker_activity(self):
        svc = MediaService(self.config)
        focus = "com.google.android.youtube.tv/com.google.android.apps.youtube.tv.profile.ProfileSwitcherActivity"
        with patch.object(svc, "get_current_focus", return_value=focus):
            should_press, source = svc._detect_youtube_profile_picker_fast()
        self.assertTrue(should_press)
        self.assertTrue(source.startswith("focus_picker:"))

    def test_detect_youtube_profile_picker_fast_other_package(self):
        svc = MediaService(self.config)
        with patch.object(svc, "get_current_focus", return_value="com.stremio.one/.MainActivity"):
            should_press, source = svc._detect_youtube_profile_picker_fast()
        self.assertTrue(should_press)
        self.assertTrue(source.startswith("focus_other:"))

    def test_youtube_second_launch_skips_profile_detection(self):
        svc = MediaService(self.config)
        main_focus = "com.google.android.youtube.tv/com.google.android.apps.youtube.tv.activity.ShellActivity"
        # First launch: not foreground, triggers detection + sets the session flag
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")):
                    with patch("services.media_service.time.sleep"):
                        with patch.object(svc, "get_current_focus", return_value=main_focus) as focus:
                            with patch.object(svc, "_adb", return_value=(True, "ok")):
                                svc.youtube_playlist("PL_FIRST")
                                self.assertTrue(svc._youtube_profile_cleared)
                                first_focus_calls = focus.call_count

        # Second launch: still not foreground (simulates backgrounding). Detection must NOT run again.
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "get_current_app", return_value="stremio"):
                with patch.object(svc, "launch_app", return_value=(True, "Opening youtube")):
                    with patch("services.media_service.time.sleep"):
                        with patch.object(svc, "get_current_focus", return_value=main_focus) as focus:
                            with patch.object(svc, "_adb", return_value=(True, "ok")) as adb:
                                svc.youtube_playlist("PL_SECOND")
                                self.assertEqual(focus.call_count, 0)  # fast path skipped
                                self.assertEqual(adb.call_count, 1)     # only the deep-link am start
                                self.assertIn("PL_SECOND", adb.call_args_list[0][0][0])

    def test_force_stop_youtube_resets_profile_cache(self):
        svc = MediaService(self.config)
        svc._youtube_profile_cleared = True
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "ok")):
                svc.force_stop_app("youtube")
        self.assertFalse(svc._youtube_profile_cleared)

    def test_dump_ui_hierarchy_uses_ui_dump_timeout(self):
        svc = MediaService(self.config)
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(
                svc,
                "_adb",
                side_effect=[(True, "UI hierchary dumped"), (True, "<hierarchy />")],
            ) as adb:
                svc.dump_ui_hierarchy()

        self.assertEqual(adb.call_args_list[0].kwargs.get("timeout_s"), svc.ui_dump_timeout_s)

    def test_detect_youtube_profile_picker_curly_apostrophe(self):
        svc = MediaService(self.config)
        xml = '<hierarchy><node text="Who\u2019s watching?" bounds="[0,0][100,100]" /></hierarchy>'
        with patch.object(svc, "dump_ui_hierarchy", return_value=xml):
            should_press, source = svc._detect_youtube_profile_picker()
        self.assertTrue(should_press)
        self.assertIn("marker_found", source)


class HdmiStateTests(unittest.TestCase):
    """
    Parsed from a REAL `dumpsys hdmi_control` capture, not a hand-written string.

    tests/fixtures/hdmi_control_dump.txt was taken off the live Mi Box on
    2026-09-05, so a firmware change to the format fails here instead of silently
    reporting "cannot tell" forever.
    """

    FIXTURE = Path(__file__).parent / "fixtures" / "hdmi_control_dump.txt"

    def _service(self, dump: str | None, connected: bool = True):
        cfg = config_for_tests(
            media={"cec_wake": {"enabled": False}, "discovery": {"enabled": False}},
        )
        svc = MediaService(cfg)
        svc.ensure_connected = Mock(return_value=connected)
        svc._adb = Mock(return_value=(dump is not None, dump or ""))
        return svc

    def test_active_source_is_read_from_a_real_dump(self):
        svc = self._service(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertIs(svc.is_active_source(), True)

    def test_active_source_is_false_when_the_input_is_parked_elsewhere(self):
        dump = self.FIXTURE.read_text(encoding="utf-8").replace(
            "mIsActiveSource: true", "mIsActiveSource: false")
        self.assertIs(self._service(dump).is_active_source(), False)

    def test_unreadable_dump_is_none_not_false(self):
        """
        None means "could not tell". Collapsing it to False would fire an HDMI
        switch on every unreadable dump, and switch_input cycles inputs.
        """
        self.assertIsNone(self._service(None).is_active_source())
        self.assertIsNone(self._service("some unrelated output").is_active_source())

    def test_active_source_is_none_when_the_box_is_unreachable(self):
        self.assertIsNone(self._service("irrelevant", connected=False).is_active_source())

    def test_tv_power_comes_from_the_last_report_not_the_first(self):
        """
        The CEC log is a capped ring (~246 entries), so the FIRST match can be
        days old. Reading the head during this work returned entries from two
        days earlier and looked exactly like "nothing happened".
        """
        dump = """
    [R] time=2026-09-04 00:08:08 message=<Report Power Status> 04:90:01
    [R] time=2026-09-05 14:18:51 message=<Report Power Status> 04:90:00
"""
        self.assertEqual(self._service(dump).tv_power_status(), "on")

    def test_tv_power_reads_standby(self):
        dump = """
    [R] time=2026-09-05 14:18:51 message=<Report Power Status> 04:90:01
"""
        self.assertEqual(self._service(dump).tv_power_status(), "standby")

    def test_tv_power_ignores_our_own_sent_reports(self):
        """
        [S] is the box answering the TV about ITSELF. Counting those would report
        the box's power as the television's.
        """
        dump = """
    [S] time=2026-09-05 14:18:51 message=<Report Power Status> 40:90:00
"""
        self.assertIsNone(self._service(dump).tv_power_status())

    def test_tv_power_on_a_real_dump(self):
        svc = self._service(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertIn(svc.tv_power_status(), {"on", "standby"})


class EnsureActiveSourceTests(unittest.TestCase):
    """
    Selecting the box's input must be VERIFIED, because the TV lies by omission.

    switch_input sends KEY_HDMI<n>, the websocket accepts it, and this set
    ignores it -- so "the send succeeded" says nothing about what is on screen.
    Measured 2026-09-05: three switch_hdmi(2) calls left the TV on HDMI 1.
    """

    def _service(self):
        cfg = config_for_tests(
            media={"cec_wake": {"enabled": False}, "discovery": {"enabled": False}},
        )
        svc = MediaService(cfg, cec_waker=Mock())
        svc.switch_hdmi = Mock()
        return svc

    def test_already_on_screen_changes_nothing(self):
        svc = self._service()
        svc.is_active_source = Mock(return_value=True)
        self.assertIs(svc.ensure_active_source(), True)
        svc.switch_hdmi.assert_not_called()
        svc.cec_waker.cycle_input.assert_not_called()

    def test_unknown_state_never_touches_the_input(self):
        """Cycling from an unknown input is not idempotent -- do not guess."""
        svc = self._service()
        svc.is_active_source = Mock(return_value=None)
        self.assertIsNone(svc.ensure_active_source())
        svc.switch_hdmi.assert_not_called()
        svc.cec_waker.cycle_input.assert_not_called()

    def test_it_escalates_from_direct_addressing_to_cycling(self):
        """
        Direct first (one deterministic press when it works), then cycling, which
        is the mechanism actually proven on this set. A direct-only loop never
        converges here.
        """
        svc = self._service()
        svc.is_active_source = Mock(side_effect=[False, False, True])
        with patch("services.media_service.time.sleep"):
            self.assertIs(svc.ensure_active_source(), True)
        svc.switch_hdmi.assert_called_once_with(svc.mibox_hdmi_port)
        svc.cec_waker.cycle_input.assert_called_once()

    def test_it_gives_up_rather_than_cycling_forever(self):
        svc = self._service()
        svc.is_active_source = Mock(return_value=False)
        with patch("services.media_service.time.sleep"):
            self.assertIs(svc.ensure_active_source(attempts=3), False)
        self.assertEqual(svc.cec_waker.cycle_input.call_count, 2)

    def test_a_dumpsys_that_stops_answering_mid_loop_returns_none(self):
        svc = self._service()
        svc.is_active_source = Mock(side_effect=[False, None])
        with patch("services.media_service.time.sleep"):
            self.assertIsNone(svc.ensure_active_source())


class BoxDiscoveryTests(unittest.TestCase):
    """
    A DHCP move must self-heal instead of surfacing as "TV is off or unreachable".

    Discovery is ON in these fixtures -- that is the point -- so every test stubs
    both `port_open` and the finder. Nothing here may touch the network.
    """

    MOVED_TO = "192.168.1.41"  # config-literal: the address discovery must find

    def setUp(self):
        # _classify_failure shells out to `arp -a` to tell "box is off" from "box
        # is up but ADB is disabled". Correct in production, not from a unit test.
        run = patch("services.device_finder.subprocess.run")
        self.subprocess_run = run.start()
        self.subprocess_run.return_value = Mock(returncode=1, stdout="", stderr="")
        self.addCleanup(run.stop)

    def _service(self, **discovery):
        cfg = config_for_tests(
            media={
                "cec_wake": {"enabled": False, "settle_ms": 10,
                             "poll_interval_ms": 10, "wake_attempts": 1},
                "discovery": {
                    "enabled": True,
                    "state_path": "nonexistent_device_state.json",
                    **discovery,
                },
            },
        )
        return MediaService(cfg)

    def test_connect_never_calls_adb_when_the_port_is_closed(self):
        """
        The 21-second guard, and the single most valuable test in this file.

        `adb connect` to a host with 5555 closed takes 21.1s and the timeout is
        not tunable. If this gate is ever removed, a subnet sweep costs 254*21s.
        """
        svc = self._service()
        with patch("services.media_service.port_open", return_value=False):
            with patch.object(svc, "_adb") as adb:
                self.assertFalse(svc.connect())
        adb.assert_not_called()

    def test_target_follows_a_rediscovered_address(self):
        svc = self._service()
        svc.ip = self.MOVED_TO
        self.assertEqual(svc.target, f"{self.MOVED_TO}:{svc.port}")
        with patch("services.media_service.port_open", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "connected to x")) as adb:
                svc.connect()
        self.assertIn(self.MOVED_TO, adb.call_args[0][0])

    def test_a_drift_self_heals_through_ensure_connected(self):
        svc = self._service()
        # ping fails, the believed address has no listener, discovery finds the box
        # connect() fails at the stale address, then succeeds once discovery has
        # moved svc.ip -- which is the whole sequence being asserted.
        with patch.object(svc, "_adb", return_value=(False, "not found")):
            with patch("services.media_service.port_open", side_effect=lambda ip, *a: ip == self.MOVED_TO):
                with patch.object(svc._finder, "resolve", return_value=self.MOVED_TO):
                    with patch.object(svc, "connect", side_effect=[False, True]):
                        self.assertTrue(svc.ensure_connected())
        self.assertEqual(svc.ip, self.MOVED_TO)

    def test_repeated_failures_scan_only_once(self):
        """The rescan cooldown: don't re-sweep a LAN we just swept."""
        svc = self._service()
        with patch.object(svc, "_adb", return_value=(False, "not found")):
            with patch("services.media_service.port_open", return_value=False):
                with patch.object(svc._finder, "resolve", return_value="") as resolve:
                    svc.ensure_connected()
                    svc._last_fail_time = 0  # defeat the offline cooldown, not the rescan one
                    svc.ensure_connected()
        self.assertEqual(resolve.call_count, 1)

    def test_the_wait_loop_does_not_rediscover_on_every_poll(self):
        """
        A booting box is at a known address. Scanning each poll would be waste --
        so discovery is suppressed during the wait and runs exactly once after it.
        """
        svc = self._service()
        with patch.object(svc, "ensure_connected", return_value=False):
            with patch("services.media_service.time.sleep"):
                with patch.object(svc._finder, "resolve", return_value="") as resolve:
                    self.assertFalse(svc._wait_for_box())
        self.assertEqual(resolve.call_count, 1)
        self.assertFalse(svc._discovery_suppressed, "the flag must be cleared again")

    def test_a_serial_mismatch_disconnects_the_stranger(self):
        """
        Leaving someone else's transport open pollutes `adb devices` and makes an
        unqualified `adb shell` ambiguous for everything else on the machine.
        """
        svc = self._service()
        with patch("services.media_service.port_open", return_value=True):
            with patch.object(svc, "_adb") as adb:
                # connect, getprop (wrong serial), then the disconnect being asserted
                adb.side_effect = [(True, "connected to x"), (True, "SOMEONE-ELSE"),
                                   (True, "disconnected")]
                self.assertFalse(svc._verify_box(self.MOVED_TO))
        self.assertIn("disconnect", adb.call_args[0][0])

    def test_a_stranger_on_the_adb_port_is_not_reported_as_box_is_off(self):
        """
        "The box is off" and "another device took its address" need opposite
        responses. The port scan only yields hosts with 5555 open, so a rejection
        at that rung means the serial did not match -- not that nothing is there.
        """
        from services.device_finder import TraceStep

        svc = self._service()
        svc._finder.last_trace = [
            TraceStep(rung="tcp_port_candidates", ip=self.MOVED_TO,
                      verdict="rejected", elapsed_s=0.4),
        ]
        self.assertEqual(svc._classify_failure(), "identity_mismatch")

    def test_nothing_on_the_lan_is_reported_as_box_is_off(self):
        svc = self._service()
        svc._finder.last_trace = []
        self.subprocess_run.return_value = Mock(returncode=0, stdout="", stderr="")
        self.assertEqual(svc._classify_failure(), "not_on_lan")

    def test_verify_box_never_reaches_adb_on_a_closed_port(self):
        svc = self._service()
        with patch("services.media_service.port_open", return_value=False):
            with patch.object(svc, "_adb") as adb:
                self.assertFalse(svc._verify_box(self.MOVED_TO))
        adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
