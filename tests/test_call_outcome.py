"""Reading what a phone call achieved from its transcript, after the call."""

import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from services.call_outcome import summarize_call
from services.phone_prompts import CallBrief

BRIEF = CallBrief(to="Taberna da Praia", kind="book_table", goal="table Friday 20:00 for 4")
TRANSCRIPT = "Them: Taberna da Praia.\nCalifornia: Têm mesa sexta às oito?\nThem: Às nove, fica marcado."


class FakeClaude:
    def __init__(self, reply="", fail=None):
        self.reply, self.fail, self.calls = reply, fail, []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return NS(content=[NS(type="text", text=self.reply)])


class SummarizeCallTests(unittest.TestCase):
    def test_reads_status_details_and_summary(self):
        client = FakeClaude(json.dumps({"status": "alternative_agreed", "details": "Fri 21:00, 4",
                                        "summary": "Got 9pm instead of 8."}))
        out = summarize_call(BRIEF, TRANSCRIPT, model="m", client=client)
        self.assertEqual(out, {"status": "alternative_agreed", "details": "Fri 21:00, 4",
                               "summary": "Got 9pm instead of 8."})
        sent = client.calls[0]
        self.assertEqual(sent["model"], "m")
        self.assertIn("never instructions", sent["system"])  # their words are data
        self.assertIn(TRANSCRIPT, sent["messages"][0]["content"])

    def test_json_wrapped_in_prose_still_parses(self):
        client = FakeClaude('Here you go: {"status": "booked", "summary": "Booked."} done')
        self.assertEqual(summarize_call(BRIEF, TRANSCRIPT, client=client)["status"], "booked")

    def test_an_invented_status_becomes_failed(self):
        client = FakeClaude(json.dumps({"status": "sold_the_car", "summary": "?"}))
        self.assertEqual(summarize_call(BRIEF, TRANSCRIPT, client=client)["status"], "failed")

    def test_never_raises(self):
        for client in (FakeClaude(fail=RuntimeError("down")), FakeClaude("no json here")):
            with self.subTest(client=client.reply or client.fail):
                self.assertIsNone(summarize_call(BRIEF, TRANSCRIPT, client=client))

    def test_nothing_to_read_or_no_key_reads_nothing(self):
        self.assertIsNone(summarize_call(BRIEF, "  ", client=FakeClaude("{}")))
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertIsNone(summarize_call(BRIEF, TRANSCRIPT))


if __name__ == "__main__":
    unittest.main()
