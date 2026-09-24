"""
The television's volume over UPnP (services/tv_volume.py) and the control_tv
volume actions that use it.

No test here reaches the network: every TvVolume gets a stubbed resolver and
`requests.post` patched to a fake TV, and a guard below proves it.
"""
import re
import unittest
from unittest.mock import Mock, patch

import requests

from core.orchestrator import _dispatch_tv, _status_line
from services.media_service import RoomStatus
from services.tv_volume import TvVolume, VolumeResult
from tests.config_fixture import config_for_tests


class _FakeTv:
    """Answers RenderingControl SOAP like the UE49M5505 does."""

    def __init__(self, volume=13, muted=False, down=False, fail_first=0):
        self.volume = volume
        self.muted = muted
        self.down = down
        self.fail_first = fail_first
        self.calls = []

    def post(self, url, data, headers, timeout):
        action = headers["SOAPACTION"].split("#")[1].strip('"')
        body = data.decode()
        self.calls.append((url, action, body))
        if self.down or self.fail_first > 0:
            self.fail_first -= 1
            raise requests.ConnectionError("refused")
        response = Mock(status_code=200)
        if action == "GetVolume":
            response.text = f"<CurrentVolume>{self.volume}</CurrentVolume>"
        elif action == "GetMute":
            response.text = f"<CurrentMute>{int(self.muted)}</CurrentMute>"
        elif action == "SetVolume":
            self.volume = int(re.search(r"<DesiredVolume>(\d+)<", body).group(1))
            response.text = ""
        elif action == "SetMute":
            self.muted = re.search(r"<DesiredMute>(\d)<", body).group(1) == "1"
            response.text = ""
        return response

    def actions(self):
        return [a for _, a, _ in self.calls]


def _volume(tv, resolve=None, **cfg):
    config = config_for_tests(media={"tv_volume": cfg} if cfg else {})
    resolver = resolve or Mock(return_value="192.168.1.201")  # config-literal: the resolver stub's answer
    service = TvVolume(config, resolve_ip=resolver)
    patcher = patch("services.tv_volume.requests.post", side_effect=tv.post)
    patcher.start()
    return service, resolver, patcher


