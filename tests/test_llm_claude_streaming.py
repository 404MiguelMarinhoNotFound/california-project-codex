"""
Claude is the default provider, so its streaming behaviour is what the whole
"speak the first sentence while the model is still writing" design rests on.
It used to call the non-streaming messages.create(), which meant nothing
reached TTS until the last token was generated.

These tests pin two things that were broken together in that path:
  - text leaves the generator as it arrives, not in one block at the end
  - a response carrying several tool_use blocks builds a valid next request
"""

import unittest
import unittest.mock as mock

from services.llm import LLMService


class _Block:
    """Stands in for an Anthropic content block."""

    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class _FinalMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    """
    Mimics the context manager returned by client.messages.stream().

    `events` records the interleaving of deltas and the final-message fetch so a
    test can prove text was handed over before the response was complete.
    """

    def __init__(self, deltas, content, stop_reason, events):
        self._deltas = deltas
        self._content = content
        self._stop_reason = stop_reason
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for delta in self._deltas:
            self._events.append(("delta", delta))
            yield delta

    def get_final_message(self):
        self._events.append(("final", None))
        return _FinalMessage(self._content, self._stop_reason)


class _FakeMessages:
    def __init__(self, turns, events):
        self._turns = list(turns)
        self._events = events
        self.calls = []          # the `messages` payload sent on each request

    def stream(self, **kwargs):
        self.calls.append([dict(m) for m in kwargs["messages"]])
        deltas, content, stop_reason = self._turns.pop(0)
        return _FakeStream(deltas, content, stop_reason, self._events)


class _FakeClient:
    def __init__(self, turns, events):
        self.messages = _FakeMessages(turns, events)


def _config() -> dict:
    return {
        "llm": {
            "provider": "claude",
            "system_prompt": "BASE PROMPT",
            "conversation_history_size": 6,
            "claude": {"model": "m", "max_tokens": 100, "web_search": False},
        },
        "media": {"enabled": True},
        "govee": {"enabled": True, "default_light": "attic",
                  "lights": {"attic": {"mac": "AA"}}},
    }


