"""
Runtime logs: a rotating file for everything, and one timing record per turn.

The console log disappears with the window, so every "she felt slow" was a
feeling rather than a number. Two outputs fix that, both under `logging.dir`
(gitignored, local only):

- `california.log` -- the ordinary log, at `file_level` (DEBUG by default)
  even while the console stays at INFO, rotated by size.
- `turns.jsonl` -- one JSON line per activation, with milliseconds since the
  wake word fired for each stage that happened:

    speech_end      he stopped talking (VAD stop)
    stt_done        Whisper returned
    llm_first_token Claude's first text token
    first_sentence  the chunker released the first sentence to TTS
    first_audio     her first reply audio started playing
    reply_done      the turn's last audio finished

  plus every tool call with its own duration. A stage that did not happen is
  absent, never zero: a dropped false wake has `speech_end` and an outcome of
  `no_speech`, and nothing after it.

`TurnTimer` is written to from three threads (main, TTS, player), so marks go
through a lock and `mark_once` keeps the first write. Nothing in here may ever
break a turn: every public method swallows its own errors.
"""

import itertools
import json
import logging
import logging.handlers
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(threadName)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
# Fields that carry what he said or what she answered. `include_transcripts:
# false` keeps them out of turns.jsonl AND off the live bus.
_TEXT_FIELDS = ("transcript", "reply", "target")
_TURN_IDS = itertools.count(1)
_CHATTY_LIBRARIES = (
    "anthropic", "groq", "openai", "asyncio", "PIL", "bleak",
    "playwright", "websocket", "numba", "matplotlib", "filelock",
)


def setup_file_logging(config: dict) -> str | None:
    """
    Attach a rotating file handler to the root logger. Returns the log path,
    or None when disabled or when the file cannot be opened (the console log
    keeps working either way).
    """
    cfg = (config or {}).get("logging") or {}
    if cfg.get("enabled", True) is False:
        return None
    try:
        log_dir = cfg.get("dir", "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "california.log")
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=int(cfg.get("max_bytes", 5_000_000)),
            backupCount=int(cfg.get("backup_count", 5)),
            encoding="utf-8",
        )
        level = getattr(logging, str(cfg.get("file_level", "DEBUG")).upper(), logging.DEBUG)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))

        root = logging.getLogger()
        # The console handler carries the console's level from here on, so
        # lowering the root to DEBUG for the file does not flood the terminal.
        for existing in root.handlers:
            if existing.level == logging.NOTSET:
                existing.setLevel(root.level)
        root.addHandler(handler)
        root.setLevel(min(root.level, level))
        # SDK clients log every request body at DEBUG -- the whole system
        # prompt, history and tool schemas, several times a turn. That would
        # bury everything else in the file.
        for name in _CHATTY_LIBRARIES:
            lib = logging.getLogger(name)
            if lib.level == logging.NOTSET or lib.level < logging.INFO:
                lib.setLevel(logging.INFO)
        return path
    except Exception:
        logger.exception("Could not open the log file; console logging only")
        return None


class TurnLogWriter:
    """Appends finished turns to turns.jsonl. One per process."""

    def __init__(self, config: dict):
        cfg = (config or {}).get("logging") or {}
        self.enabled = cfg.get("enabled", True) is not False and cfg.get("turns", True) is not False
        self.include_text = cfg.get("include_transcripts", True) is not False
        self.path = os.path.join(cfg.get("dir", "logs"), "turns.jsonl")
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        if not self.enabled:
            return
        try:
            if not self.include_text:
                record = {k: v for k, v in record.items() if k not in _TEXT_FIELDS}
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            logger.exception("Could not write the turn record")


