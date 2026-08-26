import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from services.govee_service import (
    BLE_POWER_OFF,
    BLE_POWER_ON,
    GoveeService,
    ble_brightness,
    ble_color,
    ble_packet,
    clamp_percent,
    resolve_color,
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _cloud_config(**overrides) -> dict:
    govee = {
        "enabled": True,
        "transport": "cloud",
        "api_key": "test-key",
        "base_url": "https://openapi.api.govee.com",
        "request_timeout_ms": 5000,
        "default_light": "attic",
        "lights": {
            "attic": {
                "sku": "H6199",
                "device": "AA:BB:CC:DD:EE:FF:00:11",
                "aliases": ["loft", "upstairs", "my lights"],
            },
            "bedroom": {"sku": "H6143", "device": "11:22:33:44:55:66:77:88"},
        },
    }
    govee.update(overrides)
    return {"govee": govee}


def _ble_config(**overrides) -> dict:
    govee = {
        "enabled": True,
        "transport": "ble",
        "default_light": "attic",
        "ble": {"scan_timeout_ms": 1000, "connect_timeout_ms": 1000, "retries": 2},
        "lights": {
            "attic": {
                "mac": "EE:2E:01:06:53:0E",
                "aliases": ["loft", "upstairs", "my lights"],
            },
            "bedroom": {"mac": "AA:BB:CC:DD:EE:FF"},
        },
    }
    govee.update(overrides)
    return {"govee": govee}


def _cloud_service(**overrides) -> GoveeService:
    """Blank the ambient GOVEE_API_KEY so a real key in .env cannot change assertions."""
    with patch.dict(os.environ, {"GOVEE_API_KEY": ""}):
        return GoveeService(_cloud_config(**overrides))


def _ble_service(**overrides) -> GoveeService:
    return GoveeService(_ble_config(**overrides))


class BlePacketTests(unittest.TestCase):
    def test_packet_is_20_bytes_with_xor_checksum(self):
        packet = ble_packet(0x33, 0x01, 0x01)
        self.assertEqual(len(packet), 20)
        checksum = 0
        for byte in packet[:19]:
            checksum ^= byte
        self.assertEqual(packet[19], checksum)

    def test_power_packets_match_the_documented_bytes(self):
        # Confirmed working against a real H617E.
        self.assertEqual(BLE_POWER_ON.hex(), "3301010000000000000000000000000000000033")
        self.assertEqual(BLE_POWER_OFF.hex(), "3301000000000000000000000000000000000032")


class ColorResolutionTests(unittest.TestCase):
    def test_basic_names(self):
        self.assertEqual(resolve_color("red"), (255, 0, 0))
        self.assertEqual(resolve_color("blue"), (0, 0, 255))
        self.assertEqual(resolve_color("green"), (0, 255, 0))

    def test_multi_word_and_fuzzy_names(self):
        self.assertEqual(resolve_color("warm white"), (255, 170, 80))
        self.assertEqual(resolve_color("warmwhite"), (255, 170, 80))
        self.assertEqual(resolve_color("  Warm White  "), (255, 170, 80))

    def test_hex_forms(self):
        for text in ("#FF7F00", "FF7F00", "0xff7f00", "#ff7f00"):
            self.assertEqual(resolve_color(text), (255, 127, 0), text)

    def test_unknown_colour_returns_none(self):
        self.assertIsNone(resolve_color("burnt sienna"))
        self.assertIsNone(resolve_color(""))
        self.assertIsNone(resolve_color(None))

    def test_hex_takes_priority_over_fuzzy_matching(self):
        self.assertEqual(resolve_color("00FF00"), (0, 255, 0))


class ClampPercentTests(unittest.TestCase):
    def test_clamps_into_range(self):
        self.assertEqual(clamp_percent(0), 1)
        self.assertEqual(clamp_percent(-20), 1)
        self.assertEqual(clamp_percent(150), 100)
        self.assertEqual(clamp_percent(42), 42)

    def test_non_numeric_falls_back_to_full(self):
        self.assertEqual(clamp_percent(None), 100)
        self.assertEqual(clamp_percent("loud"), 100)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(clamp_percent("30"), 30)


class BleCapabilityPacketTests(unittest.TestCase):
    def test_brightness_packet_uses_a_1_to_100_scale(self):
        self.assertEqual(ble_brightness(100)[:3].hex(), "330464")
        self.assertEqual(ble_brightness(1)[:3].hex(), "330401")
        # out of range is clamped, never wrapped into a bogus byte
        self.assertEqual(ble_brightness(255)[:3].hex(), "330464")

    def test_color_packet_carries_rgb_and_the_all_segments_marker(self):
        packet = ble_color(255, 127, 0)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[:4].hex(), "33051501")
        self.assertEqual((packet[4], packet[5], packet[6]), (255, 127, 0))
        # FF 7F at offsets 12-13 selects all segments; without it colour is ignored
        self.assertEqual((packet[12], packet[13]), (0xFF, 0x7F))


