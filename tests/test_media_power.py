"""
Power path: turning the Mi Box off is ADB, turning it on is the television.

The box leaves Wi-Fi entirely in standby, so every "turn it on" route through
ADB is unreachable by construction. These tests pin the two things that made
the old implementation wrong: KEYCODE_POWER was sent blind (a toggle, so it
turned the TV *off* when asked to turn it on), and `wake` sat behind a
reachability gate that answered first.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.orchestrator import _dispatch_tv, _ensure_playable
from services.cec_wake import CecWaker, WakeResult
from services.media_service import MediaService, RoomStatus
from services.now_playing import NowPlaying
from tests.config_fixture import config_for_tests, real_config


def _config(**media_overrides) -> dict:
    """The real config.yaml with the wake path made safe and fast.

    cec_wake is disabled so a unit test can never Wake-on-LAN the television,
    and its waits drop from 25s to 100ms. Everything else -- mibox_ip, adb_path,
    the app table -- comes from what ships, so a moved box fails here.

    discovery is disabled for the same reason cec_wake is: _wait_for_box
    rediscovers once after it times out, and a live DeviceFinder would socket-probe
    the operator's whole subnet from a unit test. Tests that exercise discovery
    turn it back on and stub the finder.
    """
    return config_for_tests(
        media={
            "cec_wake": {
                "enabled": False,
                "settle_ms": 100,
                "poll_interval_ms": 100,
                "wake_attempts": 1,
            },
            "discovery": {"enabled": False},
            **media_overrides,
        }
    )


def _service(waker=None) -> MediaService:
    return MediaService(_config(), cec_waker=waker or Mock(spec=CecWaker))


class TurnOnTests(unittest.TestCase):
    def test_already_awake_sends_no_keyevent(self):
        """The one-way door: a blind KEYCODE_POWER here turns the TV off."""
        svc = _service()
        with patch.object(svc, "is_awake", return_value=True):
            with patch.object(svc, "_adb") as adb:
                self.assertTrue(svc.turn_on())
        adb.assert_not_called()
        svc.cec_waker.wake.assert_not_called()

    def test_asleep_but_reachable_uses_wakeup_not_power(self):
        svc = _service()
        with patch.object(svc, "is_awake", return_value=False):
            with patch.object(svc, "_adb", return_value=(True, "")) as adb:
                self.assertTrue(svc.turn_on())
        command = adb.call_args[0][0]
        self.assertIn("KEYCODE_WAKEUP", command)
        self.assertNotIn("KEYCODE_POWER", command)
        svc.cec_waker.wake.assert_not_called()

    def test_unreachable_falls_back_to_cec(self):
        waker = Mock(spec=CecWaker)
        waker.wake.return_value = WakeResult(True, "pair request sent")
        svc = _service(waker)
        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "ensure_connected", return_value=True):
                with patch.object(svc, "is_boot_completed", return_value=True):
                    self.assertTrue(svc.turn_on())
        waker.wake.assert_called_once()

    def test_wake_failure_is_reported_not_swallowed(self):
        waker = Mock(spec=CecWaker)
        waker.wake.return_value = WakeResult(False, "samsungtvws not installed")
        svc = _service(waker)
        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "_adb") as adb:
                self.assertFalse(svc.turn_on())
        adb.assert_not_called()

    def test_network_never_returns_means_failure(self):
        """A pair request that lands but leaves the box offline is not a win."""
        waker = Mock(spec=CecWaker)
        waker.wake.return_value = WakeResult(True, "pair request sent")
        svc = _service(waker)
        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "ensure_connected", return_value=False):
                with patch("services.media_service.time.sleep"):
                    self.assertFalse(svc.turn_on())

    def test_wait_loop_clears_the_offline_cooldown(self):
        """
        ensure_connected() refuses to retry for _OFFLINE_COOLDOWN after a miss.
        That is right normally and wrong while waiting out a boot, so the loop
        must clear it or it polls exactly once and gives up.
        """
        waker = Mock(spec=CecWaker)
        waker.wake.return_value = WakeResult(True, "sent")
        svc = _service(waker)
        svc._last_fail_time = 1.0

        seen = []

        def record():
            seen.append(svc._last_fail_time)
            svc._last_fail_time = 999.0  # simulate ensure_connected stamping a miss
            return len(seen) >= 2

        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "ensure_connected", side_effect=record):
                # The wait loop asks the box whether it has finished booting once
                # the connection comes back. Unpatched that is a real getprop at
                # whatever adb is attached to.
                with patch.object(svc, "is_boot_completed", return_value=True):
                    with patch("services.media_service.time.sleep"):
                        self.assertTrue(svc.turn_on())

        self.assertEqual(seen, [0, 0], "cooldown was not cleared before each retry")


class TurnOffTests(unittest.TestCase):
    def test_sends_sleep(self):
        svc = _service()
        with patch.object(svc, "is_awake", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "")) as adb:
                self.assertTrue(svc.turn_off())
        self.assertIn("KEYCODE_SLEEP", adb.call_args[0][0])

    def test_unreachable_is_already_off(self):
        svc = _service()
        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "_adb") as adb:
                self.assertTrue(svc.turn_off())
        adb.assert_not_called()


class PowerToggleTests(unittest.TestCase):
    def test_awake_toggles_off(self):
        svc = _service()
        with patch.object(svc, "is_awake", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "")) as adb:
                svc.power_toggle()
        self.assertIn("KEYCODE_SLEEP", adb.call_args[0][0])

    def test_unreachable_toggles_on_over_cec(self):
        waker = Mock(spec=CecWaker)
        waker.wake.return_value = WakeResult(True, "sent")
        svc = _service(waker)
        with patch.object(svc, "is_awake", return_value=None):
            with patch.object(svc, "ensure_connected", return_value=True):
                with patch.object(svc, "is_boot_completed", return_value=True):
                    svc.power_toggle()
        waker.wake.assert_called_once()

    def test_keycode_power_is_never_sent(self):
        """Guard the regression directly, across every state."""
        for state in (True, False, None):
            with self.subTest(state=state):
                waker = Mock(spec=CecWaker)
                waker.wake.return_value = WakeResult(True, "sent")
                svc = _service(waker)
                with patch.object(svc, "is_awake", return_value=state):
                    with patch.object(svc, "ensure_connected", return_value=True):
                        with patch.object(svc, "_adb", return_value=(True, "")) as adb:
                            svc.power_toggle()
                sent = " ".join(str(c[0][0]) for c in adb.call_args_list)
                self.assertNotIn("KEYCODE_POWER", sent)


class IsAwakeTests(unittest.TestCase):
    def test_parses_wakefulness(self):
        svc = _service()
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "  mWakefulness=Awake\n")):
                self.assertIs(svc.is_awake(), True)
            with patch.object(svc, "_adb", return_value=(True, "  mWakefulness=Asleep\n")):
                self.assertIs(svc.is_awake(), False)

    def test_unreachable_is_none_not_false(self):
        """None and False take different branches -- collapsing them breaks wake."""
        svc = _service()
        with patch.object(svc, "ensure_connected", return_value=False):
            self.assertIsNone(svc.is_awake())

    def test_unparseable_output_is_none(self):
        svc = _service()
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "_adb", return_value=(True, "garbage")):
                self.assertIsNone(svc.is_awake())


class DispatchTests(unittest.TestCase):
    """turn_on must survive the requires_tv gate, which returns early when the
    TV is unreachable -- the exact state it exists to fix."""

    def _dispatch(self, action, media_svc):
        return _dispatch_tv({"action": action}, media_svc, None, None, {})

    def _dispatch_params(self, params, media_svc):
        return _dispatch_tv(params, media_svc, None, None, {})

    def test_turn_on_reaches_the_service_while_unreachable(self):
        media = Mock()
        media.ensure_connected.return_value = False
        media.turn_on.return_value = True
        self.assertEqual(self._dispatch("turn_on", media), "TV is on")
        media.turn_on.assert_called_once()

    def test_failed_turn_on_says_so(self):
        media = Mock()
        media.ensure_connected.return_value = False
        media.turn_on.return_value = False
        media.last_wake_result = WakeResult(False, "no network after wake")
        self.assertIn("remote", self._dispatch("turn_on", media))

    def test_revoked_token_says_approve_me_not_use_the_remote(self):
        """
        These need opposite fixes. Sending Master Miguel to the remote when the
        real answer is "accept the prompt on screen" is the whole failure mode.
        """
        media = Mock()
        media.ensure_connected.return_value = False
        media.turn_on.return_value = False
        media.last_wake_result = WakeResult(False, "token rejected", needs_pairing=True)
        reply = self._dispatch("turn_on", media)
        self.assertIn("approved on screen", reply)
        self.assertNotIn("remote", reply)

    def test_switch_hdmi_routes_with_the_port(self):
        media = Mock()
        media.switch_hdmi.return_value = WakeResult(True, "ok")
        self.assertEqual(
            self._dispatch_params({"action": "switch_hdmi", "hdmi_port": 2}, media),
            "switched to HDMI 2")
        media.switch_hdmi.assert_called_once_with(2)

    def test_switch_hdmi_without_a_port_asks(self):
        media = Mock()
        self.assertIn("which HDMI", self._dispatch_params({"action": "switch_hdmi"}, media))
        media.switch_hdmi.assert_not_called()

    def test_legacy_action_names_still_route(self):
        media = Mock()
        media.turn_on.return_value = True
        media.turn_off.return_value = True
        self.assertEqual(self._dispatch("wake", media), "TV is on")
        self.assertEqual(self._dispatch("sleep", media), "TV going to standby")

    def test_turn_off_routes(self):
        media = Mock()
        media.turn_off.return_value = True
        self.assertEqual(self._dispatch("turn_off", media), "TV going to standby")


class EnsurePlayableTests(unittest.TestCase):
    """
    "Put on show X" with the room asleep must work, not return an error.

    The old gate was binary and gave up; recovery depended on the model deciding
    to call turn_on and re-issue the action. One tool call, one outcome.
    """

    def _media(self, *, reachable=True, active_source=True):
        media = Mock()
        media.ensure_connected.return_value = reachable
        # The verified selector; MediaService owns the retry/cycle loop, and
        # EnsureActiveSourceTests covers it. Here we only care what the
        # dispatcher does with each answer.
        media.ensure_active_source.return_value = active_source
        media.mibox_hdmi_port = 2
        media.unreachable_reason = ""
        media.last_wake_result = None
        return media

    def test_a_ready_room_is_left_alone(self):
        media = self._media()
        self.assertEqual(_ensure_playable(media), "")
        media.turn_on.assert_not_called()

    def test_a_parked_input_is_selected_before_playing(self):
        """Box awake but the TV is on terrestrial: the one real 'TV is wrong' case."""
        media = self._media()
        self.assertEqual(_ensure_playable(media), "")
        media.ensure_active_source.assert_called_once()
        media.turn_on.assert_not_called()

    def test_an_input_that_cannot_be_selected_is_admitted_not_faked(self):
        """
        Returning "" here would claim success while the TV shows another source.
        Playback would start off-screen and look like nothing happened.
        """
        media = self._media(active_source=False)
        result = _ensure_playable(media)
        self.assertIn("remote", result)

    def test_unknown_active_source_still_proceeds(self):
        """
        None is "could not tell", not False. Refusing to play because a dumpsys
        was unreadable would be worse than playing on a screen that is probably
        already right.
        """
        media = self._media(active_source=None)
        self.assertEqual(_ensure_playable(media), "")

    def test_a_sleeping_room_is_woken_and_then_used(self):
        media = self._media(reachable=False)
        media.ensure_connected.side_effect = [False, True]
        media.turn_on.return_value = True
        self.assertEqual(_ensure_playable(media), "")
        media.turn_on.assert_called_once()

    def test_the_wake_is_announced_before_it_blocks(self):
        """A ~25s tool call that says nothing reads as a hang."""
        media = self._media(reachable=False)
        media.ensure_connected.side_effect = [False, True]
        media.turn_on.return_value = True
        spoken = []
        _ensure_playable(media, say_now=spoken.append)
        self.assertEqual(len(spoken), 1)
        self.assertTrue(spoken[0].strip())

    def test_a_ready_room_says_nothing(self):
        spoken = []
        _ensure_playable(self._media(), say_now=spoken.append)
        self.assertEqual(spoken, [], "no interim line when there is no wait")

    def test_a_failed_wake_reports_the_classified_reason(self):
        media = self._media(reachable=False)
        media.turn_on.return_value = False
        media.unreachable_reason = "no_adb_port"
        result = _ensure_playable(media)
        self.assertIn("ADB over Wi-Fi", result)

    def test_a_rejected_token_says_approve_not_use_the_remote(self):
        """Opposite fixes: the wrong line strands him."""
        media = self._media(reachable=False)
        media.turn_on.return_value = False
        media.last_wake_result = WakeResult(False, "rejected", needs_pairing=True)
        result = _ensure_playable(media)
        self.assertIn("Approve me on screen", result)

    def test_playback_actions_escalate_but_transport_controls_do_not(self):
        """
        A 25s wake is right for "put something on" and wrong for "pause" -- there
        is nothing to pause on a sleeping box, and he did not ask to turn it on.
        """
        media = self._media(reachable=False)
        media.turn_on.return_value = False
        media.unreachable_reason = "not_on_lan"

        _dispatch_tv({"action": "play_pause"}, media, None, None, {})
        media.turn_on.assert_not_called()

        _dispatch_tv({"action": "stremio_play", "title": "Fallout"}, media, None, None, {})
        media.turn_on.assert_called_once()


def _cec_config(**overrides) -> dict:
    """The real cec_wake block, with waits shortened and caches redirected.

    tv_mac and tv_duid are the TV's identity and come from config.yaml -- they
    used to be re-typed here, so a test could keep asserting against a set that
    had been replaced. Only the two paths and the three timings are overridden:
    the paths so a test never reads or writes the real caches, the timings so a
    failed-WoL test takes 100ms instead of 40 seconds.
    """
    cec = {
        "tv_state_path": "nonexistent_tv_state.json",
        "token_path": "nonexistent_samsung_token.txt",
        "key_delay_ms": 10,
        "wol_attempts": 2,
        "tv_boot_timeout_ms": 100,
        **overrides,
    }
    return config_for_tests(media={"cec_wake": cec})


class _NoNetworkScanMixin:
    """
    Fail loudly instead of scanning the operator's LAN.

    The TV now rides the shared DeviceFinder ladder: `arp -a` via subprocess and
    a TCP scan of the /24 on :8001 via socket. Both are correct in production and
    unacceptable in a unit test, for the same reason StremioService may not reach
    a live adb: the suite must not touch the network it happens to be run on.
    Both boundaries are stubbed here -- patching subprocess alone cannot stop a
    SYN scan. Individual tests still stub the probe they exercise.
    """

    def setUp(self):
        super().setUp()
        run = patch("services.device_finder.subprocess.run")
        self.subprocess_run = run.start()
        self.subprocess_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
        self.addCleanup(run.stop)
        conn = patch("services.device_finder.socket.create_connection",
                     side_effect=OSError("unit tests do not open sockets"))
        self.create_connection = conn.start()
        self.addCleanup(conn.stop)
        # The finder persists a HINT hit too (the private ladder only wrote on a
        # discovery hit), so a test that patches _probe to True would otherwise
        # create tv_state_path in the CWD. Tests that assert on remember() patch
        # it again on the instance, which wins over this class-level stub.
        remember = patch("services.device_finder.DeviceFinder.remember")
        remember.start()
        self.addCleanup(remember.stop)


class CecWakerTests(_NoNetworkScanMixin, unittest.TestCase):
    def test_unit_tests_never_scan_the_real_subnet(self):
        # The setUp stubs are the guard. If someone removes them, this fails here
        # rather than by SYN-scanning 254 hosts of whatever LAN the suite runs on.
        # Every rung is port-gated, so a refused socket is enough to walk the whole
        # ladder without a single HTTP request.
        waker = CecWaker(_cec_config())
        with patch("services.cec_wake.urllib.request.urlopen") as urlopen:
            self.assertEqual(waker.resolve_tv_ip(force=True), "")
        self.subprocess_run.assert_called()
        self.create_connection.assert_called()
        urlopen.assert_not_called()

    def test_probe_never_opens_http_when_the_port_is_closed(self):
        """
        The TV-side 21-second rule. A TV that is off does not refuse :8001, it
        stays silent, and the old probe waited out a 4s HTTP timeout to learn
        that -- on the cached address, then the hint, then 254 more times in the
        sweep. Measured 2026-09-11 at ~35s per miss. The port gate makes it 0.3s.
        """
        waker = CecWaker(_cec_config())
        with patch("services.cec_wake.port_open", return_value=False) as gate:
            with patch("services.cec_wake.urllib.request.urlopen") as urlopen:
                self.assertFalse(waker._probe("192.168.1.34"))  # config-literal: any address; the gate is what is under test
        gate.assert_called_once()
        urlopen.assert_not_called()

    def test_self_disables_without_a_mac(self):
        waker = CecWaker(_cec_config(tv_mac=""))
        self.assertFalse(waker.available)
        self.assertIn("tv_mac", waker.unavailable_reason)

    def test_self_disables_without_a_duid(self):
        """Without it, a stale IP some other device took would be trusted."""
        waker = CecWaker(_cec_config(tv_duid=""))
        self.assertFalse(waker.available)
        self.assertIn("tv_duid", waker.unavailable_reason)

    def test_self_disables_when_flag_is_off(self):
        self.assertFalse(CecWaker(_cec_config(enabled=False)).available)

    def test_disabled_waker_returns_result_and_touches_nothing(self):
        waker = CecWaker(_cec_config(enabled=False))
        with patch.object(CecWaker, "_send_wol") as wol:
            result = waker.wake()
        self.assertFalse(result)
        wol.assert_not_called()

    def test_wol_is_sent_even_when_the_tv_answers_at_its_address(self):
        """
        This test used to assert the OPPOSITE, and the opposite was the bug.

        The set has two standby depths: in the shallow one :8001/api/v2/ still
        answers while the screen is off. Skipping Wake-on-LAN because the probe
        answered therefore skipped the only step that powers the TV on, and
        wake() reported success against a dark television -- observed on the real
        set 2026-09-05. The probe verifies the ADDRESS, not the power state (this
        model exposes no PowerState field at all), and WoL is a harmless no-op on
        a TV that is already on. So it always goes out.
        """
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=True):
            with patch.object(CecWaker, "_send_wol") as wol:
                with patch.object(CecWaker, "_send_keys",
                                  return_value=WakeResult(True, "sent")):
                    self.assertTrue(waker.wake())
        wol.assert_called()

    def test_wol_is_sent_and_retried_when_the_tv_is_down(self):
        # An unreachable TV sends every poll into discovery. The mixin stubs both
        # network boundaries, so the real candidate sources run and find nothing.
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=False):
            with patch.object(CecWaker, "_send_wol") as wol:
                with patch("services.cec_wake.time.sleep"):
                    result = waker.wake()
        self.assertFalse(result)
        self.assertIn("did not come up", result.detail)
        # One unconditional burst (see the shallow-standby note above) plus
        # wol_attempts retries while the TV stays unreachable.
        self.assertEqual(wol.call_count, 3)

    def test_wol_wait_rediscovers_on_every_poll(self):
        """
        Miss -> rediscover -> retry, the same rule as MediaService._wait_for_box.

        2026-09-11: the TV came back from deep standby on a new lease (.34 -> .59)
        and the box did the same. A poll loop that only re-probed the known
        address would wait out its whole boot timeout and then fail, so each poll
        walks the full ladder and the wait ends the moment the TV is up anywhere.
        The pre-WoL check is the one exception: known addresses only, because a
        TV that is off cannot be found by scanning and the scan just delays the
        packet that turns it on.
        """
        moved_to = "192.168.1.59"  # config-literal: the lease the TV came back on
        waker = CecWaker(_cec_config())
        scans = []
        waker._finder.candidate_sources = [lambda base: scans.append(base) or [moved_to]]
        with patch.object(CecWaker, "_probe", side_effect=lambda ip: ip == moved_to):
            with patch.object(CecWaker, "_send_wol"):
                with patch.object(waker._finder, "remember") as remember:
                    with patch("services.cec_wake.time.sleep"):
                        self.assertTrue(waker.power_on_tv())
        self.assertEqual(waker._tv_ip, moved_to)
        self.assertEqual(len(scans), 1, "the first poll must rediscover, not wait for a timeout")
        remember.assert_called_once_with(moved_to)

    def test_pre_wol_check_does_not_scan(self):
        """A TV that is off cannot be found by a scan; the scan only delays the WoL."""
        waker = CecWaker(_cec_config())
        scans = []
        waker._finder.candidate_sources = [lambda base: scans.append(base) or []]
        with patch.object(CecWaker, "_probe", return_value=False):
            self.assertFalse(waker._tv_is_up())
        self.assertEqual(scans, [])

    def test_wake_resolves_the_host_when_the_tv_already_answers(self):
        """
        The happy path must reach _remote() with a real host.

        `power_on_tv` returns early on `_tv_is_up`, so if that probe does not
        record where the TV answered, `self._tv_ip` is still None by the time
        SamsungTVWS is constructed and every wake fails with "Can't build URL
        with port but without host". Patching _send_keys hides this, so this
        test deliberately patches one layer lower.
        """
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=True):
            with patch.object(CecWaker, "_remote") as remote:
                result = waker.wake()
        self.assertTrue(result)
        self.assertTrue(waker._tv_ip, "wake() built the remote without a host")
        remote.assert_called()

    def test_wake_sends_the_input_key_exactly_twice(self):
        """Away then back. The return press is what emits Set Stream Path."""
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=True):
            with patch.object(CecWaker, "_send_keys",
                              return_value=WakeResult(True, "sent")) as keys:
                waker.wake()
        self.assertEqual(keys.call_args[0][0], ["KEY_HDMI", "KEY_HDMI"])

    def test_auth_failure_is_flagged_as_needing_pairing(self):
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=True):
            with patch.object(CecWaker, "_remote",
                              side_effect=Exception("ms.channel.unauthorized")):
                result = waker.wake()
        self.assertFalse(result)
        self.assertTrue(result.needs_pairing)

    def test_other_remote_failures_are_not_pairing_failures(self):
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=True):
            with patch.object(CecWaker, "_remote", side_effect=OSError("no route")):
                result = waker.wake()
        self.assertFalse(result)
        self.assertFalse(result.needs_pairing)


class TvDiscoveryTests(_NoNetworkScanMixin, unittest.TestCase):
    """
    A DHCP move must self-heal, not surface as 'couldn't turn the TV on'.

    The ladder itself is DeviceFinder's and is pinned in test_device_discovery;
    these pin that the TV is wired to it the way the box is.
    """

    def test_cached_ip_that_verifies_skips_discovery(self):
        cached_ip = real_config()["media"]["cec_wake"]["tv_ip"]
        waker = CecWaker(_cec_config())
        scans = []
        waker._finder.candidate_sources = [lambda base: scans.append(base) or []]
        with patch.object(CecWaker, "_probe", return_value=True):
            self.assertEqual(waker.resolve_tv_ip(), cached_ip)
        self.assertEqual(scans, [])

    def test_wrong_duid_at_the_cached_ip_is_rejected(self):
        """Another device taking the address must not be mistaken for the TV."""
        moved_to = "192.168.1.77"  # config-literal: an address the TV moved to, found by ARP
        waker = CecWaker(_cec_config())
        waker._finder.candidate_sources = [lambda base: [moved_to]]
        with patch.object(CecWaker, "_probe", side_effect=lambda ip: ip == moved_to):
            with patch.object(waker._finder, "remember") as remember:
                self.assertEqual(waker.resolve_tv_ip(), moved_to)
        remember.assert_called_once_with(moved_to)

    def test_the_tcp_scan_is_the_last_resort(self):
        moved_to = "192.168.1.90"  # config-literal: an address only the subnet scan finds
        waker = CecWaker(_cec_config())
        order = []
        waker._finder.candidate_sources = [
            lambda base: order.append("arp") or [],
            lambda base: order.append("scan") or [moved_to],
        ]
        with patch.object(CecWaker, "_probe", side_effect=lambda ip: ip == moved_to):
            with patch.object(waker._finder, "remember"):
                self.assertEqual(waker.resolve_tv_ip(), moved_to)
        self.assertEqual(order, ["arp", "scan"])

    def test_the_tv_is_wired_to_arp_then_a_port_scan(self):
        """
        What ships: warm ARP by MAC, then a TCP scan of the /24 on :8001. No ping
        flood and no all-hosts HTTP sweep -- both were the private ladder's cost.
        """
        waker = CecWaker(_cec_config())
        names = [getattr(src, "__qualname__", "").split(".")[0]
                 for src in waker._finder.candidate_sources]
        self.assertEqual(names, ["arp_table_candidates", "tcp_port_candidates"])
        self.assertEqual(waker._finder.mac, real_config()["media"]["cec_wake"]["tv_mac"])

    def test_total_failure_returns_empty_not_an_exception(self):
        waker = CecWaker(_cec_config())
        with patch.object(CecWaker, "_probe", return_value=False):
            self.assertEqual(waker.resolve_tv_ip(), "")


class HdmiInventoryTests(unittest.TestCase):
    def _svc(self, ports, media_enabled=True):
        from services.llm import LLMService
        svc = LLMService.__new__(LLMService)
        svc.media_enabled = media_enabled
        svc.hdmi_ports = ports
        return svc

    def test_configured_ports_are_advertised(self):
        line = self._svc({2: "mi box", 1: "playstation"})._hdmi_inventory()
        self.assertIn("HDMI 1 is the playstation", line)
        self.assertIn("HDMI 2 is the mi box", line)

    def test_no_ports_advertises_nothing(self):
        """Offering an input nobody labelled is worse than offering none."""
        self.assertEqual(self._svc({})._hdmi_inventory(), "")

    def test_media_disabled_advertises_nothing(self):
        self.assertEqual(self._svc({2: "mi box"}, media_enabled=False)._hdmi_inventory(), "")




class StatusDispatchTests(unittest.TestCase):
    """
    get_status used to report TV power from cec_waker._tv_is_up(), a REST
    reachability probe. This set answers that endpoint in standby, so she said
    "TV: on" about a television that was off. cec_wake.py already said not to:
    "What we must NOT do is treat the answer as 'powered on'."

    It also stapled up to 15 raw lines of `dumpsys media_session` into the reply,
    which then went to a voice model to be read out loud.
    """

    def _status(self, **fields):
        media = Mock()
        fields.setdefault("reachable", True)
        media.ensure_connected.return_value = fields["reachable"]
        media.room_status.return_value = RoomStatus(**fields)
        return media, _dispatch_tv({"action": "get_status"}, media, None, None, {})

    def test_a_television_in_standby_is_reported_off(self):
        # The regression. Before this, a standby TV came back as "TV: on".
        _, reply = self._status(reachable=True, awake=True, tv_power="standby")
        self.assertIn("TV off", reply)
        self.assertNotIn("TV on", reply)

    def test_power_comes_from_cec_and_never_from_the_rest_probe(self):
        media, _ = self._status(reachable=True, awake=True, tv_power="on")
        media.cec_waker._tv_is_up.assert_not_called()

    def test_a_field_it_could_not_read_is_a_clause_it_does_not_say(self):
        # None means "could not tell". The old branch printed "unknown" for
        # exactly the fields it had failed to read.
        _, reply = self._status(reachable=True, awake=True)
        self.assertNotIn("unknown", reply.lower())
        self.assertNotIn("None", reply)

    def test_the_reply_is_one_line_with_no_dumpsys_in_it(self):
        _, reply = self._status(
            reachable=True, awake=True, tv_power="on", on_the_box=True,
            app="stremio", playing=True,
        )
        self.assertNotIn("\n", reply)
        self.assertNotIn("state=", reply)
        self.assertNotIn("mIsActiveSource", reply)

    def test_a_paused_title_is_still_named(self):
        # Stremio keeps its metadata across a pause, so it still knows what is
        # loaded. Saying "nothing playing" and dropping the title throws away
        # the more useful half of what was read.
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=False,
            title="Fallout, The Strip", playback="paused",
        )
        self.assertIn("paused on Fallout, The Strip", reply)

    def test_the_pause_word_is_read_not_guessed(self):
        # Without a session state there is no way to know it is paused rather
        # than stopped or buffering, so the title is dropped instead of being
        # attached to a guessed verb.
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=False,
            title="Fallout, The Strip", playback=None,
        )
        self.assertIn("nothing playing", reply)
        self.assertNotIn("paused", reply)

    def test_a_buffering_session_says_buffering(self):
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=False,
            title="Fallout, The Strip", playback="buffering",
        )
        self.assertIn("buffering on Fallout, The Strip", reply)

    def test_elapsed_time_is_reported_when_something_is_named(self):
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=True,
            title="Fallout, The Strip", position_s=732,
        )
        self.assertIn("12 minutes in", reply)

    def test_elapsed_time_is_never_a_percentage_or_time_left(self):
        # dumpsys media_session prints no duration, so there is nothing to
        # measure against and nothing may imply there is.
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=True,
            title="Fallout, The Strip", position_s=732,
        )
        self.assertNotIn("%", reply)
        self.assertNotIn("left", reply)

    def test_the_box_volume_is_reported(self):
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=True,
            volume=8, volume_max=15, muted=False,
        )
        self.assertIn("box volume 8 of 15", reply)

    def test_muted_replaces_the_level_rather_than_joining_it(self):
        _, reply = self._status(
            reachable=True, awake=True, app="stremio", playing=True,
            volume=8, volume_max=15, muted=True,
        )
        self.assertIn("box muted", reply)
        self.assertNotIn("8 of 15", reply)

    def test_an_unreadable_volume_is_simply_not_mentioned(self):
        _, reply = self._status(reachable=True, awake=True, app="stremio", playing=True)
        self.assertNotIn("volume", reply)

    def test_the_session_title_beats_the_launch_memory(self):
        # The box saw it. She only remembers launching it, which is weaker
        # evidence and does not survive him using the remote.
        media = Mock()
        media.ensure_connected.return_value = True
        media.room_status.return_value = RoomStatus(
            reachable=True, awake=True, app="stremio", playing=True,
            title="Fallout, The Strip",
        )
        store = NowPlaying()
        store.remember("stremio", "something else entirely")
        reply = _dispatch_tv(
            {"action": "get_status"}, media, None, None, {}, now_playing=store
        )
        self.assertIn("Fallout, The Strip", reply)
        self.assertNotIn("something else entirely", reply)

    def test_it_names_the_app_and_whether_anything_is_playing(self):
        _, reply = self._status(reachable=True, awake=True, app="youtube", playing=False)
        self.assertIn("youtube", reply)
        self.assertIn("nothing playing", reply)

    def test_an_input_parked_elsewhere_is_worth_saying(self):
        _, reply = self._status(reachable=True, awake=True, tv_power="on", on_the_box=False)
        self.assertIn("another input", reply)

    def test_a_sleeping_box_says_so_and_stops(self):
        _, reply = self._status(reachable=True, awake=False, tv_power="standby")
        self.assertIn("asleep", reply)
        self.assertNotIn("playing", reply)

    def test_an_unreachable_box_gets_the_classified_line_not_a_status(self):
        media = Mock()
        media.ensure_connected.return_value = False
        media.unreachable_reason = "no_adb_port"
        reply = _dispatch_tv({"action": "get_status"}, media, None, None, {})
        self.assertIn("developer options", reply)
        media.room_status.assert_not_called()

    def test_asking_what_is_on_never_wakes_the_room(self):
        # get_status is in requires_tv, deliberately NOT needs_screen. A question
        # about the room must not turn the room on for 25 seconds.
        media = Mock()
        media.ensure_connected.return_value = False
        media.unreachable_reason = "not_on_lan"
        _dispatch_tv({"action": "get_status"}, media, None, None, {})
        media.turn_on.assert_not_called()



class NowPlayingDispatchTests(unittest.TestCase):
    """
    She launched it, so she can name it -- but only while the box still agrees
    the app is up, and only if she actually played it rather than opening a
    search. Both rules exist so "now playing" cannot become the next confident
    wrong answer.
    """

    def _media(self, app, playing=True):
        media = Mock()
        media.ensure_connected.return_value = True
        media.room_status.return_value = RoomStatus(
            reachable=True, awake=True, app=app, playing=playing
        )
        return media

    def _status(self, media, store):
        return _dispatch_tv(
            {"action": "get_status"}, media, None, None, {}, now_playing=store
        )

    def test_she_names_what_she_put_on(self):
        store = NowPlaying()
        store.remember("stremio", "Fallout")
        self.assertIn("Fallout", self._status(self._media("stremio"), store))

    def test_the_memory_is_dropped_when_he_switched_apps(self):
        store = NowPlaying()
        store.remember("stremio", "Fallout")
        reply = self._status(self._media("youtube"), store)
        self.assertNotIn("Fallout", reply)
        self.assertIn("youtube", reply)

    def test_she_admits_it_when_he_started_it_himself(self):
        reply = self._status(self._media("stremio"), NowPlaying())
        self.assertIn("didn't start it", reply)

    def test_a_search_is_never_reported_as_playing_something(self):
        # She opened a results page. What he then picked is not hers to claim.
        store = NowPlaying()
        store.remember("youtube", "bossa nova", "opened")
        reply = self._status(self._media("youtube"), store)
        self.assertNotIn("bossa nova", reply)
        self.assertIn("didn't start it", reply)

    def test_dispatch_without_a_store_still_answers(self):
        # The 11 older _dispatch_tv tests pass no store at all.
        reply = _dispatch_tv({"action": "get_status"}, self._media("stremio"), None, None, {})
        self.assertIn("stremio", reply)


class LaunchMemoryRecordingTests(unittest.TestCase):
    """What gets remembered, and what deliberately does not."""

    def _media(self):
        media = Mock()
        media.ensure_connected.return_value = True
        return media

    def _stremio(self, success=True, target_mode="episode"):
        svc = Mock()
        svc.play.return_value = SimpleNamespace(
            success=success, requires_confirmation=False,
            target_mode=target_mode, message="nope",
        )
        return svc

    def test_a_successful_stremio_play_is_remembered(self):
        store = NowPlaying()
        _dispatch_tv(
            {"action": "stremio_play", "title": "Fallout"},
            self._media(), self._stremio(), None, {}, now_playing=store,
        )
        self.assertEqual(store.current("stremio").label, "Fallout")

    def test_a_failed_stremio_play_is_not_remembered(self):
        # Never claim playback that did not happen. Same rule as the autoplay
        # verification: media_session decides, not optimism.
        store = NowPlaying()
        _dispatch_tv(
            {"action": "stremio_play", "title": "Fallout"},
            self._media(), self._stremio(success=False), None, {}, now_playing=store,
        )
        self.assertIsNone(store.current("stremio"))

    def test_a_series_page_is_remembered_as_opened_not_playing(self):
        store = NowPlaying()
        _dispatch_tv(
            {"action": "stremio_play", "title": "Fallout"},
            self._media(), self._stremio(target_mode="detail"), None, {},
            now_playing=store,
        )
        self.assertEqual(store.current("stremio").kind, "opened")

    def test_a_youtube_playlist_remembers_the_category_he_asked_for(self):
        store = NowPlaying()
        _dispatch_tv(
            {"action": "youtube_playlist", "playlist_name": "samba"},
            self._media(), None, None, {"samba": ["RD1"]}, now_playing=store,
        )
        self.assertIn("samba", store.current("youtube").label)

    def test_go_home_forgets(self):
        store = NowPlaying()
        store.remember("stremio", "Fallout")
        _dispatch_tv({"action": "go_home"}, self._media(), None, None, {}, now_playing=store)
        self.assertIsNone(store.current("stremio"))

    def test_launching_a_bare_app_forgets(self):
        # "Open Stremio" puts nothing specific on, so the old label is now wrong.
        store = NowPlaying()
        store.remember("stremio", "Fallout")
        media = self._media()
        media.launch_app.return_value = (True, "opened stremio")
        _dispatch_tv(
            {"action": "launch_app", "app_name": "stremio"},
            media, None, None, {}, now_playing=store,
        )
        self.assertIsNone(store.current("stremio"))



class BootCompletedTests(unittest.TestCase):
    """
    Reachable is not ready. adbd comes up early in boot, so ensure_connected()
    can succeed against a box that cannot yet launch an app -- and a Stremio deep
    link fired into that window is a launch that quietly does nothing.

    `sys.boot_completed` is the signal for it. Verified present on the real box
    (Android 11, SDK 30).
    """

    def _svc(self, reply):
        svc = _service()
        svc._adb = Mock(return_value=reply)
        return svc

    def test_one_means_booted(self):
        self.assertIs(self._svc((True, "1")).is_boot_completed(), True)

    def test_zero_means_still_booting(self):
        self.assertIs(self._svc((True, "0")).is_boot_completed(), False)

    def test_a_failed_getprop_is_none_not_false(self):
        # None is "cannot tell". False would mean "still booting" and would burn
        # the whole settle window waiting for something that already happened.
        self.assertIsNone(self._svc((False, "")).is_boot_completed())

    def test_an_empty_answer_is_none_not_false(self):
        # The property is unset early in boot, and unset is not the same claim
        # as a read that came back saying zero.
        self.assertIsNone(self._svc((True, "")).is_boot_completed())

    def test_it_asks_for_the_right_property(self):
        svc = self._svc((True, "1"))
        svc.is_boot_completed()
        self.assertIn("sys.boot_completed", svc._adb.call_args[0][0])


class WaitForBootTests(unittest.TestCase):
    def test_the_loop_keeps_waiting_while_the_box_is_still_booting(self):
        """
        The point of the change. Returning on connection alone handed back a box
        that answered ADB but could not launch anything yet.
        """
        svc = _service()
        booted = [False, False, True]
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "is_boot_completed", side_effect=booted):
                with patch("services.media_service.time.sleep"):
                    self.assertTrue(svc._wait_for_box())

    def test_an_unreadable_boot_flag_is_accepted_rather_than_waited_out(self):
        """
        Fail open. Before this change the loop returned on the connection alone,
        so a box that will not answer getprop must not become WORSE than that.
        """
        svc = _service()
        with patch.object(svc, "ensure_connected", return_value=True):
            with patch.object(svc, "is_boot_completed", return_value=None):
                with patch("services.media_service.time.sleep"):
                    self.assertTrue(svc._wait_for_box())

    def test_the_boot_check_is_skipped_while_the_box_is_unreachable(self):
        # No point asking a box that is not answering, and `and` must short-circuit
        # so the poll stays one round trip while the box is still down.
        svc = _service()
        with patch.object(svc, "ensure_connected", return_value=False):
            with patch.object(svc, "is_boot_completed") as boot:
                with patch("services.media_service.time.sleep"):
                    with patch.object(svc._finder, "resolve", return_value=""):
                        svc._wait_for_box()
        boot.assert_not_called()

    def test_the_wait_loop_never_shells_out_to_a_real_adb(self):
        """
        Regression guard, and it is here because this exact thing happened.

        Adding the boot read inside the loop made two existing tests fire a real
        `getprop` at whatever box adb happened to be attached to, and they went
        on passing. Anything the loop asks the box must be a named, patchable
        method rather than a bare _adb call.
        """
        svc = _service()
        with patch("services.media_service.subprocess.run",
                   side_effect=AssertionError("the wait loop reached a real adb")):
            with patch.object(svc, "ensure_connected", return_value=True):
                with patch.object(svc, "is_boot_completed", return_value=True):
                    self.assertTrue(svc._wait_for_box())

if __name__ == "__main__":
    unittest.main()
