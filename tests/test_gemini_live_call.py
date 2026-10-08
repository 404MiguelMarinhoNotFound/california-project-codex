"""
The Gemini Live call agent, driven by a fake session and fake audio.

No test here opens a websocket, a sound device or the loopback: the client,
the capture and the cable speaker are all injected.
"""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from services import gemini_live_call as glc
from services.gemini_live_call import GeminiLiveAgent


def _content(**kw):
    base = dict(interrupted=False, model_turn=None, input_transcription=None, output_transcription=None)
    base.update(kw)
    return NS(server_content=NS(**base), tool_call=None)


def heard(text):
    return _content(input_transcription=NS(text=text))


def said(text):
    return _content(output_transcription=NS(text=text))


def audio(data):
    return _content(model_turn=NS(parts=[NS(inline_data=NS(data=data))]))


def interrupted():
    return _content(interrupted=True)


def tool(name, **args):
    return NS(server_content=None, tool_call=NS(function_calls=[NS(id=f"id-{name}", name=name, args=args)]))


def wrap_up():
    """A realistic close: she spoke, they answered, she said goodbye and hung up."""
    return [
        [said("Boa noite, fala a California.")],
        [heard("Sim, diga.")],
        [said(" Obrigada, boa noite!")],
        [tool("end_call")],
    ]


