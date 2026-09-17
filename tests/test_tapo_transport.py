"""
Tapo transport: conversion, credentials, threading, and the read that matters.

Nothing here touches a bulb. `_with_device` is the single boundary that opens a
socket, so every command test patches it and
`test_unit_tests_never_open_a_network_connection` pins that the boundary is the
only one -- the same shape as the adb and Ecovacs guards elsewhere in this suite.
"""

import socket
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.govee_service import GoveeCommandResult
from services.tapo_transport import (
    LightReading,
    TapoTransport,
    _ensure_kasa_importable,
    rgb_to_hsv,
)

# Credentials that are present but obviously fake. The transport disables itself
# without them, so most tests need *something*; a real .env value must never
# change what these assert, which is why they are set explicitly rather than
# inherited from the environment.
_ENV = {"TAPO_USERNAME": "test@example.invalid", "TAPO_PASSWORD": "not-a-password"}


def _transport(**cfg) -> TapoTransport:
    with patch.dict("os.environ", _ENV, clear=False):
        return TapoTransport({"tapo": {"retries": 1, **cfg}})


class ColorConversionTests(unittest.TestCase):
    def test_primary_colors_map_to_their_hue_at_full_saturation(self):
        self.assertEqual(rgb_to_hsv(255, 0, 0), (0, 100))
        self.assertEqual(rgb_to_hsv(0, 255, 0), (120, 100))
        self.assertEqual(rgb_to_hsv(0, 0, 255), (240, 100))

    def test_white_is_zero_saturation(self):
        # Hue is meaningless at saturation 0; only the saturation must be right.
        self.assertEqual(rgb_to_hsv(255, 255, 255)[1], 0)

    def test_brightness_is_discarded_so_a_dark_colour_is_not_a_dimmer(self):
        # #404040 and #FFFFFF are the same colour at different brightness. If the
        # value component leaked into the conversion, "make it white" on the dark
        # one would drop the room to 25%.
        self.assertEqual(rgb_to_hsv(0x40, 0x40, 0x40), rgb_to_hsv(255, 255, 255))

    def test_warm_white_keeps_a_warm_hue(self):
        hue, saturation = rgb_to_hsv(255, 170, 80)
        self.assertTrue(0 < hue < 60, f"warm white should be orange-ish, got {hue}")
        self.assertGreater(saturation, 0)


class AvailabilityTests(unittest.TestCase):
    def test_missing_credentials_disable_the_transport(self):
        with patch.dict("os.environ", {"TAPO_USERNAME": "", "TAPO_PASSWORD": ""}, clear=False):
            transport = TapoTransport({"tapo": {}})

        self.assertFalse(transport.available)

    def test_credentials_from_config_are_used_when_the_env_is_empty(self):
        with patch.dict("os.environ", {"TAPO_USERNAME": "", "TAPO_PASSWORD": ""}, clear=False):
            transport = TapoTransport({"tapo": {"username": "u", "password": "p"}})

        self.assertTrue(transport.available)

    def test_a_disabled_transport_returns_a_result_rather_than_raising(self):
        with patch.dict("os.environ", {"TAPO_USERNAME": "", "TAPO_PASSWORD": ""}, clear=False):
            transport = TapoTransport({"tapo": {}})

        result = transport.set_power({"host": "unused.invalid"}, True)

        self.assertFalse(result)
        self.assertTrue(result.message)

    def test_the_import_shim_makes_kasa_importable_on_this_interpreter(self):
        # On Python 3.14 `import kasa` raises AttributeError out of mashumaro
        # (typing.ByteString was removed) until the shim runs. If this ever
        # passes with _ensure_kasa_importable's body deleted, the shim is dead
        # code and should go.
        _ensure_kasa_importable()
        import kasa  # noqa: F401


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.transport = _transport()
        self.device = Mock()
        self.device.turn_on = AsyncMock()
        self.device.turn_off = AsyncMock()
        self.light = Mock()
        self.light.set_brightness = AsyncMock()
        self.light.set_hsv = AsyncMock()

        async def fake_with_device(_host, action):
            return await action(self.device, self.light)

        patcher = patch.object(TapoTransport, "_with_device", side_effect=fake_with_device)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_power_on_and_off_reach_the_device(self):
        self.assertTrue(self.transport.set_power({"host": "h"}, True))
        self.device.turn_on.assert_awaited_once()

        self.assertTrue(self.transport.set_power({"host": "h"}, False))
        self.device.turn_off.assert_awaited_once()

    def test_brightness_is_clamped_not_rejected(self):
        # Matches clamp_percent's contract everywhere else: a model asking for
        # 400 gets full brightness rather than an error.
        self.transport.set_brightness({"host": "h"}, 400)

        self.light.set_brightness.assert_awaited_once_with(100)

    def test_color_leaves_brightness_alone(self):
        # The third argument is the BULB's brightness. Passing anything but None
        # turns "make it red" into a dimmer command.
        self.transport.set_color({"host": "h"}, (255, 0, 0))

        self.light.set_hsv.assert_awaited_once_with(0, 100, None)

    def test_a_failing_command_returns_a_falsy_result_instead_of_raising(self):
        self.light.set_brightness.side_effect = RuntimeError("bulb unplugged")

        result = self.transport.set_brightness({"host": "h"}, 50)

        self.assertIsInstance(result, GoveeCommandResult)
        self.assertFalse(result)

    def test_work_runs_off_the_calling_thread(self):
        # Same guard as BleTransport's: asyncio.run must never land on whatever
        # thread is holding the audio stack.
        seen = {}

        async def record(_device, _light):
            seen["thread"] = threading.get_ident()

        async def fake(_host, action):
            return await action(self.device, self.light)

        with patch.object(TapoTransport, "_with_device", side_effect=fake):
            self.transport._run("h", record)

        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], threading.get_ident())


