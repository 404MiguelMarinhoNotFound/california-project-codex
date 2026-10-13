"""
The live UI's tool satellites: what each tool call tells the page.

Three things are new on the wire and each is pinned here:

- a short `target` label on `turn_tool_start` / `turn_tool` ("attic", "to
  Marta"), never a message body, a call brief or a phone number, and gone
  entirely with `logging.include_transcripts: false`;
- an `awaiting` outcome, separate from `ok`, for a tool that asked him
  something (a read-back, "which one?") -- it moves no mood;
- web search, which runs at Anthropic inside the stream, reported through
  `Orchestrator._on_server_tool` onto the same turn.

No spoken line changes anywhere in this; several tests assert the line too.
"""

import contextvars
import unittest
from unittest.mock import Mock

from core.event_bus import EventBus
from core.orchestrator import (
    _TOOL_AWAITING,
    _TOOL_OUTCOME,
    Orchestrator,
    _dispatch_phone,
    _dispatch_tv,
    _dispatch_whatsapp,
    _tool_target,
)
from core.turn_log import NULL_TURN, TurnLogWriter, TurnTimer
from services.govee_service import GoveeCommandResult
from services.phone_service import PhoneCommandResult
from services.stremio_service import StremioPlayResult
from services.tv_volume import VolumeResult
from services.whatsapp_service import WhatsAppCommandResult


def _flags(fn, *args, **kwargs):
    """Run one dispatcher call in a fresh context: (line, ok, awaiting)."""

    def run():
        _TOOL_OUTCOME.set(None)
        _TOOL_AWAITING.set(False)
        line = fn(*args, **kwargs)
        return line, _TOOL_OUTCOME.get(), _TOOL_AWAITING.get()

    return contextvars.copy_context().run(run)


def _events(bus, kind=None):
    backlog = bus.subscribe()[0]
    return [e["data"] for e in backlog if kind is None or e["kind"] == kind]


def _orch(include_text=True):
    orch = Orchestrator.__new__(Orchestrator)
    orch.bus = EventBus()
    orch.config = {}
    orch._activation_seq = 1
    orch._turn = TurnTimer(_writer(include_text), bus=orch.bus)
    return orch


def _writer(include_text):
    writer = TurnLogWriter({"logging": {"enabled": False, "include_transcripts": include_text}})
    return writer


class TargetLabelTests(unittest.TestCase):
    def test_each_tool_names_what_it_is_pointed_at(self):
        cases = [
            ("control_tv", {"action": "stremio_play", "title": "Fallout", "season": 2, "episode": 9}, "Fallout S2E9"),
            ("control_tv", {"action": "stremio_continue", "title": "Severance"}, "Severance"),
            ("control_tv", {"action": "youtube_search", "query": "lofi beats"}, "lofi beats"),
            ("control_tv", {"action": "youtube_playlist", "playlist_name": "samba"}, "samba"),
            ("control_tv", {"action": "volume_set", "volume_percent": 30}, "volume 30"),
            ("control_tv", {"action": "switch_hdmi", "hdmi_port": 2}, "HDMI 2"),
            ("control_tv", {"action": "turn_on"}, None),
            ("control_tv", {"action": "get_status"}, None),
            ("control_lights", {"action": "light_on", "light": "attic"}, "attic"),
            ("control_lights", {"action": "light_on"}, "default light"),
            ("control_lights", {"action": "light_color", "light": "sala", "color": "warm white"}, "sala, warm white"),
            ("control_lights", {"action": "light_brightness", "light": "sala", "brightness_percent": 40}, "sala, 40%"),
            ("control_vacuum", {"action": "vacuum_clean_rooms", "rooms": ["kitchen", "hall"]}, "kitchen, hall"),
            ("control_vacuum", {"action": "vacuum_clean_all"}, "everywhere"),
            ("control_vacuum", {"action": "vacuum_status"}, None),
            ("control_whatsapp", {"action": "whatsapp_send", "to": "Marta", "message": "I'm late"}, "to Marta"),
            ("control_whatsapp", {"action": "whatsapp_read", "to": "Rui"}, "Rui's chat"),
            ("control_whatsapp", {"action": "whatsapp_unread"}, None),
            ("control_phone", {"action": "phone_call", "to": "Taberna da Praia", "goal": "a table for two"}, "Taberna da Praia"),
            ("control_phone", {"action": "phone_find"}, "your phone"),
            ("web_search", {"query": "benfica result"}, "benfica result"),
        ]
        for tool, params, expected in cases:
            with self.subTest(tool=tool, action=params.get("action")):
                self.assertEqual(_tool_target(tool, params), expected)

    def test_never_the_message_or_a_number(self):
        send = {"action": "whatsapp_send", "to": "+351 912 345 678", "message": "the code is 4471"}
        self.assertEqual(_tool_target("control_whatsapp", send), "to a number")
        call = {"action": "phone_call", "to": "912345678", "number": "912345678",
                "goal": "tell them the door code", "details": "4471"}
        self.assertEqual(_tool_target("control_phone", call), "a number")
        read = {"action": "whatsapp_read", "to": "00351912345678"}
        self.assertEqual(_tool_target("control_whatsapp", read), "a chat")

    def test_junk_input_never_raises(self):
        for junk in (None, "string", 5, [], {"action": None}, {"action": "stremio_play", "title": object()}):
            with self.subTest(junk=junk):
                self.assertIsNone(_tool_target("control_tv", junk))
        self.assertIsNone(_tool_target("control_fridge", {"action": "open"}))


