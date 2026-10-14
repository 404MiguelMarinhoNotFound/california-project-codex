"""The phone's ring state, parsed from `dumpsys telephony.registry`.

Fixtures are invented and follow the probed layout (2026-10-13): one block
per modem, each with mCallState, mPreciseCallState and mCallIncomingNumber.
The second modem has no SIM and reads -1 throughout. Numbers are fake.
"""

import unittest

from services.incoming_call import INCOMING, WAITING, RingState, parse_ring_state


def _block(ringing: int, number: str = "", foreground: int = 0) -> str:
    return (
        "    mCallState=0\n"
        f"    mPreciseCallState=Ringing call state: {ringing}, Foreground call state: {foreground}, "
        "Background call state: 0, Disconnect cause: -1, Precise disconnect cause: -1\n"
        f"    mCallIncomingNumber={number}\n"
    )


def _dump(first: str, second: str) -> str:
    return "  Phone Id=0\n" + first + "  Phone Id=1\n" + second


NO_SIM = _block(-1, "")


class ParseRingStateTests(unittest.TestCase):
    def test_constants_are_the_precise_call_states(self):
        self.assertEqual(INCOMING, 5)
        self.assertEqual(WAITING, 6)

    def test_idle_is_none_kind(self):
        self.assertEqual(parse_ring_state(_dump(_block(0), NO_SIM)), RingState("none", ""))

    def test_incoming_with_number(self):
        text = _dump(_block(INCOMING, "+351910004567"), NO_SIM)
        self.assertEqual(parse_ring_state(text), RingState("incoming", "+351910004567"))

    def test_incoming_without_a_number_is_empty(self):
        self.assertEqual(parse_ring_state(_dump(_block(INCOMING), NO_SIM)), RingState("incoming", ""))

    def test_the_ringing_modem_can_be_the_second_block(self):
        text = _dump(_block(0, "+351910001111"), _block(INCOMING, "+351910002222"))
        self.assertEqual(parse_ring_state(text), RingState("incoming", "+351910002222"))

    def test_a_number_is_never_borrowed_from_another_block(self):
        # The ringing block has no number line of its own; the idle block's must not leak in.
        text = (
            "    mPreciseCallState=Ringing call state: 0, Foreground call state: 0\n"
            "    mCallIncomingNumber=+351910001111\n"
            "    mPreciseCallState=Ringing call state: 5, Foreground call state: 0\n"
        )
        self.assertEqual(parse_ring_state(text), RingState("incoming", ""))

    def test_waiting_is_its_own_kind(self):
        text = _dump(_block(WAITING, "+351910004567", foreground=1), NO_SIM)
        state = parse_ring_state(text)
        self.assertEqual(state.kind, "waiting")
        self.assertEqual(state.number, "+351910004567")

    def test_the_watcher_grep_output_parses(self):
        # The watcher greps only the two lines, with a separator between samples.
        text = (
            "---\n"
            "    mPreciseCallState=Ringing call state: 5, Foreground call state: 0, Background call state: 0\n"
            "    mCallIncomingNumber=+351910004567\n"
            "    mPreciseCallState=Ringing call state: -1, Foreground call state: -1, Background call state: -1\n"
            "    mCallIncomingNumber=\n"
        )
        self.assertEqual(parse_ring_state(text), RingState("incoming", "+351910004567"))

    def test_unreadable_text_is_none(self):
        self.assertIsNone(parse_ring_state(""))
        self.assertIsNone(parse_ring_state("error: closed"))
        self.assertIsNone(parse_ring_state("    mCallIncomingNumber=+351910004567\n"))

    def test_crlf_output_parses(self):
        text = _dump(_block(INCOMING, "+351910004567"), NO_SIM).replace("\n", "\r\n")
        self.assertEqual(parse_ring_state(text), RingState("incoming", "+351910004567"))

    def test_ring_state_has_no_truthiness_trap(self):
        # "none" is a reading, not a failure; only None means unreadable.
        self.assertTrue(RingState("none", ""))


if __name__ == "__main__":
    unittest.main()
