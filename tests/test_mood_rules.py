"""
Phase-1 moods for the live UI's halo: happy when a device action worked,
sheepish when it failed or a tool raised, nothing for a status read or a
question back.

Dispatchers report through `_report` into a context var rather than a new
parameter, so they stay module-level and service-injected. Every report sits
next to an existing success check; nothing here changes a spoken line.
"""

import contextvars
import unittest
from unittest.mock import Mock, patch

from core.event_bus import EventBus
from core.orchestrator import (
    _TOOL_OUTCOME,
    Orchestrator,
    _dispatch_lights,
    _dispatch_phone,
    _dispatch_tv,
    _dispatch_vacuum,
    _dispatch_whatsapp,
    _report,
)
from core.turn_log import TurnTimer
from services.deebot_service import DeebotCommandResult, VacuumStatus
from services.govee_service import GoveeCommandResult
from services.media_service import RoomStatus
from services.stremio_service import StremioPlayResult


def _outcome(fn, *args, **kwargs):
    """Run one dispatcher call in a fresh context; return what it reported."""

    def run():
        _TOOL_OUTCOME.set(None)
        fn(*args, **kwargs)
        return _TOOL_OUTCOME.get()

    return contextvars.copy_context().run(run)


def _lights(result=None):
    svc = Mock()
    svc.enabled = True
    svc.resolve_light.return_value = ("attic", {"mac": "x", "aliases": []})
    svc.set_power.return_value = result
    svc.set_brightness.return_value = result
    svc.set_color.return_value = result
    svc.can_read_state = False
    return svc


def _vacuum(result=None):
    svc = Mock()
    svc.enabled = True
    svc.status.return_value = VacuumStatus(available=True, state="docked", battery=90)
    for name in ("clean_all", "clean_rooms", "stop", "dock"):
        getattr(svc, name).return_value = result
    svc.resolve_room.return_value = ("kitchen", {"id": 1})
    return svc


def _stremio(result):
    svc = Mock()
    svc.play.return_value = result
    return svc


def _tv(action, stremio_svc, **params):
    return _dispatch_tv({"action": action, "title": "Fallout", **params},
                        Mock(), stremio_svc, None, {}, {})