class TvVolumeServiceTests(unittest.TestCase):
    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _svc(self, tv, **kw):
        service, resolver, patcher = _volume(tv, **kw)
        self._patchers.append(patcher)
        return service, resolver

    def test_the_shipped_config_enables_it_with_the_agreed_defaults(self):
        service, _ = self._svc(_FakeTv())
        self.assertTrue(service.enabled)
        self.assertEqual((service.port, service.step, service.max_percent), (9197, 5, 40))

    def test_no_resolver_means_disabled(self):
        self.assertFalse(TvVolume(config_for_tests(), resolve_ip=None).enabled)

    def test_get_volume_reads_the_tv(self):
        service, _ = self._svc(_FakeTv(volume=13))
        self.assertEqual(service.get_volume(), 13)

    def test_the_soap_call_goes_to_rendering_control_on_9197(self):
        tv = _FakeTv()
        service, _ = self._svc(tv)
        service.get_volume()
        url, action, body = tv.calls[0]
        self.assertTrue(url.endswith(":9197/upnp/control/RenderingControl1"))
        self.assertIn("<Channel>Master</Channel>", body)

    def test_an_unreachable_tv_reads_as_none_not_zero(self):
        service, _ = self._svc(_FakeTv(down=True))
        self.assertIsNone(service.get_volume())
        self.assertIsNone(service.get_mute())

    def test_one_flake_is_retried_with_a_forced_rediscovery(self):
        # Measured: :9197 refused one connect and accepted the next.
        tv = _FakeTv(volume=13, fail_first=1)
        service, resolver = self._svc(tv)
        self.assertEqual(service.get_volume(), 13)
        self.assertEqual([c.kwargs for c in resolver.call_args_list],
                         [{"force": False}, {"force": True}])

    def test_an_unknown_address_is_unreachable_without_a_request(self):
        tv = _FakeTv()
        service, _ = self._svc(tv, resolve=Mock(return_value=""))
        self.assertIsNone(service.get_volume())
        self.assertEqual(tv.calls, [])

    def test_up_is_one_step_and_reports_both_levels(self):
        tv = _FakeTv(volume=13)
        service, _ = self._svc(tv)
        result = service.change(+5)
        self.assertTrue(result)
        self.assertEqual((result.previous, result.level, tv.volume), (13, 18, 18))

    def test_down_clamps_at_zero(self):
        tv = _FakeTv(volume=3)
        service, _ = self._svc(tv)
        self.assertEqual(service.change(-5).level, 0)

    def test_a_step_that_crosses_the_cap_lands_on_it(self):
        tv = _FakeTv(volume=38)
        service, _ = self._svc(tv)
        result = service.change(+5)
        self.assertTrue(result.capped)
        self.assertEqual(tv.volume, 40)

    def test_set_above_the_cap_needs_asking_twice(self):
        tv = _FakeTv(volume=13)
        service, _ = self._svc(tv)
        first = service.set_volume(60)
        self.assertFalse(first)
        self.assertTrue(first.needs_confirm)
        self.assertNotIn("SetVolume", tv.actions())
        self.assertTrue(service.set_volume(60))
        self.assertEqual(tv.volume, 60)

    def test_a_different_number_does_not_count_as_the_confirmation(self):
        tv = _FakeTv(volume=13)
        service, _ = self._svc(tv)
        service.set_volume(60)
        self.assertTrue(service.set_volume(70).needs_confirm)
        self.assertEqual(tv.volume, 13)

    def test_the_confirmation_expires(self):
        tv = _FakeTv(volume=13)
        service, _ = self._svc(tv, confirm_window_s=5)
        with patch("services.tv_volume.time.monotonic", side_effect=[100.0, 200.0, 200.0]):
            service.set_volume(60)
            self.assertTrue(service.set_volume(60).needs_confirm)

    def test_coming_down_from_above_the_cap_is_never_gated(self):
        tv = _FakeTv(volume=70)
        service, _ = self._svc(tv)
        self.assertTrue(service.set_volume(50))
        self.assertEqual(tv.volume, 50)

    def test_stepping_up_from_the_cap_needs_asking_twice(self):
        tv = _FakeTv(volume=40)
        service, _ = self._svc(tv)
        self.assertTrue(service.change(+5).needs_confirm)
        self.assertTrue(service.change(+5))
        self.assertEqual(tv.volume, 45)

    def test_mute_and_unmute_are_explicit_not_a_toggle(self):
        tv = _FakeTv(muted=False)
        service, _ = self._svc(tv)
        service.set_mute(True)
        service.set_mute(True)
        self.assertTrue(tv.muted)
        service.set_mute(False)
        self.assertFalse(tv.muted)

    def test_a_failed_result_is_falsy_but_exists(self):
        result = VolumeResult(False, unreachable=True)
        self.assertFalse(result)
        self.assertIsNotNone(result)


