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
    BUSY             a call is already up on the phone; nothing dialled
    LOCKED_DOWN      HyperOS "USB debugging (Security settings)" is off
    NO_INTERCEPTION  the phone will not hand over call audio
    HELPER_MISMATCH  the helper on the phone is not this build; not run
    DIALED / NOT_PLACED, ENDED / NO_CALL, PLAYING / ALREADY_PLAYING / RING_FAILED

Answering a ring and the speaker have their own result types; see
`answer_incoming` and `set_speaker`. Call identity there is telecom's own call
key (`TC@<n>`), read from `dumpsys telecom`'s live mCalls rows only: it is the
one source that stays the same from ringing to active (measured 2026-10-14,
docs/superpowers/probes/2026-10-14-native-incoming-answer.md).

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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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


_ROW = re.compile(r"Row: \d+ ")
_NO_RESULT = "No result found."
_SAFE_URI = re.compile(r"[\w:/.]+")
_SAFE_KEY = re.compile(r"\w+")


def parse_content_rows(text: str, keys: list[str]) -> tuple[list[dict[str, str | None]], int]:
    """
    Rows of `adb shell content query --projection a:b:c` as dicts, plus how
    many `Row:` lines could not be parsed.

    The tool prints `Row: N a=1, b=2, c=3` with nothing escaped, so a value
    can hold ", " or "=" or even a newline. Each value therefore runs up to
    the FIRST `, <next key>=` after it, and the last key takes the rest of the
    line plus any continuation lines. That is only safe with **at most one
    free-text column, and it goes last** in `keys`: a free-text value in the
    middle could contain the next key's separator. `NULL` becomes None.
    """
    rows: list[dict[str, str | None]] = []
    unparsed = 0
    current: dict[str, str | None] | None = None
    lines = (text or "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the output's own final newline, not an empty line of a value
    for line in lines:
        line = line[:-1] if line.endswith("\r") else line
        start = _ROW.match(line)
        if not start:
            # A newline inside the last value. Before any row (or after one
            # that could not be parsed) there is nothing to continue.
            if current is not None:
                last = keys[-1]
                current[last] = (current[last] or "") + "\n" + line
            continue
        current = None
        rest = line[start.end():]
        if not rest.startswith(keys[0] + "="):
            unparsed += 1
            continue
        row: dict[str, str] = {}
        pos = len(keys[0]) + 1
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                row[key] = rest[pos:]
                break
            sep = f", {keys[i + 1]}="
            end = rest.find(sep, pos)
            if end < 0:
                break
            row[key] = rest[pos:end]
            pos = end + len(sep)
        if len(row) != len(keys):
            unparsed += 1
            continue
        current = {k: (None if v == "NULL" else v) for k, v in row.items()}
        rows.append(current)
    return rows, unparsed


# ------------------------------------------------------------- call topology

# A telecom call key as it may appear in a shell command: only characters a
# shell passes through unquoted, so a key can never become a second command.
_CALL_KEY = re.compile(r"[A-Za-z0-9@_.:-]{1,128}")
_ROW_ID = re.compile(r"\bid=([A-Za-z0-9@_.:-]+)")
_ROW_STATE = re.compile(r"\bstate=([A-Z_]+)")
_TELECOM_STATES = {
    "RINGING": "ringing",
    "ACTIVE": "active",
    "ON_HOLD": "holding",
    "DIALING": "dialing", "CONNECTING": "dialing", "NEW": "dialing", "SELECT_PHONE_ACCOUNT": "dialing",
    "ANSWERED": "answered",  # observed between acceptRingingCall and ACTIVE
    "DISCONNECTING": "ended", "DISCONNECTED": "ended", "ABORTED": "ended",
}


@dataclass(frozen=True)
class CallLeg:
    key: str    # telecom's call id, "TC@<n>"
    state: str  # ringing / active / holding / dialing / answered / ended / unknown


@dataclass(frozen=True)
class CallTopology:
    legs: tuple[CallLeg, ...]


def parse_call_topology(text: str) -> CallTopology | None:
    """
    The phone's live calls from `dumpsys telecom`: the CallsManager `mCalls`
    rows between `mCalls:` and `mCallAudioManager:`, never the call history
    printed elsewhere in the dump. Each row must carry exactly one `id=` and
    one `state=`; a row that does not, or a dump without the section, is None
    ("could not tell"), never an empty topology. A state telecom names that is
    not in the table above is `unknown`, which blocks every mutation.
    """
    text = text or ""
    start = text.find("mCalls:")
    end = text.find("mCallAudioManager:", start) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    legs = []
    for row in text[start + len("mCalls:"):end].splitlines():
        if not row.strip():
            continue
        ids, states = _ROW_ID.findall(row), _ROW_STATE.findall(row)
        if len(ids) != 1 or len(states) != 1:
            return None
        legs.append(CallLeg(ids[0], _TELECOM_STATES.get(states[0], "unknown")))
    return CallTopology(tuple(legs))


# ------------------------------------------------------------- the speaker button

_DIALER = "com.google.android.dialer"
_SPEAKER_WORDS = re.compile(r"speaker|alta-?voz|altifalante|viva-?voz|alto-?falante", re.I)
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


@dataclass(frozen=True)
class SpeakerButton:
    center: tuple[int, int]
    checked: bool


def parse_speaker_button(xml: str) -> SpeakerButton | None:
    """
    Google Phone's own Speaker button in a `uiautomator dump`: the nearest
    clickable ancestor of the node whose text or content-desc says Speaker
    (the word is a plain TextView inside a clickable View with no
    resource-id). Its centre comes from its bounds on every read, never a
    remembered position, and its `checked` is the speaker's on/off state.
    None for zero or two candidates, another app's button, a disabled one,
    unparseable bounds or an unreadable screen.
    """
    xml = xml or ""
    start, end = xml.find("<?xml"), xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return None
    try:
        root = ET.fromstring(xml[start:end + len("</hierarchy>")])
    except ET.ParseError:
        return None
    parent = {child: node for node in root.iter() for child in node}
    buttons: list[ET.Element] = []
    for node in root.iter("node"):
        label = f"{node.get('text', '')} {node.get('content-desc', '')}"
        if node.get("package") != _DIALER or not _SPEAKER_WORDS.search(label):
            continue
        button = node
        while button is not None and button.get("clickable") != "true":
            button = parent.get(button)
        if button is not None and all(button is not b for b in buttons):
            buttons.append(button)
    if len(buttons) != 1:
        return None
    button = buttons[0]
    bounds = _BOUNDS.fullmatch(button.get("bounds", ""))
    checked = button.get("checked")
    if (button.get("package") != _DIALER or button.get("enabled") != "true" or bounds is None
            or checked not in ("true", "false")):
        return None
    x1, y1, x2, y2 = map(int, bounds.groups())
    return SpeakerButton(center=((x1 + x2) // 2, (y1 + y2) // 2), checked=checked == "true")


_ROUTE = re.compile(r"^[ \t]*Active communication device:[^\n]*?\btype:(\w+)", re.M)
_MIC_MUTE = re.compile(r"mic mute FromSwitch=(true|false)")


def parse_audio_route(text: str) -> tuple[str | None, bool | None]:
    """(route, mic muted) from `dumpsys audio`: `speaker`/`earpiece`/... and the
    phone mic's switch mute, each None when the dump does not say."""
    route, mic = _ROUTE.search(text or ""), _MIC_MUTE.search(text or "")
    return (route.group(1) if route else None, (mic.group(1) == "true") if mic else None)


@dataclass(frozen=True)
class AnswerResult:
    status: str
    call_key: str = ""
    accepted: bool = False          # True only once the same key was read back ACTIVE
    speaker_verified: bool = False  # True only once the audio route read back speaker


@dataclass(frozen=True)
class SpeakerResult:
    status: str
    speaker_verified: bool


_ACCEPT_ACTIVE_S = 5.0     # acceptRingingCall -> the same key ACTIVE (ANSWERED in between)
_SPEAKER_WINDOW_S = 8.0    # screen read (~2.5 s) + tap + audio read back (ruling R3)
_SCREEN_TRIES = 3          # right after an answer the in-call screen may still be drawing
_POLL_S = 0.25
_HELPER_REFUSALS = {
    "REFUSED_STALE": "STALE_RING",
    "REFUSED_BUSY": "BUSY",
    "REFUSED_NO_RING": "NO_RING",
    "REFUSED_UNREADABLE": "UNREADABLE",
    "REFUSED_BAD_KEY": "UNSUPPORTED",
    "ACCEPT_UNSUPPORTED": "UNSUPPORTED",
}


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
        self._ring: subprocess.Popen | None = None
        self.last_status = ""  # query()'s ready() status, or UNREADABLE

    @classmethod
    def from_config(cls, config: dict, **kw) -> "AdbPhone":
        """Built from `phone.adb` in config.yaml; `kw` (runner=, popen=, ...) passes through."""
        adb_cfg = ((config or {}).get("phone", {}) or {}).get("adb", {}) or {}
        return cls(
            adb_path=str(adb_cfg.get("path") or "adb"),
            serial=str(adb_cfg.get("serial") or ""),
            port=int(adb_cfg.get("port") or 27183),
            mute_phone_mic=adb_cfg.get("mute_phone_mic", True) is not False,
            **kw,
        )

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

    def _still_attached(self) -> bool:
        """
        Is the cached serial still a live adb device? Wireless debugging takes
        a new port on every reboot or toggle, and a shell to a stale serial
        answers nothing (adb's "device not found" goes to stderr): that read as
        an empty Security-settings property and spoke LOCKED_DOWN, the wrong
        fix, until a restart (final review, 2026-10-13).
        """
        for line in self._adb("devices").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == self.serial and parts[1] == "device":
                return True
        return False

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

    def stream(self, command: str) -> subprocess.Popen | None:
        """
        A long-running plain `adb shell <command>`, for the incoming-call
        watcher's read-only RING_LOOP and nothing else. Never the helper, no
        push, no Security-settings gate: `reachable()` only. None when the
        phone is not on adb or adb would not start; never raises.
        """
        try:
            if self.reachable() != "OK":
                return None
            return self._popen([self.adb_path, "-s", self.serial, "shell", command])
        except Exception as exc:  # the watcher restarts on None; a raise would end it
            logger.warning("adb stream did not start: %s", type(exc).__name__)
            return None

    def forward(self, port: int) -> None:
        self._adb("-s", self.serial, "forward", f"tcp:{port}", f"tcp:{port}")

    def unforward(self, port: int) -> None:
        self._adb("-s", self.serial, "forward", "--remove", f"tcp:{port}")

    # ------------------------------------------------------------- readiness

    def reachable(self) -> str:
        """
        OK or NO_PHONE: is the phone on adb right now? The stale-serial check
        and discovery only -- no helper push, no Security-settings gate, no
        interception check -- so the plain reads below need nothing else, and
        `ready()` starts here so discovery lives in one place.
        """
        if self.serial and not self._still_attached():
            self.serial = ""  # moved port or gone: find it again
        return "OK" if self._ensure_serial() else "NO_PHONE"

    def ready(self, need_interception: bool = True, need_helper: bool = True) -> str:
        """OK, or the first thing standing between us and the phone."""
        status = self.reachable()
        if status != "OK":
            return status
        if need_helper:
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

    # ------------------------------------------------------------- reading

    def query(self, uri: str, projection: list[str], timeout: float = 30.0) -> list[dict[str, str | None]] | None:
        """
        Read-only `content query` against one of the phone's providers.

        A list of rows (`[]` when the provider says "No result found."), or
        None when the phone could not be read; `last_status` then says why
        (NO_PHONE, LOCKED_DOWN, or UNREADABLE for output with neither rows nor
        the no-result line, e.g. a provider error). Needs no helper and pushes
        nothing. The URI and keys are checked so nothing reaches the shell but
        a query; see `parse_content_rows` for the one-free-text-column rule.
        """
        if not _SAFE_URI.fullmatch(uri or ""):
            raise ValueError(f"unsafe content URI: {uri!r}")
        if not projection or not all(_SAFE_KEY.fullmatch(k or "") for k in projection):
            raise ValueError(f"unsafe projection: {projection!r}")
        self.last_status = self.ready(need_interception=False, need_helper=False)
        if self.last_status != "OK":
            return None
        out = self.sh(f"content query --uri {uri} --projection {':'.join(projection)}", timeout=timeout)
        rows, unparsed = parse_content_rows(out, projection)
        if unparsed:
            logger.warning("content query: %d row(s) could not be parsed", unparsed)
        if rows:
            return rows
        if any(line.strip() == _NO_RESULT for line in out.splitlines()):
            return []
        self.last_status = "UNREADABLE"
        logger.warning("content query: unreadable output (%d chars)", len(out))
        return None

    # `_adb()` answers "" on any adb failure, so the raw reads below return
    # None for output that is not recognisably theirs: an unreachable phone
    # must never read as "no missed calls" or "no notifications".

    def missed_calls_raw(self, since_ms: int) -> str | None:
        """
        The call log's missed calls since `since_ms`, newest first, as raw
        `content query` text for `services.phone_digest.parse_call_log`, or
        None when the phone could not be read. Read-only, no helper.
        """
        # Imported here: phone_digest imports parse_content_rows from this module.
        from services.phone_digest import CALL_LOG_PROJECTION

        if self.reachable() != "OK":
            return None
        out = self.sh(
            "content query --uri content://call_log/calls"
            f" --projection {':'.join(CALL_LOG_PROJECTION)}"
            f' --where "type=3 AND date>={int(since_ms)}" --sort "date DESC"',
            timeout=30.0,
        )
        if "Row:" not in out and _NO_RESULT not in out:
            logger.warning("call log: unreadable output (%d chars)", len(out))
            return None
        return out

    def notifications_raw(self) -> str | None:
        """
        `dumpsys notification --noredact` for `services.phone_digest.parse_notifications`,
        or None when the phone could not be read. Read-only: nothing is
        dismissed or marked seen.
        """
        if self.reachable() != "OK":
            return None
        out = self.sh("dumpsys notification --noredact", timeout=20.0)
        if "Notification List:" not in out:
            logger.warning("notifications: unreadable output (%d chars)", len(out))
            return None
        return out

    # ------------------------------------------------------------- the dialer interface

    def call_state(self) -> int | None:
        if not self._ensure_serial():
            return None
        # One block per modem: on a dual-SIM phone the live call can be in any
        # of them, so the busiest foreground state wins, never just the first.
        out = self.sh("dumpsys telephony.registry | grep mPreciseCallState")
        states = [int(m) for m in re.findall(r"Foreground call state: (\d+)", out)]
        if not states:
            return None
        live = [st for st in states if st in _LIVE]
        return live[0] if live else states[0]

    def dial(self, number: str) -> str:
        if not re.fullmatch(r"\+?\d{3,15}", number or ""):
            return "NOT_PLACED"
        status = self.ready()
        if status != "OK":
            return status
        # A call already up (his own, or one on hold) must not be dialled over:
        # the bridge would open on it and the hang-up would end it.
        if self.call_state() in _LIVE:
            return "BUSY"
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
        # Telecom took the call but it never showed in the state (a SIM picker,
        # a slow registration): it may still ring someone with nobody on the
        # line, so end it before saying it was not placed.
        self._run_helper("hangup")
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
        # "Again" while it still rings must not start a second ring: the second
        # would read the max volume the first set and restore THAT, leaving
        # his alarm at full volume (final review, 2026-10-13).
        if self._ring is not None and self._ring.poll() is None:
            return "ALREADY_PLAYING"
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
            self._ring = proc
            return "PLAYING"
        logger.warning("Ring did not start: %r", line[:200])
        try:
            proc.kill()  # a slow start must not ring later anyway
        except OSError:
            pass
        return "RING_FAILED"

    # ------------------------------------------------------------- answering and the speaker
    #
    # Neither path dials, hangs up, mutes, records or opens the bridge. After
    # an acceptance nothing ever ends the call: a partial success (answered,
    # speaker not confirmed) leaves him on the phone's actual route.

    def _read_topology(self) -> CallTopology | None:
        return parse_call_topology(self.sh("dumpsys telecom", timeout=20.0))

    def call_topology(self) -> CallTopology | None:
        """The phone's live calls (see parse_call_topology), or None when the
        phone is not on adb or the dump could not be read. Read-only, no helper."""
        if self.reachable() != "OK":
            return None
        return self._read_topology()

    def _read_screen(self) -> str:
        """The current screen as `uiautomator` XML ("" on failure); readable over the keyguard."""
        return self._adb("-s", self.serial, "exec-out", "uiautomator", "dump", "/dev/tty", timeout=20.0)

    def _read_audio(self) -> str:
        return self.sh("dumpsys audio", timeout=20.0)

    def answer_incoming(self, expected_key: str, speaker: bool = True) -> AnswerResult:
        """
        Answer the ring telecom calls `expected_key`, and put it on the phone's
        own speaker when `speaker`.

        Only one RINGING call and nothing else on the phone: a ring alongside
        any other call is call waiting (BUSY), another key ringing is
        STALE_RING, and a call already answered by hand is NO_RING. The helper
        re-reads telecom itself right before its one global acceptRingingCall,
        so the window for a different call to take the ring's place is the
        width of one binder call, not of this whole method; it cannot be
        closed from the shell. Afterwards the same key must read back ACTIVE
        within 5 s before `accepted` is True or the speaker is touched; any
        other key, an extra call or an unreadable read stops everything.
        """
        if not _CALL_KEY.fullmatch(expected_key or ""):
            return AnswerResult("UNSUPPORTED")
        status = self.ready(need_interception=False)
        if status != "OK":
            return AnswerResult(status, expected_key)
        before = self._read_topology()
        if before is None or any(leg.state == "unknown" for leg in before.legs):
            return AnswerResult("UNREADABLE", expected_key)
        ringing = [leg for leg in before.legs if leg.state == "ringing"]
        if not ringing:
            return AnswerResult("NO_RING", expected_key)
        if len(before.legs) > 1:
            return AnswerResult("BUSY", expected_key)
        if ringing[0].key != expected_key:
            return AnswerResult("STALE_RING", expected_key)

        out = self._run_helper("accept", expected_key, timeout=15.0)
        lines = [line.strip() for line in out.splitlines()]
        if "HELPER_MISMATCH" in lines:
            return AnswerResult("HELPER_MISMATCH", expected_key)
        if "ACCEPT_REQUESTED" not in lines:
            for marker, refused in _HELPER_REFUSALS.items():
                if marker in lines:
                    return AnswerResult(refused, expected_key)
            if any(line.startswith("ACCEPT_FAILED") for line in lines):
                logger.warning("Phone answer: the helper's acceptance failed")
                return AnswerResult("NOT_ANSWERED", expected_key)
            # It may or may not have accepted: no further reads or writes.
            logger.warning("Phone answer: unrecognised helper output (%d chars)", len(out))
            return AnswerResult("ANSWER_UNCERTAIN", expected_key)

        status = self._await_active(expected_key)
        if status != "ACTIVE":
            logger.warning("Phone answer: %s after acceptance", status)
            return AnswerResult(status, expected_key)
        if not speaker:
            return AnswerResult("ANSWERED", expected_key, accepted=True)
        route = self.set_speaker(expected_key, True)
        if route.speaker_verified:
            return AnswerResult("ANSWERED_SPEAKER", expected_key, accepted=True, speaker_verified=True)
        logger.warning("Phone answer: answered, speaker %s", route.status)
        return AnswerResult("ANSWERED_ROUTE_FAILED", expected_key, accepted=True)

    def _await_active(self, key: str) -> str:
        """
        ACTIVE once `key` is the sole call and active, read-only polling up to
        5 s. Ringing/answered/unknown on the same sole key may still become
        active; the call ending is STALE_RING (they hung up); anything else --
        another key, an extra call, an unreadable read, the deadline -- is
        ANSWER_UNCERTAIN, because the acceptance was global.
        """
        deadline = self._clock() + _ACCEPT_ACTIVE_S
        while True:
            topology = self._read_topology()
            if topology is None:
                return "ANSWER_UNCERTAIN"
            legs = topology.legs
            if not legs or (len(legs) == 1 and legs[0].key == key and legs[0].state == "ended"):
                return "STALE_RING"
            if len(legs) != 1 or legs[0].key != key:
                return "ANSWER_UNCERTAIN"
            if legs[0].state == "active":
                return "ACTIVE"
            if legs[0].state not in ("ringing", "answered", "unknown") or self._clock() >= deadline:
                return "ANSWER_UNCERTAIN"
            self._sleep(_POLL_S)

    def _sole_active(self, key: str) -> str:
        topology = self._read_topology()
        if topology is None:
            return "UNREADABLE"
        return "OK" if topology.legs == (CallLeg(key, "active"),) else "STALE_CALL"

    def set_speaker(self, expected_key: str, on: bool) -> SpeakerResult:
        """
        Speaker on or off for the connected call `expected_key`, by one tap on
        Google Phone's own Speaker button -- the default dialer owns the call's
        audio route, and AudioManager's setters from the shell do not move it
        (probe, 2026-10-14). The tap goes out only for the same sole ACTIVE
        call, re-read right before it, and only when the button's `checked`
        disagrees with `on`; never a second tap, which would toggle it back.
        Success is the phone's audio route read back (speaker / earpiece) with
        the mic unmuted, within 8 s; a tap alone proves nothing.
        """
        if not _CALL_KEY.fullmatch(expected_key or ""):
            return SpeakerResult("STALE_CALL", False)
        if self.reachable() != "OK":
            return SpeakerResult("NO_PHONE", False)
        deadline = self._clock() + _SPEAKER_WINDOW_S
        status = self._sole_active(expected_key)
        if status != "OK":
            return SpeakerResult(status, False)
        screen = ""
        for attempt in range(_SCREEN_TRIES):
            screen = self._read_screen()
            if "</hierarchy>" in screen or self._clock() >= deadline:
                break
            if attempt < _SCREEN_TRIES - 1:
                self._sleep(0.7)
        if "</hierarchy>" not in screen:
            return SpeakerResult("UNREADABLE", False)
        button = parse_speaker_button(screen)
        if button is None:
            return SpeakerResult("NO_BUTTON", False)
        if button.checked == on:
            done = "ALREADY"
        else:
            status = self._sole_active(expected_key)
            if status != "OK":
                return SpeakerResult(status, False)
            self.sh(f"input tap {button.center[0]} {button.center[1]}")
            done = "SPEAKER_ON" if on else "SPEAKER_OFF"
        wanted = "speaker" if on else "earpiece"
        while True:
            if parse_audio_route(self._read_audio()) == (wanted, False):
                return SpeakerResult(done, True)
            if self._clock() >= deadline:
                return SpeakerResult("ROUTE_FAILED", False)
            self._sleep(0.5)

    def audio(self, record_path: str = ""):
        from services.phone_call_audio import PhoneCallAudio

        return PhoneCallAudio(self, record_path=record_path)
