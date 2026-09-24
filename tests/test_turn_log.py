"""
Runtime logs: the rotating log file and the per-turn timing record.

The turn record is only worth having if every way a turn ends writes exactly
one line, the stages are in the order they happened, and nothing about the
logging can take a turn down. Everything here is faked and writes to a tmpdir.
"""

import json
import logging
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.orchestrator import Orchestrator
from core.turn_log import NULL_TURN, TurnLogWriter, TurnTimer, setup_file_logging
from tests.test_reply_barge_in import SENTENCES, _FakeMic, _orchestrator


def _read_turns(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _log_config(tmp, **overrides):
    cfg = {"enabled": True, "dir": tmp, "turns": True, "include_transcripts": True}
    cfg.update(overrides)
    return {"logging": cfg}


class TurnTimerTests(unittest.TestCase):
    def test_mark_once_keeps_the_first_time(self):
        turn = TurnTimer()
        turn.mark_once("first_audio")
        first = turn._marks["first_audio"]
        with patch("core.turn_log.time.monotonic", return_value=turn._t0 + 5):
            turn.mark_once("first_audio")
        self.assertEqual(turn._marks["first_audio"], first)

    def test_finish_writes_once_and_later_calls_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TurnLogWriter(_log_config(tmp))
            turn = TurnTimer(writer)
            turn.set(outcome="reply")
            self.assertIsNotNone(turn.finish())
            self.assertIsNone(turn.finish("error"))
            turns = _read_turns(writer.path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["outcome"], "reply")

    def test_a_stage_that_did_not_happen_is_absent_not_zero(self):
        turn = TurnTimer()
        turn.mark("speech_end")
        record = turn.finish("no_speech")
        self.assertEqual(set(record["marks"]), {"speech_end"})

    def test_transcripts_can_be_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TurnLogWriter(_log_config(tmp, include_transcripts=False))
            turn = TurnTimer(writer)
            turn.set(transcript="tell marta I'm late", reply="Sent.")
            turn.finish("reply")
            record = _read_turns(writer.path)[0]
        self.assertNotIn("transcript", record)
        self.assertNotIn("reply", record)
        self.assertEqual(record["outcome"], "reply")

    def test_disabled_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TurnLogWriter(_log_config(tmp, turns=False))
            TurnTimer(writer).finish("reply")
            self.assertFalse(os.path.exists(writer.path))

    def test_an_unwritable_log_never_raises(self):
        writer = TurnLogWriter(_log_config("unused"))
        with patch("core.turn_log.open", side_effect=OSError("disk full"), create=True):
            TurnTimer(writer).finish("reply")

    def test_the_null_turn_accepts_everything(self):
        NULL_TURN.mark("x")
        NULL_TURN.mark_once("x")
        NULL_TURN.set(a=1)
        NULL_TURN.tool("control_tv", "turn_on", 0, "ok")
        self.assertIsNone(NULL_TURN.finish())


class FileLoggingTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self._handlers = list(self.root.handlers)
        self._level = self.root.level

    def tearDown(self):
        for h in list(self.root.handlers):
            if h not in self._handlers:
                self.root.removeHandler(h)
                h.close()
        self.root.setLevel(self._level)

    def test_debug_reaches_the_file_while_the_console_keeps_its_level(self):
        console = logging.StreamHandler()
        self.root.addHandler(console)
        self.root.setLevel(logging.INFO)
        with tempfile.TemporaryDirectory() as tmp:
            path = setup_file_logging(_log_config(tmp, file_level="DEBUG"))
            logging.getLogger("california.test").debug("only in the file")
            for h in self.root.handlers:
                h.flush()
            with open(path, encoding="utf-8") as fh:
                self.assertIn("only in the file", fh.read())
            self.assertEqual(console.level, logging.INFO)
            self.root.removeHandler(console)
            for h in list(self.root.handlers):
                if h not in self._handlers:
                    self.root.removeHandler(h)
                    h.close()

    def test_disabled_returns_none(self):
        self.assertIsNone(setup_file_logging({"logging": {"enabled": False}}))


def _activation_orchestrator(tmp, sentences=SENTENCES, fire_on=None, recorded=True):
    orch = _orchestrator(fire_on=fire_on, sentences=sentences)
    orch._turn_writer = TurnLogWriter(_log_config(tmp))
    orch._wake_count = 0
    orch._last_record_outcome = "ok" if recorded else "no_speech"
    orch.audio.play_activation_sound = Mock(return_value=None)
    orch._record_speech = Mock(
        return_value=np.ones(16000, dtype=np.int16) if recorded else None
    )
    orch.stt = Mock()
    orch.stt.transcribe = Mock(return_value="what's the weather")
    return orch


class TurnRecordTests(unittest.TestCase):
    def setUp(self):
        # The turn prints the transcript with an emoji; main.py reconfigures
        # stdout to UTF-8 but a test runner on a cp1252 console does not.
        patcher = patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_reply_records_every_stage_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = _activation_orchestrator(tmp)
            orch._handle_activation(_FakeMic(), pre_roll=[])
            turns = _read_turns(orch._turn_writer.path)

        self.assertEqual(len(turns), 1)
        rec = turns[0]
        self.assertEqual(rec["outcome"], "reply")
        self.assertFalse(rec["chained"])
        self.assertEqual(rec["transcript"], "what's the weather")
        self.assertEqual(rec["speech_s"], 1.0)
        order = ["speech_end", "stt_done", "llm_first_token",
                 "first_sentence", "first_audio", "reply_done"]
        times = [rec["marks"][k] for k in order]
        self.assertEqual(times, sorted(times))
        self.assertIs(orch._turn, NULL_TURN)

    def test_a_dropped_wake_is_recorded_with_its_outcome_and_nothing_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = _activation_orchestrator(tmp, recorded=False)
            orch._handle_activation(_FakeMic(), pre_roll=[])
            rec = _read_turns(orch._turn_writer.path)[0]
        self.assertEqual(rec["outcome"], "no_speech")
        self.assertEqual(set(rec["marks"]), {"speech_end"})
        orch.stt.transcribe.assert_not_called()

    def test_a_barge_in_is_recorded_and_the_chained_turn_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = _activation_orchestrator(tmp, fire_on=8)
            self.assertTrue(orch._handle_activation(_FakeMic(), pre_roll=[]))
            orch.wake_word.fire_on = None
            orch._handle_activation(_FakeMic())
            turns = _read_turns(orch._turn_writer.path)
        self.assertEqual([t["outcome"] for t in turns], ["barged_in", "reply"])
        self.assertEqual([t["chained"] for t in turns], [False, True])

    def test_a_turn_that_raises_still_leaves_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            orch = _activation_orchestrator(tmp)
            orch.stt.transcribe.side_effect = RuntimeError("groq down")
            with self.assertRaises(RuntimeError):
                orch._handle_activation(_FakeMic(), pre_roll=[])
            rec = _read_turns(orch._turn_writer.path)[0]
        self.assertEqual(rec["outcome"], "error")

    def test_tool_calls_are_timed_with_their_action_and_result(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch._turn = TurnTimer()
        orch._dispatch_tool = Mock(return_value="TV is on")
        self.assertEqual(orch._handle_tool_call("control_tv", {"action": "turn_on"}), "TV is on")
        rec = orch._turn.finish("reply")
        self.assertEqual(len(rec["tools"]), 1)
        tool = rec["tools"][0]
        self.assertEqual((tool["name"], tool["action"], tool["result"]),
                         ("control_tv", "turn_on", "TV is on"))
        self.assertGreaterEqual(tool["ms"], 0)

    def test_a_tool_that_raises_is_still_timed(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch._turn = TurnTimer()
        orch._dispatch_tool = Mock(side_effect=RuntimeError("adb gone"))
        with self.assertRaises(RuntimeError):
            orch._handle_tool_call("control_tv", {"action": "get_status"})
        self.assertEqual(orch._turn.finish("error")["tools"][0]["result"], "<raised>")

    def test_the_llm_stream_is_still_closed_on_barge_in_through_the_wrapper(self):
        """The timing wrapper must not stop GeneratorExit reaching the LLM."""
        orch = _orchestrator(fire_on=8, sentences=SENTENCES)
        orch._turn = TurnTimer()
        self.assertTrue(orch._stream_response("hi", _FakeMic()))
        self.assertTrue(orch.llm.closed)


if __name__ == "__main__":
    unittest.main()