class ClaudeStreamingTests(unittest.TestCase):

    def _service(self, turns, events=None):
        events = events if events is not None else []
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}), \
             mock.patch("anthropic.Anthropic"):
            svc = LLMService(_config())
        svc.client = _FakeClient(turns, events)
        return svc, events

    # --- streaming ---------------------------------------------------------

    def test_text_is_yielded_delta_by_delta(self):
        turns = [(["Sure", " thing", ", Master Miguel."], [], "end_turn")]
        svc, _ = self._service(turns)
        chunks = list(svc.stream_response("hey"))
        self.assertEqual(chunks, ["Sure", " thing", ", Master Miguel."])

    def test_a_delta_reaches_the_caller_before_the_response_is_complete(self):
        """
        The whole point of the fix: the first chunk must be usable while the
        model is still writing. If this yields only at the end, the sentence
        chunker and TTS overlap buy nothing on the default provider.
        """
        turns = [(["first ", "second"], [], "end_turn")]
        svc, events = self._service(turns)

        gen = svc.stream_response("hey")
        first = next(gen)

        self.assertEqual(first, "first ")
        self.assertNotIn(("final", None), events)   # response not finished yet
        list(gen)                                   # drain

    def test_full_text_is_accumulated_into_history(self):
        turns = [(["a", "b", "c"], [], "end_turn")]
        svc, _ = self._service(turns)
        list(svc.stream_response("hey"))
        self.assertEqual(svc.history[-1], {"role": "assistant", "content": "abc"})

    def test_text_is_streamed_before_any_tool_runs(self):
        """Speech should start while the tool call is still to be dispatched."""
        order = []
        tool_block = _Block("tool_use", name="control_lights",
                            input={"action": "light_on"}, id="t1")
        turns = [
            (["On it. "], [_Block("text", text="On it. "), tool_block], "tool_use"),
            (["Done."], [], "end_turn"),
        ]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: order.append("tool") or "ok"

        for chunk in svc.stream_response("lights on"):
            order.append(f"yield:{chunk}")

        self.assertEqual(order[0], "yield:On it. ")
        self.assertIn("tool", order)
        self.assertLess(order.index("yield:On it. "), order.index("tool"))

    # --- tool loop ---------------------------------------------------------

    def test_two_tool_calls_in_one_turn_build_one_assistant_and_one_user_message(self):
        """
        "Turn the lights on and play my show" can come back as two tool_use
        blocks. The old per-block append resent the assistant message twice and
        split the results across two user messages, which the API rejects.
        """
        content = [
            _Block("tool_use", name="control_lights",
                   input={"action": "light_on"}, id="t1"),
            _Block("tool_use", name="control_tv",
                   input={"action": "stremio_continue"}, id="t2"),
        ]
        turns = [([], content, "tool_use"), (["All set."], [], "end_turn")]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: f"{name} ok"

        list(svc.stream_response("lights on and play my show"))

        second_request = svc.client.messages.calls[1]
        # user turn, assistant tool_use, single user tool_result message
        self.assertEqual(len(second_request), 3)
        self.assertEqual(second_request[1]["role"], "assistant")
        self.assertEqual(second_request[2]["role"], "user")

        results = second_request[2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])
        self.assertEqual([r["content"] for r in results],
                         ["control_lights ok", "control_tv ok"])

    def test_single_tool_call_still_round_trips(self):
        content = [_Block("tool_use", name="control_tv",
                          input={"action": "play_pause"}, id="t1")]
        turns = [([], content, "tool_use"), (["Paused."], [], "end_turn")]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: "paused"

        chunks = list(svc.stream_response("pause"))

        self.assertEqual(chunks, ["Paused."])
        results = svc.client.messages.calls[1][2]["content"]
        self.assertEqual(results, [{"type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": "paused"}])

    def test_missing_tool_handler_answers_rather_than_dropping_the_block(self):
        content = [_Block("tool_use", name="control_tv",
                          input={"action": "play_pause"}, id="t1")]
        turns = [([], content, "tool_use"), (["ok"], [], "end_turn")]
        svc, _ = self._service(turns)
        svc.tool_handler = None

        list(svc.stream_response("pause"))

        results = svc.client.messages.calls[1][2]["content"]
        self.assertEqual(results[0]["content"], "tool not available")

    def test_an_unknown_tool_is_answered_instead_of_looping_forever(self):
        """
        An unanswered tool_use block makes the next request invalid, and the
        old code would have resent an identical payload every pass.
        """
        content = [_Block("tool_use", name="control_fridge", input={}, id="t1")]
        turns = [([], content, "tool_use"), (["Can't do that."], [], "end_turn")]
        svc, _ = self._service(turns)

        chunks = list(svc.stream_response("chill the beers"))

        self.assertEqual(chunks, ["Can't do that."])
        results = svc.client.messages.calls[1][2]["content"]
        self.assertEqual(results[0]["content"], "tool not available")

    def test_tool_use_stop_reason_with_nothing_to_answer_terminates(self):
        """Guards the loop when stop_reason says tool_use but no block asks."""
        turns = [(["hm"], [_Block("text", text="hm")], "tool_use")]
        svc, _ = self._service(turns)

        chunks = list(svc.stream_response("hey"))

        self.assertEqual(chunks, ["hm"])
        self.assertEqual(len(svc.client.messages.calls), 1)

    def test_web_search_blocks_do_not_count_as_local_tool_calls(self):
        """Server-side web search is resolved at Anthropic, not dispatched here."""
        content = [
            _Block("server_tool_use", name="web_search", input={}, id="s1"),
            _Block("web_search_tool_result", id="s1"),
            _Block("text", text="It's 22 degrees."),
        ]
        turns = [(["It's 22 degrees."], content, "end_turn")]
        svc, _ = self._service(turns)
        svc.tool_handler = mock.Mock()

        chunks = list(svc.stream_response("weather?"))

        self.assertEqual(chunks, ["It's 22 degrees."])
        svc.tool_handler.assert_not_called()
        self.assertEqual(len(svc.client.messages.calls), 1)


if __name__ == "__main__":
    unittest.main()