class TurnTimerTargetTests(unittest.TestCase):
    def test_the_label_is_published_and_recorded(self):
        bus = EventBus()
        turn = TurnTimer(_writer(True), bus=bus)
        start = turn.tool_started("control_lights", "light_on", target="attic")
        turn.tool("control_lights", "light_on", start, "Attic on.", ok=True, target="attic")
        self.assertEqual(_events(bus, "turn_tool_start")[0]["target"], "attic")
        self.assertEqual(_events(bus, "turn_tool")[0]["target"], "attic")
        self.assertEqual(turn.finish("reply")["tools"][0]["target"], "attic")

    def test_no_transcripts_means_no_label_anywhere(self):
        bus = EventBus()
        turn = TurnTimer(_writer(False), bus=bus)
        start = turn.tool_started("control_whatsapp", "whatsapp_send", target="to Marta")
        turn.tool("control_whatsapp", "whatsapp_send", start, "Sent to Marta.", ok=True, target="to Marta")
        record = turn.finish("reply")
        for event in bus.subscribe()[0]:
            self.assertNotIn("Marta", repr(event["data"].get("target")))
            self.assertNotIn("target", event["data"])
        self.assertNotIn("target", record["tools"][0])

    def test_no_transcripts_means_no_tool_result_either(self):
        """A result can quote the message or the number ("I've got Marta at
        +351..."), so it is text, and goes with the transcripts."""
        bus = EventBus()
        turn = TurnTimer(_writer(False), bus=bus)
        turn.tool("control_whatsapp", "whatsapp_send", 0, "I've got Marta at +351900000000.", awaiting=True)
        record = turn.finish("reply")
        self.assertNotIn("result", record["tools"][0])
        self.assertNotIn("+351", repr(bus.subscribe()[0]))
        self.assertIs(record["tools"][0]["awaiting"], True)

    def test_a_long_label_is_one_short_line(self):
        bus = EventBus()
        turn = TurnTimer(None, bus=bus)
        turn.tool_started("control_tv", "youtube_search", target="a\nvery " * 30)
        label = _events(bus, "turn_tool_start")[0]["target"]
        self.assertLessEqual(len(label), 40)
        self.assertNotIn("\n", label)
        self.assertTrue(label.endswith("…"))

    def test_a_second_start_with_the_same_start_ms_updates_the_label(self):
        bus = EventBus()
        turn = TurnTimer(None, bus=bus)
        start = turn.tool_started("web_search", "search")
        again = turn.tool_started("web_search", "search", target="benfica", start_ms=start)
        self.assertEqual(again, start)
        starts = _events(bus, "turn_tool_start")
        self.assertEqual([s["start_ms"] for s in starts], [start, start])
        self.assertNotIn("target", starts[0])
        self.assertEqual(starts[1]["target"], "benfica")

    def test_awaiting_is_its_own_outcome(self):
        turn = TurnTimer(None)
        turn.tool("control_whatsapp", "whatsapp_send", 0, "read back", ok=None, awaiting=True)
        turn.tool("control_lights", "light_on", 0, "ok", ok=True, awaiting=True)  # ok wins
        turn.tool("control_lights", "light_status", 0, "on", ok=None)
        tools = turn.finish("reply")["tools"]
        self.assertIs(tools[0]["awaiting"], True)
        self.assertNotIn("ok", tools[0])
        self.assertNotIn("awaiting", tools[1])
        self.assertNotIn("awaiting", tools[2])

    def test_a_broken_bus_never_reaches_the_caller(self):
        bus = Mock()
        bus.publish.side_effect = RuntimeError("bus down")
        turn = TurnTimer(None, bus=bus)
        start = turn.tool_started("control_tv", "turn_on", target="x")
        turn.tool("control_tv", "turn_on", start, "on", ok=True)
        self.assertEqual(turn.finish("reply")["tools"][0]["ok"], True)

    def test_the_null_turn_returns_a_start(self):
        self.assertEqual(NULL_TURN.tool_started("control_tv", "turn_on", target="x"), 0)


