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
    """A realistic close: she spoke, they answered, outcome, then hang up."""
    return [
        [said("Boa noite, fala a California.")],
        [heard("Sim, diga.")],
        [tool("record_outcome", status="answered", summary="ok")],
        [tool("end_call")],
    ]


class FakeSession:
    """receive() yields one scripted batch per call, then waits forever."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.sent_audio = 0
        self.tool_responses = []

    async def send_realtime_input(self, audio=None, **kw):
        self.sent_audio += 1

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(function_responses)

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
        for name, value in (("_WATCH_INTERVAL_S", 0.02), ("_GOODBYE_GRACE_S", 0.05)):
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
            [tool("record_outcome", status="alternative_agreed", details="sexta 21:30, 4", summary="Got 21:30 on Friday.")],
            [tool("end_call", reason="done")],
        ])
        result = self.agent(session).run("prompt", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")
        self.assertEqual(result.outcome["status"], "alternative_agreed")
        self.assertEqual(result.outcome["summary"], "Got 21:30 on Friday.")
        self.assertEqual([(l.who, l.text) for l in result.lines], [
            ("them", "Estou?"),
            ("california", "Boa noite, aqui é a California, assistente do Miguel."),
            ("them", "Só temos às nove e meia."),
        ])
        self.assertEqual(self.speaker.written, [b"pcm1"])
        self.assertEqual(len(self.session.tool_responses), 2)
        self.assertTrue(self.capture.stopped and self.speaker.stopped)

    def test_interruption_drops_queued_audio(self):
        session = FakeSession([[audio(b"a"), interrupted()], *wrap_up()])
        self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(self.speaker.flushes, 1)

    def test_an_invented_status_is_recorded_as_failed(self):
        session = FakeSession([[said("Olá.")], [heard("Sim?")], [tool("record_outcome", status="sold_the_car", summary="x")], [tool("end_call")]])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.outcome["status"], "failed")

    def test_unknown_tool_is_answered_not_ignored(self):
        session = FakeSession([[tool("transfer_money", amount=5)], *wrap_up()])
        self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(self.session.tool_responses[0][0]["response"], {"error": "unknown tool"})

    def test_config_carries_prompt_voice_and_tools(self):
        self.agent(FakeSession(wrap_up())).run("THE PROMPT", max_call_s=30, no_answer_s=30)
        config = self.client.config
        self.assertEqual(config.system_instruction, "THE PROMPT")
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, "Aoede")
        names = [f.name for f in config.tools[0].function_declarations]
        self.assertEqual(names, ["record_outcome", "end_call"])
        self.assertEqual(config.realtime_input_config.automatic_activity_detection.silence_duration_ms, 700)


class PrematureEndTests(_Base):
    """Seen live on Vertex: record_outcome + end_call 0.75s in, before a word was said."""

    def test_outcome_and_hang_up_before_she_speaks_are_refused(self):
        session = FakeSession([
            [heard("Estou?")],
            [tool("record_outcome", status="booked", summary="booked")],
            [tool("end_call")],
            [said("Boa noite, fala a California, assistente do Miguel.")],
            [heard("Diga.")],
            [tool("record_outcome", status="answered", summary="talked")],
            [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual(result.ended_by, "end_call")
        self.assertEqual(result.outcome["status"], "answered")
        early = self.session.tool_responses[0][0]["response"]
        self.assertIn("not spoken yet", early["error"])
        self.assertIn("no outcome", self.session.tool_responses[1][0]["response"]["error"])

    def test_outcome_waits_for_their_reply(self):
        session = FakeSession([
            [heard("Estou?")], [said("Olá, fala a California.")],
            [tool("record_outcome", status="booked", summary="x")],
        ])
        result = self.agent(session).run("p", max_call_s=0.1, no_answer_s=30)
        self.assertIsNone(result.outcome)
        self.assertIn("not answered you yet", self.session.tool_responses[0][0]["response"]["error"])

    def test_voicemail_needs_no_reply(self):
        session = FakeSession([
            [heard("Deixe a sua mensagem.")], [said("Olá, fala a California, o Miguel liga depois.")],
            [tool("record_outcome", status="voicemail", summary="left a message")], [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual((result.ended_by, result.outcome["status"]), ("end_call", "voicemail"))


    def test_a_message_said_after_they_picked_up_counts_as_delivered(self):
        # 2026-10-07 21:15: "Olá" -> her message + "Adeus" -> record_outcome
        # refused ten times for want of a reply, dead air until they hung up.
        session = FakeSession([
            [heard("Olá.")], [said("Olá, aqui é a California. O Miguel pediu para dizer que a ama. Adeus.")],
            [tool("record_outcome", status="message_delivered", summary="told her")], [tool("end_call")],
        ])
        result = self.agent(session).run("p", max_call_s=30, no_answer_s=30)
        self.assertEqual((result.ended_by, result.outcome["status"]), ("end_call", "message_delivered"))

    def test_a_booking_still_needs_their_reply(self):
        session = FakeSession([
            [heard("Estou?")], [said("Queria uma mesa.")],
            [tool("record_outcome", status="booked", summary="x")],
        ])
        result = self.agent(session).run("p", max_call_s=0.1, no_answer_s=30)
        self.assertIsNone(result.outcome)


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
