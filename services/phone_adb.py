"""
AdbPhone: Master Miguel's phone over Wireless debugging, for PhoneService.

It keeps the dialer interface PhoneService has always used -- dial, in_call,
hang_up, play_sound -- and adds `audio()`, the call's ears and mouth
(services/phone_call_audio.py). Everything on the phone goes through the
callaudio helper (android/callaudio/) run as the shell user, because HyperOS
hooks the shell's ordinary paths (`am start -a CALL`, `input keyevent`).

Statuses are plain strings, one per thing that can go wrong, because each
needs a different fix and PhoneService speaks each one:

    OK               ready
    NO_PHONE         not found on ADB: Wireless debugging is off / phone away
    LOCKED_DOWN      HyperOS "USB debugging (Security settings)" is off
    NO_INTERCEPTION  the phone will not hand over call audio
    HELPER_MISMATCH  the helper on the phone is not this build; not run
    DIALED / NOT_PLACED, ENDED / NO_CALL, PLAYING / RING_FAILED

Construction runs nothing. Every adb call goes through `runner`, every
long-running helper through `popen`, so tests never touch a real phone.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import subprocess
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER_DEX = os.path.join(ROOT, "android", "callaudio", "callaudio.dex")
HELPER_REMOTE = "/data/local/tmp/callaudio.dex"

# android.telephony.PreciseCallState, foreground call.
IDLE, ACTIVE, HOLDING, DIALING, ALERTING = 0, 1, 2, 3, 4
_LIVE = (ACTIVE, HOLDING, DIALING, ALERTING)

_DIAL_REGISTER_S = 10.0  # placeCall returns at once; the call shows up in the state within ~1 s
_RING_START_S = 5.0
_IP_PORT = re.compile(r"\d+\.\d+\.\d+\.\d+:\d+")
_MI_BOX_PORT = ":5555"  # the Mi Box is on the same adb server, always on 5555


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")


def _popen(argv: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **kw)


class AdbPhone:
    def __init__(
        self,
        adb_path: str = "adb",
        serial: str = "",
        helper_dex: str = HELPER_DEX,
        port: int = 27183,
        mute_phone_mic: bool = True,
        runner: Callable[[list[str], float], subprocess.CompletedProcess] | None = None,
        popen: Callable[..., subprocess.Popen] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.adb_path = adb_path
        self.configured_serial = serial
        self.serial = serial
        self.helper_dex = helper_dex
        self.port = port
        self.mute_phone_mic = mute_phone_mic
        self._runner = runner or _run
        self._popen = popen or _popen
        self._sleep = sleep
        self._clock = clock
        self._hash = ""
        self._interceptable: bool | None = None
        self._seen = False

    # ------------------------------------------------------------- adb

    def _adb(self, *args: str, timeout: float = 20.0) -> str:
        try:
            return self._runner([self.adb_path, *args], timeout).stdout or ""
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("adb %s failed: %s", args[:1], exc)
            return ""

    def sh(self, command: str, timeout: float = 20.0) -> str:
        return self._adb("-s", self.serial, "shell", command, timeout=timeout)

    def _discover(self) -> str:
        """The phone's serial, or "" -- never the Mi Box. See module docstring."""
        if self.configured_serial:
            return self.configured_serial
        services = self._adb("mdns", "services")
        match = re.search(r"(\S+)\s+_adb-tls-connect\._tcp\s+(\d+\.\d+\.\d+\.\d+:\d+)", services)
        names = {f"{match.group(1)}._adb-tls-connect._tcp", match.group(2)} if match else set()
        for line in self._adb("devices").splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[1] != "device":
                continue
            name = parts[0]
            # mDNS goes quiet when the phone's screen is off; the attached
            # IP:port is still the phone. Any other IP:port is not on 5555.
            wireless = _IP_PORT.fullmatch(name) and not name.endswith(_MI_BOX_PORT)
            if name in names or "_adb-tls-connect._tcp" in name or wireless:
                return name
        if match and not match.group(2).endswith(_MI_BOX_PORT):
            self._adb("connect", match.group(2))
            return match.group(2)
        return ""

    def _ensure_serial(self) -> bool:
        if not self.serial:
            self.serial = self._discover()
        return bool(self.serial)

    # ------------------------------------------------------------- helper

    def _local_hash(self) -> str:
        if not self._hash:
            with open(self.helper_dex, "rb") as handle:
                self._hash = hashlib.sha256(handle.read()).hexdigest()
        return self._hash

    def _helper_cmd(self, *args: str) -> str:
        """
        One shell command that checks the helper's sha256 and runs it.

        /data/local/tmp is shared by everything that runs as shell, and the
        helper runs with the shell's call and audio permissions: the check and
        the exec are one command so nothing can swap the file in between
        (security review, 2026-10-13). `exec` keeps the fallback echo from
        running after a helper that merely exited non-zero.
        """
        return (
            f'[ "$(sha256sum {HELPER_REMOTE} | cut -d" " -f1)" = "{self._local_hash()}" ] '
            f"&& CLASSPATH={HELPER_REMOTE} exec app_process / CallAudio {' '.join(args)} "
            f"|| echo HELPER_MISMATCH"
        )

    def _run_helper(self, *args: str, timeout: float = 30.0) -> str:
        # Braces so 2>&1 covers the helper too, not only the fallback echo.
        return self.sh("{ " + self._helper_cmd(*args) + "; } 2>&1", timeout=timeout)

    def _push_helper(self) -> None:
        remote = self.sh(f"sha256sum {HELPER_REMOTE} 2>/dev/null").split()
        if not remote or remote[0] != self._local_hash():
            self.sh(f"rm -f {HELPER_REMOTE}")
            self._adb("-s", self.serial, "push", self.helper_dex, HELPER_REMOTE, timeout=60.0)
        self.sh(f"chmod 444 {HELPER_REMOTE}")

    def helper(self, *args: str) -> subprocess.Popen:
        return self._popen([self.adb_path, "-s", self.serial, "shell", self._helper_cmd(*args)])

    def forward(self, port: int) -> None:
        self._adb("-s", self.serial, "forward", f"tcp:{port}", f"tcp:{port}")

    def unforward(self, port: int) -> None:
        self._adb("-s", self.serial, "forward", "--remove", f"tcp:{port}")

    # ------------------------------------------------------------- readiness

    def ready(self, need_interception: bool = True) -> str:
        """OK, or the first thing standing between us and the phone."""
        if not self._ensure_serial():
            return "NO_PHONE"
        self._push_helper()
        if self.sh("getprop persist.security.adbinput").strip() != "1":
            return "LOCKED_DOWN"
        if need_interception:
            if self._interceptable is None:
                out = self._run_helper("check")
                if "HELPER_MISMATCH" in out:
                    return "HELPER_MISMATCH"
                if "INTERCEPTABLE=" in out:
                    self._interceptable = "INTERCEPTABLE=true" in out
            if self._interceptable is not True:
                return "NO_INTERCEPTION"
        return "OK"

    # ------------------------------------------------------------- the dialer interface

    def call_state(self) -> int | None:
        if not self._ensure_serial():
            return None
        out = self.sh("dumpsys telephony.registry | grep -m1 mPreciseCallState")
        match = re.search(r"Foreground call state: (\d+)", out)
        return int(match.group(1)) if match else None

    def dial(self, number: str) -> str:
        if not re.fullmatch(r"\+?\d{3,15}", number or ""):
            return "NOT_PLACED"
        status = self.ready()
        if status != "OK":
            return status
        self._seen = False
        out = self._run_helper("dial", number)
        if "HELPER_MISMATCH" in out:
            return "HELPER_MISMATCH"
        if "DIALED" not in out:
            logger.warning("Phone dial refused: %s", out.strip()[:200])
            return "NOT_PLACED"
        deadline = self._clock() + _DIAL_REGISTER_S
        while self._clock() < deadline:
            if self.call_state() in _LIVE:
                self._seen = True
                return "DIALED"
            self._sleep(0.3)
        return "NOT_PLACED"

    def in_call(self) -> bool | None:
        """True while a call is up; False only once one was seen; None when unknown."""
        state = self.call_state()
        if state is None:
            return None
        if state in _LIVE:
            self._seen = True
            return True
        return False if self._seen else None

    def hang_up(self) -> str:
        if not self._ensure_serial():
            return ""
        out = self._run_helper("hangup")
        if "ENDED=true" in out:
            return "ENDED"
        if "ENDED=false" in out:
            return "NO_CALL"
        return ""

    def play_sound(self, seconds: int = 20) -> str:
        """Find my phone. PLAYING once the beep started; it runs on for `seconds`."""
        status = self.ready(need_interception=False)
        if status != "OK":
            return status
        try:
            proc = self.helper("ring", str(int(seconds)))
        except OSError as exc:
            logger.warning("Ring failed to start: %s", exc)
            return "RING_FAILED"
        first: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: first.put(next(iter(proc.stdout), "")), daemon=True).start()
        try:
            line = first.get(timeout=_RING_START_S).strip()
        except queue.Empty:
            line = ""
        if line == "RINGING":
            return "PLAYING"
        logger.warning("Ring did not start: %r", line[:200])
        return "RING_FAILED"

    def audio(self, record_path: str = ""):
        from services.phone_call_audio import PhoneCallAudio

        return PhoneCallAudio(self, record_path=record_path)