class AwaitingTests(unittest.TestCase):
    def _whatsapp(self, send_result=None, certain=False, candidates=()):
        svc = Mock()
        svc.enabled = True
        svc.backend = "playwright"
        match = Mock(certain=certain, candidates=list(candidates), key="Marta Zuka", phone="+351900000000")
        match.__bool__ = lambda self: True
        svc.resolve_contact.return_value = match
        svc.resolve_group.return_value = None
        svc.send.return_value = send_result
        return svc

    def test_a_whatsapp_read_back_is_awaiting_and_not_a_failure(self):
        line = "I've got Marta Zuka at +351900000000. Say send it and I will."
        svc = self._whatsapp(WhatsAppCommandResult(False, line, awaiting=True))
        spoken, ok, awaiting = _flags(_dispatch_whatsapp, {"action": "whatsapp_send", "to": "marta", "message": "late"}, svc)
        self.assertEqual(spoken, line)
        self.assertIsNone(ok)
        self.assertIs(awaiting, True)

    def test_which_one_is_awaiting(self):
        svc = self._whatsapp(certain=False, candidates=["Ana Costa", "Ana Silva"])
        spoken, ok, awaiting = _flags(_dispatch_whatsapp, {"action": "whatsapp_send", "to": "ana", "message": "hi"}, svc)
        self.assertIn("Which one?", spoken)
        self.assertIs(awaiting, True)
        svc.send.assert_not_called()

    def test_a_sent_message_is_not_awaiting(self):
        svc = self._whatsapp(WhatsAppCommandResult(True), certain=True)
        spoken, _, awaiting = _flags(_dispatch_whatsapp, {"action": "whatsapp_send", "to": "marta", "message": "late"}, svc)
        self.assertEqual(spoken, "Sent to Marta Zuka.")
        self.assertIs(awaiting, False)

    def test_a_bare_mock_result_never_asks(self):
        svc = self._whatsapp(Mock(), certain=True)
        _, _, awaiting = _flags(_dispatch_whatsapp, {"action": "whatsapp_send", "to": "marta", "message": "late"}, svc)
        self.assertIs(awaiting, False)

    def test_a_phone_read_back_is_awaiting(self):
        phone = Mock()
        phone.enabled = True
        phone.call.return_value = PhoneCommandResult(False, "Ready to call X.", awaiting=True)
        spoken, ok, awaiting = _flags(_dispatch_phone, {"action": "phone_call", "to": "X"}, phone)
        self.assertEqual(spoken, "Ready to call X.")
        self.assertIsNone(ok)
        self.assertIs(awaiting, True)

    def test_stremio_asking_for_a_source_is_awaiting(self):
        stremio = Mock()
        stremio.play.return_value = StremioPlayResult(False, message="Want the first source?", requires_confirmation=True)
        spoken, ok, awaiting = _flags(_dispatch_tv, {"action": "stremio_play", "title": "Fallout"}, Mock(), stremio, None, {}, {})
        self.assertEqual(spoken, "Want the first source?")
        self.assertIsNone(ok)
        self.assertIs(awaiting, True)

    def test_volume_past_the_cap_is_awaiting(self):
        tv_volume = Mock()
        tv_volume.max_percent = 40
        tv_volume.enabled = True
        tv_volume.set_volume.return_value = VolumeResult(ok=False, needs_confirm=True, target=70)
        media = Mock()
        _, ok, awaiting = _flags(_dispatch_tv, {"action": "volume_set", "volume_percent": 70},
                                 media, None, None, {}, {}, tv_volume=tv_volume)
        self.assertIsNone(ok)
        self.assertIs(awaiting, True)

    def test_through_handle_tool_call_it_parks_without_a_mood(self):
        orch = _orch()
        orch.whatsapp_service = self._whatsapp(WhatsAppCommandResult(False, "Say send it.", awaiting=True))
        orch._say_now = lambda text: None
        orch._handle_tool_call("control_whatsapp", {"action": "whatsapp_send", "to": "marta", "message": "the secret"})
        tool = _events(orch.bus, "turn_tool")[0]
        self.assertIs(tool["awaiting"], True)
        self.assertNotIn("ok", tool)
        self.assertEqual(tool["target"], "to marta")
        self.assertEqual(_events(orch.bus, "mood"), [])
        self.assertNotIn("the secret", repr(_events(orch.bus)))

    def test_the_awaiting_flag_does_not_leak_into_the_next_call(self):
        orch = _orch()
        orch.whatsapp_service = self._whatsapp(WhatsAppCommandResult(False, "Say send it.", awaiting=True))
        orch._say_now = lambda text: None
        orch._handle_tool_call("control_whatsapp", {"action": "whatsapp_send", "to": "marta", "message": "x"})
        orch.light_shadow = None
        govee = Mock()
        govee.enabled = True
        govee.resolve_light.return_value = ("attic", {"mac": "x", "aliases": []})
        govee.set_power.return_value = GoveeCommandResult(True)
        govee.can_read_state = False
        orch.govee_service = govee
        # Even a stale True left in the context by a dispatcher called outside
        # _handle_tool_call (as other tests do) must not reach this call.
        stale = _TOOL_AWAITING.set(True)
        try:
            orch._handle_tool_call("control_lights", {"action": "light_on", "light": "attic"})
            self.assertIs(_TOOL_AWAITING.get(), True, "the caller's value is restored")
        finally:
            _TOOL_AWAITING.reset(stale)
        second = _events(orch.bus, "turn_tool")[1]
        self.assertIs(second["ok"], True)
        self.assertNotIn("awaiting", second)


