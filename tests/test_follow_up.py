"""
Answering her question without the wake word.

When California's reply asks Master Miguel something -- a read-back, "which
one?", or any reply ending in a question mark -- the mic stays open for a
short window and his answer is taken without "California" first. Speech in
that window that is not an answer to her (the TV, someone else) is recognised
by the LLM, which replies exactly "[ignore]", and is dropped silently.

Spec: docs/superpowers/specs/2026-10-15-follow-up-listening-design.md

Everything here is faked: no mic, no model, no TTS provider, no network.
"""

import json
import os
import tempfile
import unittest

from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from services.llm import ERROR_LINE, LLMService
from tests.config_fixture import config_for_tests


def _llm(history):
    svc = LLMService.__new__(LLMService)
    svc.history = list(history)
    return svc


class DropLastExchangeTests(unittest.TestCase):
    def test_removes_the_last_exchange_and_its_tool_rounds(self):
        svc = _llm([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            {"role": "assistant", "content": "y"},
        ])
        self.assertTrue(svc.drop_last_exchange())
        self.assertEqual(svc.history, [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
        ])

    def test_a_partial_ignore_reply_is_removed_with_its_question(self):
        svc = _llm([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "[Heard without the wake word...] tv"},
            {"role": "assistant", "content": "[ignore]"},
        ])
        self.assertTrue(svc.drop_last_exchange())
        self.assertEqual(len(svc.history), 2)
        self.assertEqual(svc.history[-1]["content"], "x")

    def test_no_exchange_is_a_logged_no_op(self):
        svc = _llm([])
        with self.assertLogs("services.llm", level="WARNING"):
            self.assertFalse(svc.drop_last_exchange())
        self.assertEqual(svc.history, [])

    def test_error_line_constant_is_what_she_says(self):
        # It ends in a question mark, which is exactly why a reply carrying it
        # must never open a follow-up window (one outage would loop).
        self.assertTrue(ERROR_LINE.endswith("?"))
        self.assertEqual(
            ERROR_LINE, "Sorry, I had trouble thinking about that. Could you try again?"
        )


TIMEOUT_TEXT = "I'll take that as a no. Say my name if you need me."
CAP_TEXT = "Okay, I'll stop listening now. Say California if you need me."