class TurnTimer:
    """
    Timestamps for one activation, relative to the wake word.

    With a `bus`, every mark, field and tool call is also published the moment
    it is made, so the live UI watches the turn build rather than reading it
    afterwards. Publishing happens outside the lock and cannot raise.
    """

    def __init__(self, writer: TurnLogWriter | None = None, chained: bool = False, bus=None):
        self._writer = writer
        self._bus = bus
        self._include_text = writer.include_text if writer is not None else True
        self._turn_id = next(_TURN_IDS)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._marks: dict[str, int] = {}
        self._tools: list[dict] = []
        self._fields: dict = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "chained": chained,
        }
        self._done = False
        self._publish("turn_start", {"chained": chained, "ts": self._fields["ts"]})

    def _publish(self, kind: str, data: dict) -> None:
        if self._bus is None:
            return
        try:
            data = {"turn": self._turn_id, **data}
            if not self._include_text:
                data = {k: v for k, v in data.items() if k not in _TEXT_FIELDS}
                if "fields" in data:
                    data["fields"] = {k: v for k, v in data["fields"].items() if k not in _TEXT_FIELDS}
                if "record" in data:
                    data["record"] = {k: v for k, v in data["record"].items() if k not in _TEXT_FIELDS}
            self._bus.publish(kind, data)
        except Exception:
            pass

    def _ms(self) -> int:
        return int(round((time.monotonic() - self._t0) * 1000))

    def mark(self, name: str) -> None:
        with self._lock:
            ms = self._marks[name] = self._ms()
        self._publish("turn_mark", {"name": name, "ms": ms})

    def mark_once(self, name: str) -> None:
        with self._lock:
            if name in self._marks:
                return
            ms = self._marks[name] = self._ms()
        self._publish("turn_mark", {"name": name, "ms": ms})

    def set(self, **fields) -> None:
        with self._lock:
            self._fields.update(fields)
        self._publish("turn_set", {"fields": fields})

    def tool_started(self, name: str, action: str | None, target: str | None = None,
                     start_ms: int | None = None) -> int:
        """Live only: a tool call began. The record gets it from tool().

        `target` is a short label for the live UI ("attic", "to Marta"), never
        message text or a number; it is dropped with `include_transcripts:
        false`. Calling again with the same `start_ms` updates the label of a
        call already shown (web search learns its query after it starts).
        Returns the start, for the matching tool() call. Never raises.
        """
        try:
            update = start_ms is not None
            if start_ms is None:
                start_ms = self.now_ms()
            data = {"name": name, "action": action, "start_ms": start_ms}
            label = _short_target(target) if self._include_text else None
            if label:
                data["target"] = label
            elif update:
                return start_ms   # an update with nothing to say
            self._publish("turn_tool_start", data)
        except Exception:
            pass
        return start_ms if start_ms is not None else 0

    def tool(self, name: str, action: str | None, started_ms: int, result: str | None,
             ok: bool | None = None, target: str | None = None, awaiting: bool = False) -> None:
        """`ok`: did the device action happen (True/False), absent when the tool
        had nothing to report -- a status read, or a question back.
        `awaiting`: the tool asked him something (a read-back, "which one?") and
        is parked until he answers. Never set together with `ok`."""
        with self._lock:
            entry = {
                "name": name,
                "action": action,
                "start_ms": started_ms,
                "ms": self._ms() - started_ms,
            }
            # A result can quote what he asked for ("I've got Marta at +351...",
            # a group read-back carries the message), so it goes with the
            # transcripts: `include_transcripts: false` keeps only timings.
            if self._include_text:
                entry["result"] = (result or "")[:160]
            if ok is True or ok is False:
                entry["ok"] = ok
            elif awaiting is True:
                entry["awaiting"] = True
            label = _short_target(target) if self._include_text else None
            if label:
                entry["target"] = label
            self._tools.append(entry)
        self._publish("turn_tool", entry)

    def now_ms(self) -> int:
        return self._ms()

    def finish(self, outcome: str | None = None) -> dict | None:
        """
        Write the record once. Returns it, for the summary line and tests.
        `outcome` overrides one set earlier with set(outcome=...).
        """
        with self._lock:
            if self._done:
                return None
            self._done = True
            record = dict(self._fields)
            if outcome is not None or "outcome" not in record:
                record["outcome"] = outcome or "unknown"
            record["total_ms"] = self._ms()
            record["marks"] = dict(self._marks)
            if self._tools:
                record["tools"] = list(self._tools)
        try:
            logger.info("[turn] %s", _summary(record))
        except Exception:
            pass
        if self._writer is not None:
            self._writer.write(record)
        self._publish("turn_end", {"record": record})
        return record


# A satellite label is a glance, not a sentence.
_TARGET_MAX = 40


def _short_target(target) -> str | None:
    """One line, at most _TARGET_MAX characters, or None."""
    if not isinstance(target, str):
        return None
    text = " ".join(target.split())
    if not text:
        return None
    if len(text) > _TARGET_MAX:
        text = text[: _TARGET_MAX - 1].rstrip() + "\u2026"
    return text


class _NullTurn:
    """Stand-in when no turn is active, so call sites never branch."""

    def mark(self, name):
        pass

    def mark_once(self, name):
        pass

    def set(self, **fields):
        pass

    def tool_started(self, *args, **kwargs):
        return 0

    def tool(self, *args, **kwargs):
        pass

    def now_ms(self):
        return 0

    def finish(self, outcome=None):
        return None


NULL_TURN = _NullTurn()

_SUMMARY_ORDER = (
    "speech_end", "stt_done", "llm_first_token",
    "first_sentence", "first_audio", "reply_done",
)


def _summary(record: dict) -> str:
    marks = record.get("marks", {})
    parts = [f"{record.get('outcome')} total={record.get('total_ms')}ms"]
    parts += [f"{k}={marks[k]}" for k in _SUMMARY_ORDER if k in marks]
    for t in record.get("tools", []):
        parts.append(f"tool {t['name']}.{t.get('action')}={t['ms']}ms")
    return " ".join(parts)
