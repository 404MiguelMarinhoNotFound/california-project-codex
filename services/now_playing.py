"""
What California put on the screen, and when.

This is the FALLBACK, not the primary source. Measured on the real box on
2026-09-08, Stremio publishes the show and the episode in its media session
("Fallout, The Strip"), and `MediaService.room_status()` reads it -- which
beats memory outright, because it survives Master Miguel starting something
with the remote.

This still earns its place for everything that does not publish metadata.
YouTube exposes only its package, so without this she can say YouTube is
playing and nothing more. She fired the deep link herself, so she knows what
she put on, and this remembers that one thing in the words she used -- handed
back only when a live reading still agrees.

Two rules make the difference between memory and a lie:

- **Corroborate.** `current()` answers None unless the foreground app still
  matches what was launched. If Master Miguel has moved on, the memory is stale
  and is dropped rather than spoken.
- **Do not promise playback you did not start.** `kind` separates "playing"
  from "opened", because a YouTube search and a Stremio detail page were put on
  screen, not played. Reporting those as playing is the same class of error as
  reporting a standby television as on.

In memory only, deliberately. A restart is the moment this is least
trustworthy, and persisting it would mean answering confidently about a room
that has had hours to change.
"""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Launch:
    app: str      # the friendly key get_current_app returns, e.g. "stremio"
    label: str    # what to call it out loud: "Fallout", "samba"
    kind: str     # "playing" | "opened"
    at: float     # time.monotonic()


class NowPlaying:
    """One slot, not a history."""

    def __init__(self):
        # One slot on purpose. Only one thing is on screen at a time, and a list
        # would imply we can tell which entry is live. We cannot.
        self._launch: Launch | None = None

    def remember(self, app: str, label: str, kind: str = "playing") -> None:
        app = (app or "").strip().lower()
        label = (label or "").strip()
        if not app or not label:
            return
        self._launch = Launch(app, label, kind, time.monotonic())

    def forget(self) -> None:
        """Going home, stopping, or powering off ends whatever was on."""
        self._launch = None

    def current(self, foreground_app: str) -> Launch | None:
        """
        What she put on, but only while the box still agrees it is up.

        Matching is a substring test in one direction so a raw package name
        works too: "youtube" is in "com.google.android.youtube.tv", which is
        what get_current_app returns when its reverse lookup misses.
        """
        if self._launch is None:
            return None
        foreground = (foreground_app or "").strip().lower()
        if not foreground:
            return None
        if self._launch.app == foreground or self._launch.app in foreground:
            return self._launch
        return None

    def age_s(self) -> float | None:
        """Seconds since the last launch, or None if there has not been one."""
        if self._launch is None:
            return None
        return time.monotonic() - self._launch.at
