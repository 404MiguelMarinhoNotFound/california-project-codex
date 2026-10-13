"""The live event bus, and the places that publish into it.

The bus sits on the hot path of every turn, so the guards here are about what
it must never do: block, raise, or let a slow phone hold up a reply.
"""

import logging
import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock

from core.event_bus import NULL_BUS, BusLogHandler, EventBus, WakeScorePublisher
from core.turn_log import TurnLogWriter, TurnTimer


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class EventBusTests(unittest.TestCase):
    def test_ids_increase_and_the_ring_replays_everything_to_a_new_page(self):
        bus = EventBus(ring_size=10)
        bus.publish("a", {"n": 1})
        bus.publish("b", {"n": 2})
        backlog, _ = bus.subscribe()
        self.assertEqual([e["kind"] for e in backlog], ["a", "b"])
        self.assertLess(backlog[0]["id"], backlog[1]["id"])

    def test_a_reconnect_gets_only_what_it_missed(self):
        bus = EventBus()
        for i in range(5):
            bus.publish("x", {"i": i})
        backlog, _ = bus.subscribe(last_id=3)
        self.assertEqual([e["data"]["i"] for e in backlog], [3, 4])

    def test_the_ring_is_bounded(self):
        bus = EventBus(ring_size=3)
        for i in range(10):
            bus.publish("x", {"i": i})
        backlog, _ = bus.subscribe()
        self.assertEqual([e["data"]["i"] for e in backlog], [7, 8, 9])

    def test_wake_scores_stream_live_but_never_push_turns_out_of_the_replay(self):
        bus = EventBus(ring_size=5)
        bus.publish("turn_end", {"turn": 1})
        _, q = bus.subscribe()
        for i in range(1000):
            bus.publish("wake", {"score": 0.01})
        self.assertEqual(q.qsize(), 1000)  # every score reached the live page
        backlog, _ = bus.subscribe()
        self.assertEqual([e["kind"] for e in backlog], ["turn_end"])

    def test_logs_replay_from_their_own_ring_in_order(self):
        bus = EventBus(ring_size=2)
        bus.publish("state", {"state": "idle"})
        for i in range(50):
            bus.publish("log", {"message": str(i)})
        bus.publish("state", {"state": "listening"})
        backlog, _ = bus.subscribe()
        self.assertEqual([e["kind"] for e in backlog][:1], ["state"])
        self.assertEqual(backlog[-1]["data"], {"state": "listening"})
        self.assertEqual(sum(e["kind"] == "log" for e in backlog), 50)
        ids = [e["id"] for e in backlog]
        self.assertEqual(ids, sorted(ids))

    def test_events_after_subscribing_arrive_live(self):
        bus = EventBus()
        _, q = bus.subscribe()
        bus.publish("state", {"state": "listening"})
        event = q.get(timeout=1)
        self.assertEqual(event["data"], {"state": "listening"})

    def test_a_slow_subscriber_drops_events_and_never_blocks_publish(self):
        bus = EventBus()
        _, q = bus.subscribe()
        started = time.monotonic()
        for i in range(5000):  # far past the per-client queue size
            bus.publish("wake", {"i": i})
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(q.full())

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        _, q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish("x")
        self.assertTrue(q.empty())
        self.assertEqual(bus.subscriber_count, 0)

    def test_publish_never_raises(self):
        bus = EventBus()
        broken = Mock(spec=queue.Queue)
        broken.put_nowait.side_effect = RuntimeError("boom")
        bus._subscribers.append(broken)
        bus.publish("x", {"a": 1})  # must not raise

    def test_the_null_bus_is_inert(self):
        NULL_BUS.publish("x", {"a": 1})
        backlog, q = NULL_BUS.subscribe()
        self.assertEqual(backlog, [])
        NULL_BUS.unsubscribe(q)


class BusLogHandlerTests(unittest.TestCase):
    def test_info_records_are_published_and_debug_are_not(self):
        bus = EventBus()
        log = logging.getLogger("test.bus.handler")
        log.propagate = False
        log.setLevel(logging.DEBUG)
        handler = BusLogHandler(bus)
        log.addHandler(handler)
        try:
            log.debug("quiet")
            log.info("hello %s", "there")
        finally:
            log.removeHandler(handler)
        backlog, _ = bus.subscribe()
        self.assertEqual([e["data"]["message"] for e in backlog], ["hello there"])
        self.assertEqual(backlog[0]["data"]["level"], "INFO")


