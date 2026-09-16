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
from services.sentence_chunker import TOOL_BOUNDARY
from tests.config_fixture import config_for_tests


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
    """The real config.yaml with web search off and a sentinel prompt.

    web_search is true in the shipped config and runs server-side at Anthropic,
    so it is never dispatched locally; switching it off here keeps the tool loop
    under test to the two local tools. The model name and max_tokens come from
    the file -- the fake client ignores them, but a renamed llm.claude key
    should still fail here.
    """
    return config_for_tests(
        llm={"system_prompt": "BASE PROMPT", "claude": {"web_search": False}},
    )


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

    def test_tool_boundary_is_yielded_between_the_preamble_and_the_dispatch(self):
        """
        The chunker holds a short preamble until it is worth synthesizing, and
        the dispatch blocks inside this generator, so "On it." would play after
        a 47s wake. The marker is what lets the chunker ship it first.
        """
        order = []
        tool_block = _Block("tool_use", name="control_tv",
                            input={"action": "turn_on"}, id="t1")
        turns = [
            (["On it. "], [_Block("text", text="On it. "), tool_block], "tool_use"),
            (["TV's on."], [], "end_turn"),
        ]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: order.append("tool") or "TV is on"

        for chunk in svc.stream_response("turn on the tv"):
            order.append("boundary" if chunk is TOOL_BOUNDARY else f"yield:{chunk}")

        self.assertEqual(order, ["yield:On it. ", "boundary", "tool", "yield:TV's on."])

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

        self.assertEqual(chunks, [TOOL_BOUNDARY, "Paused."])
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

    # --- history -----------------------------------------------------------
    #
    # History used to keep only the spoken text. After two turns whose
    # tool_use/tool_result blocks had been stripped, the model's own context
    # showed device actions apparently done by announcing them, and it began
    # doing exactly that: "Sending him back to dock now. Done." with no
    # vacuum_dock in the log, "Your attic lights are off now" with no
    # control_lights call. The round has to survive into the next turn.

    def test_a_tool_round_is_kept_in_history_not_just_its_spoken_text(self):
        tool_block = _Block("tool_use", name="control_vacuum",
                            input={"action": "vacuum_dock"}, id="t1")
        first_content = [_Block("text", text="On it. "), tool_block]
        turns = [
            (["On it. "], first_content, "tool_use"),
            (["He's heading home."], [_Block("text", text="He's heading home.")], "end_turn"),
        ]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: "Sending the vacuum home."

        list(svc.stream_response("dock him"))

        self.assertEqual(
            svc.history,
            [
                {"role": "user", "content": "dock him"},
                {"role": "assistant", "content": first_content},
                {"role": "user", "content": [{"type": "tool_result",
                                              "tool_use_id": "t1",
                                              "content": "Sending the vacuum home."}]},
                {"role": "assistant", "content": "He's heading home."},
            ],
        )

    def test_the_final_message_is_only_the_last_generation_not_the_whole_turn(self):
        """The preamble already sits in the tool_use message's text block."""
        tool_block = _Block("tool_use", name="control_lights",
                            input={"action": "light_off"}, id="t1")
        turns = [
            (["Okay, on it. "], [_Block("text", text="Okay, on it. "), tool_block], "tool_use"),
            (["Attic's off."], [], "end_turn"),
        ]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: "off"

        list(svc.stream_response("lights off"))

        self.assertEqual(svc.history[-1], {"role": "assistant", "content": "Attic's off."})
        self.assertNotIn("Okay, on it. Attic's off.", str(svc.history[-1]))

    def test_the_next_turn_sends_the_previous_round_to_the_model(self):
        tool_block = _Block("tool_use", name="control_vacuum",
                            input={"action": "vacuum_status"}, id="t1")
        turns = [
            ([], [tool_block], "tool_use"),
            (["Docked."], [], "end_turn"),
            (["Sure."], [], "end_turn"),
        ]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: "docked, 100%"

        list(svc.stream_response("where is he"))
        list(svc.stream_response("ok dock him"))

        roles = [(m["role"], type(m["content"]).__name__) for m in svc.client.messages.calls[2]]
        self.assertEqual(roles, [("user", "str"), ("assistant", "list"), ("user", "list"),
                                 ("assistant", "str"), ("user", "str")])

    def test_an_empty_final_generation_is_not_stored_as_an_empty_message(self):
        """An empty text block is an invalid message on the next request."""
        tool_block = _Block("tool_use", name="control_tv",
                            input={"action": "play_pause"}, id="t1")
        turns = [([], [tool_block], "tool_use"), ([], [], "end_turn")]
        svc, _ = self._service(turns)
        svc.tool_handler = lambda name, args: "paused"

        list(svc.stream_response("pause"))

        self.assertEqual(svc.history[-1]["role"], "user")
        self.assertEqual(svc.history[-1]["content"][0]["type"], "tool_result")

    def test_trimming_drops_whole_exchanges_and_never_orphans_a_tool_result(self):
        """
        With tool rounds in history an exchange is no longer two messages.
        A count-based trim would cut a round in half and put a tool_result
        first, which the API rejects. The test drives more turns than the
        configured window and checks the front of history is always a spoken
        user message.
        """
        def tool_turn():
            block = _Block("tool_use", name="control_lights",
                           input={"action": "light_on"}, id="t")
            return [([], [block], "tool_use"), (["ok"], [], "end_turn")]

        turns = []
        for _ in range(8):
            turns += tool_turn()
        svc, _ = self._service(turns)
        svc.max_history = 2
        svc.tool_handler = lambda name, args: "on"

        for i in range(8):
            list(svc.stream_response(f"turn {i}"))

        starts = [m for m in svc.history if m["role"] == "user" and isinstance(m["content"], str)]
        self.assertEqual([m["content"] for m in starts], ["turn 6", "turn 7"])
        self.assertEqual(svc.history[0], {"role": "user", "content": "turn 6"})
        self.assertEqual(len(svc.history), 8)   # 2 exchanges x 4 messages

    # --- interruption ------------------------------------------------------

    def test_closing_the_stream_mid_reply_keeps_the_partial_answer(self):
        """
        A barge-in closes the generator before it finishes. History must still
        end on an assistant message, or the next request opens with two user
        turns in a row.
        """
        svc, _ = self._service([
            (["The lights ", "are on. ", "Anything else ", "tonight?"], [], "end_turn"),
        ])

        gen = svc.stream_response("lights on")
        self.assertEqual(next(gen), "The lights ")
        self.assertEqual(next(gen), "are on. ")
        gen.close()

        self.assertEqual(svc.history[-1]["role"], "assistant")
        self.assertEqual(svc.history[-1]["content"], "The lights are on.")
        self.assertEqual(svc.history[-2], {"role": "user", "content": "lights on"})

    def test_closing_before_any_text_stores_no_empty_assistant_turn(self):
        svc, _ = self._service([(["", "hello"], [], "end_turn")])
        gen = svc.stream_response("hi")
        self.assertEqual(next(gen), "")
        gen.close()
        self.assertEqual(svc.history[-1], {"role": "user", "content": "hi"})


if __name__ == "__main__":
    unittest.main()
