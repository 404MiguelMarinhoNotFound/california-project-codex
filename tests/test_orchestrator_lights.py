import unittest
from unittest.mock import Mock

from core.orchestrator import _dispatch_lights
from services.govee_service import GoveeCommandResult
from services.light_shadow import LightShadow
from services.tapo_transport import LightReading


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




class LightStatusTests(unittest.TestCase):
    """
    The Govee characteristic is Write Without Response and there is no notify
    beside it, so the strip's real state is not obtainable by any means. The
    only honest answer is what she last SENT, said as what she last sent.
    """

    def test_it_reports_the_last_command_and_says_it_is_memory(self):
        shadow = LightShadow()
        shadow.record_power("attic", True)
        shadow.record_brightness("attic", 40)
        shadow.record_color("attic", "warm white")

        reply = _dispatch_lights({"action": "light_status"}, _svc(), shadow)

        self.assertIn("on", reply)
        self.assertIn("40 percent", reply)
        self.assertIn("warm white", reply)
        self.assertIn("memory", reply)

    def test_an_untouched_light_admits_it_does_not_know(self):
        reply = _dispatch_lights({"action": "light_status"}, _svc(), LightShadow())
        self.assertIn("don't know", reply)

    def test_it_never_asks_the_service_for_state(self):
        """
        Guards the fixture, not the feature. _svc() is a bare Mock, so a
        GoveeService.get_state() would auto-stub TRUTHY and this whole class
        would pass while reading nothing. Resolving the light is the only call
        that may reach the service.
        """
        service = _svc()
        _dispatch_lights({"action": "light_status"}, service, LightShadow())

        service.resolve_light.assert_called_once()
        service.set_power.assert_not_called()
        service.set_brightness.assert_not_called()
        service.set_color.assert_not_called()

    def test_an_unknown_room_still_says_so(self):
        reply = _dispatch_lights(
            {"action": "light_status", "light": "garage"},
            _svc(resolve=(None, None)),
            LightShadow(),
        )
        self.assertIn("garage", reply)

    def test_dispatch_without_a_store_still_answers(self):
        # The 18 older _dispatch_lights call sites pass two positionals.
        reply = _dispatch_lights({"action": "light_status"}, _svc())
        self.assertIn("don't know", reply)


class LightMemoryRecordingTests(unittest.TestCase):
    def test_a_successful_command_is_remembered(self):
        shadow = LightShadow()
        _dispatch_lights({"action": "light_on"}, _svc(), shadow)
        self.assertIs(shadow.remembered("attic").power, True)

    def test_a_failed_command_is_not_remembered(self):
        """
        `if result:` is a SUCCESS check. GoveeCommandResult defines __bool__, so
        a failed write is falsy -- and a light that never got the command must
        not be reported as if it had.
        """
        failed = GoveeCommandResult(False, "I couldn't reach your lights.")
        shadow = LightShadow()
        _dispatch_lights({"action": "light_on"}, _svc(power=failed), shadow)
        self.assertIsNone(shadow.remembered("attic"))

    def test_brightness_is_remembered_clamped_not_as_asked(self):
        shadow = LightShadow()
        _dispatch_lights(
            {"action": "light_brightness", "brightness_percent": 400}, _svc(), shadow
        )
        self.assertEqual(shadow.remembered("attic").percent, 100)

    def test_colour_is_remembered_as_the_spoken_word_not_rgb(self):
        # Nothing in this project maps rgb back to a name, so the word Master
        # Miguel said is the only thing worth reading back to him.
        shadow = LightShadow()
        _dispatch_lights({"action": "light_color", "color": "warm white"}, _svc(), shadow)
        self.assertEqual(shadow.remembered("attic").color_word, "warm white")

    def test_brightness_does_not_imply_the_light_is_on(self):
        # The strip accepts brightness while it is off. Inferring power from it
        # would be a guess wearing the clothes of a fact.
        shadow = LightShadow()
        _dispatch_lights(
            {"action": "light_brightness", "brightness_percent": 50}, _svc(), shadow
        )
        self.assertIsNone(shadow.remembered("attic").power)


class ReadableLightStatusTests(unittest.TestCase):
    """A transport that can be read (Tapo) answers with a fact, not the hedge."""

    @staticmethod
    def _readable(reading):
        service = _svc()
        service.can_read_state = True
        service.get_state.return_value = reading
        return service

    def test_a_live_reading_is_stated_without_the_memory_hedge(self):
        service = self._readable(LightReading(power=True, percent=60))

        reply = _dispatch_lights({"action": "light_status"}, service, LightShadow())

        self.assertEqual(reply, "The attic light is on at 60 percent.")
        self.assertNotIn("memory", reply)

    def test_brightness_is_not_quoted_on_a_light_that_is_off(self):
        # The bulb keeps reporting its last level while switched off, and
        # "off at 60 percent" invites an argument.
        service = self._readable(LightReading(power=False, percent=60))

        reply = _dispatch_lights({"action": "light_status"}, service, LightShadow())

        self.assertEqual(reply, "The attic light is off.")

    def test_a_failed_read_falls_back_to_memory_rather_than_silence(self):
        service = self._readable(None)
        shadow = LightShadow()
        shadow.record_power("attic", True)

        reply = _dispatch_lights({"action": "light_status"}, service, shadow)

        self.assertIn("memory, not a reading", reply)

    def test_a_reading_with_no_power_is_not_a_reading(self):
        # power is None means the bulb did not answer. Narrating that as fact
        # would be the exact TV-power bug CLAUDE.md documents.
        service = self._readable(LightReading(power=None, percent=60))
        shadow = LightShadow()
        shadow.record_power("attic", True)

        reply = _dispatch_lights({"action": "light_status"}, service, shadow)

        self.assertIn("memory, not a reading", reply)

    def test_a_bare_mock_service_never_produces_a_reading_line(self):
        # The trap this whole `is True` check exists for: every attribute of a
        # Mock is truthy, so `if svc.can_read_state:` would claim a live reading
        # off a service that reads nothing at all.
        service = _svc()  # no can_read_state set, so Mock auto-stubs it

        reply = _dispatch_lights({"action": "light_status"}, service, LightShadow())

        service.get_state.assert_not_called()
        self.assertIn("don't know", reply)


if __name__ == "__main__":
    unittest.main()