class WakeScorePublisherTests(unittest.TestCase):
    def test_throttled_but_the_peak_between_publishes_is_kept(self):
        bus = EventBus()
        _, q = bus.subscribe()
        now = [100.0]
        meter = WakeScorePublisher(bus, hz=10, clock=lambda: now[0])
        meter.offer(0.1, "idle")          # published
        now[0] += 0.02
        meter.offer(0.93, "idle")         # held: inside the 100ms window
        now[0] += 0.02
        meter.offer(0.05, "idle")         # held
        now[0] += 0.1
        meter.offer(0.02, "idle")         # published, carrying the 0.93
        self.assertEqual([e["data"]["score"] for e in _drain(q)], [0.1, 0.93])

    def test_a_bad_score_does_not_raise(self):
        WakeScorePublisher(EventBus()).offer("not a number", "idle")


class TurnTimerLiveTests(unittest.TestCase):
    def _writer(self, include_text=True):
        writer = TurnLogWriter({"logging": {"turns": False, "include_transcripts": include_text}})
        return writer

    def test_each_mark_is_published_as_it_happens_not_at_finish(self):
        bus = EventBus()
        _, q = bus.subscribe()
        turn = TurnTimer(self._writer(), bus=bus)
        self.assertEqual(q.get(timeout=1)["kind"], "turn_start")
        turn.mark("speech_end")
        event = q.get(timeout=1)
        self.assertEqual(event["kind"], "turn_mark")
        self.assertEqual(event["data"]["name"], "speech_end")
        self.assertTrue(q.empty())  # nothing waits for finish()

    def test_mark_once_publishes_once(self):
        bus = EventBus()
        turn = TurnTimer(self._writer(), bus=bus)
        turn.mark_once("first_audio")
        turn.mark_once("first_audio")
        backlog, _ = bus.subscribe()
        self.assertEqual(sum(e["kind"] == "turn_mark" for e in backlog), 1)

    def test_tools_and_the_final_record_are_published(self):
        bus = EventBus()
        turn = TurnTimer(self._writer(), bus=bus)
        turn.tool_started("control_tv", "turn_on")
        turn.tool("control_tv", "turn_on", 0, "TV is on.")
        turn.finish("reply")
        kinds = [e["kind"] for e in bus.subscribe()[0]]
        self.assertEqual(kinds, ["turn_start", "turn_tool_start", "turn_tool", "turn_end"])
        end = bus.subscribe()[0][-1]["data"]["record"]
        self.assertEqual(end["outcome"], "reply")

    def test_include_transcripts_false_keeps_what_he_said_off_the_bus(self):
        bus = EventBus()
        turn = TurnTimer(self._writer(include_text=False), bus=bus)
        turn.set(transcript="tell mum I'm late", tier="warm")
        turn.set(reply="On it.")
        turn.finish("reply")
        for event in bus.subscribe()[0]:
            flat = repr(event)
            self.assertNotIn("mum", flat)
            self.assertNotIn("On it", flat)
        sets = [e for e in bus.subscribe()[0] if e["kind"] == "turn_set"]
        self.assertEqual(sets[0]["data"]["fields"], {"tier": "warm"})

    def test_a_broken_bus_never_breaks_a_turn(self):
        bus = MagicMock()
        bus.publish.side_effect = RuntimeError("boom")
        turn = TurnTimer(self._writer(), bus=bus)
        turn.mark("speech_end")
        turn.set(outcome="reply")
        record = turn.finish()
        self.assertEqual(record["marks"]["speech_end"] >= 0, True)

    def test_without_a_bus_nothing_changes(self):
        turn = TurnTimer(self._writer())
        turn.mark("speech_end")
        self.assertIsNotNone(turn.finish("reply"))


class OrchestratorPublishTests(unittest.TestCase):
    def _orch(self):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.leds = Mock()
        orch.bus = EventBus()
        return orch

    def test_set_state_drives_the_leds_and_publishes(self):
        orch = self._orch()
        orch._set_state("thinking")
        orch.leds.set_state.assert_called_once_with("thinking")
        backlog, _ = orch.bus.subscribe()
        self.assertEqual(backlog[-1]["data"], {"state": "thinking"})

    def test_an_orchestrator_built_without_a_ui_uses_the_null_bus(self):
        from core.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch.leds = Mock()
        orch._set_state("idle")  # must not raise
        self.assertIs(orch.bus, NULL_BUS)

    def test_wake_scores_are_offered_with_the_phase_threshold(self):
        orch = self._orch()
        _, q = orch.bus.subscribe()
        orch._wake_meter = WakeScorePublisher(orch.bus, hz=0)
        orch.wake_word = Mock(threshold=0.81, last_score=0.42)
        orch._barge_in_threshold = 0.5
        orch._offer_wake_score("idle")
        orch._offer_wake_score("reply")
        data = [e["data"] for e in _drain(q)]
        self.assertEqual([(d["phase"], d["threshold"]) for d in data], [("idle", 0.81), ("reply", 0.5)])


if __name__ == "__main__":
    unittest.main()
