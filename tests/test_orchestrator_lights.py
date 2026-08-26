import unittest
from unittest.mock import Mock

from core.orchestrator import _dispatch_lights
from services.govee_service import GoveeCommandResult


def _svc(resolve=("attic", {"sku": "H6199", "device": "D1", "aliases": []}), power=None):
    service = Mock()
    service.enabled = True
    service.resolve_light.return_value = resolve
    # `is None`, not `or` — a failed GoveeCommandResult is falsy by design.
    ok = GoveeCommandResult(True, code=200)
    service.set_power.return_value = ok if power is None else power
    service.set_brightness.return_value = ok
    service.set_color.return_value = ok
    return service


class DispatchLightsTests(unittest.TestCase):
    def test_light_on_confirms_with_the_resolved_room_name(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_on", "light": "attic"}, service)

        self.assertEqual(response, "attic lights on.")
        service.set_power.assert_called_once_with("attic", on=True)

    def test_light_off_confirms_and_sends_off(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_off", "light": "attic"}, service)

        self.assertEqual(response, "attic lights off.")
        service.set_power.assert_called_once_with("attic", on=False)

    def test_missing_light_param_falls_through_to_the_service_default(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_on"}, service)

        self.assertEqual(response, "attic lights on.")
        service.resolve_light.assert_called_once_with("")

    def test_unknown_room_name_is_reported_back_to_the_llm(self):
        service = _svc(resolve=(None, None))

        response = _dispatch_lights({"action": "light_on", "light": "garage"}, service)

        self.assertEqual(response, "I don't have a light called garage saved.")
        service.set_power.assert_not_called()

    def test_no_lights_configured_at_all(self):
        service = _svc(resolve=(None, None))

        response = _dispatch_lights({"action": "light_on"}, service)

        self.assertEqual(response, "I don't have any lights saved yet.")
        service.set_power.assert_not_called()

    def test_disabled_service_reports_setup_missing(self):
        service = _svc()
        service.enabled = False

        response = _dispatch_lights({"action": "light_on", "light": "attic"}, service)

        self.assertEqual(response, "light control isn't set up right now")
        service.set_power.assert_not_called()

    def test_missing_service_reports_setup_missing(self):
        self.assertEqual(
            _dispatch_lights({"action": "light_on"}, None),
            "light control isn't set up right now",
        )

    def test_unknown_action_does_not_touch_the_device(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_disco", "light": "attic"}, service)

        self.assertEqual(response, "unknown action")
        service.set_power.assert_not_called()

    def test_failed_command_surfaces_the_service_message(self):
        service = _svc(power=GoveeCommandResult(False, "Govee is rate limiting me, give it a second.", code=429))

        response = _dispatch_lights({"action": "light_on", "light": "attic"}, service)

        self.assertEqual(response, "Govee is rate limiting me, give it a second.")

    def test_brightness_sets_and_confirms_the_percentage(self):
        service = _svc()

        response = _dispatch_lights(
            {"action": "light_brightness", "light": "attic", "brightness_percent": 30}, service
        )

        self.assertEqual(response, "attic lights at 30 percent.")
        service.set_brightness.assert_called_once_with("attic", 30)

    def test_brightness_out_of_range_is_clamped_not_rejected(self):
        service = _svc()

        response = _dispatch_lights(
            {"action": "light_brightness", "light": "attic", "brightness_percent": 400}, service
        )

        self.assertEqual(response, "attic lights at 100 percent.")
        service.set_brightness.assert_called_once_with("attic", 100)

    def test_brightness_without_a_value_asks_for_one(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_brightness", "light": "attic"}, service)

        self.assertEqual(response, "Tell me what brightness you want, from 1 to 100.")
        service.set_brightness.assert_not_called()

    def test_color_by_name_resolves_to_rgb(self):
        service = _svc()

        response = _dispatch_lights(
            {"action": "light_color", "light": "attic", "color": "red"}, service
        )

        self.assertEqual(response, "attic lights set to red.")
        service.set_color.assert_called_once_with("attic", (255, 0, 0))

    def test_color_by_hex_resolves(self):
        service = _svc()

        _dispatch_lights({"action": "light_color", "light": "attic", "color": "#FF7F00"}, service)

        service.set_color.assert_called_once_with("attic", (255, 127, 0))

    def test_unknown_colour_is_reported_without_touching_the_light(self):
        service = _svc()

        response = _dispatch_lights(
            {"action": "light_color", "light": "attic", "color": "burnt sienna"}, service
        )

        self.assertEqual(response, "I don't know the colour burnt sienna.")
        service.set_color.assert_not_called()

    def test_color_without_a_value_asks_for_one(self):
        service = _svc()

        response = _dispatch_lights({"action": "light_color", "light": "attic"}, service)

        self.assertEqual(response, "Tell me what colour you want.")
        service.set_color.assert_not_called()

    def test_failed_brightness_surfaces_the_service_message(self):
        service = _svc()
        service.set_brightness.return_value = GoveeCommandResult(False, "nope")

        response = _dispatch_lights(
            {"action": "light_brightness", "light": "attic", "brightness_percent": 50}, service
        )

        self.assertEqual(response, "nope")

    def test_failed_command_without_a_message_falls_back(self):
        service = _svc(power=GoveeCommandResult(False, ""))

        response = _dispatch_lights({"action": "light_on", "light": "attic"}, service)

        self.assertEqual(response, "I couldn't reach your lights just now.")


if __name__ == "__main__":
    unittest.main()
