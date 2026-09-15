"""
DeebotService: self-disable rules, room resolution, the cached-id-then-live
fallback, and the auth-first-retry-once contract. Nothing here may reach
Ecovacs or Gmail: the network boundary is stubbed in every test and a guard
test pins that.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import deebot_session
from services.deebot_service import DeebotCommandResult, DeebotService, VacuumStatus
from tests.config_fixture import config_for_tests

FAKE_ENV = {
    "ECOVACS_EMAIL": "vacuum@example.com",
    "ECOVACS_PASSWORD": "hunter2",
    "ECOVACS_COUNTRY": "PT",
    "GMAIL_APP_PASSWORD": "",
    "ECOVACS_VERIFICATION_CODE": "",
}

ROOMS = {
    "kitchen": {"id": 7, "aliases": ["the kitchen", "cozinha"]},
    "living room": {"id": 0, "aliases": ["lounge"]},
    "study": {"aliases": ["office"]},  # no cached id -> live fallback
    "Default": {"id": 1},  # unnamed on the robot, must be dropped
}


def _service(**deebot) -> DeebotService:
    tmp = tempfile.mkdtemp()
    overrides = {"enabled": True, "state_dir": tmp, "rooms": ROOMS, "command_timeout_ms": 5000}
    overrides.update(deebot)
    config = config_for_tests(deebot=overrides)
    config["deebot"]["rooms"] = overrides["rooms"]  # replace, don't merge with the real file
    with patch.dict(os.environ, FAKE_ENV):
        return DeebotService(config)


class _Event:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _Room:
    def __init__(self, room_id, name):
        self.id, self.name = room_id, name


class FakeBot:
    """Stands in for deebot_client.device.Device: REST replies + subscribe-time events."""

    def __init__(self, replies=None, events=None):
        self.commands = []
        self._replies = list(replies or [])
        self._events = events or {}
        self.events = self
        self.torn_down = False

    async def execute_command(self, command):
        self.commands.append(command)
        if self._replies:
            return self._replies.pop(0)
        return {"ret": "ok", "resp": {"body": {"code": 0}}}

    def subscribe(self, event_type, callback):
        event = self._events.get(event_type.__name__)
        if event is not None:
            asyncio.get_running_loop().create_task(callback(event))
        return lambda: None

    async def teardown(self):
        self.torn_down = True


def _drive(service: DeebotService, bot: FakeBot):
    """Skip auth and run the command body straight against a fake bot."""

    async def _command(fn):
        return await fn(bot)

    service._command = _command
    return service


OK = {"ret": "ok", "resp": {"body": {"code": 0, "msg": "ok"}}}
REJECTED = {"ret": "ok", "resp": {"body": {"code": 500, "msg": "bad area"}}}


class NoNetworkGuardTests(unittest.TestCase):
    def test_this_file_never_opens_a_session(self):
        service = _service()
        with patch("aiohttp.ClientSession", side_effect=AssertionError("network")):
            with patch.object(deebot_session, "build_authenticator", new=AsyncMock()) as build:
                _drive(service, FakeBot()).status()
                build.assert_not_called()


class SelfDisableTests(unittest.TestCase):
    def test_enabled_with_flag_dependency_and_credentials(self):
        self.assertTrue(_service().enabled)

    def test_flag_off(self):
        self.assertFalse(_service(enabled=False).enabled)

    def test_missing_credentials(self):
        with patch.dict(os.environ, {**FAKE_ENV, "ECOVACS_EMAIL": "", "ECOVACS_PASSWORD": ""}):
            config = config_for_tests(deebot={"enabled": True, "email": "", "password": ""})
            self.assertFalse(DeebotService(config).enabled)

    def test_missing_dependency(self):
        with patch.dict("sys.modules", {"deebot_client": None}):
            self.assertFalse(_service().enabled)

    def test_missing_config_block_is_tolerated(self):
        with patch.dict(os.environ, FAKE_ENV):
            self.assertFalse(DeebotService({}).enabled)

    def test_disabled_service_answers_without_a_command(self):
        service = _service(enabled=False)
        self.assertFalse(service.status().available)
        self.assertFalse(service.clean_all())
        self.assertFalse(service.clean_rooms(["kitchen"]))
        self.assertEqual(service.sync_rooms(), {})


class RoomConfigTests(unittest.TestCase):
    def test_default_rooms_are_dropped(self):
        self.assertNotIn("Default", _service().rooms)
        self.assertEqual(set(_service().rooms), {"kitchen", "living room", "study"})

    def test_missing_id_is_none_not_zero(self):
        self.assertIsNone(_service().rooms["study"]["id"])
        self.assertEqual(_service().rooms["living room"]["id"], 0)

    def test_resolve_by_key_alias_and_despaced(self):
        service = _service()
        self.assertEqual(service.resolve_room("kitchen")[0], "kitchen")
        self.assertEqual(service.resolve_room("the kitchen")[0], "kitchen")
        self.assertEqual(service.resolve_room("cozinha")[0], "kitchen")
        self.assertEqual(service.resolve_room("livingroom")[0], "living room")
        self.assertEqual(service.resolve_room("office")[0], "study")

    def test_unknown_and_empty_resolve_to_nothing(self):
        service = _service()
        self.assertEqual(service.resolve_room("garage"), (None, None))
        self.assertEqual(service.resolve_room(""), (None, None))

    def test_live_names_match_config_keys(self):
        service = _service()
        live = {"Kitchen": 7, "Living Room": 0, "Study": 3}
        self.assertEqual(
            service._resolve_live_ids(["kitchen", "study", "garage"], live),
            {"kitchen": 7, "study": 3, "garage": None},
        )


class CleanRoomsTests(unittest.TestCase):
    def _clean_area(self, bot):
        from deebot_client.commands.json.clean import CleanArea

        return [c for c in bot.commands if isinstance(c, CleanArea)]

    def test_cached_ids_go_straight_to_the_robot(self):
        bot = FakeBot()
        result = _drive(_service(), bot).clean_rooms(["kitchen", "living room"])
        self.assertTrue(result)
        self.assertEqual(len(bot.commands), 1)
        self.assertEqual(self._clean_area(bot)[0]._additional_args["content"], "7,0")

    def test_missing_id_falls_back_to_live_names(self):
        rooms_event = _Event(rooms=[_Room(3, "Study"), _Room(7, "Kitchen")])
        bot = FakeBot(events={"RoomsEvent": rooms_event})
        result = _drive(_service(), bot).clean_rooms(["study"])
        self.assertTrue(result)
        self.assertEqual(self._clean_area(bot)[0]._additional_args["content"], "3")

    def test_live_fallback_that_finds_nothing_is_an_unknown_room(self):
        bot = FakeBot(events={"RoomsEvent": _Event(rooms=[_Room(7, "Kitchen")])})
        result = _drive(_service(), bot).clean_rooms(["study"])
        self.assertFalse(result)
        self.assertEqual(result.message, "I don't have that room on the vacuum's map.")
        self.assertEqual(self._clean_area(bot), [])

    def test_rejected_cached_id_re_resolves_live_and_retries_once(self):
        # The robot was remapped: kitchen is now id 9, config still says 7.
        bot = FakeBot(
            replies=[REJECTED, OK],
            events={"RoomsEvent": _Event(rooms=[_Room(9, "Kitchen")])},
        )
        result = _drive(_service(), bot).clean_rooms(["kitchen"])
        self.assertTrue(result)
        areas = self._clean_area(bot)
        self.assertEqual([a._additional_args["content"] for a in areas], ["7", "9"])

    def test_rejected_with_no_live_change_gives_up(self):
        bot = FakeBot(
            replies=[REJECTED, REJECTED],
            events={"RoomsEvent": _Event(rooms=[_Room(7, "Kitchen")])},
        )
        result = _drive(_service(), bot).clean_rooms(["kitchen"])
        self.assertFalse(result)
        self.assertEqual(len(self._clean_area(bot)), 1)

    def test_unknown_keys_never_reach_the_robot(self):
        bot = FakeBot()
        result = _drive(_service(), bot).clean_rooms(["garage"])
        self.assertFalse(result)
        self.assertEqual(bot.commands, [])


class SimpleCommandTests(unittest.TestCase):
    def test_clean_all_stop_dock_send_one_command_each(self):
        from deebot_client.commands.json.charge import Charge
        from deebot_client.commands.json.clean import Clean

        bot = FakeBot()
        service = _drive(_service(), bot)
        self.assertTrue(service.clean_all())
        self.assertTrue(service.stop())
        self.assertTrue(service.dock())
        self.assertIsInstance(bot.commands[0], Clean)
        self.assertIsInstance(bot.commands[1], Clean)
        self.assertIsInstance(bot.commands[2], Charge)

    def test_a_rejected_reply_is_a_failed_result(self):
        bot = FakeBot(replies=[REJECTED])
        result = _drive(_service(), bot).dock()
        self.assertFalse(result)
        self.assertEqual(result.message, "The vacuum didn't accept that command.")


class StatusTests(unittest.TestCase):
    def test_reads_battery_state_and_error(self):
        from deebot_client.models import State

        bot = FakeBot(
            events={
                "BatteryEvent": _Event(value=70),
                "StateEvent": _Event(state=State.DOCKED),
                "ErrorEvent": _Event(code=0, description="NoError"),
            }
        )
        status = _drive(_service(), bot).status()
        self.assertEqual(status, VacuumStatus(available=True, state="docked", battery=70))

    def test_no_battery_means_unreachable(self):
        service = _service(command_timeout_ms=5000)
        with patch.object(DeebotService, "_await_event", new=AsyncMock(return_value=None)):
            status = _drive(service, FakeBot()).status()
        self.assertFalse(status.available)

    def test_a_nonzero_error_code_wins_over_state(self):
        from deebot_client.models import State

        bot = FakeBot(
            events={
                "BatteryEvent": _Event(value=12),
                "StateEvent": _Event(state=State.CLEANING),
                "ErrorEvent": _Event(code=104, description="Wheel stuck"),
            }
        )
        status = _drive(_service(), bot).status()
        self.assertEqual(status.state, "error")
        self.assertEqual(status.error, "Wheel stuck")


class AuthFirstTests(unittest.TestCase):
    """Every command authenticates first; a stale token mid-command re-auths ONCE."""

    def _run_with_auth(self, service, fn, auth_results):
        auth = AsyncMock(side_effect=auth_results)
        bot = FakeBot()
        with patch.object(deebot_session, "build_authenticator", new=AsyncMock(return_value=Mock(_credentials=None))), \
             patch.object(deebot_session, "authenticate", new=auth), \
             patch.object(DeebotService, "_device", new=AsyncMock(return_value=bot)):
            result = asyncio.run(service._with_auth(Mock(), fn))
        return result, auth, bot

    def test_auth_runs_before_the_command(self):
        calls = []

        async def fn(bot):
            calls.append("command")
            return DeebotCommandResult(True)

        result, auth, _ = self._run_with_auth(_service(), fn, [(True, "cached")])
        self.assertTrue(result)
        self.assertEqual(auth.await_count, 1)
        self.assertEqual(calls, ["command"])

    def test_auth_failure_stops_the_command(self):
        async def fn(bot):
            raise AssertionError("must not run")

        result, _, _ = self._run_with_auth(_service(), fn, [(False, "gmail_timeout")])
        self.assertFalse(result)
        self.assertIn("verification code", result.message)

    def test_stale_token_mid_command_re_auths_once_and_retries(self):
        from deebot_client.exceptions import DeviceVerificationRequiredError

        attempts = []

        async def fn(bot):
            attempts.append(1)
            if len(attempts) == 1:
                raise DeviceVerificationRequiredError("1013")
            return DeebotCommandResult(True)

        result, auth, _ = self._run_with_auth(
            _service(), fn, [(True, "cached"), (True, "verified_via_gmail")]
        )
        self.assertTrue(result)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(auth.await_count, 2)

    def test_a_second_stale_token_is_not_retried_again(self):
        from deebot_client.exceptions import DeviceVerificationRequiredError

        async def fn(bot):
            raise DeviceVerificationRequiredError("1013")

        with self.assertRaises(DeviceVerificationRequiredError):
            self._run_with_auth(_service(), fn, [(True, "cached"), (True, "cached")])

    def test_api_errors_become_unreachable_results(self):
        from deebot_client.exceptions import ApiError

        async def fn(bot):
            raise ApiError("boom")

        result, _, bot = self._run_with_auth(_service(), fn, [(True, "cached")])
        self.assertFalse(result)
        self.assertEqual(result.message, "I couldn't reach the vacuum just now.")
        self.assertTrue(bot.torn_down)


class WorkerTests(unittest.TestCase):
    def test_a_hung_command_times_out_as_unreachable(self):
        service = _service(command_timeout_ms=5000)
        service.command_timeout_s = 0.2

        async def hang():
            await asyncio.sleep(2)

        result = service._run(hang)
        self.assertFalse(result)
        self.assertEqual(result.message, "I couldn't reach the vacuum just now.")

    def test_an_exception_never_escapes(self):
        async def boom():
            raise RuntimeError("x")

        self.assertFalse(_service()._run(boom))


if __name__ == "__main__":
    unittest.main()
