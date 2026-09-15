"""
control_vacuum dispatch: the spoken strings, the status-first guard, and that
the dispatcher never sends a clean into a robot it cannot see or that is
already busy. The service is a bare Mock, same as test_orchestrator_lights.
"""

import unittest
from unittest.mock import Mock

from core.orchestrator import _dispatch_vacuum
from services.deebot_service import DeebotCommandResult, VacuumStatus

ROOMS = {
    "kitchen": {"id": 7, "aliases": ["the kitchen"]},
    "bedroom": {"id": 5, "aliases": ["the bedroom"]},
}


def _svc(state="docked", battery=70, available=True, result=None):
    service = Mock()
    service.enabled = True
    service.rooms = ROOMS
    service.status.return_value = VacuumStatus(available=available, state=state, battery=battery)

    def resolve(hint):
        hint = hint.lower().replace("the ", "").strip()
        return (hint, ROOMS[hint]) if hint in ROOMS else (None, None)

    service.resolve_room.side_effect = resolve
    ok = DeebotCommandResult(True) if result is None else result
    service.clean_all.return_value = ok
    service.clean_rooms.return_value = ok
    service.stop.return_value = ok
    service.dock.return_value = ok
    return service


class SetupGateTests(unittest.TestCase):
    def test_missing_service(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, None),
            "vacuum control isn't set up right now",
        )

    def test_disabled_service_sends_nothing(self):
        service = _svc()
        service.enabled = False
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "vacuum control isn't set up right now",
        )
        service.clean_all.assert_not_called()

    def test_unknown_action(self):
        self.assertEqual(_dispatch_vacuum({"action": "vacuum_dance"}, _svc()), "unknown action")


class StatusTests(unittest.TestCase):
    def test_docked_with_battery(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, _svc()),
            "The vacuum is docked at 70 percent.",
        )

    def test_cleaning(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, _svc(state="cleaning", battery=43)),
            "The vacuum is cleaning, battery 43 percent.",
        )

    def test_returning_is_spoken_as_heading_home(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, _svc(state="returning")),
            "The vacuum is heading home, battery 70 percent.",
        )

    def test_error_carries_the_description(self):
        service = _svc()
        service.status.return_value = VacuumStatus(
            available=True, state="error", battery=12, error="Wheel stuck"
        )
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, service),
            "The vacuum has an error: Wheel stuck.",
        )

    def test_unreachable(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, _svc(available=False)),
            "I couldn't reach the vacuum just now.",
        )

    def test_unknown_battery_is_not_mentioned(self):
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_status"}, _svc(battery=None)),
            "The vacuum is docked.",
        )


class CleanAllTests(unittest.TestCase):
    def test_starts_from_dock(self):
        service = _svc()
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "Cleaning the whole house.",
        )
        service.clean_all.assert_called_once_with()

    def test_refuses_when_already_cleaning(self):
        service = _svc(state="cleaning")
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "The vacuum's already cleaning.",
        )
        service.clean_all.assert_not_called()

    def test_never_sends_into_an_unreachable_robot(self):
        service = _svc(available=False)
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "I couldn't reach the vacuum just now.",
        )
        service.clean_all.assert_not_called()

    def test_failed_command_surfaces_the_service_message(self):
        service = _svc(result=DeebotCommandResult(False, "The vacuum didn't accept that command."))
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "The vacuum didn't accept that command.",
        )

    def test_failed_command_with_no_message_falls_back(self):
        service = _svc(result=DeebotCommandResult(False))
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_all"}, service),
            "I couldn't reach the vacuum just now.",
        )


class CleanRoomsTests(unittest.TestCase):
    def test_one_room(self):
        service = _svc()
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_rooms", "rooms": ["the kitchen"]}, service),
            "Cleaning the kitchen.",
        )
        service.clean_rooms.assert_called_once_with(["kitchen"])

    def test_two_rooms_in_one_call(self):
        service = _svc()
        self.assertEqual(
            _dispatch_vacuum(
                {"action": "vacuum_clean_rooms", "rooms": ["kitchen", "bedroom"]}, service
            ),
            "Cleaning the kitchen and the bedroom.",
        )
        service.clean_rooms.assert_called_once_with(["kitchen", "bedroom"])

    def test_a_bare_string_is_accepted(self):
        service = _svc()
        _dispatch_vacuum({"action": "vacuum_clean_rooms", "rooms": "bedroom"}, service)
        service.clean_rooms.assert_called_once_with(["bedroom"])

    def test_duplicates_collapse(self):
        service = _svc()
        _dispatch_vacuum(
            {"action": "vacuum_clean_rooms", "rooms": ["kitchen", "the kitchen"]}, service
        )
        service.clean_rooms.assert_called_once_with(["kitchen"])

    def test_no_rooms_asks(self):
        service = _svc()
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_rooms", "rooms": []}, service),
            "Tell me which rooms to clean.",
        )
        service.status.assert_not_called()

    def test_unknown_room_is_refused_before_any_status_read(self):
        service = _svc()
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_rooms", "rooms": ["garage"]}, service),
            "I don't have a room called garage on the vacuum's map.",
        )
        service.status.assert_not_called()
        service.clean_rooms.assert_not_called()

    def test_refuses_when_already_cleaning(self):
        service = _svc(state="cleaning")
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_clean_rooms", "rooms": ["kitchen"]}, service),
            "The vacuum's already cleaning.",
        )
        service.clean_rooms.assert_not_called()


class StopAndDockTests(unittest.TestCase):
    def test_stop(self):
        service = _svc(state="cleaning")
        self.assertEqual(_dispatch_vacuum({"action": "vacuum_stop"}, service), "Vacuum stopped.")
        service.stop.assert_called_once_with()

    def test_dock(self):
        service = _svc(state="cleaning")
        self.assertEqual(
            _dispatch_vacuum({"action": "vacuum_dock"}, service), "Sending the vacuum home."
        )
        service.dock.assert_called_once_with()

    def test_stop_and_dock_skip_the_status_guard(self):
        # Stopping a robot you cannot see is harmless and should never be gated.
        service = _svc(available=False)
        _dispatch_vacuum({"action": "vacuum_stop"}, service)
        _dispatch_vacuum({"action": "vacuum_dock"}, service)
        service.status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
