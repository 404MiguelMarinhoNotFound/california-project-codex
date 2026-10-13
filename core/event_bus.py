"""
Live events for the UI: what she is doing, published the moment it happens.

Everything worth watching used to reach the outside only after the fact --
`turns.jsonl` is written when a turn ends, the state was a `print` with a
carriage return, the wake score a log line after each reply. `EventBus` is the
one place those moments are published to as they occur, from whichever thread
they occur on, and `core/live_ui.py` streams it to a browser.

Two rules, both load-bearing:

- **It can never slow a turn down.** `publish` is a lock, a deque append and a
  `put_nowait` per subscriber. A subscriber that falls behind (a phone on bad
  Wi-Fi) loses events rather than holding anyone up; it sees the gap in `id`.
- **It never raises.** Same contract as `core/turn_log.py`: a broken UI must
  not be able to take a turn down with it.

The ring buffer is the current session only, in memory. It exists so a page
opened mid-turn is not blank, not to make anything historical.
"""

import itertools
import logging
import queue
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

# Per-subscriber backlog before events start being dropped for that client.
_SUBSCRIBER_QUEUE = 1000

# Streamed live but never replayed. The wake score arrives ten times a second;
# kept in the ring it would push every turn out of a 500-event replay within a
# minute, and a page opened mid-session would show an hour of history as
# nothing but the last 50 seconds of room tone.
# The halo's inputs are the same shape of problem: a replayed clip would
# animate audio that finished long ago, and the mic level arrives 15 a second.
_LIVE_ONLY = frozenset({"wake", "ping", "clip_start", "clip_end", "mic"})
# Logs replay from their own ring, so a chatty minute cannot evict turns either.
_LOG_KINDS = frozenset({"log"})
_LOG_RING = 300


class EventBus:
    def __init__(self, ring_size: int = 500):
        self._lock = threading.Lock()
        self._ring: deque[dict] = deque(maxlen=max(1, int(ring_size)))
        self._log_ring: deque[dict] = deque(maxlen=_LOG_RING)
        self._subscribers: list[queue.Queue] = []
        self._ids = itertools.count(1)

    def publish(self, kind: str, data: dict | None = None) -> None:
        try:
            with self._lock:
                event = {
                    "id": next(self._ids),
                    "kind": kind,
                    "ts": time.time(),
                    "data": data or {},
                }
                if kind in _LOG_KINDS:
                    self._log_ring.append(event)
                elif kind not in _LIVE_ONLY:
                    self._ring.append(event)
                subscribers = list(self._subscribers)
            for q in subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # that client is behind; it sees the id gap
        except Exception:
            # Not logger.exception: the log handler publishes too, and a
            # failing bus must not recurse through it.
            pass

    def subscribe(self, last_id: int | None = None) -> tuple[list[dict], queue.Queue]:
        """
        The events this client has not seen yet, and a queue for the rest.

        Taken under one lock, so nothing published in between is lost or
        delivered twice.
        """
        q: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_QUEUE)
        with self._lock:
            backlog = sorted(list(self._ring) + list(self._log_ring), key=lambda e: e["id"])
            if last_id is not None:
                backlog = [e for e in backlog if e["id"] > last_id]
            self._subscribers.append(q)
        return backlog, q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class _NullBus:
    """Stand-in when no UI is running, so call sites never branch."""

    def publish(self, kind, data=None):
        pass

    def subscribe(self, last_id=None):
        return [], queue.Queue()

    def unsubscribe(self, q):
        pass

    subscriber_count = 0


NULL_BUS = _NullBus()


class BusLogHandler(logging.Handler):
    """Publishes log records at INFO and above as `log` events."""

    def __init__(self, bus, level=logging.INFO):
        super().__init__(level)
        self._bus = bus
        # A record logged from inside publish would come straight back here.
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            self._bus.publish("log", {
                "level": record.levelname,
                "logger": record.name,
                "thread": record.threadName,
                "message": record.getMessage()[:2000],
            })
        except Exception:
            pass
        finally:
            self._local.busy = False


class PeakThrottle:
    """
    Publish one number as `kind`, throttled to `hz`, keeping the peak in between.

    A phone does not need every frame, but a one-frame spike is exactly the
    frame worth seeing, so each publish carries the maximum since the last one
    rather than a sample. `offer` never raises.
    """

    def __init__(self, bus, kind: str, key: str, hz: float = 10.0, clock=time.monotonic, digits: int = 4):
        self._bus = bus
        self._kind = kind
        self._key = key
        self._interval = 1.0 / hz if hz and hz > 0 else 0.0
        self._clock = clock
        self._digits = digits
        self._peak = 0.0
        self._last = 0.0

    def offer(self, value: float, **extra) -> None:
        try:
            value = float(value or 0.0)
            self._peak = max(self._peak, value)
            now = self._clock()
            if now - self._last < self._interval:
                return
            self._last = now
            peak, self._peak = self._peak, 0.0
            self._bus.publish(self._kind, {self._key: round(peak, self._digits), **extra})
        except Exception:
            pass


class WakeScorePublisher(PeakThrottle):
    """
    The live wake score, throttled to `hz`, keeping the peak in between.

    The detector scores every 80ms; a one-frame spike to 0.9 is the one that
    matters, hence the peak.
    """

    def __init__(self, bus, hz: float = 10.0, clock=time.monotonic):
        super().__init__(bus, "wake", "score", hz=hz, clock=clock, digits=4)

    def offer(self, score: float, phase: str, **extra) -> None:
        super().offer(score, phase=phase, **extra)


class MicLevelPublisher(PeakThrottle):
    """The mic's loudness (core/audio_levels.level) as `mic` events for the halo."""

    def __init__(self, bus, hz: float = 15.0, clock=time.monotonic):
        super().__init__(bus, "mic", "level", hz=hz, clock=clock, digits=2)

    def offer(self, level: float, phase: str, **extra) -> None:
        super().offer(level, phase=phase, **extra)