class DispatchTvVolumeTests(unittest.TestCase):
    """control_tv volume actions, with the TV service mocked."""

    def _dispatch(self, params, tv_volume, media=None):
        media = media if media is not None else Mock()
        reply = _dispatch_tv(params, media, None, None, {}, tv_volume=tv_volume)
        return reply, media

    def _tv(self):
        tv = Mock()
        tv.enabled = True
        tv.step = 5
        tv.max_percent = 40
        return tv

    def test_up_uses_the_tv_and_never_touches_the_box(self):
        tv = self._tv()
        tv.change.return_value = VolumeResult(True, level=18, previous=13)
        reply, media = self._dispatch({"action": "volume_up"}, tv)
        tv.change.assert_called_once_with(5)
        self.assertEqual(reply, "TV volume 13 to 18")
        media.ensure_connected.assert_not_called()
        media.volume_up.assert_not_called()

    def test_down_with_an_amount(self):
        tv = self._tv()
        tv.change.return_value = VolumeResult(True, level=3, previous=13)
        self._dispatch({"action": "volume_down", "volume_steps": 10}, tv)
        tv.change.assert_called_once_with(-10)

    def test_set_reports_the_level(self):
        tv = self._tv()
        tv.set_volume.return_value = VolumeResult(True, level=20, previous=13)
        reply, _ = self._dispatch({"action": "volume_set", "volume_percent": 20}, tv)
        self.assertEqual(reply, "TV volume 13 to 20")

    def test_set_without_a_number_asks_instead_of_guessing_fifty(self):
        tv = self._tv()
        reply, _ = self._dispatch({"action": "volume_set"}, tv)
        self.assertIn("what volume", reply)
        tv.set_volume.assert_not_called()

    def test_over_the_cap_asks_to_hear_it_again(self):
        tv = self._tv()
        tv.set_volume.return_value = VolumeResult(False, previous=13, needs_confirm=True, target=60)
        reply, _ = self._dispatch({"action": "volume_set", "volume_percent": 60}, tv)
        self.assertIn("above my 40 cap", reply)
        self.assertIn("again", reply)

    def test_a_capped_step_says_so(self):
        tv = self._tv()
        tv.change.return_value = VolumeResult(True, level=40, previous=38, capped=True)
        reply, _ = self._dispatch({"action": "volume_up"}, tv)
        self.assertIn("cap", reply)

    def test_tv_off_says_so_and_does_not_fall_back_to_the_box(self):
        tv = self._tv()
        tv.change.return_value = VolumeResult(False, unreachable=True)
        reply, media = self._dispatch({"action": "volume_up"}, tv)
        self.assertIn("TV looks off", reply)
        media.volume_up.assert_not_called()

    def test_mute_and_unmute(self):
        tv = self._tv()
        tv.set_mute.return_value = VolumeResult(True)
        self.assertEqual(self._dispatch({"action": "mute"}, tv)[0], "TV muted")
        self.assertEqual(self._dispatch({"action": "unmute"}, tv)[0], "TV unmuted")
        self.assertEqual([c.args for c in tv.set_mute.call_args_list], [(True,), (False,)])

    def test_disabled_tv_volume_keeps_the_old_box_path(self):
        tv = self._tv()
        tv.enabled = False
        media = Mock()
        media.ensure_connected.return_value = True
        reply, _ = self._dispatch({"action": "volume_up"}, tv, media)
        media.volume_up.assert_called_once_with(10)
        tv.change.assert_not_called()

    def test_no_tv_volume_at_all_keeps_the_old_box_path(self):
        media = Mock()
        media.ensure_connected.return_value = True
        self._dispatch({"action": "volume_set", "volume_percent": 30}, None, media)
        media.volume_set.assert_called_once_with(30)


class StatusLineTvVolumeTests(unittest.TestCase):
    def _status(self, **kw):
        base = dict(reachable=True, awake=True, tv_power="on", on_the_box=True,
                    app="stremio", playing=True, volume=15, volume_max=15, muted=False)
        base.update(kw)
        return RoomStatus(**base)

    def test_the_tv_volume_is_reported_and_a_maxed_box_is_not(self):
        reply = _status_line(self._status(), tv_level=13, tv_muted=False)
        self.assertIn("TV volume 13", reply)
        self.assertNotIn("box volume", reply)

    def test_a_box_below_max_is_still_mentioned(self):
        reply = _status_line(self._status(volume=6), tv_level=13, tv_muted=False)
        self.assertIn("box volume 6 of 15", reply)

    def test_tv_muted_replaces_the_level(self):
        reply = _status_line(self._status(), tv_level=13, tv_muted=True)
        self.assertIn("TV muted", reply)
        self.assertNotIn("TV volume", reply)

    def test_without_a_tv_reading_the_box_volume_is_what_we_say(self):
        reply = _status_line(self._status())
        self.assertIn("box volume 15 of 15", reply)

    def test_get_status_skips_the_tv_when_the_bus_says_standby(self):
        media = Mock()
        media.ensure_connected.return_value = True
        media.room_status.return_value = self._status(tv_power="standby")
        tv = Mock()
        tv.enabled = True
        _dispatch_tv({"action": "get_status"}, media, None, None, {}, tv_volume=tv)
        tv.get_volume.assert_not_called()

    def test_get_status_reads_the_tv_when_it_is_on(self):
        media = Mock()
        media.ensure_connected.return_value = True
        media.room_status.return_value = self._status()
        tv = Mock()
        tv.enabled = True
        tv.get_volume.return_value = 13
        tv.get_mute.return_value = False
        reply = _dispatch_tv({"action": "get_status"}, media, None, None, {}, tv_volume=tv)
        self.assertIn("TV volume 13", reply)


class NoNetworkGuardTests(unittest.TestCase):
    def test_this_file_never_reaches_a_real_tv(self):
        # _volume() patches requests.post; if that patch were removed, this
        # would try the network and the side effect below would fire instead.
        tv = _FakeTv()
        service, _, patcher = _volume(tv)
        try:
            with patch("requests.sessions.Session.request",
                       side_effect=AssertionError("real HTTP from a unit test")):
                self.assertEqual(service.get_volume(), 13)
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