class StopLinesTests(unittest.TestCase):
    def _write(self, folder, names, manifest=True):
        for name in names:
            sf.write(os.path.join(folder, f"{name}.wav"), np.zeros(2400, dtype=np.float32), 24000)
        if manifest:
            with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"timeout": TIMEOUT_TEXT, "cap": CAP_TEXT}, fh)

    def test_loads_both_clips_with_their_text(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout", "cap"])
            lines = load_stop_lines(folder)
        self.assertEqual(set(lines), {"timeout", "cap"})
        audio, sr, text = lines["cap"]
        self.assertEqual(sr, 24000)
        self.assertEqual(len(audio), 2400)
        self.assertEqual(text, CAP_TEXT)
        self.assertEqual(lines["timeout"][2], TIMEOUT_TEXT)

    def test_a_missing_clip_is_skipped_with_a_warning(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout"])
            with self.assertLogs("services.stop_lines", level="WARNING") as logs:
                lines = load_stop_lines(folder)
        self.assertEqual(set(lines), {"timeout"})
        self.assertIn("generate_stop_lines.py", "\n".join(logs.output))

    def test_a_missing_manifest_leaves_the_text_empty(self):
        from services.stop_lines import load_stop_lines

        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, ["timeout", "cap"], manifest=False)
            lines = load_stop_lines(folder)
        self.assertEqual(lines["cap"][2], "")

    def test_a_missing_directory_returns_empty_and_never_raises(self):
        from services.stop_lines import load_stop_lines

        with self.assertLogs("services.stop_lines", level="WARNING"):
            self.assertEqual(load_stop_lines("/nonexistent/stop_lines"), {})
        with self.assertLogs("services.stop_lines", level="WARNING"):
            self.assertEqual(load_stop_lines(""), {})

    def test_generator_text_matches_the_spec(self):
        import generate_stop_lines

        self.assertEqual(generate_stop_lines.STOP_LINES, {"timeout": TIMEOUT_TEXT, "cap": CAP_TEXT})

    def test_shipped_config_points_at_the_aoede_folder(self):
        self.assertEqual(
            config_for_tests()["sounds"]["stop_lines_dir"],
            "sounds/stop_lines/google_en-US-Chirp3-HD-Aoede",
        )


# ── Deciding a follow-up at the end of a turn ─────────────────────────────

from tests.test_reply_barge_in import SENTENCES, _FakeMic, _orchestrator, _tts_synthesize  # noqa: E402

QUESTION = "I found two sources for that one, do you want the first source?"
STATEMENT = "Done, the attic lights are off now and the room is dark again."


def _stop_lines():
    return {
        "timeout": (_tts_synthesize("timeout line")[0], 24000, "timeout line"),
        "cap": (_tts_synthesize("cap line")[0], 24000, "cap line"),
    }


def _follow_up_orch(sentences, fire_on=None):
    orch = _orchestrator(fire_on=fire_on, sentences=sentences)
    orch.tts.synthesize = Mock(side_effect=_tts_synthesize)
    orch._follow_up_enabled = True
    orch._stop_lines = _stop_lines()
    orch._follow_up_index = 0
    orch._turn_awaiting = False
    orch._next_follow_up = False
    return orch


class FollowUpDecisionTests(unittest.TestCase):
    def test_a_reply_ending_in_a_question_wants_a_follow_up(self):
        orch = _follow_up_orch([STATEMENT, QUESTION])
        orch._stream_response("play fallout", _FakeMic())
        self.assertTrue(orch._next_follow_up)

    def test_a_statement_does_not(self):
        orch = _follow_up_orch([STATEMENT])
        orch._stream_response("lights off", _FakeMic())
        self.assertFalse(orch._next_follow_up)

    def test_a_question_mid_reply_followed_by_a_statement_does_not(self):
        orch = _follow_up_orch([QUESTION, STATEMENT])
        orch._stream_response("hi", _FakeMic())
        self.assertFalse(orch._next_follow_up)

    def test_an_awaiting_tool_wants_one_without_a_question_mark(self):
        orch = _follow_up_orch([STATEMENT])
        orch._turn_awaiting = True
        orch._stream_response("tell marta im late", _FakeMic())
        self.assertTrue(orch._next_follow_up)

    def test_the_llm_error_line_never_opens_a_window(self):
        orch = _follow_up_orch([ERROR_LINE])
        orch._stream_response("hi", _FakeMic())
        self.assertFalse(orch._next_follow_up)

    def test_a_barged_in_reply_never_wants_one(self):
        orch = _follow_up_orch(SENTENCES + [QUESTION], fire_on=8)
        orch._turn_awaiting = True
        barged = orch._stream_response("hi", _FakeMic())
        self.assertTrue(barged)
        self.assertFalse(orch._next_follow_up)

    def test_disabled_never_wants_one(self):
        orch = _follow_up_orch([QUESTION])
        orch._follow_up_enabled = False
        orch._turn_awaiting = True
        orch._stream_response("hi", _FakeMic())
        self.assertFalse(orch._next_follow_up)

    def test_the_tenth_follow_up_asking_again_says_the_stop_line_once(self):
        orch = _follow_up_orch([QUESTION])
        orch._follow_up_index = 10
        orch._stream_response("yes", _FakeMic())
        self.assertFalse(orch._next_follow_up)
        self.assertEqual(orch.audio.played.count("cap line"), 1)
        self.assertNotIn("timeout line", orch.audio.played)
        spoken_by_tts = [c.args[0] for c in orch.tts.synthesize.call_args_list]
        self.assertNotIn("cap line", spoken_by_tts)
        self.assertNotIn("timeout line", spoken_by_tts)

    def test_below_the_cap_no_stop_line(self):
        orch = _follow_up_orch([QUESTION])
        orch._follow_up_index = 9
        orch._stream_response("yes", _FakeMic())
        self.assertTrue(orch._next_follow_up)
        self.assertNotIn("cap line", orch.audio.played)

    def test_a_missing_stop_clip_plays_nothing_and_does_not_raise(self):
        orch = _follow_up_orch([QUESTION])
        orch._stop_lines = {}
        orch._follow_up_index = 10
        orch._stream_response("yes", _FakeMic())
        self.assertFalse(orch._next_follow_up)
        self.assertEqual(" ".join(orch.audio.played), QUESTION)

    def test_handle_tool_call_marks_the_turn_awaiting(self):
        import core.orchestrator as orch_mod

        orch = _follow_up_orch([STATEMENT])
        orch.bus = Mock()

        def dispatch(name, tool_input):
            orch_mod._await_answer()
            return "ok"

        with patch.object(orch, "_dispatch_tool", side_effect=dispatch), \
                patch.object(orch, "_publish_mood_for_tool"):
            orch._handle_tool_call("control_whatsapp", {"action": "whatsapp_send"})
        self.assertTrue(orch._turn_awaiting)

    def test_shipped_config_carries_the_conversation_block(self):
        self.assertEqual(config_for_tests()["conversation"], {
            "follow_up_enabled": True,
            "follow_up_timeout_s": 3.0,
            "max_follow_ups": 10,
        })


# ── "That wasn't for me": the [ignore] gate ──────────────────────────────

import threading  # noqa: E402

from services.sentence_chunker import TOOL_BOUNDARY  # noqa: E402


def _gate(tokens):
    from core.orchestrator import _ignore_gate

    ignored = threading.Event()
    out = list(_ignore_gate(iter(tokens), ignored))
    return out, ignored.is_set()


class IgnoreGateTests(unittest.TestCase):
    def test_a_split_sentinel_yields_nothing(self):
        self.assertEqual(_gate(["[ign", "ore]"]), ([], True))

    def test_case_and_leading_space_and_trailing_text(self):
        self.assertEqual(_gate(["  [IGNORE]", " anyway"]), ([], True))

    def test_a_real_answer_passes_through(self):
        self.assertEqual(_gate(["Yes", ", sending"]), (["Yes", ", sending"], False))

    def test_a_partial_sentinel_at_stream_end_is_spoken(self):
        self.assertEqual(_gate(["[ign"]), (["[ign"], False))

    def test_a_tool_boundary_releases_the_gate(self):
        self.assertEqual(_gate([TOOL_BOUNDARY, "Done."]), ([TOOL_BOUNDARY, "Done."], False))

    def test_the_word_ignore_later_in_a_reply_is_not_the_sentinel(self):
        self.assertEqual(_gate(["I'll ignore that"]), (["I'll ignore that"], False))


class _RecordingLLM:
    def __init__(self, sentences):
        self.sentences = sentences
        self.received = []
        self.closed = False
        self.drop_last_exchange = Mock(return_value=True)

    def stream_response(self, text):
        self.received.append(text)
        try:
            for s in self.sentences:
                yield s + " "
        except GeneratorExit:
            self.closed = True
            raise


class IgnoredFollowUpTests(unittest.TestCase):
    def _orch(self, sentences):
        orch = _follow_up_orch(sentences)
        orch.llm = _RecordingLLM(sentences)
        return orch

    def test_a_follow_up_reply_of_ignore_is_silent_and_dropped(self):
        orch = self._orch(["[ignore]"])
        orch._next_follow_up = True
        barged = orch._stream_response("maybe tomorrow", _FakeMic(), follow_up=True)
        self.assertFalse(barged)
        self.assertEqual(orch.audio.played, [])
        orch.llm.drop_last_exchange.assert_called_once_with()
        self.assertTrue(orch.llm.closed)
        self.assertFalse(orch._next_follow_up)

    def test_the_follow_up_message_carries_the_prefix(self):
        from core.orchestrator import FOLLOW_UP_PREFIX

        orch = self._orch([STATEMENT])
        orch._stream_response("yes", _FakeMic(), follow_up=True)
        self.assertEqual(orch.llm.received, [FOLLOW_UP_PREFIX + "yes"])
        self.assertTrue(FOLLOW_UP_PREFIX.startswith("[Heard without the wake word"))
        self.assertTrue(FOLLOW_UP_PREFIX.endswith("reply with exactly [ignore].] "))

    def test_a_normal_turn_never_gates(self):
        orch = self._orch(["[ignore] hm, that was odd but I will say it anyway."])
        orch._stream_response("hi", _FakeMic())
        self.assertEqual(orch.llm.received, ["hi"])
        self.assertTrue(orch.audio.played)
        orch.llm.drop_last_exchange.assert_not_called()

    def test_a_real_answer_is_spoken(self):
        orch = self._orch([STATEMENT])
        orch._stream_response("yes", _FakeMic(), follow_up=True)
        self.assertEqual(" ".join(orch.audio.played), STATEMENT)
        orch.llm.drop_last_exchange.assert_not_called()


# ── The follow-up turn and the conversation loop ──────────────────────────

from tests.test_reply_barge_in import _FakeWake  # noqa: E402

SPEECH = np.ones(16000, dtype=np.int16)


class _ScriptedLLM:
    """One scripted reply per call, each a list of sentences."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.received = []
        self.drop_last_exchange = Mock(return_value=True)

    def stream_response(self, text):
        self.received.append(text)
        reply = self.replies.pop(0) if self.replies else [STATEMENT]
        for s in reply:
            yield s + " "


def _loop_orch(replies, recordings, transcripts=None):
    """
    `recordings` scripts _record_speech for each follow-up window: an array
    (he answered) or None (silence). `replies` scripts the LLM per follow-up.
    """
    orch = _follow_up_orch([STATEMENT])
    orch.llm = _ScriptedLLM(replies)
    orch.stt = Mock()
    orch.stt.transcribe = Mock(side_effect=list(transcripts or ["yes"] * len(recordings)))
    orch._handle_command = Mock(return_value=False)
    orch._capture_incoming_target = Mock()
    orch.audio.play_activation_sound = Mock()
    orch._wake_count = 1
    orch.record_calls = []
    script = list(recordings)

    def record(mic_stream, playback=None, speech_timeout=None):
        orch.record_calls.append((playback, speech_timeout))
        audio = script.pop(0) if script else None
        orch._last_record_outcome = "ok" if audio is not None else "no_speech"
        return audio

    orch._record_speech = Mock(side_effect=record)
    return orch


class ConversationLoopTests(unittest.TestCase):
    def test_an_answer_in_the_window_is_a_follow_up_turn_with_no_acknowledgement(self):
        orch = _loop_orch(replies=[[STATEMENT]], recordings=[SPEECH])
        orch._next_follow_up = True  # the wake-word turn asked something

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(orch.record_calls, [(None, 3.0)])
        orch.audio.play_activation_sound.assert_not_called()
        self.assertEqual(orch._wake_count, 1)
        self.assertEqual(" ".join(orch.audio.played), STATEMENT)
        self.assertEqual(len(orch.llm.received), 1)
        self.assertFalse(orch._next_follow_up)
        self.assertEqual(orch._follow_up_index, 0)

    def test_silence_after_the_first_question_ends_quietly(self):
        orch = _loop_orch(replies=[], recordings=[None])
        orch._next_follow_up = True

        orch._run_conversation(_FakeMic(), False)

        orch.stt.transcribe.assert_not_called()
        self.assertEqual(orch.llm.received, [])
        self.assertEqual(orch.audio.played, [])

    def test_silence_in_a_chained_window_says_the_stop_line_once(self):
        orch = _loop_orch(replies=[[QUESTION]], recordings=[SPEECH, None])
        orch._next_follow_up = True

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(len(orch.record_calls), 2)
        self.assertEqual(orch.audio.played.count("timeout line"), 1)
        self.assertNotIn("cap line", orch.audio.played)

    def test_an_empty_transcript_in_a_chained_window_counts_as_no_reply(self):
        orch = _loop_orch(replies=[[QUESTION]], recordings=[SPEECH, SPEECH], transcripts=["yes", ""])
        orch._next_follow_up = True

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(len(orch.llm.received), 1)
        self.assertEqual(orch.audio.played.count("timeout line"), 1)

    def test_ten_follow_ups_then_the_cap_line(self):
        orch = _loop_orch(replies=[[QUESTION]] * 12, recordings=[SPEECH] * 12)
        orch._next_follow_up = True

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(len(orch.llm.received), 10)
        self.assertEqual(orch.audio.played.count("cap line"), 1)
        self.assertNotIn("timeout line", orch.audio.played)
        self.assertFalse(orch._next_follow_up)

    def test_a_barge_in_inside_a_follow_up_resets_the_chain(self):
        orch = _loop_orch(replies=[[QUESTION]], recordings=[SPEECH])
        orch._next_follow_up = True
        seen = []
        results = iter([True, False])

        def follow_up(mic_stream, index):
            seen.append(("follow_up", index))
            orch._follow_up_index = index
            return next(results)

        def activation(mic_stream, pre_roll=None):
            seen.append(("activation", orch._follow_up_index))
            orch._next_follow_up = False
            return False

        orch._handle_follow_up = Mock(side_effect=follow_up)
        orch._handle_activation = Mock(side_effect=activation)

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(seen, [("follow_up", 1), ("activation", 0)])
        self.assertNotIn("cap line", orch.audio.played)
        self.assertNotIn("timeout line", orch.audio.played)

    def test_an_ignored_follow_up_ends_the_conversation_without_a_line(self):
        orch = _loop_orch(replies=[["[ignore]"]], recordings=[SPEECH])
        orch._next_follow_up = True

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(orch.audio.played, [])
        orch.llm.drop_last_exchange.assert_called_once_with()
        self.assertEqual(len(orch.record_calls), 1)

    def test_the_follow_up_turn_is_logged_as_one(self):
        orch = _loop_orch(replies=[[STATEMENT]], recordings=[SPEECH])
        orch._next_follow_up = True
        records = []
        orch._turn_writer = Mock()
        orch._turn_writer.include_text = True
        orch._turn_writer.write = Mock(side_effect=records.append)

        orch._run_conversation(_FakeMic(), False)

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].get("follow_up"))
        self.assertEqual(records[0].get("follow_up_index"), 1)
        self.assertTrue(records[0].get("chained"))

    def test_disabled_is_todays_flow(self):
        orch = _loop_orch(replies=[], recordings=[SPEECH])
        orch._follow_up_enabled = False
        orch._stream_response("hi", _FakeMic())  # a plain reply, even ending in "?"
        orch._run_conversation(_FakeMic(), False)
        orch._record_speech.assert_not_called()

    def test_a_phone_report_question_opens_no_window(self):
        orch = _loop_orch(replies=[[QUESTION]], recordings=[SPEECH])
        report = Mock()
        report.label = "Taberna"
        report.status = "booked"
        with patch("core.orchestrator._phone_report_message", return_value="report"):
            orch._deliver_phone_report(report, _FakeMic())
        orch._record_speech.assert_not_called()
        self.assertEqual(orch.audio.speaker_opens, 1)
        self.assertEqual(orch.audio.speaker_closes, 1)

    def test_idle_loop_holds_one_speaker_session_for_the_whole_conversation(self):
        orch = _loop_orch(replies=[[QUESTION], [STATEMENT]], recordings=[SPEECH, SPEECH])
        orch.wake_word = _FakeWake(fire_on=1)

        def wake_turn(mic_stream, pre_roll=None):
            orch._next_follow_up = True
            orch._follow_up_index = 0
            return False

        orch._handle_activation = Mock(side_effect=wake_turn)

        orch._idle_loop(_FakeMic())

        self.assertEqual(len(orch.llm.received), 2)
        self.assertEqual(orch.audio.speaker_opens, 1)
        self.assertEqual(orch.audio.speaker_closes, 1)


if __name__ == "__main__":
    unittest.main()
