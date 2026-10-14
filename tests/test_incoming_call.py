"""The phone's ring state, parsed from `dumpsys telephony.registry`.

Fixtures are invented and follow the probed layout (2026-10-13): one block
per modem, each with mCallState, mPreciseCallState and mCallIncomingNumber.
The second modem has no SIM and reads -1 throughout. Numbers are fake.
"""

import threading
import time
import unittest

from services.incoming_call import (INCOMING, RING_LOOP, WAITING, IncomingCall, IncomingCallWatcher,
                                    RingState, parse_ring_state)


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


# --------------------------------------------------------------- the watcher

RINGER = "+351910004567"  # invented


def _lines(block: str) -> list:
    """One loop iteration as the phone prints it: the separator, then grep's lines."""
    return ["---\n"] + block.splitlines(keepends=True)


IDLE = _lines(_dump(_block(0), NO_SIM))
RINGING = _lines(_dump(_block(INCOMING, RINGER), NO_SIM))


class FakeProc:
    """Yields scripted stdout; a callable in the script runs when it is reached."""

    def __init__(self, script):
        self.killed = False
        self.stdout = self._read(script)

    def _read(self, script):
        for item in script:
            if callable(item):
                item()
            else:
                yield item

    def kill(self):
        self.killed = True


class FakePhone:
    """Each stream() hands out the next scripted process; when they run out, stop."""

    def __init__(self, scripts, stop):
        self.scripts = list(scripts)
        self.stop = stop
        self.commands: list[str] = []
        self.procs: list[FakeProc] = []

    def stream(self, command):
        self.commands.append(command)
        if not self.scripts:
            self.stop.set()
            return None
        script = self.scripts.pop(0)
        if script is None:
            return None
        self.procs.append(FakeProc(script))
        return self.procs[-1]


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.waits: list[float] = []
        self.seen: list = []

    def watcher(self, scripts, resolve_name=lambda n: ""):
        self.phone = FakePhone(scripts, self.stop)
        return IncomingCallWatcher(self.phone, resolve_name=resolve_name, clock=lambda: 42.0,
                                   sleep=self.waits.append)

    def test_construction_runs_nothing(self):
        w = self.watcher([])
        self.assertEqual(self.phone.commands, [])
        self.assertFalse(w.ringing())
        self.assertIsNone(w.pop())

    def test_the_stream_runs_the_ring_loop(self):
        w = self.watcher([IDLE + ["---\n"]])
        w.run(self.stop)
        self.assertEqual(self.phone.commands[0], RING_LOOP)
        self.assertEqual(RING_LOOP, "while true; do echo ---; dumpsys telephony.registry | grep -E "
                                    "'mPreciseCallState|mCallIncomingNumber'; sleep 1; done")

    def test_a_new_ring_is_queued_once(self):
        w = None

        def look():
            self.seen.append((w.ringing(), w.pop(), w.pop()))

        w = self.watcher([IDLE + RINGING + RINGING + RINGING + [look] + IDLE + ["---\n"]])
        w.run(self.stop)
        ringing, first, second = self.seen[0]
        self.assertTrue(ringing)
        self.assertEqual(first, IncomingCall("incoming", RINGER, "", 42.0))
        self.assertIsNone(second)  # three blocks of the same ring queued one call
        self.assertFalse(w.ringing())

    def test_a_ring_that_ended_is_not_announced(self):
        w = None
        w = self.watcher([IDLE + RINGING + IDLE + ["---\n", lambda: self.seen.append(w.pop())]])
        w.run(self.stop)
        self.assertEqual(self.seen, [None])

    def test_a_newer_ring_replaces_an_older_one(self):
        other = "+351910009999"  # invented
        w = None
        w = self.watcher([RINGING + IDLE + _lines(_dump(_block(WAITING, other), NO_SIM))
                          + ["---\n", lambda: self.seen.append(w.pop())]])
        w.run(self.stop)
        self.assertEqual(self.seen, [IncomingCall("waiting", other, "", 42.0)])

    def test_a_number_that_arrives_late_follows_the_queued_call(self):
        w = None
        w = self.watcher([IDLE + _lines(_dump(_block(INCOMING, ""), NO_SIM)) + RINGING
                          + ["---\n", lambda: self.seen.append(w.pop())]],
                         resolve_name=lambda n: "Ana Exemplo" if n == RINGER else "")
        w.run(self.stop)
        self.assertEqual(self.seen, [IncomingCall("incoming", RINGER, "Ana Exemplo", 42.0)])

    def test_the_name_comes_from_the_book(self):
        w = None
        w = self.watcher([RINGING + ["---\n", lambda: self.seen.append(w.pop())]],
                         resolve_name=lambda n: "Ana Exemplo" if n == RINGER else "")
        w.run(self.stop)
        self.assertEqual(self.seen[0].name, "Ana Exemplo")

    def test_a_failing_resolver_gives_no_name(self):
        def boom(number):
            raise RuntimeError("book unreadable")

        w = None
        w = self.watcher([RINGING + ["---\n", lambda: self.seen.append(w.pop())]], resolve_name=boom)
        w.run(self.stop)
        self.assertEqual(self.seen[0], IncomingCall("incoming", RINGER, "", 42.0))

    def test_a_dead_stream_restarts_with_backoff(self):
        w = self.watcher([[], [], [], IDLE + ["---\n"], []])
        w.run(self.stop)
        self.assertEqual(self.waits, [5.0, 10.0, 20.0, 5.0, 5.0])
        self.assertTrue(all(p.killed for p in self.phone.procs))

    def test_the_backoff_is_capped(self):
        w = self.watcher([[]] * 7)
        w.run(self.stop)
        self.assertEqual(self.waits, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0])

    def test_an_unreadable_block_restarts_the_stream(self):
        w = self.watcher([["---\n", "error: device offline\n", "---\n"] + RINGING + ["---\n"]])
        with self.assertLogs("services.incoming_call", "WARNING"):
            w.run(self.stop)
        self.assertTrue(self.phone.procs[0].killed)
        self.assertEqual(self.waits, [5.0])
        self.assertIsNone(w.pop())  # the ring after the bad block was never read

    def test_no_phone_waits_instead_of_spinning(self):
        w = self.watcher([None, None])
        w.run(self.stop)
        self.assertEqual(self.waits, [5.0, 10.0])
        self.assertEqual(len(self.phone.commands), 3)

    def test_a_raising_stream_never_escapes(self):
        class Raising:
            def stream(self, command):
                raise OSError("adb gone")

        waits = []

        def sleep(s):
            waits.append(s)
            if len(waits) == 2:
                self.stop.set()

        with self.assertLogs("services.incoming_call", "WARNING"):
            IncomingCallWatcher(Raising(), sleep=sleep).run(self.stop)
        self.assertEqual(waits, [5.0, 10.0])

    def test_a_stream_restart_mid_ring_does_not_announce_it_twice(self):
        w = None
        w = self.watcher([RINGING + ["---\n", lambda: self.seen.append(w.pop())],
                          RINGING + ["---\n", lambda: self.seen.append(w.pop())]])
        w.run(self.stop)
        self.assertIsNotNone(self.seen[0])
        self.assertIsNone(self.seen[1])

    def test_nothing_is_ringing_while_the_stream_is_down(self):
        w = None
        w = self.watcher([RINGING + ["---\n"], None])
        w.run(self.stop)
        self.assertFalse(w.ringing())
        self.assertIsNone(w.pop())

    def test_stop_ends_the_thread(self):
        stop = threading.Event()

        class Endless:
            def __init__(self):
                self.killed = False
                self.stdout = self._read()

            def _read(self):
                while True:
                    for line in IDLE:
                        yield line
                    time.sleep(0.01)

            def kill(self):
                self.killed = True

        proc = Endless()

        class Phone:
            def stream(self, command):
                return proc

        watcher = IncomingCallWatcher(Phone(), sleep=lambda s: None)
        watcher.start(stop)
        time.sleep(0.1)
        stop.set()
        watcher.thread.join(2.0)
        self.assertFalse(watcher.thread.is_alive())
        self.assertTrue(watcher.thread.daemon)
        self.assertEqual(watcher.thread.name, "incoming-call")
        self.assertTrue(proc.killed)