class GoveeTransportSelectionTests(unittest.TestCase):
    def test_ble_transport_is_selected_and_available(self):
        service = _ble_service()
        self.assertEqual(service.transport_name, "ble")
        self.assertTrue(service.enabled)

    def test_cloud_transport_is_selected(self):
        service = _cloud_service()
        self.assertEqual(service.transport_name, "cloud")
        self.assertTrue(service.enabled)

    def test_unknown_transport_disables_cleanly_without_raising(self):
        service = GoveeService({"govee": {"enabled": True, "transport": "carrier-pigeon"}})
        self.assertIsNone(service.transport)
        self.assertFalse(service.enabled)
        self.assertEqual(service.lights, {})

    def test_disabled_when_config_flag_is_off(self):
        self.assertFalse(_ble_service(enabled=False).enabled)

    def test_cloud_disabled_when_api_key_is_missing(self):
        self.assertFalse(_cloud_service(api_key=None).enabled)

    def test_missing_govee_config_block_is_tolerated(self):
        service = GoveeService({})
        self.assertFalse(service.enabled)

    def test_ble_lights_require_a_mac(self):
        service = _ble_service(lights={"attic": {"aliases": ["loft"]}, "hall": {"mac": "AA:BB"}})
        self.assertNotIn("attic", service.lights)
        self.assertIn("hall", service.lights)

    def test_cloud_lights_require_sku_and_device(self):
        service = _cloud_service(lights={"attic": {"sku": "H6199"}, "hall": {"sku": "H1", "device": "D1"}})
        self.assertNotIn("attic", service.lights)
        self.assertIn("hall", service.lights)


class GoveeResolveLightTests(unittest.TestCase):
    def setUp(self):
        self.service = _ble_service()

    def test_exact_key_resolves(self):
        key, light = self.service.resolve_light("attic")
        self.assertEqual(key, "attic")
        self.assertEqual(light["mac"], "EE:2E:01:06:53:0E")

    def test_alias_resolves(self):
        self.assertEqual(self.service.resolve_light("upstairs")[0], "attic")

    def test_empty_hint_falls_back_to_default_light(self):
        self.assertEqual(self.service.resolve_light("")[0], "attic")

    def test_empty_hint_uses_the_only_light_when_no_default_is_set(self):
        service = _ble_service(default_light=None, lights={"bedroom": {"mac": "AA:BB"}})
        self.assertEqual(service.resolve_light("")[0], "bedroom")

    def test_unknown_name_returns_none_pair(self):
        self.assertEqual(self.service.resolve_light("garage"), (None, None))


class BleSetPowerTests(unittest.TestCase):
    def setUp(self):
        self.service = _ble_service()

    def _patch_ble(self, client_cm):
        """Patch the bleak imports that BleTransport does lazily inside its methods."""
        module = MagicMock()
        module.BleakClient.return_value = client_cm
        return patch.dict("sys.modules", {"bleak": module}), module

    def test_set_power_on_writes_the_on_packet(self):
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        ctx, module = self._patch_ble(client)

        with ctx, patch.object(self.service.transport, "_find", new=AsyncMock(return_value="dev")):
            result = self.service.set_power("attic", on=True)

        self.assertTrue(result)
        args, kwargs = client.write_gatt_char.call_args
        self.assertEqual(args[1], BLE_POWER_ON)
        self.assertFalse(kwargs["response"])

    def test_set_power_off_writes_the_off_packet(self):
        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        ctx, _ = self._patch_ble(client)

        with ctx, patch.object(self.service.transport, "_find", new=AsyncMock(return_value="dev")):
            self.service.set_power("attic", on=False)

        self.assertEqual(client.write_gatt_char.call_args.args[1], BLE_POWER_OFF)

    def test_device_not_found_reports_a_spoken_message(self):
        module = MagicMock()
        with patch.dict("sys.modules", {"bleak": module}), \
             patch.object(self.service.transport, "_find", new=AsyncMock(return_value=None)):
            result = self.service.set_power("attic", on=True)

        self.assertFalse(result)
        self.assertEqual(result.message, "I couldn't reach your lights over Bluetooth.")

    def test_connect_failure_retries_then_reports(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(side_effect=OSError("device unreachable"))
        client.__aexit__ = AsyncMock(return_value=False)
        ctx, _ = self._patch_ble(client)
        find = AsyncMock(return_value="dev")

        with ctx, patch.object(self.service.transport, "_find", new=find):
            result = self.service.set_power("attic", on=True)

        self.assertFalse(result)
        # retries: 2 in the test config, and each failure forces a fresh scan
        self.assertEqual(find.await_count, 2)
        self.assertEqual(result.message, "I couldn't reach your lights over Bluetooth.")

    def test_unknown_light_key_fails_without_touching_bluetooth(self):
        with patch.object(self.service.transport, "_find", new=AsyncMock()) as find:
            result = self.service.set_power("garage", on=True)

        find.assert_not_called()
        self.assertFalse(result)
        self.assertEqual(result.message, "I don't have that light saved.")

    def test_ble_work_runs_off_the_calling_thread(self):
        """
        Regression guard: bleak's WinRT backend dies with "Thread is configured
        for Windows GUI but callbacks are not working" if driven from an STA
        thread, and the orchestrator's audio stack puts its thread into STA.
        All BLE work must therefore hop to the transport's own worker thread.
        """
        import threading

        calling_thread = threading.get_ident()
        seen = {}

        client = MagicMock()
        client.write_gatt_char = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        ctx, _ = self._patch_ble(client)

        async def fake_find(_mac):
            seen["thread"] = threading.get_ident()
            return "dev"

        with ctx, patch.object(self.service.transport, "_find", new=fake_find):
            result = self.service.set_power("attic", on=True)

        self.assertTrue(result)
        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], calling_thread)

    def test_unknown_light_returns_the_error_not_a_transport_call(self):
        """
        Regression guard: GoveeCommandResult defines __bool__, so a failed result
        is falsy. `return error if error else transport.set_power(...)` silently
        fell through to the transport with light=None. Must be `is not None`.
        """
        for call in (
            lambda s: s.set_power("garage", on=True),
            lambda s: s.set_brightness("garage", 50),
            lambda s: s.set_color("garage", (255, 0, 0)),
        ):
            with patch.object(self.service.transport, "_write") as write:
                result = call(self.service)
            write.assert_not_called()
            self.assertFalse(result)
            self.assertEqual(result.message, "I don't have that light saved.")

    def test_missing_bleak_disables_the_transport(self):
        with patch.dict("sys.modules", {"bleak": None}):
            service = _ble_service()
        self.assertFalse(service.transport.available)
        self.assertFalse(service.enabled)


