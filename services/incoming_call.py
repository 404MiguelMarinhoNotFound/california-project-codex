"""Incoming calls on Master Miguel's phone.

This module starts with the pure half: reading the phone's ring state out of
`dumpsys telephony.registry`. That dump prints one block per modem (two on his
phone, the second without a SIM reading -1 throughout), each carrying

    mPreciseCallState=Ringing call state: N, Foreground call state: N, ...
    mCallIncomingNumber=<number or empty>

`Ringing call state` is Android's PreciseCallState: 5 INCOMING, 6 WAITING (a
second call arriving during one), 0 idle.

`IncomingCallWatcher` is the live half: one long-running read-only adb shell
(RING_LOOP, a dumpsys a second) whose blocks it parses, queueing at most one
call for the orchestrator to announce.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

INCOMING = 5
WAITING = 6

_KINDS = {INCOMING: "incoming", WAITING: "waiting"}

_PRECISE = re.compile(r"mPreciseCallState=Ringing call state:\s*(-?\d+)")
_NUMBER = re.compile(r"mCallIncomingNumber=(.*)")


@dataclass(frozen=True)
class RingState:
    kind: str  # "incoming" | "waiting" | "none"
    number: str  # "" when the phone gave none


def parse_ring_state(text: str) -> RingState | None:
    """The phone's ring state, or None when `text` is not a readable dump.

    None means "could not tell" (adb failed, empty output) and is never
    "not ringing". Each mPreciseCallState line is paired with the next
    mCallIncomingNumber line before the following block starts; the first
    block ringing (incoming or waiting) wins.
    """
    states: list[int] = []
    numbers: list[str] = []
    awaiting_number = False
    for line in (text or "").splitlines():
        precise = _PRECISE.search(line)
        if precise:
            states.append(int(precise.group(1)))
            numbers.append("")
            awaiting_number = True
            continue
        number = _NUMBER.search(line)
        if number and awaiting_number:
            numbers[-1] = number.group(1).strip()
            awaiting_number = False
    if not states:
        return None
    for state, number in zip(states, numbers):
        kind = _KINDS.get(state)
        if kind:
            return RingState(kind, number)
    return RingState("none", "")


# One read-only shell for the life of the watcher: a block a second, each
# closed by "---", so a block is complete the moment its dump ends.
RING_LOOP = ("while true; do dumpsys telephony.registry | grep -E "
             "'mPreciseCallState|mCallIncomingNumber'; echo ---; sleep 1; done")
_SEPARATOR = "---"
# Blocks arrive every ~1.2-1.9 s (measured 2026-10-14). A shell silent this
# long is hung -- `adb shell` over TCP can stall with no output and no EOF
# when the phone drops off Wi-Fi -- so it is killed and the backoff restarts it.
STALL_S = 5.0
_WATCHDOG_POLL_S = 0.05


@dataclass(frozen=True)
class IncomingCall:
    kind: str  # "incoming" | "waiting"
    number: str  # "" when the phone gave none
    name: str  # from the contact book, "" when unknown
    started: float  # the watcher's clock when the ring was first read


class IncomingCallWatcher:
    """
    Watches the phone's ring state and queues the newest ring, one slot.

    A not-ringing -> ringing transition queues one IncomingCall; later
    blocks of the same ring queue nothing, and a newer ring replaces an
    older one still waiting. `pop()` hands the call over only while the
    latest reading still shows that same ring. EOF or an unreadable block
    kills the shell, sleeps the current backoff (5 s doubling to 60 s,
    back to 5 s after a stream that read anything) and reopens it. Nothing
    here raises into the caller; nothing ever spins without sleeping.
    Construction runs nothing.
    """

    def __init__(
        self,
        phone,
        resolve_name: Callable[[str], str] = lambda n: "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: tuple[float, float] = (5.0, 60.0),
    ):
        self._phone = phone
        self._resolve_name = resolve_name
        self._clock = clock
        self._sleep = sleep
        self._backoff_min, self._backoff_max = backoff_s
        self._lock = threading.Lock()
        self._pending: IncomingCall | None = None
        # The last readable state survives a restart, so a ring still going
        # when the stream comes back is not announced a second time.
        self._state = RingState("none", "")
        self._live = False  # the stream is up and its last block was readable
        self._last_block = 0.0  # clock() of the last readable block, for the watchdog
        self.thread: threading.Thread | None = None

    def start(self, stop: threading.Event) -> None:
        self.thread = threading.Thread(target=self.run, args=(stop,), name="incoming-call", daemon=True)
        self.thread.start()

    def ringing(self) -> bool:
        with self._lock:
            return self._live and self._state.kind != "none"

    def pop(self) -> IncomingCall | None:
        with self._lock:
            call, self._pending = self._pending, None
            if call is None or not self._live:
                return None
            if (self._state.kind, self._state.number) != (call.kind, call.number):
                return None
            return call

    # ------------------------------------------------------------- the loop

    def run(self, stop: threading.Event) -> None:
        """The watcher's loop, on `start()`'s thread; returns once `stop` is set."""
        backoff = self._backoff_min
        while not stop.is_set():
            try:
                read_any = self._watch(stop)
            except Exception as exc:  # never into the orchestrator
                logger.warning("Incoming-call watcher: %s", type(exc).__name__)
                read_any = False
            with self._lock:
                self._live = False
            if stop.is_set():
                break
            if read_any:
                backoff = self._backoff_min
            self._wait(stop, backoff)
            if not read_any:
                backoff = min(backoff * 2, self._backoff_max)

    def _wait(self, stop: threading.Event, seconds: float) -> None:
        if self._sleep is time.sleep:
            stop.wait(seconds)  # the real wait: a stop does not sit out a 60 s backoff
        else:
            self._sleep(seconds)

    def _watch(self, stop: threading.Event) -> bool:
        """One shell, read until EOF, an unreadable block, a stall or stop. True if any block read."""
        proc = self._phone.stream(RING_LOOP)
        if proc is None:
            return False
        read_any = False
        block: list[str] = []
        self._last_block = self._clock()
        done = threading.Event()
        watchdog = threading.Thread(target=self._watchdog, args=(proc, stop, done),
                                    name="incoming-call-watchdog", daemon=True)
        watchdog.start()
        try:
            for line in proc.stdout:
                if stop.is_set():
                    break
                if line.strip() != _SEPARATOR:
                    block.append(line)
                    continue
                state = parse_ring_state("".join(block))
                block = []
                if state is None:
                    logger.warning("Incoming-call watcher: unreadable block, restarting")
                    break
                read_any = True
                self._last_block = self._clock()
                self._read(state)
        finally:
            done.set()
            self._kill(proc)
            watchdog.join(1.0)
        return read_any

    def _watchdog(self, proc, stop: threading.Event, done: threading.Event) -> None:
        """Kill a shell that went silent or outlived `stop`; the kill ends its read with EOF."""
        while not done.wait(_WATCHDOG_POLL_S):
            stalled = self._clock() - self._last_block > STALL_S
            if stop.is_set() or stalled:
                if stalled and not stop.is_set():
                    logger.warning("Incoming-call watcher: no block for %.0f s, restarting", STALL_S)
                with self._lock:
                    self._live = False  # never a stale ring while the shell is dying
                self._kill(proc)
                return

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.kill()
        except Exception:
            pass

    def _read(self, state: RingState) -> None:
        name = ""
        with self._lock:
            before = self._state
            new_ring = state.kind != "none" and before.kind == "none"
            changed = state.kind != "none" and (state.kind, state.number) != (before.kind, before.number)
            pending = self._pending
        if new_ring or (changed and pending is not None):
            # The number can fill in a beat after the ring starts: a call still
            # waiting to be announced follows it rather than being dropped.
            name = self._name(state.number)
        with self._lock:
            if new_ring:
                self._pending = IncomingCall(state.kind, state.number, name, self._clock())
                logger.info("Incoming-call watcher: phone ringing (%s)", state.kind)
            elif changed and self._pending is not None:
                self._pending = IncomingCall(state.kind, state.number, name, self._pending.started)
            self._state = state
            self._live = True

    def _name(self, number: str) -> str:
        if not number:
            return ""
        try:
            return str(self._resolve_name(number) or "")
        except Exception:
            return ""
