"""Incoming calls on Master Miguel's phone.

This module starts with the pure half: reading the phone's ring state out of
`dumpsys telephony.registry`. That dump prints one block per modem (two on his
phone, the second without a SIM reading -1 throughout), each carrying

    mPreciseCallState=Ringing call state: N, Foreground call state: N, ...
    mCallIncomingNumber=<number or empty>

`Ringing call state` is Android's PreciseCallState: 5 INCOMING, 6 WAITING (a
second call arriving during one), 0 idle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
