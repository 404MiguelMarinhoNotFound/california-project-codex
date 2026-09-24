"""
The television's own volume, over its UPnP RenderingControl service.

`control_tv`'s volume actions used to press KEYCODE_VOLUME_UP/DOWN on the Mi Box
over ADB: one `adb shell` per step, up to 30 of them for a `volume_set`, and on
the BOX's 0-15 scale. On this setup the box usually sits pinned at 15/15 while
the room is ridden from the Samsung's remote, so "turn it up" was often a
multi-second no-op.

The Samsung answers SOAP on :9197 at /upnp/control/RenderingControl1 while it is
on: GetVolume / SetVolume on its own 0-100 scale (the number it shows on
screen), GetMute / SetMute. Measured on the UE49M5505 2026-09-24: GetVolume
36ms, SetVolume 46ms, no pairing and no 401. Home Assistant's own samsungtv
integration sets volume the same way.

Three facts shape this module:

- **:9197 only answers while the TV is on**, and flakes: one connect succeeded
  and the next one, a moment later, was refused. So every call retries once,
  and reachability is "GetVolume answered", never "the port opened".
- **The address comes from CecWaker's finder**, the same MAC + duid ladder as
  the rest of the TV path. A refused call re-resolves once with force=True, so
  a moved TV self-heals here too.
- **Upward jumps past `max_percent` need saying twice.** Speech recognition
  hears "thirteen" as "thirty", and a mistake on a 0-100 scale is loud. The
  first request above the cap is refused with a question; the same target
  again within `confirm_window_s` goes through. Down is never gated.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import requests

log = logging.getLogger(__name__)

_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"
_CONTROL_PATH = "/upnp/control/RenderingControl1"


class TvVolumeUnreachable(Exception):
    """The TV did not answer on :9197: off, in standby, or not on the LAN."""


@dataclass
class VolumeResult:
    """
    Outcome of a volume change. Falsy on failure, like GoveeCommandResult --
    so test existence with `is None`, never truthiness.
    """
    ok: bool
    level: int | None = None
    previous: int | None = None
    capped: bool = False            # an upward step stopped at max_percent
    needs_confirm: bool = False     # above max_percent, not yet said twice
    target: int | None = None       # what was asked for, when needs_confirm
    unreachable: bool = False

    def __bool__(self) -> bool:
        return self.ok


def _tag(xml: str, name: str) -> str | None:
    match = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return match.group(1) if match else None


class TvVolume:
    def __init__(self, config: dict, resolve_ip: Callable[..., str] | None = None):
        media_cfg = config.get("media") or {}
        cfg = media_cfg.get("tv_volume") or {}
        self.enabled = bool(cfg.get("enabled", False)) and resolve_ip is not None
        self.port = int(cfg.get("port", 9197))
        self.step = max(1, int(cfg.get("step", 5)))
        self.max_percent = max(0, min(100, int(cfg.get("max_percent", 40))))
        self.confirm_window_s = max(1.0, float(cfg.get("confirm_window_s", 60)))
        self.timeout_s = max(0.2, int(cfg.get("timeout_ms", 2000)) / 1000)
        self._resolve_ip = resolve_ip
        # (target, monotonic time) of the last request refused for the cap.
        self._pending: tuple[int, float] | None = None
        if not cfg.get("enabled", False):
            log.info("TV volume over UPnP disabled; volume falls back to the box over ADB")

    # --------------------------------------------------------------- SOAP ---

    def _post(self, ip: str, action: str, args: str) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{_SERVICE}">'
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>{args}"
            f"</u:{action}></s:Body></s:Envelope>"
        )
        response = requests.post(
            f"http://{ip}:{self.port}{_CONTROL_PATH}",
            data=body.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{_SERVICE}#{action}"',
            },
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise TvVolumeUnreachable(f"{action}: HTTP {response.status_code}")
        return response.text

    def _call(self, action: str, args: str = "") -> str:
        """
        One SOAP call, retried once. The retry re-resolves the address with
        force=True, which covers both a flaky :9197 and a TV that moved.
        """
        last: Exception | None = None
        for force in (False, True):
            try:
                ip = self._resolve_ip(force=force)
            except Exception as exc:  # the finder never should, but never let it end a turn
                last = exc
                continue
            if not ip:
                last = TvVolumeUnreachable("TV address unknown")
                continue
            try:
                return self._post(ip, action, args)
            except (requests.RequestException, TvVolumeUnreachable) as exc:
                last = exc
                log.info("TV volume %s at %s failed (%s)%s", action, ip, exc,
                         "" if force else "; retrying")
        raise TvVolumeUnreachable(str(last))

    # ------------------------------------------------------------ readers ---

    def get_volume(self) -> int | None:
        """The TV's volume, 0-100. None when it could not be read."""
        try:
            value = _tag(self._call("GetVolume"), "CurrentVolume")
            return int(value) if value is not None else None
        except (TvVolumeUnreachable, ValueError):
            return None

    def get_mute(self) -> bool | None:
        try:
            value = _tag(self._call("GetMute"), "CurrentMute")
        except TvVolumeUnreachable:
            return None
        if value is None:
            return None
        return value.strip() in ("1", "true", "True")

    # ------------------------------------------------------------ writers ---

    def _confirmed(self, target: int) -> bool:
        """True if this over-cap target was already asked for, recently."""
        pending = self._pending
        return (
            pending is not None
            and pending[0] == target
            and time.monotonic() - pending[1] <= self.confirm_window_s
        )

    def _write(self, target: int, previous: int | None, capped: bool = False) -> VolumeResult:
        try:
            self._call("SetVolume", f"<DesiredVolume>{target}</DesiredVolume>")
        except TvVolumeUnreachable:
            return VolumeResult(False, previous=previous, unreachable=True)
        self._pending = None
        return VolumeResult(True, level=target, previous=previous, capped=capped)

    def set_volume(self, target: int) -> VolumeResult:
        target = max(0, min(100, int(target)))
        current = self.get_volume()
        if current is None:
            return VolumeResult(False, unreachable=True)
        # Only a RISE past the cap is gated. Coming down from 60 to 50 is never
        # the dangerous direction.
        if target > self.max_percent and target > current and not self._confirmed(target):
            self._pending = (target, time.monotonic())
            return VolumeResult(False, previous=current, needs_confirm=True, target=target)
        return self._write(target, current)

    def change(self, delta: int) -> VolumeResult:
        """Relative change. Positive is louder. Stops at the cap on the way up."""
        current = self.get_volume()
        if current is None:
            return VolumeResult(False, unreachable=True)
        target = max(0, min(100, current + int(delta)))
        if delta > 0 and target > self.max_percent:
            if current < self.max_percent:
                # A plain "louder" that crosses the cap lands ON it. Going
                # further is a deliberate second request, gated below.
                return self._write(self.max_percent, current, capped=True)
            if not self._confirmed(target):
                self._pending = (target, time.monotonic())
                return VolumeResult(False, previous=current, needs_confirm=True, target=target)
        return self._write(target, current)

    def set_mute(self, muted: bool) -> VolumeResult:
        try:
            self._call("SetMute", f"<DesiredMute>{1 if muted else 0}</DesiredMute>")
        except TvVolumeUnreachable:
            return VolumeResult(False, unreachable=True)
        return VolumeResult(True)