class CloudSetPowerTests(unittest.TestCase):
    def setUp(self):
        self.service = _cloud_service()

    def test_set_power_on_builds_the_expected_request(self):
        with patch("services.govee_service.requests.post") as post:
            post.return_value = _Response({"code": 200, "msg": "success"})
            result = self.service.set_power("attic", on=True)

        self.assertTrue(result)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://openapi.api.govee.com/router/api/v1/device/control")
        self.assertEqual(kwargs["headers"]["Govee-API-Key"], "test-key")
        self.assertEqual(kwargs["timeout"], 5.0)
        payload = kwargs["json"]["payload"]
        self.assertEqual(payload["sku"], "H6199")
        self.assertEqual(
            payload["capability"],
            {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 1},
        )
        self.assertTrue(kwargs["json"]["requestId"])

    def test_set_power_off_sends_zero(self):
        with patch("services.govee_service.requests.post") as post:
            post.return_value = _Response({"code": 200, "msg": "success"})
            self.service.set_power("attic", on=False)
        self.assertEqual(post.call_args.kwargs["json"]["payload"]["capability"]["value"], 0)

    def test_rate_limit_returns_a_spoken_message(self):
        with patch("services.govee_service.requests.post") as post:
            post.return_value = _Response({"code": 429, "msg": "Too Many Requests"}, status_code=429)
            result = self.service.set_power("attic", on=True)
        self.assertFalse(result)
        self.assertEqual(result.code, 429)
        self.assertEqual(result.message, "Govee is rate limiting me, give it a second.")

    def test_bad_key_returns_a_spoken_message(self):
        with patch("services.govee_service.requests.post") as post:
            post.return_value = _Response({"code": 401, "msg": "Unauthorized"}, status_code=401)
            result = self.service.set_power("attic", on=True)
        self.assertEqual(result.message, "My Govee key isn't working.")

    def test_network_failure_is_caught_and_reported(self):
        with patch("services.govee_service.requests.post") as post:
            post.side_effect = requests.RequestException("boom")
            result = self.service.set_power("attic", on=True)
        self.assertFalse(result)
        self.assertEqual(result.message, "I couldn't reach Govee just now.")

    def test_body_code_error_inside_http_200_is_treated_as_failure(self):
        with patch("services.govee_service.requests.post") as post:
            post.return_value = _Response({"code": 404, "msg": "device not found"}, status_code=200)
            result = self.service.set_power("attic", on=True)
        self.assertEqual(result.message, "I can't find that light on your Govee account.")


class CloudListDevicesTests(unittest.TestCase):
    def test_list_devices_returns_the_data_array(self):
        service = _cloud_service()
        with patch("services.govee_service.requests.get") as get:
            get.return_value = _Response({"code": 200, "data": [{"sku": "H6199"}]})
            self.assertEqual(service.transport.list_devices(), [{"sku": "H6199"}])

    def test_list_devices_raises_on_body_level_error_inside_http_200(self):
        service = _cloud_service()
        with patch("services.govee_service.requests.get") as get:
            get.return_value = _Response({"code": 401, "message": "invalid key"}, status_code=200)
            with self.assertRaises(ValueError) as ctx:
                service.transport.list_devices()
        self.assertIn("401", str(ctx.exception))
        self.assertIn("invalid key", str(ctx.exception))

    def test_empty_data_on_genuine_success_is_not_an_error(self):
        """The real H617E symptom: valid key, real success, zero devices."""
        service = _cloud_service()
        with patch("services.govee_service.requests.get") as get:
            get.return_value = _Response({"code": 200, "message": "success", "data": []})
            self.assertEqual(service.transport.list_devices(), [])


if __name__ == "__main__":
    unittest.main()
