"""
Phase marks: when did each step of a wake or a playback happen.

`tools/bench_tv_power.py` records one timeline per run. The network half of
that timeline (TV ports, the box's 5555) is watched from outside; this module
is the half only the code can see -- Wake-on-LAN sent, box ready, deep link
fired, the one OK, playing.

`mark()` sits on the live paths, so outside a recording it is one lock and a
None check: no state, no allocation, nothing that could change behaviour.
Inside one, the FIRST occurrence of a name wins, in ms since the recording
started, from whichever thread got there.
"""

import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_active: dict[str, float] | None = None
_t0 = 0.0


def mark(name: str) -> None:
    with _lock:
        if _active is None or name in _active:
            return
        _active[name] = (time.monotonic() - _t0) * 1000


@contextmanager
def recording():
    global _active, _t0
    with _lock:
        if _active is not None:
            raise RuntimeError("a phase recording is already running")
        marks: dict[str, float] = {}
        _t0 = time.monotonic()
        _active = marks
    try:
        yield marks
    finally:
        with _lock:
            _active = None