class FakeSession:
    """receive() yields one scripted batch per call, then waits forever."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.sent_audio = 0
        self.tool_responses = []
        self.nudges = []

    async def send_realtime_input(self, audio=None, **kw):
        self.sent_audio += 1

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)

    async def send_client_content(self, turns=None, turn_complete=False):
        self.nudges.append(turns["parts"][0]["text"])

    async def receive(self):
        if not self.batches:
            await asyncio.Event().wait()
        for msg in self.batches.pop(0):
            yield msg


class FakeClient:
    def __init__(self, session, fail=None):
        outer = self

        class Live:
            def connect(self, model, config):
                outer.model, outer.config = model, config
                if fail:
                    raise fail

                class Ctx:
                    async def __aenter__(self_inner):
                        return session

                    async def __aexit__(self_inner, *exc):
                        return False

                return Ctx()

        self.aio = NS(live=Live())


class FakeCapture:
    def __init__(self):
        self.stopped = False

    def start(self, on_block):
        on_block(b"\x01\x00" * 160)  # not silence, so muting is visible

    def stop(self):
        self.stopped = True


class FakeSpeaker:
    def __init__(self):
        self.written, self.flushes, self.stopped = [], 0, False

    def start(self):
        pass

    def write(self, pcm):
        self.written.append(pcm)

    def flush(self):
        self.flushes += 1

    def idle(self):
        return True

    def stop(self):
        self.stopped = True


class _Base(unittest.TestCase):
    def setUp(self):
        for name, value in (("_WATCH_INTERVAL_S", 0.02), ("_GOODBYE_GRACE_S", 0.05), ("_NUDGE_AFTER_S", 0.3)):
            patcher = mock.patch.object(glc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.speaker, self.capture = FakeSpeaker(), FakeCapture()

    def agent(self, session=None, fail=None):
        self.session = session or FakeSession([])
        self.client = FakeClient(self.session, fail=fail)
        return GeminiLiveAgent(
            client_kwargs={"api_key": "test"}, model="gemini-test", voice="Aoede",
            client_factory=lambda kwargs: self.client,
            capture_factory=lambda: self.capture,
            speaker_factory=lambda: self.speaker,
        )


class ConversationTests(_Base):
    def test_full_booking_call(self):
        session = FakeSession([
            [heard("Estou"), heard("?")],
            [said("Boa noite, aqui é a California, "), said("assistente do Miguel."), audio(b"pcm1")],
            [heard("Só temos às nove e meia.")],
            [tool("end_call", reason="done")],
        ])
        result = self.agent(session).run("prompt", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")
        self.assertIsNone(result.outcome)  # read from the transcript after the call
        self.assertEqual([(l.who, l.text) for l in result.lines], [
            ("them", "Estou?"),
            ("california", "Boa noite, aqui é a California, assistente do Miguel."),
            ("them", "Só temos às nove e meia."),
        ])
        self.assertEqual(self.speaker.written, [b"pcm1"])
        self.assertEqual(len(self.session.tool_responses), 1)
        self.assertTrue(self.capture.stopped and self.speaker.stopped)

    def test_interruption_drops_queued_audio(self):
        session = FakeSession([[audio(b"a"), interrupted()], *wrap_up()])
        self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(self.speaker.flushes, 1)

    def test_unknown_tool_is_answered_not_ignored(self):
        # record_outcome included: a model that remembers it gets an error, not a hang.
        for name in ("transfer_money", "record_outcome"):
            with self.subTest(name=name):
                session = FakeSession([[tool(name, amount=5)], *wrap_up()])
                self.agent(session).run("p", max_call_s=30, no_answer_s=30)
                self.assertEqual(self.session.tool_responses[0][0]["response"], {"error": "unknown tool"})

    def test_config_carries_prompt_voice_and_only_the_hang_up_tool(self):
        self.agent(FakeSession(wrap_up())).run("THE PROMPT", max_call_s=30, no_answer_s=30)
        config = self.client.config
        self.assertEqual(config.system_instruction, "THE PROMPT")
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, "Aoede")
        names = [f.name for f in config.tools[0].function_declarations]
        # record_outcome was removed on purpose: see TOOL_DECLARATIONS.
        self.assertEqual(names, ["end_call"])
        self.assertEqual(config.realtime_input_config.automatic_activity_detection.silence_duration_ms, 700)


class ToolReplySchedulingTests(_Base):
    """A tool reply must not interrupt her: refusals restarted her sentence (221 in 8 calls)."""

    def test_every_reply_is_silent_refused_hang_ups_included(self):
        session = FakeSession([
            [heard("Estou?")],
            [tool("end_call")],                    # too early -> SILENT
            [tool("transfer_money")],              # unknown -> SILENT
            [said("Olá, fala a California.")], [heard("Diga.")],
            [tool("end_call")],                    # allowed -> SILENT
        ])
        self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        schedules = [r[0]["scheduling"] for r in self.session.tool_responses]
        self.assertEqual(schedules, ["SILENT", "SILENT", "SILENT"])

    def test_the_sdk_accepts_the_reply_shape(self):
        from google.genai import types

        from services.gemini_live_call import _reply

        reply = _reply(NS(id="1", name="end_call"), {"ok": True}, "WHEN_IDLE")
        self.assertEqual(types.FunctionResponse(**reply).scheduling.value, "WHEN_IDLE")


class OpeningNudgeTests(_Base):
    """One call in three, she heard "Estou?" and never began (2026-10-08)."""

    def test_she_is_nudged_when_they_spoke_and_she_did_not(self):
        session = FakeSession([[heard("Estou?")]])
        self.agent(session).run("p", max_call_s=1.5, no_answer_s=30)
        self.assertEqual(len(self.session.nudges), glc._NUDGE_MAX)
        self.assertIn("answered", self.session.nudges[0])

    def test_no_nudge_once_she_has_spoken(self):
        session = FakeSession([[heard("Estou?")], [said("Boa noite!")]])
        self.agent(session).run("p", max_call_s=1.0, no_answer_s=30)
        self.assertEqual(self.session.nudges, [])

    def test_no_nudge_before_anyone_speaks(self):
        self.agent(FakeSession([])).run("p", max_call_s=1.0, no_answer_s=30)
        self.assertEqual(self.session.nudges, [])

    def test_a_failing_nudge_does_not_end_the_call(self):
        class Broken(FakeSession):
            async def send_client_content(self, **kw):
                raise RuntimeError("nope")

        result = self.agent(Broken([[heard("Estou?")]])).run("p", max_call_s=1.0, no_answer_s=30)
        self.assertEqual(result.ended_by, "max_duration")


class PrematureEndTests(_Base):
    """Seen live on Vertex: a hang-up 0.75s in, before a word was said."""

    def test_hang_up_before_she_speaks_is_refused(self):
        session = FakeSession([
            [heard("Estou?")],
            [tool("end_call")],
            [said("Boa noite, fala a California, assistente do Miguel.")],
            [heard("Diga.")],
            [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")
        self.assertIn("not spoken yet", self.session.tool_responses[0][0]["response"]["error"])

    def test_hang_up_before_anyone_is_heard_is_refused(self):
        session = FakeSession([[said("Olá?")], [tool("end_call")]])
        result = self.agent(session).run("p", max_call_s=0.1, no_answer_s=30)
        self.assertEqual(result.ended_by, "max_duration")
        self.assertIn("nobody has spoken", self.session.tool_responses[0][0]["response"]["error"])

    def test_voicemail_can_be_left_and_hung_up(self):
        # A recorded greeting counts as heard: a message left on voicemail ends the call.
        session = FakeSession([
            [heard("Deixe a sua mensagem.")], [said("Olá, fala a California, o Miguel liga depois.")],
            [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")

    def test_a_message_said_after_they_picked_up_can_end_the_call(self):
        # 2026-10-07 21:15: "Olá" -> her message + "Adeus" -> refused ten times
        # for want of a reply, dead air until they hung up.
        session = FakeSession([
            [heard("Olá.")], [said("Olá, aqui é a California. O Miguel pediu para dizer que a ama. Adeus.")],
            [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")



class MuteTests(_Base):
    def test_room_speech_reaches_the_agent_as_silence(self):
        sent = []

        class Recording(FakeSession):
            async def send_realtime_input(self, audio=None, **kw):
                sent.append(audio.data)

        self.agent(Recording(wrap_up())).run("p", max_call_s=30, no_answer_s=30, mute=lambda: True)
        self.assertTrue(sent)
        self.assertTrue(all(not any(block) for block in sent))

    def test_a_raising_mute_hook_does_not_silence_the_call(self):
        def boom():
            raise RuntimeError("audio gone")

        result = self.agent(FakeSession(wrap_up())).run("p", max_call_s=30, no_answer_s=30, mute=boom)
        self.assertEqual(result.ended_by, "end_call")


class EndingTests(_Base):
    def test_nobody_answers(self):
        result = self.agent().run("p", max_call_s=30, no_answer_s=0.05)
        self.assertEqual(result.ended_by, "no_answer")
        self.assertFalse(result.heard_them)

    def test_they_hang_up(self):
        session = FakeSession([[heard("Estou?")]])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30, still_connected=lambda: False)
        self.assertEqual(result.ended_by, "hung_up")

    def test_max_duration(self):
        session = FakeSession([[heard("Estou?")]])
        result = self.agent(session).run("p", max_call_s=0.05, no_answer_s=30)
        self.assertEqual(result.ended_by, "max_duration")

    def test_stop_event(self):
        stop = mock.Mock(is_set=mock.Mock(return_value=True))
        result = self.agent().run("p", max_call_s=30, no_answer_s=30, stop=stop)
        self.assertEqual(result.ended_by, "stopped")

    def test_dial_failure_ends_before_any_audio_is_sent(self):
        result = self.agent().run("p", max_call_s=30, no_answer_s=30, dial=lambda: False)
        self.assertEqual(result.ended_by, "dial_failed")
        self.assertEqual(self.session.sent_audio, 0)
        self.assertTrue(self.speaker.stopped)

    def test_a_dropped_session_ends_the_call_instead_of_leaving_it_silent(self):
        class Dropping(FakeSession):
            async def receive(self):
                raise ConnectionError("websocket closed")
                yield  # pragma: no cover

        result = self.agent(Dropping([])).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "error")
        self.assertIn("websocket closed", result.error)

    def test_goodbye_audio_after_end_call_is_still_played(self):
        session = FakeSession([*wrap_up(), [audio(b"goodbye")]])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")
        self.assertIn(b"goodbye", self.speaker.written)

    def test_connect_failure_comes_back_as_a_result(self):
        result = self.agent(fail=RuntimeError("401 bad key")).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "error")
        self.assertIn("401 bad key", result.error)
        self.assertTrue(self.speaker.stopped)


class TranscriptTests(unittest.TestCase):
    def test_transcript_labels_and_trims(self):
        result = glc.CallResult(lines=[glc.CallLine("them", "a" * 50), glc.CallLine("california", "b")])
        self.assertEqual(result.transcript(), "Them: " + "a" * 50 + "\nCalifornia: b")
        self.assertTrue(result.transcript(max_chars=10).startswith("..."))


if __name__ == "__main__":
    unittest.main()