class ServerToolTests(unittest.TestCase):
    def test_a_web_search_goes_on_the_turn_with_its_query(self):
        orch = _orch()
        orch._on_server_tool("start", {"id": "s1", "name": "web_search"})
        orch._on_server_tool("input", {"id": "s1", "name": "web_search", "query": "benfica result"})
        orch._on_server_tool("end", {"id": "s1", "name": "web_search", "ok": True,
                                     "query": "benfica result", "result": "3 results"})
        starts = _events(orch.bus, "turn_tool_start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0]["start_ms"], starts[1]["start_ms"])
        self.assertEqual((starts[0]["name"], starts[0]["action"]), ("web_search", "search"))
        self.assertEqual(starts[1]["target"], "benfica result")
        end = _events(orch.bus, "turn_tool")[0]
        self.assertEqual((end["ok"], end["result"], end["start_ms"]), (True, "3 results", starts[0]["start_ms"]))
        self.assertEqual(_events(orch.bus, "mood"), [], "a search is not a device action")

    def test_a_failed_search_is_ok_false(self):
        orch = _orch()
        orch._on_server_tool("start", {"id": "s2", "name": "web_search"})
        orch._on_server_tool("end", {"id": "s2", "name": "web_search", "ok": False, "result": "error: too_many"})
        self.assertIs(_events(orch.bus, "turn_tool")[0]["ok"], False)

    def test_no_transcripts_means_no_query_on_the_page(self):
        orch = _orch(include_text=False)
        orch._on_server_tool("start", {"id": "s3", "name": "web_search"})
        orch._on_server_tool("input", {"id": "s3", "name": "web_search", "query": "private thing"})
        orch._on_server_tool("end", {"id": "s3", "name": "web_search", "ok": True, "query": "private thing"})
        self.assertNotIn("private thing", repr(_events(orch.bus)))
        self.assertEqual(len(_events(orch.bus, "turn_tool_start")), 1)

    def test_an_end_from_an_earlier_turn_is_dropped(self):
        orch = _orch()
        orch._on_server_tool("start", {"id": "old", "name": "web_search"})
        orch._turn.finish("reply")
        orch._turn = TurnTimer(None, bus=orch.bus)
        orch._on_server_tool("end", {"id": "old", "name": "web_search", "ok": True})
        self.assertEqual(_events(orch.bus, "turn_tool"), [])

    def test_unknown_ids_and_junk_never_raise(self):
        orch = _orch()
        orch._on_server_tool("end", {"id": "never-started"})
        orch._on_server_tool("input", {})
        orch._on_server_tool("start", None)
        orch._on_server_tool("weird", {"id": 5})
        self.assertEqual(_events(orch.bus, "turn_tool"), [])

    def test_stale_starts_do_not_pile_up(self):
        orch = _orch()
        for n in range(50):
            orch._turn = TurnTimer(None, bus=orch.bus)
            orch._on_server_tool("start", {"id": f"s{n}", "name": "web_search"})
        self.assertEqual(len(orch._server_tool_starts), 1)

    def test_the_orchestrator_hook_is_wired_to_the_llm(self):
        import inspect

        import core.orchestrator as orchestrator
        self.assertIn("self.llm.on_server_tool = self._on_server_tool", inspect.getsource(orchestrator.Orchestrator))


if __name__ == "__main__":
    unittest.main()