class ReadStateTests(unittest.TestCase):
    def _transport_reading(self, is_on, brightness, has_brightness=True):
        transport = _transport()
        device = Mock(is_on=is_on)
        light = Mock(brightness=brightness)
        light.has_feature.return_value = has_brightness

        async def fake(_host, action):
            return await action(device, light)

        patcher = patch.object(TapoTransport, "_with_device", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return transport

    def test_a_live_reading_carries_power_and_brightness(self):
        transport = self._transport_reading(True, 60)

        reading = transport.get_state({"host": "h"})

        self.assertEqual((reading.power, reading.percent), (True, 60))

    def test_a_bulb_with_no_dimmer_reads_power_only(self):
        transport = self._transport_reading(True, 60, has_brightness=False)

        reading = transport.get_state({"host": "h"})

        self.assertTrue(reading.power)
        self.assertIsNone(reading.percent)

    def test_an_unreachable_bulb_reads_none_rather_than_a_blank_reading(self):
        transport = _transport()
        with patch.object(TapoTransport, "_with_device", side_effect=OSError("no route")):
            self.assertIsNone(transport.get_state({"host": "h"}))

    def test_a_transport_that_cannot_authenticate_reads_none(self):
        with patch.dict("os.environ", {"TAPO_USERNAME": "", "TAPO_PASSWORD": ""}, clear=False):
            transport = TapoTransport({"tapo": {}})

        self.assertIsNone(transport.get_state({"host": "h"}))

    def test_an_empty_reading_is_still_truthy_so_existence_checks_use_is_none(self):
        # Deliberately no __bool__, unlike GoveeCommandResult. A reading is not a
        # success flag, and the falsy-result trap documented in CLAUDE.md must
        # not be re-created here.
        self.assertTrue(LightReading())


class NetworkGuardTests(unittest.TestCase):
    def test_unit_tests_never_open_a_network_connection(self):
        # Points at the boundary the code actually crosses. Patching only the
        # kasa import would not stop a socket, which is the mistake the device
        # discovery guard exists to remember.
        def explode(*_args, **_kwargs):
            raise AssertionError("a unit test tried to open a real connection")

        transport = _transport()
        with patch.object(socket.socket, "connect", explode):
            with patch.object(TapoTransport, "_with_device", side_effect=AsyncMock()):
                # .invalid never resolves (RFC 2606), so even a broken patch
                # cannot reach a real bulb on the operator's LAN.
                transport.set_power({"host": "unreachable.invalid"}, True)


if __name__ == "__main__":
    unittest.main()