class DispatcherReportTests(unittest.TestCase):
    def test_light_on_success_is_happy(self):
        self.assertIs(_outcome(_dispatch_lights, {"action": "light_on"},
                               _lights(GoveeCommandResult(True))), True)

    def test_light_on_failure_is_sheepish(self):
        self.assertIs(_outcome(_dispatch_lights, {"action": "light_on"},
                               _lights(GoveeCommandResult(False, "out of range"))), False)

    def test_brightness_and_colour_report_too(self):
        ok = _lights(GoveeCommandResult(True))
        self.assertIs(_outcome(_dispatch_lights, {"action": "light_brightness",
                                                  "brightness_percent": 40}, ok), True)
        self.assertIs(_outcome(_dispatch_lights, {"action": "light_color", "color": "red"}, ok), True)

    def test_vacuum_dock_success_is_happy(self):
        self.assertIs(_outcome(_dispatch_vacuum, {"action": "vacuum_dock"},
                               _vacuum(DeebotCommandResult(True))), True)

    def test_vacuum_dock_failure_is_sheepish(self):
        self.assertIs(_outcome(_dispatch_vacuum, {"action": "vacuum_dock"},
                               _vacuum(DeebotCommandResult(False, "timeout"))), False)

    def test_vacuum_clean_reports_after_the_status_guard(self):
        self.assertIs(_outcome(_dispatch_vacuum, {"action": "vacuum_clean_all"},
                               _vacuum(DeebotCommandResult(True))), True)
        busy = _vacuum(DeebotCommandResult(True))
        busy.status.return_value = VacuumStatus(available=True, state="cleaning")
        self.assertIsNone(_outcome(_dispatch_vacuum, {"action": "vacuum_clean_all"}, busy))

    def test_stremio_episode_playing_is_happy(self):
        result = StremioPlayResult(True, target_mode="episode")
        self.assertIs(_outcome(_tv, "stremio_play", _stremio(result)), True)
        self.assertIs(_outcome(_tv, "stremio_continue", _stremio(result)), True)

    def test_stremio_detail_page_reports_nothing(self):
        result = StremioPlayResult(True, target_mode="series_detail")
        self.assertIsNone(_outcome(_tv, "stremio_play", _stremio(result)))

    def test_stremio_requires_confirmation_reports_nothing(self):
        result = StremioPlayResult(False, message="Which source?", requires_confirmation=True)
        self.assertIsNone(_outcome(_tv, "stremio_play", _stremio(result)))

    def test_stremio_that_did_not_start_is_sheepish(self):
        result = StremioPlayResult(False, message="Hit OK on the remote.")
        self.assertIs(_outcome(_tv, "stremio_play", _stremio(result)), False)

    def test_whatsapp_read_back_reports_nothing(self):
        orch = _orch()
        orch.whatsapp_service = Mock()
        with patch("core.orchestrator._dispatch_whatsapp",
                                 return_value="Send it to Marta Zuka: I'm late?"):
            self.assertIsNone(_outcome(orch._guard_whatsapp_send,
                                       {"action": "whatsapp_send", "to": "marta", "message": "late"}))

    def test_whatsapp_sent_is_happy(self):
        orch = _orch()
        orch.whatsapp_service = Mock()
        with patch("core.orchestrator._dispatch_whatsapp", return_value="Sent to Marta."):
            self.assertIs(_outcome(orch._guard_whatsapp_send,
                                   {"action": "whatsapp_send", "to": "marta", "message": "late"}), True)

    def test_whatsapp_that_did_not_go_out_is_sheepish(self):
        orch = _orch()
        orch.whatsapp_service = Mock()
        line = "It's in WhatsApp but hasn't gone out yet, so I can't say it's sent."
        with patch("core.orchestrator._dispatch_whatsapp", return_value=line):
            self.assertIs(_outcome(orch._guard_whatsapp_send,
                                   {"action": "whatsapp_send", "to": "marta", "message": "late"}), False)

    def test_status_actions_report_nothing(self):
        phone = Mock()
        phone.status_line.return_value = "No call right now."
        phone.recent_calls.return_value = []

        whatsapp = Mock()
        whatsapp.resolve_contact.return_value = Mock(certain=True, candidates=[], key="Marta")
        whatsapp.unread.return_value = Mock(chats=[])
        whatsapp.find_unread_chat.return_value = None
        whatsapp.read_chat.return_value = Mock(messages=[])
        whatsapp.find_contacts.return_value = [("Marta", "+351 900 000 000")]

        media = Mock()
        media.room_status.return_value = RoomStatus(reachable=True, awake=True, tv_power="on")
        stremio = Mock()
        stremio.get_progress.return_value = {"type": "series", "title": "Fallout",
                                             "season": 2, "episode": 4}

        cases = [
            ("light_status", _dispatch_lights, ({"action": "light_status"},
                                                _lights(GoveeCommandResult(True)))),
            ("vacuum_status", _dispatch_vacuum, ({"action": "vacuum_status"}, _vacuum())),
            ("phone_status", _dispatch_phone, ({"action": "phone_status"}, phone)),
            ("phone_log", _dispatch_phone, ({"action": "phone_log"}, phone)),
            ("get_status", _dispatch_tv, ({"action": "get_status"}, media, None, None, {}, {})),
            ("stremio_get_progress", _dispatch_tv,
             ({"action": "stremio_get_progress", "title": "Fallout"}, media, stremio, None, {}, {})),
        ]
        for action in ("whatsapp_unread", "whatsapp_read", "whatsapp_find_contact"):
            cases.append((action, _dispatch_whatsapp, ({"action": action, "to": "marta"}, whatsapp)))
        for action, fn, args in cases:
            with self.subTest(action):
                self.assertIsNone(_outcome(fn, *args))

    def test_phone_reads_report_nothing(self):
        # Reading what is waiting on his phone is a status read: a worked read
        # is not a happy moment and a failed one is not his fault to apologise for.
        phone = Mock()
        phone.enabled = True
        for action in ("phone_missed", "phone_digest"):
            with self.subTest(action):
                self.assertIsNone(_outcome(_dispatch_phone, {"action": action}, phone))

    def test_bare_mock_result_is_never_a_success(self):
        self.assertIsNone(_outcome(_dispatch_lights, {"action": "light_on"}, _lights(Mock())))
        self.assertIsNone(_outcome(_dispatch_vacuum, {"action": "vacuum_stop"}, _vacuum(Mock())))
        self.assertIsNone(_outcome(_tv, "stremio_play", _stremio(Mock())))
        self.assertIsNone(_outcome(_dispatch_tv, {"action": "turn_on"}, Mock(), None, None, {}, {}))
        self.assertIsNone(_outcome(_report, Mock()))


