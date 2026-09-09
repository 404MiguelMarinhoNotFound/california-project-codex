"""
What California last told the lights to do.

The Govee BLE characteristic (00010203-...-2b11) is Write Without Response. It
is not a read, and there is no notify characteristic beside it -- the strip
takes orders and says nothing back. The cloud API cannot help either: the attic
H617E is BLE-only and absent from Govee's supported-model list, so
`GET /user/devices` returns an empty array with a perfectly valid key.

So "what are my lights doing" has exactly one honest answer available, and it
is not a reading. It is memory of the last command that succeeded, and it has
to be *said* as memory -- he may have used the Govee app, or the wall switch,
or the power may have dropped, and none of that reaches us.

Deliberately in memory only. Persisting it would let her answer confidently
about a room that has had hours to change while she was not running, which is
the failure this whole feature exists to remove.

Two things this does not do:

- **A brightness or colour command does not imply the light is on.** The strip
  accepts both while off. Inferring power from them would be a guess dressed as
  a fact.
- **It does not store RGB.** There is no rgb-to-name mapping in this project
  and there should not be one; the dispatcher already holds the word Master
  Miguel actually said, and "warm white" is what he wants read back, not
  "#FFE0B2".
"""

import time
from dataclasses import dataclass


@dataclass
class LightMemory:
    """The last command of each kind. None means "never told it that"."""
    power: bool | None = None
    percent: int | None = None
    color_word: str | None = None
    at: float = 0.0


class LightShadow:
    """Keyed by the RESOLVED light key, never the spoken hint."""

    def __init__(self):
        self._lights: dict[str, LightMemory] = {}

    def _entry(self, key: str) -> LightMemory:
        return self._lights.setdefault(key, LightMemory())

    def record_power(self, key: str, on: bool) -> None:
        entry = self._entry(key)
        entry.power = on
        entry.at = time.monotonic()

    def record_brightness(self, key: str, percent: int) -> None:
        # The clamped value, which is what the strip was actually sent.
        entry = self._entry(key)
        entry.percent = percent
        entry.at = time.monotonic()

    def record_color(self, key: str, word: str) -> None:
        word = (word or "").strip()
        if not word:
            return
        entry = self._entry(key)
        entry.color_word = word
        entry.at = time.monotonic()

    def remembered(self, key: str) -> LightMemory | None:
        """What she last sent this light, or None if she has not touched it."""
        return self._lights.get(key)