def _orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch.bus = EventBus()
    orch.config = {}
    orch._activation_seq = 1
    return orch


def _moods(orch):
    return [(e["data"]["mood"], e["data"]["reason"]) for e in orch.bus.subscribe()[0]
            if e["kind"] == "mood"]


class OrchestratorMoodTests(unittest.TestCase):
    def test_raising_tool_is_sheepish(self):
        orch = _orch()
        orch._dispatch_tool = Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            orch._handle_tool_call("control_lights", {"action": "light_on"})
        self.assertEqual(_moods(orch), [("sheepish", "control_lights.light_on raised")])

    def test_turn_error_is_sheepish(self):
        orch = _orch()
        orch._turn_writer = None
        orch._run_activation = Mock(side_effect=RuntimeError("stt died"))
        with self.assertRaises(RuntimeError):
            orch._handle_activation(Mock(), pre_roll=[])
        self.assertIn(("sheepish", "turn error"), _moods(orch))

    def test_light_tool_moods_through_handle_tool_call(self):
        orch = _orch()
        orch.light_shadow = None
        orch.govee_service = _lights(GoveeCommandResult(True))
        orch._handle_tool_call("control_lights", {"action": "light_on"})
        self.assertEqual(_moods(orch), [("happy", "control_lights.light_on worked")])

    def test_status_tool_publishes_no_mood(self):
        orch = _orch()
        orch.light_shadow = None
        orch.govee_service = _lights(GoveeCommandResult(True))
        orch._handle_tool_call("control_lights", {"action": "light_status"})
        self.assertEqual(_moods(orch), [])

    def test_outcome_does_not_leak_between_tool_calls(self):
        orch = _orch()
        orch.light_shadow = None
        orch.govee_service = _lights(GoveeCommandResult(True))
        orch._turn = TurnTimer(None)
        orch._handle_tool_call("control_lights", {"action": "light_on"})
        orch._activation_seq = 2  # a new turn
        # Even a stale value left in the context (a dispatcher called outside
        # _handle_tool_call, as other tests do) must not reach the next call.
        stale = _TOOL_OUTCOME.set(True)
        try:
            orch._handle_tool_call("control_lights", {"action": "light_status"})
            self.assertIs(_TOOL_OUTCOME.get(), True, "the caller's value is restored")
        finally:
            _TOOL_OUTCOME.reset(stale)
        self.assertEqual(_moods(orch), [("happy", "control_lights.light_on worked")])
        self.assertEqual([t.get("ok") for t in orch._turn.finish("reply")["tools"]], [True, None])

    def test_failure_in_a_turn_beats_a_later_success(self):
        orch = _orch()
        orch.light_shadow = None
        orch.govee_service = _lights(GoveeCommandResult(False, "unreachable"))
        orch._handle_tool_call("control_lights", {"action": "light_on"})
        orch.govee_service = _lights(GoveeCommandResult(True))
        orch._handle_tool_call("control_lights", {"action": "light_off"})
        self.assertEqual(_moods(orch), [("sheepish", "control_lights.light_on failed")])
        # The next turn starts clean.
        orch._activation_seq = 2
        orch._handle_tool_call("control_lights", {"action": "light_off"})
        self.assertEqual(_moods(orch)[-1], ("happy", "control_lights.light_off worked"))

    def test_turn_record_carries_tool_ok(self):
        orch = _orch()
        orch.light_shadow = None
        orch._turn = TurnTimer(None)
        orch.govee_service = _lights(GoveeCommandResult(True))
        orch._handle_tool_call("control_lights", {"action": "light_on"})
        orch._handle_tool_call("control_lights", {"action": "light_status"})
        orch.govee_service = _lights(GoveeCommandResult(False))
        orch._handle_tool_call("control_lights", {"action": "light_off"})
        tools = orch._turn.finish("reply")["tools"]
        self.assertEqual([t.get("ok") for t in tools], [True, None, False])
        self.assertNotIn("ok", tools[1])


if __name__ == "__main__":
    unittest.main()
