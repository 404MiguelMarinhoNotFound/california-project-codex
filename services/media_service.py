import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time
import yaml
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from services.cec_wake import CecWaker, WakeResult
from services.device_finder import (
    DeviceFinder,
    arp_table_candidates,
    port_open,
    tcp_port_candidates,
)

log = logging.getLogger(__name__)

# How long to wait before retrying after a failed connection (seconds)
_OFFLINE_COOLDOWN = 30

_WAKEFULNESS_RE = re.compile(r"mWakefulness=(\w+)")

# dumpsys hdmi_control: "    mIsActiveSource: true"
_ACTIVE_SOURCE_RE = re.compile(r"mIsActiveSource:\s*(\w+)")

# A CEC <Report Power Status> the box RECEIVED from the television. The header
# nibbles are source/destination, so "04" is source 0 (the TV) to us; the trailing
# byte is 00=on, 01=standby. finditer + take the last: the log is a capped ring.
_TV_POWER_RE = re.compile(
    r"\[R\][^\n]*<Report Power Status>\s*0[0-9A-Fa-f]:90:([0-9A-Fa-f]{2})"
)

# dumpsys media_session: "state=PlaybackState {state=3, position=240301, ..."
_SESSION_STATE_RE = re.compile(r"state=PlaybackState\s*\{state=(\d+)")
_SESSION_POSITION_RE = re.compile(r"state=PlaybackState\s*\{[^}]*?position=(-?\d+)")
# "updated=" is the box's elapsed-realtime stamp of the last state publish. A
# session an app has walked away from keeps its old line verbatim -- measured on
# the real box 2026-09-14: YouTube sat on a results page for 20s still reporting
# the previous video at state=3 with the same stamp -- so "is YouTube playing"
# cannot tell a new playback from a stale one. A changed stamp can.
_SESSION_UPDATED_RE = re.compile(r"state=PlaybackState\s*\{[^}]*?updated=(\d+)")

# After a YouTube watch deep link the session shows a loading stub first (new
# stamp, no description) and the title a second or two later -- when the video
# publishes one at all. How long to keep polling for it once the stub has
# already confirmed the launch.
_YOUTUBE_TITLE_GRACE_S = 1.5

# PlaybackState constants. 0, 2 and 3 observed on the real box; the rest are
# the documented Android values and fall back to the plain wording if they
# ever turn up.
_PLAYBACK_WORDS = {
    1: "stopped",
    2: "paused",
    3: "playing",
    6: "buffering",
}

# dumpsys audio, the STREAM_MUSIC block:
#   - STREAM_MUSIC:
#      Muted: false
#      Max: 15
#      streamVolume:15
_VOLUME_MUTED_RE = re.compile(r"^Muted:\s*(\w+)")
_VOLUME_MAX_RE = re.compile(r"^Max:\s*(\d+)")
_VOLUME_LEVEL_RE = re.compile(r"^streamVolume:\s*(\d+)")

# dumpsys media_session: "metadata: size=4, description=Fallout, The Strip, null"
_SESSION_METADATA_RE = re.compile(r"metadata:.*?\bdescription=(.*)$")

# The TV broadcasting that it is going off: <Standby> 0F:36, source 0 (the TV)
# to F (everyone). Unlike <Report Power Status> this is VOLUNTEERED, so it is
# the only evidence that survives Master Miguel using the television's own
# remote. Confirmed on the real box 2026-09-08 and captured in
# tests/fixtures/hdmi_control_standby_dump.txt.
_TV_STANDBY_RE = re.compile(r"\[R\][^\n]*<Standby>\s*0[0-9A-Fa-f]:36")

# Traffic only a television that is ON sends: enumerating the bus on its way
# up, or routing between inputs. In standby this set sends nothing but
# <Give Device Power Status> and <Standby> (fixtures hdmi_control_standby_dump
# and hdmi_control_tv_poweron_dump, 2026-09-06 / 2026-09-21), so those two stay
# out of this list. Lets the fast path see the TV come on even though nothing
# asks it for a power report once the box is already awake.
_TV_ON_TRAFFIC_RE = re.compile(
    r"\[R\][^\n]*<(?:Set Stream Path|Routing Change|Request Active Source|"
    r"Give Physical Address|Give Osd Name|Get Cec Version|Give Deck Status)>\s*0[0-9A-Fa-f]:"
)

# Every CEC line carries the BOX's own clock: "time=2026-09-05 14:18:51".
_CEC_TIME_RE = re.compile(r"time=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# How old a <Report Power Status> may be before it stops meaning anything.
#
# The TV does not volunteer its power state, it answers when ASKED, and the only
# thing that asks is the box's own wake sequence (<Give Device Power Status> ->
# <Report Power Status> one second later, visible in the fixture). So the last
# report can be hours stale, and if Master Miguel kills the TV with its own
# remote afterwards there may be no newer line at all. Reporting that as "on" is
# the same lie as the REST probe this replaced, only quieter.
_TV_POWER_MAX_AGE_S = 300


def _parse_wakefulness(dump: str) -> bool | None:
    """True awake, False asleep, None when the dump does not say."""
    match = _WAKEFULNESS_RE.search(dump or "")
    if not match:
        return None
    return match.group(1).strip().lower() == "awake"


def _parse_active_source(dump: str) -> bool | None:
    """True when the box is what the TV is showing. None is not False."""
    match = _ACTIVE_SOURCE_RE.search(dump or "")
    if not match:
        return None
    return match.group(1).strip().lower() == "true"


def _stamp_age_s(stamp: str, newest: str) -> float:
    """Seconds between two CEC log timestamps. 0.0 when either will not parse."""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(newest, fmt) - datetime.strptime(stamp, fmt)).total_seconds()
    except ValueError:
        return 0.0


def _newest_cec_stamp(dump: str) -> str | None:
    """The latest CEC log timestamp in a dumpsys hdmi_control dump, or None."""
    newest = None
    for match in _CEC_TIME_RE.finditer(dump or ""):
        stamp = match.group(1)
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _parse_tv_power(dump: str, since: str | None = None) -> str | None:
    """
    The TV's power state as the box last heard it: "on" | "standby" | None.

    `since` is a CEC log timestamp (the box's own clock, see _newest_cec_stamp);
    evidence stamped at or before it is ignored. The fast wake passes the stamp
    it read before acting, so a <Report Power Status> 01 the TV answered while
    still dark, or a stale "standby" from an hour ago, cannot masquerade as the
    answer to THIS wake. Measured 2026-09-21: after Master Miguel turned the set
    back on with its own remote the tail still read "standby" indefinitely,
    because nothing re-asked.

    THREE kinds of evidence, and whichever came LAST wins:

    - `<Report Power Status>` is an ANSWER. The only thing that asks is the
      box's own wake sequence, so on its own it goes stale the moment Master
      Miguel touches the television's remote.
    - `<Standby>` is VOLUNTEERED. The TV broadcasts it on its way off, and the
      box logs it even though it was not addressed to us. That is what makes
      "he turned it off himself" observable at all.
    - Enumeration and routing traffic (_TV_ON_TRAFFIC_RE) only comes from a
      set that is on. It is what makes "he turned it back on himself", and the
      fast path's "the TV just came up under an awake box", observable.

    Without the second, a dump whose last report says "on" from four hours ago
    and carries three <Standby> broadcasts since reads as either a confident
    lie or, with the age check, an unnecessary "I cannot tell".

    Read the tail, never the head: dumpsys keeps a capped ring of ~246 entries,
    so the first match can be days old. Then check that the tail is not itself
    stale, see _TV_POWER_MAX_AGE_S -- a television can be switched on again at
    an input the box never hears about. Both timestamps come off the same box,
    so this never touches the host clock or timezone.

    A missing or unparseable timestamp is trusted rather than discarded. The age
    check is an extra guard, not a new way to answer "I cannot tell".
    """
    last_value = None
    last_stamp = None
    newest_stamp = None
    for line in (dump or "").splitlines():
        stamp_match = _CEC_TIME_RE.search(line)
        stamp = stamp_match.group(1) if stamp_match else None
        # Lexical compare is chronological for this fixed-width format.
        if stamp and (newest_stamp is None or stamp > newest_stamp):
            newest_stamp = stamp
        if since and stamp and stamp <= since:
            continue
        report = _TV_POWER_RE.search(line)
        if report:
            last_value = {"00": "on", "01": "standby"}.get(report.group(1))
            last_stamp = stamp
        elif _TV_STANDBY_RE.search(line):
            last_value = "standby"
            last_stamp = stamp
        elif _TV_ON_TRAFFIC_RE.search(line):
            last_value = "on"
            last_stamp = stamp

    if last_value is None:
        return None
    if (
        last_stamp
        and newest_stamp
        and _stamp_age_s(last_stamp, newest_stamp) > _TV_POWER_MAX_AGE_S
    ):
        return None
    return last_value


@dataclass(frozen=True)
class MediaSession:
    """One entry from the "Sessions Stack" of `dumpsys media_session`."""
    package: str
    state: int | None
    title: str | None
    position_ms: int | None = None
    updated: int | None = None


def _parse_media_sessions(dump: str) -> list[MediaSession]:
    """
    Every media session the box is holding, in order.

    There is usually more than one. Captured off the live box with Stremio
    playing, Spotify ALSO held an active session sitting at state=0, so a flat
    "is state=3 anywhere in this dump" answers a different question than the
    one being asked and would report Spotify's playback as Stremio's.
    """
    sessions: list[MediaSession] = []
    package = None
    state = None
    title = None
    position = None
    updated = None

    def flush():
        if package is not None:
            sessions.append(MediaSession(package, state, title, position, updated))

    for line in (dump or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("package="):
            flush()
            package = stripped.split("=", 1)[1].strip()
            state = None
            title = None
            position = None
            updated = None
            continue
        if package is None:
            continue
        match = _SESSION_STATE_RE.search(stripped)
        if match:
            state = int(match.group(1))
            spot = _SESSION_POSITION_RE.search(stripped)
            # A stopped session reports position=-1, which is not a place in
            # the film and must not be read back as one.
            if spot and int(spot.group(1)) >= 0:
                position = int(spot.group(1))
            stamp = _SESSION_UPDATED_RE.search(stripped)
            if stamp:
                updated = int(stamp.group(1))
            continue
        match = _SESSION_METADATA_RE.search(stripped)
        if match:
            title = _clean_description(match.group(1))
    flush()
    return sessions


def _clean_description(raw: str) -> str | None:
    """
    MediaDescription prints as "title, subtitle, iconUri", and Stremio fills
    the first two: "Fallout, The Strip" is the show and the episode. Unset
    fields print as the literal "null", so drop those and keep the rest as he
    would want to hear it.
    """
    parts = [p.strip() for p in (raw or "").split(",")]
    parts = [p for p in parts if p and p != "null"]
    return ", ".join(parts) or None


def _parse_playing(dump: str, package: str | None = None) -> bool:
    """
    Is something actually playing? `state=3` is PlaybackState.STATE_PLAYING.

    Scoped to `package` when one is given, because more than one app holds a
    session at a time. Falls back to the flat scan only for dumps with no
    session block at all.

    StremioService._is_playing does the flat check and deliberately keeps doing
    it: it has a standalone-ADB path for when no MediaService exists, and its
    bool return is load-bearing in the autoplay retry.
    """
    sessions = _parse_media_sessions(dump)
    if not sessions:
        return "state=3" in (dump or "").lower()
    if package:
        # Asked about a specific app: no session of its own means it is not
        # playing. Falling back to "is anything playing" here would hand it
        # another app's playback, which is the bug this scoping exists to close.
        scoped = _session_for(sessions, package)
        return scoped is not None and scoped.state == 3
    return any(session.state == 3 for session in sessions)


def _session_for(sessions: list, package: str | None):
    """The named app's session, else whichever one is actually playing."""
    if package:
        for session in sessions:
            if package in session.package:
                return session
        return None
    for session in sessions:
        if session.state == 3:
            return session
    return None


def _is_new_playback(before: MediaSession | None, after: MediaSession | None) -> bool:
    """
    Did a playback start between these two readings of the same app's session?

    `after` has to be playing, and it has to differ from `before` -- a new
    stamp or a new title. Same stamp and same title is the stale line the app
    left behind, which is exactly what a results page that never started
    anything looks like.
    """
    if after is None or after.state != 3:
        return False
    if before is None:
        return True
    return after.updated != before.updated or after.title != before.title


@dataclass(frozen=True)
class YoutubePlayback:
    """
    What became of a YouTube watch deep link. `opened` is the launch itself;
    `started` is whether the session then showed a NEW playback -- None when
    the session could not be read. No __bool__ on purpose: see
    GoveeCommandResult for what a falsy result object does to callers.
    """
    opened: bool
    started: bool | None
    title: str | None


@dataclass(frozen=True)
class VolumeState:
    """The box's own STREAM_MUSIC volume. Not the television's."""
    level: int
    maximum: int
    muted: bool


def _parse_volume(dump: str) -> VolumeState | None:
    """
    STREAM_MUSIC out of `dumpsys audio`.

    Scoped to that one block on purpose: STREAM_VOICE_CALL sits above it with
    a Max of 5, and "- VOLUME GROUP AUDIO_STREAM_MUSIC" sits below with its own
    numbers, so a loose scan picks up whichever it meets first.

    This is the volume `volume_set` moves, which is what makes it worth
    reporting. It is NOT the television's volume -- that lives on the Samsung
    and is not readable over ADB.
    """
    level = maximum = None
    muted = False
    inside = False
    for line in (dump or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if inside:  # left the block we wanted
                break
            inside = stripped == "- STREAM_MUSIC:"
            continue
        if not inside:
            continue
        match = _VOLUME_MUTED_RE.match(stripped)
        if match:
            muted = match.group(1).strip().lower() == "true"
            continue
        match = _VOLUME_MAX_RE.match(stripped)
        if match:
            maximum = int(match.group(1))
            continue
        match = _VOLUME_LEVEL_RE.match(stripped)
        if match:
            level = int(match.group(1))
    if level is None or maximum is None:
        return None
    return VolumeState(level, maximum, muted)


@dataclass(frozen=True)
class HdmiState:
    """Both facts `dumpsys hdmi_control` carries, from one round trip."""
    active_source: bool | None
    tv_power: str | None
    # The box-clock stamp of the newest CEC line in the dump, so a caller can
    # ask the next read to ignore everything up to here. None when unreadable.
    newest_stamp: str | None = None


@dataclass(frozen=True)
class RoomStatus:
    """
    Everything readable about the room, as DATA. The sentence is built in the
    orchestrator, the same split as unreachable_reason / _unreachable_line.

    Every field but `reachable` is optional, and None means "could not tell"
    rather than "no". Callers drop a clause rather than guess at it.
    """
    reachable: bool
    awake: bool | None = None
    tv_power: str | None = None
    on_the_box: bool | None = None
    app: str | None = None
    playing: bool | None = None
    title: str | None = None
    playback: str | None = None      # paused / buffering / stopped, when known
    position_s: int | None = None
    volume: int | None = None
    volume_max: int | None = None
    muted: bool | None = None


class MediaService:
    def __init__(self, config: dict, cec_waker: CecWaker | None = None):
        media_cfg = config["media"]
        # mibox_ip is a HINT, not the address. The box moved .35 -> .40 on a DHCP
        # renewal and broke every tool call; self.ip is what we currently believe
        # and self.target follows it, so a rediscovery propagates everywhere.
        self.ip_hint = media_cfg["mibox_ip"]
        self.port = media_cfg["adb_port"]
        self.apps = media_cfg["apps"]
        self.app_launch_components = media_cfg.get("app_launch_components", {})
        self.app_launch_categories = media_cfg.get("app_launch_categories", {})
        self.adb_path = media_cfg.get("adb_path", "adb")
        self.adb_timeout_s = max(1, int(media_cfg.get("adb_timeout_ms", 15000))) / 1000
        self.volume_max_steps = media_cfg.get("volume_max_steps", 15)
        self.youtube_warm_launch_delay_s = media_cfg.get("youtube_warm_launch_delay_ms", 1500) / 1000
        self.youtube_profile_select_on_cold_start = media_cfg.get("youtube_profile_select_on_cold_start", True)
        self.youtube_profile_select_delay_s = media_cfg.get("youtube_profile_select_delay_ms", 1200) / 1000
        # A search deep link stops at the results page. See youtube_play_video
        # and services/youtube_search.py.
        self.youtube_search_autoplay = bool(media_cfg.get("youtube_search_autoplay", True))
        self.youtube_search_resolve_timeout_s = max(1, int(media_cfg.get("youtube_search_resolve_timeout_ms", 5000))) / 1000
        self.youtube_search_verify_s = max(0, int(media_cfg.get("youtube_search_verify_ms", 6000))) / 1000
        self.ui_dump_retry_count = max(1, int(media_cfg.get("ui_dump_retry_count", 2)))
        self.ui_dump_retry_delay_s = max(0, int(media_cfg.get("ui_dump_retry_delay_ms", 700)) / 1000)
        self.ui_dump_timeout_s = max(1, int(media_cfg.get("ui_dump_timeout_ms", 6000))) / 1000

        # Wake. ADB can put the box to sleep, and can bring it back only while
        # it is still on the LAN (shallow standby). From deep standby turn_on
        # goes through the television over CEC. See services/cec_wake.py.
        wake_cfg = media_cfg.get("cec_wake", {}) or {}
        self.wake_settle_s = max(0, int(wake_cfg.get("settle_ms", 25000))) / 1000
        self.wake_poll_interval_s = max(0.1, int(wake_cfg.get("poll_interval_ms", 2000)) / 1000)
        self.wake_attempts = max(1, int(wake_cfg.get("wake_attempts", 3)))
        self.mibox_hdmi_port = int(wake_cfg.get("mibox_hdmi_port", 2))
        # Fast path budgets: the box half and the TV half each get
        # fast_wake_timeout, then the CEC bus gets tv_confirm_timeout to say the
        # television is on and showing the box.
        self.fast_wake_timeout_s = max(1, int(wake_cfg.get("fast_wake_timeout_ms", 20000))) / 1000
        self.tv_confirm_timeout_s = max(0, int(wake_cfg.get("tv_confirm_timeout_ms", 12000))) / 1000
        self.tv_confirm_poll_s = max(0.1, int(wake_cfg.get("tv_confirm_poll_ms", 1500)) / 1000)
        # A box that just woke re-asserts itself as active source through its
        # own One Touch Play about a second later. Selecting the input before
        # that lands costs ~18s of key traffic (measured 2026-09-21: 20.7s vs
        # 2.2s for the same wake), so a parked input gets this long to fix
        # itself before ensure_active_source() is called.
        self.otp_grace_s = max(0, int(wake_cfg.get("otp_grace_ms", 4000))) / 1000
        self.cec_waker = cec_waker if cec_waker is not None else CecWaker(config)

        # media.power.tv_only_standby: turn_off puts only the television into
        # standby and leaves the box awake, so the next turn_on never pays for a
        # resume from deep standby. Needs hdmi_control_auto_device_off_enabled=0
        # on the box or the TV's own standby drags the box down with it.
        power_cfg = media_cfg.get("power", {}) or {}
        self.tv_only_standby = bool(power_cfg.get("tv_only_standby", False))

        # Set by turn_on so the dispatcher can tell a rejected pairing token
        # (needs a human at the screen) and an unconfirmed television from any
        # other wake outcome. last_input_result is the same for input selection.
        self.last_wake_result = None
        self.last_input_result = None

        # Discovery. Identity keys off the MAC and ro.serialno, both stable; the
        # address is cached, never written back to config.yaml.
        disc_cfg = media_cfg.get("discovery", {}) or {}
        self.discovery_enabled = bool(disc_cfg.get("enabled", False))
        self.mibox_mac = str(disc_cfg.get("mibox_mac") or "").strip()
        self.mibox_serial = str(disc_cfg.get("mibox_serial") or "").strip()
        self.port_probe_timeout_s = max(0.05, int(disc_cfg.get("port_probe_timeout_ms", 300)) / 1000)
        self.rescan_cooldown_s = max(0, int(disc_cfg.get("rescan_cooldown_ms", 120000)) / 1000)
        self._finder = DeviceFinder(
            key="mibox",
            label="Mi Box",
            mac=self.mibox_mac,
            hint=self.ip_hint,
            cache_path=Path(str(disc_cfg.get("state_path") or "device_state.json")),
            verify=self._verify_box,
            candidate_sources=[
                # Warm ARP first (0.16s, and warm right after a DHCP renewal), then
                # a TCP scan of the /24 (1.55s). No ping flood: the scan's SYNs warm
                # the ARP table anyway, which is what the failure classifier reads.
                arp_table_candidates(self.mibox_mac),
                tcp_port_candidates(
                    self.port,
                    timeout_s=self.port_probe_timeout_s,
                    workers=max(1, int(disc_cfg.get("scan_workers", 64))),
                ),
            ],
        )
        # Believed address. Reads the cache file only -- construction never
        # touches the network, matching CecWaker's contract.
        self.ip = self._finder.cached_or_hint() if self.discovery_enabled else self.ip_hint
        self._last_discovery_t: float = 0
        # Why the box is unreachable, for the dispatcher's spoken line. "off",
        # "moved" and "ADB disabled" need different answers from Master Miguel.
        self.unreachable_reason = ""

        # Connection state tracking
        self._connected = False
        self._last_fail_time: float = 0  # monotonic timestamp of last failed reconnect

        # Cleared once per YouTube cold start; see _prepare_youtube_launch.
        self._youtube_profile_cleared: bool = False

    @property
    def target(self) -> str:
        """`ip:port` for the address we currently believe in. Never assigned."""
        return f"{self.ip}:{self.port}"

    def _adb(self, command: str, use_target: bool = True, timeout_s: float | None = None) -> tuple[bool, str]:
        adb = self.adb_path
        cmd = f'"{adb}" -s {self.target} {command}' if use_target else f'"{adb}" {command}'
        log.debug(f"ADB exec: {cmd}")
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s if timeout_s is not None else self.adb_timeout_s,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            ok = result.returncode == 0
            elapsed = time.monotonic() - t0
            if stderr:
                log.debug(f"ADB stderr: {stderr}")
            if not ok:
                log.warning(f"ADB failed (rc={result.returncode}): cmd={cmd} | stdout={stdout} | stderr={stderr}")
            else:
                log.debug(f"ADB ok: {stdout}")
            log.info("[timing] ADB cmd='%s' took %.3fs ok=%s", command[:80], elapsed, ok)
            return ok, stdout
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            log.warning(f"ADB command timed out: {cmd}")
            log.info("[timing] ADB cmd='%s' timed out after %.3fs", command[:80], elapsed)
            return False, "timeout"

    def _adb_exec(
        self,
        *args: str,
        use_target: bool = True,
        capture_text: bool = True,
        timeout_s: float | None = None,
    ) -> tuple[bool, str | bytes]:
        cmd = [self.adb_path]
        if use_target:
            cmd.extend(["-s", self.target])
        cmd.extend(args)
        log.debug("ADB exec list: %s", cmd)
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=capture_text,
                encoding="utf-8" if capture_text else None,
                errors="replace" if capture_text else None,
                check=False,
                timeout=timeout_s if timeout_s is not None else self.adb_timeout_s,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            log.warning("ADB command timed out: %s", cmd)
            log.info("[timing] ADB exec %s timed out after %.3fs", args[:2], elapsed)
            return False, "timeout" if capture_text else b""

        elapsed = time.monotonic() - t0
        if capture_text:
            output = (result.stdout or result.stderr or "").strip()
        else:
            output = result.stdout if result.stdout else result.stderr
        ok = result.returncode == 0
        log.info("[timing] ADB exec %s took %.3fs ok=%s", args[:2], elapsed, ok)
        return ok, output

    def _verify_box(self, ip: str) -> bool:
        """
        Is the box at `ip`? Port probe first, THEN adb. Never the other way round.

        Measured 2026-09-05: `adb connect` to a host with 5555 closed takes 21.1s
        and the timeout is not tunable from the command line. The 0.3s socket probe
        is the only thing that keeps a /24 sweep from costing 254 * 21s.

        ro.serialno is the duid-equivalent: a stale address that another Android
        device with wireless debugging on has taken would `adb connect` happily and
        then accept keyevents.
        """
        if not ip or not port_open(ip, self.port, self.port_probe_timeout_s):
            return False
        target = f"{ip}:{self.port}"
        _, output = self._adb(f"connect {target}", use_target=False)
        if "connected" not in (output or "").lower():
            return False
        ok, serial = self._adb(f"-s {target} shell getprop ro.serialno", use_target=False)
        if ok and (serial or "").strip() == self.mibox_serial:
            return True
        # Leaving a stranger's transport open pollutes `adb devices` and makes an
        # unqualified `adb shell` ambiguous for anything else on this machine.
        self._adb(f"disconnect {target}", use_target=False)
        return False

    def connect(self) -> bool:
        # Gate on the cheap socket probe first: see _verify_box for why. This also
        # turns every failed poll in _wait_for_box from 21s into 0.3s, which is what
        # lets that loop actually poll across its settle window.
        if self.discovery_enabled and not port_open(self.ip, self.port, self.port_probe_timeout_s):
            self._connected = False
            self._last_fail_time = time.monotonic()
            log.info("ADB connect skipped: %s has no listener on %s", self.ip, self.port)
            return False
        # adb connect doesn't need -s, it's a global command
        _, output = self._adb(f"connect {self.target}", use_target=False)
        success = "connected" in output.lower()
        self._connected = success
        if not success:
            self._last_fail_time = time.monotonic()
        else:
            self._youtube_profile_cleared = False
            self.unreachable_reason = ""
        log.info(f"ADB connect -> '{output}' (success={success})")
        return success

    def ensure_connected(self) -> bool:
        # 1. Warm transport. `adb -s` against a target with no open transport errors
        #    out in 0.21s (measured), so this stays cheap even when the box is gone.
        ok, output = self._adb("shell echo ping")
        if ok:
            self._connected = True
            self.unreachable_reason = ""
            return True
        self._connected = False

        # 2. Reconnect at the address we currently believe in, behind the existing
        #    cooldown. Unchanged behaviour, just no longer 21s per attempt.
        if self._last_fail_time:
            elapsed = time.monotonic() - self._last_fail_time
            if elapsed < _OFFLINE_COOLDOWN:
                log.debug(f"TV offline, skipping reconnect ({_OFFLINE_COOLDOWN - elapsed:.0f}s cooldown remaining)")
                self.unreachable_reason = "cooldown"
                return False

        log.info(f"ADB ping failed (output='{output}'), reconnecting...")
        if self.connect():
            return True

        # 3. It may simply have moved. Separate gate and separate budget from the
        #    offline cooldown, because they answer different questions: "don't retry
        #    a connect that just failed" vs "don't rescan a LAN we just scanned".
        return self._rediscover_and_connect()

    def _rediscover_and_connect(self) -> bool:
        if not self.discovery_enabled:
            return False
        now = time.monotonic()
        if self._last_discovery_t and now - self._last_discovery_t < self.rescan_cooldown_s:
            return False
        self._last_discovery_t = now

        found = self._finder.resolve(force=True)
        if not found:
            self.unreachable_reason = self._classify_failure()
            return False
        if found != self.ip:
            log.warning(
                "Mi Box moved from %s to %s. media.mibox_ip (%s) is only a hint; "
                "the discovery cache has been updated and config needs no edit.",
                self.ip, found, self.ip_hint,
            )
        self.ip = found
        return self.connect()

    def _classify_failure(self) -> str:
        """
        Why is the box unreachable? These need different answers from Master Miguel.

        Free, because the TCP scan that just ran SYNed every host on the subnet and
        so populated the ARP table for everything that exists.
        """
        # Something answered on the adb port and was rejected -- the port scan only
        # yields hosts with 5555 open, so a rejection there means the serial did not
        # match. Another Android device with wireless debugging on has the address
        # we expected, which is a different problem from the box being absent.
        if any(step.rung == "tcp_port_candidates" and step.verdict == "rejected"
               for step in self._finder.last_trace):
            return "identity_mismatch"

        if not self.mibox_mac:
            return "unknown"
        from services.device_finder import _ips_for_mac, _read_arp_table

        addresses = _ips_for_mac(_read_arp_table(), self.mibox_mac)
        if not addresses:
            return "not_on_lan"
        # It is on the network but nothing answered on the adb port. This is the
        # persist.adb.tcp.port case: ADB over TCP does not survive a reboot, and no
        # amount of retrying or rediscovery can fix it.
        log.warning(
            "Mi Box is on the LAN at %s but nothing is listening on %s. ADB over "
            "Wi-Fi does not survive a reboot on this box (persist.adb.tcp.port is "
            "unset). Re-enable it in Developer options, or run `adb tcpip 5555` "
            "over USB. Discovery cannot fix this.",
            ", ".join(addresses), self.port,
        )
        return "no_adb_port"

    # --- Playback ---

    def play_pause(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_PLAY_PAUSE")[0]

    def stop(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_STOP")[0]

    def next_track(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_NEXT")[0]

    def prev_track(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_PREVIOUS")[0]

    def fast_forward(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_FAST_FORWARD")[0]

    def rewind(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_MEDIA_REWIND")[0]

    # --- Volume ---

    def volume_up(self, steps: int = 3) -> bool:
        if not self.ensure_connected():
            return False
        for _ in range(steps):
            self._adb("shell input keyevent KEYCODE_VOLUME_UP")
        return True

    def volume_down(self, steps: int = 3) -> bool:
        if not self.ensure_connected():
            return False
        for _ in range(steps):
            self._adb("shell input keyevent KEYCODE_VOLUME_DOWN")
        return True

    def mute(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_VOLUME_MUTE")[0]

    def volume_set(self, percent: int) -> bool:
        """Set volume to approximate percentage. Floors to 0 then steps up."""
        if not self.ensure_connected():
            return False
        percent = max(0, min(100, percent))
        target_steps = round(self.volume_max_steps * percent / 100)
        log.info(f"volume_set({percent}%) -> floor then {target_steps}/{self.volume_max_steps} steps up")
        # Floor volume
        for _ in range(self.volume_max_steps):
            self._adb("shell input keyevent KEYCODE_VOLUME_DOWN")
        # Step up to target
        for _ in range(target_steps):
            self._adb("shell input keyevent KEYCODE_VOLUME_UP")
        return True

    # --- Navigation ---

    def go_home(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_HOME")[0]

    def go_back(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_BACK")[0]

    # --- App launching ---

    def _app_package(self, app_name: str) -> str | None:
        return self.apps.get((app_name or "").strip().lower())

    def _app_launch_component(self, app_name: str) -> str | None:
        return self.app_launch_components.get((app_name or "").strip().lower())

    def _app_launch_category(self, app_name: str) -> str | None:
        return self.app_launch_categories.get((app_name or "").strip().lower())

    def is_app_foreground(self, app_name: str) -> bool:
        package = self._app_package(app_name)
        if not package:
            return False
        current_app = self.get_current_app()
        return current_app == app_name.lower() or package in current_app

    def launch_app(self, app_name: str) -> tuple[bool, str]:
        normalized_name = (app_name or "").strip().lower()
        package = self._app_package(normalized_name)
        if not package:
            known = ", ".join(self.apps.keys())
            log.warning(f"Unknown app '{app_name}', known apps: {known}")
            return False, f"I don't have {app_name} in my app list"
        if not self.ensure_connected():
            return False, "I can't reach the TV right now"
        component = self._app_launch_component(normalized_name)
        if component:
            category = self._app_launch_category(normalized_name) or "android.intent.category.LAUNCHER"
            log.info("Launching %s via explicit activity %s", normalized_name, component)
            ok, output = self.start_activity(
                component=component,
                action="android.intent.action.MAIN",
                category=category,
                wait=False,
            )
            log.info("explicit start result: ok=%s, output=%s", ok, output)
            if ok:
                return True, f"Opening {normalized_name}"
            log.info("Explicit launch failed for %s, falling back to package launch", normalized_name)
            return self.launch_package(normalized_name)

        return self.launch_package(normalized_name)

    def launch_package(self, app_name: str) -> tuple[bool, str]:
        normalized_name = (app_name or "").strip().lower()
        package = self._app_package(normalized_name)
        if not package:
            known = ", ".join(self.apps.keys())
            log.warning(f"Unknown app '{app_name}', known apps: {known}")
            return False, f"I don't have {app_name} in my app list"
        if not self.ensure_connected():
            return False, "I can't reach the TV right now"

        log.info(f"Launching {normalized_name} ({package}) via monkey")
        ok, output = self._adb(
            f"shell monkey -p {package} -c android.intent.category.LAUNCHER 1")
        log.info(f"monkey result: ok={ok}, output={output}")
        return ok, f"Opening {normalized_name}" if ok else f"Couldn't open {normalized_name}"

    def force_stop_app(self, app_name: str) -> bool:
        package = self._app_package(app_name)
        if not package or not self.ensure_connected():
            return False
        log.info("Force-stopping %s (%s)", app_name, package)
        ok = self._adb(f"shell am force-stop {package}")[0]
        if ok and (app_name or "").strip().lower() == "youtube":
            self._youtube_profile_cleared = False
        return ok

    def start_activity(
        self,
        component: str,
        action: str | None = None,
        category: str | None = None,
        data_url: str | None = None,
        wait: bool = True,
    ) -> tuple[bool, str]:
        if not component:
            return False, "missing activity component"
        if not self.ensure_connected():
            return False, "I can't reach the TV right now"

        parts = ["shell", "am", "start"]
        if wait:
            parts.append("-W")
        parts.extend(["-n", component])
        if action:
            parts.extend(["-a", action])
        if category:
            parts.extend(["-c", category])
        if data_url:
            parts.extend(["-d", data_url])

        command = " ".join(parts)
        log.debug("Starting activity via adb: %s", command)
        ok, output = self._adb(command)
        success = ok and "error:" not in output.lower()
        if not success:
            log.warning("Activity start failed for %s: %s", component, output)
        return success, output

    def dump_ui_hierarchy(self) -> str:
        if not self.ensure_connected():
            return ""
        remote_path = "/sdcard/window_dump.xml"
        for attempt in range(1, self.ui_dump_retry_count + 1):
            ok, dump_output = self._adb(
                f"shell uiautomator dump --compressed {remote_path}",
                timeout_s=self.ui_dump_timeout_s,
            )
            if ok and dump_output and "error:" not in dump_output.lower():
                break
            log.warning(
                "UI dump attempt %d/%d failed: %s",
                attempt,
                self.ui_dump_retry_count,
                dump_output or "no output",
            )
            if attempt < self.ui_dump_retry_count and self.ui_dump_retry_delay_s > 0:
                time.sleep(self.ui_dump_retry_delay_s)
        else:
            log.warning("UI dump never succeeded, returning empty XML instead of stale dump")
            return ""

        ok, output = self._adb(f"shell cat {remote_path}")
        if not ok:
            log.warning("Failed reading UI dump file after successful dump: %s", output)
            return ""
        log.debug("UI dump captured (%d chars)", len(output))
        return output

    def tap(self, x: int, y: int) -> bool:
        log.debug("Input tap at (%s, %s)", int(x), int(y))
        return self.ensure_connected() and self._adb(f"shell input tap {int(x)} {int(y)}")[0]

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 250) -> bool:
        command = f"shell input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"
        log.debug(
            "Input swipe from (%s, %s) to (%s, %s) over %sms",
            int(x1),
            int(y1),
            int(x2),
            int(y2),
            int(duration_ms),
        )
        return self.ensure_connected() and self._adb(command)[0]

    def keyevent(self, key: str | int) -> bool:
        key_name = f"KEYCODE_{key}" if isinstance(key, str) and not str(key).startswith("KEYCODE_") else key
        log.debug("Input keyevent %s", key_name)
        return self.ensure_connected() and self._adb(f"shell input keyevent {key_name}")[0]

    def capture_screenshot(self, local_path: str | Path) -> bool:
        if not self.ensure_connected():
            return False

        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        remote_path = "/sdcard/california_capture.png"

        ok, _ = self._adb(f"shell screencap -p {remote_path}")
        if not ok:
            return False

        ok, _ = self._adb(f'shell cat {remote_path} > "{destination}"')
        self._adb(f"shell rm {remote_path}")
        return ok

    def capture_screenshot_bytes(self) -> bytes:
        if not self.ensure_connected():
            return b""

        ok, output = self._adb_exec("exec-out", "screencap", "-p", capture_text=False)
        if not ok:
            return b""
        return output if isinstance(output, bytes) else b""

    # --- YouTube ---

    def _youtube_package(self) -> str:
        return self._app_package("youtube") or "com.google.android.youtube.tv"

    def _youtube_is_foreground(self) -> bool:
        return self.is_app_foreground("youtube")

    _YOUTUBE_PROFILE_PICKER_MARKERS = [
        "who's watching",
        "who\u2019s watching",
        "whos watching",
    ]

    _YOUTUBE_PROFILE_ACTIVITY_MARKERS = (
        "profile",
        "switcher",
        "accountselect",
        "accountpicker",
        "account_select",
        "signin",
        "sign_in",
    )

    def _detect_youtube_profile_picker_fast(self) -> tuple[bool, str]:
        """Classify the current foreground activity without a UI dump.

        Returns (should_press_dpad_center, detection_source):
        - focus is the YouTube package with a non-picker activity -> skip press
        - focus is the YouTube package with a profile-picker-ish activity -> press
        - focus is empty / unknown / different package -> press (safe fallback)
        """
        focus = self.get_current_focus()
        if not focus:
            return True, "focus_empty"

        focus_lower = focus.lower()
        youtube_pkg = self._youtube_package().lower()
        if youtube_pkg not in focus_lower:
            return True, f"focus_other:{focus}"

        if "/" in focus:
            activity = focus.split("/", 1)[1].lower()
        else:
            activity = focus_lower

        for marker in self._YOUTUBE_PROFILE_ACTIVITY_MARKERS:
            if marker in activity:
                return True, f"focus_picker:{marker}"

        return False, f"focus_main:{activity}"

    def _detect_youtube_profile_picker(self) -> tuple[bool, str]:
        """Check if the YouTube profile picker is showing.

        Returns (should_press_dpad_center, detection_source).
        Falls back to True when the UI dump fails, preserving current behavior.
        """
        xml = self.dump_ui_hierarchy()
        if not xml:
            log.info("YouTube profile picker: UI dump empty, falling back to press")
            return True, "ui_dump_failed_fallback"

        xml_lower = xml.lower()
        for marker in self._YOUTUBE_PROFILE_PICKER_MARKERS:
            if marker in xml_lower:
                return True, f"marker_found:{marker}"

        return False, "no_marker_found"

    def _prepare_youtube_launch(self) -> bool:
        t_start = time.monotonic()

        if self._youtube_is_foreground():
            log.info("[timing] _prepare_youtube_launch: already foreground, skipped in %.3fs", time.monotonic() - t_start)
            return True

        t_launch = time.monotonic()
        ok, _ = self.launch_app("youtube")
        log.info("[timing] _prepare_youtube_launch: launch_app took %.3fs ok=%s", time.monotonic() - t_launch, ok)
        if not ok:
            return False

        if self.youtube_warm_launch_delay_s > 0:
            t_warm = time.monotonic()
            time.sleep(self.youtube_warm_launch_delay_s)
            log.info("[timing] _prepare_youtube_launch: warm_delay took %.3fs", time.monotonic() - t_warm)

        if self.youtube_profile_select_on_cold_start and not self._youtube_profile_cleared:
            t_detect = time.monotonic()
            should_press, detection_source = self._detect_youtube_profile_picker_fast()
            log.info(
                "[timing] _prepare_youtube_launch: profile_detect took %.3fs should_press=%s source=%s",
                time.monotonic() - t_detect,
                should_press,
                detection_source,
            )
            if should_press:
                self.keyevent("DPAD_CENTER")
                if self.youtube_profile_select_delay_s > 0:
                    t_profile = time.monotonic()
                    time.sleep(self.youtube_profile_select_delay_s)
                    log.info("[timing] _prepare_youtube_launch: profile_select_delay took %.3fs", time.monotonic() - t_profile)
            self._youtube_profile_cleared = True
        elif self._youtube_profile_cleared:
            log.info("[timing] _prepare_youtube_launch: profile_detect skipped (cleared earlier this session)")

        log.info("[timing] _prepare_youtube_launch: total %.3fs", time.monotonic() - t_start)
        return True

    def _open_youtube_url(self, url: str) -> bool:
        if not self.ensure_connected():
            return False
        if not self._prepare_youtube_launch():
            return False

        return self._adb(
            f'shell am start -a android.intent.action.VIEW -d "{url}" {self._youtube_package()}'
        )[0]

    def youtube_playlist(self, playlist_id: str) -> bool:
        if not playlist_id:
            return False

        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        return self._open_youtube_url(url)

    def youtube_search(self, query: str) -> bool:
        if not query:
            return False

        encoded = quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={encoded}"
        return self._open_youtube_url(url)

    def youtube_watch(self, video_id: str) -> bool:
        """Open one video in the player. Unlike the results page, this PLAYS."""
        if not video_id:
            return False
        return self._open_youtube_url(f"https://www.youtube.com/watch?v={video_id}")

    def _youtube_session(self) -> tuple[bool, MediaSession | None]:
        """
        YouTube's own media session, as (readable, session). Two Nones are not
        the same None: an unreadable dump is "cannot tell", a readable dump with
        no YouTube block is "YouTube has not played since boot", which is a
        perfectly good baseline to confirm a launch against.
        """
        ok, output = self._adb("shell dumpsys media_session")
        if not ok or not output:
            return False, None
        return True, _session_for(_parse_media_sessions(output), self._youtube_package())

    def _wait_for_new_youtube_playback(self, before: MediaSession | None) -> tuple[bool | None, str | None]:
        """
        Poll the session until a playback newer than `before` shows up.

        The first line after the launch is a loading stub (state=3,
        position=0, no description, new stamp); the title lands a second or
        two later. A new stamp is enough to believe the launch landed, but the
        title is worth a short wait, so keep polling for it inside the grace.
        """
        deadline = time.monotonic() + self.youtube_search_verify_s
        readable = False
        started: MediaSession | None = None
        while True:
            ok, session = self._youtube_session()
            readable = readable or ok
            if ok and _is_new_playback(before, session):
                if started is None:
                    # Some results never publish a description at all (seen
                    # live: playing, position advancing, description still
                    # "null, null, null" 20s in). Give the title a short
                    # grace, not the whole window -- he is waiting to hear back.
                    deadline = min(deadline, time.monotonic() + _YOUTUBE_TITLE_GRACE_S)
                started = session
                if session.title:
                    return True, session.title
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if started is not None:
            return True, started.title
        return (False if readable else None), None

    def youtube_play_video(self, video_id: str) -> YoutubePlayback:
        """
        Play one video by id and confirm it from the session.

        Measured on the real box 2026-09-14: `watch?v=` plays directly, the
        loading stub shows within ~0.5s and the title at ~2s, and firing it
        for a video that is ALREADY playing restarts it from zero with a new
        stamp -- so the before/after comparison in _is_new_playback holds in
        every case. No key presses anywhere on this path: the DPAD approach it
        replaced could not tell a video from a playlist from a channel at the
        top of the results, and its retry press was play/pause in the player.

        The baseline is read BEFORE the launch. Read after it, a fast launch
        would already be in the baseline and then be compared against itself,
        reading as "nothing changed". Before is the only honest baseline.
        """
        if not self.ensure_connected():
            return YoutubePlayback(opened=False, started=False, title=None)
        _, before = self._youtube_session()
        if not self.youtube_watch(video_id):
            return YoutubePlayback(opened=False, started=False, title=None)
        started, title = self._wait_for_new_youtube_playback(before)
        return YoutubePlayback(opened=True, started=started, title=title)

    # --- Power ---
    #
    # Turning the box on and turning it off are NOT symmetric, and nothing here
    # should pretend they are. Off is one ADB keyevent. On depends on how deep
    # the box went: while it is still on the LAN (shallow standby, or awake with
    # the television dark) one KEYCODE_WAKEUP and the television's own power-on
    # are all it takes, run side by side; once it has suspended its radio only
    # the television can reach it, over HDMI-CEC. That asymmetry is why
    # KEYCODE_POWER is never sent blind: it is a toggle, so firing it at an
    # already-awake box turns the TV off and ends every other form of control
    # until someone picks up the remote.

    def is_awake(self) -> bool | None:
        """
        True if awake, False if asleep but reachable, None if unreachable.

        None is the interesting one: it means the box is in deep standby with
        its Wi-Fi radio down, which is the only state that needs the CEC path.
        """
        if not self.ensure_connected():
            return None
        ok, output = self._adb("shell dumpsys power")
        if not ok or not output:
            return None
        return _parse_wakefulness(output)

    def turn_on(self) -> bool:
        """
        Get the box awake and the television on and showing it.

        Returns True when the box is awake and reachable. The television's side
        of the story is in last_wake_result.tv_confirmed: True (seen on the CEC
        bus), False (definitely still dark or on another input), None (could
        not tell). Callers must read that field, never truthiness-test the
        result for it -- see WakeResult.
        """
        self.last_wake_result = None
        state = self.is_awake()
        if state is None:
            return self._wake_and_wait()  # Deep standby: only the TV can reach it.
        if state is True:
            hdmi = self.hdmi_state()
            if hdmi.tv_power == "on":
                if hdmi.active_source is True:
                    # Already on. Sending anything here risks turning it off.
                    self.last_wake_result = WakeResult(True, "already on", tv_confirmed=True)
                    return True
                if hdmi.active_source is False:
                    # TV on, parked elsewhere: select the input, no wake needed.
                    selected = self.ensure_active_source() is True
                    pairing = getattr(self.last_input_result, "needs_pairing", False) is True
                    self.last_wake_result = WakeResult(
                        True, "TV on; box selected" if selected else "TV on another input",
                        needs_pairing=pairing, tv_confirmed=selected)
                    return True
                # TV on, input unknown: nothing to do that would not be a guess.
                self.last_wake_result = WakeResult(True, "TV on, input unknown", tv_confirmed=None)
                return True
            # TV in standby, or unknown: power the television. Evidence older
            # than this read is hidden from the confirm loop.
            return self._turn_on_fast(box_asleep=False, since=hdmi.newest_stamp,
                                      tv_dark=hdmi.tv_power == "standby")
        return self._turn_on_fast(box_asleep=True)

    def turn_off(self) -> bool:
        if self.is_awake() is None:
            return True  # Unreachable already means off.
        if not self.tv_only_standby:
            # The box sleeps and, with mAutoTvOff, tells the television to
            # follow. The firmware force-suspends ~15s later regardless of any
            # wakelock (measured 2026-09-21), so the next turn_on pays for a
            # resume from deep standby unless something wakes it first.
            return self._adb("shell input keyevent KEYCODE_SLEEP")[0]
        # Television-only standby: stop whatever is playing so a stream does not
        # keep running into a dark screen, park on Home, then put only the TV
        # into standby. The box stays awake and reachable.
        self._adb("shell input keyevent KEYCODE_MEDIA_STOP")
        self.go_home()
        return self._standby_tv_only()

    def _standby_tv_only(self) -> bool:
        """
        Put the television into standby and leave the box awake.

        KEY_POWEROFF over the websocket is discrete, so it is safe regardless of
        what the CEC tail last said -- and **this UE49M5505 ignores it**
        (measured 2026-09-21: two presses, no <Standby> on the bus, TV stayed
        on). The next candidate is KEYCODE_TV_POWER from the box, which is
        Android's own query-then-act (<Give Device Power Status> first, then
        <Standby> or One Touch Play), still unmeasured. Until one of them is
        proven the mode cannot deliver a dark television, which is why
        tv_only_standby ships off. Never KEYCODE_SLEEP here: that is the whole
        point of the mode.
        """
        result = self.cec_waker.standby_tv()
        self.last_wake_result = result
        if not result:
            log.warning("Television standby did not go out: %s", result.detail)
        return bool(result)

    def _send_wakeup(self) -> bool:
        """KEYCODE_WAKEUP: a no-op when already awake, never KEYCODE_POWER."""
        return self._adb("shell input keyevent KEYCODE_WAKEUP")[0]

    def _wait_for_awake(self, timeout_s: float) -> bool:
        """
        Poll is_awake() until True, for a box that was reachable a moment ago.

        Unlike _wait_for_box this NEVER rediscovers and never clears the
        cooldowns: the precondition is a box on the LAN, and a miss here means
        it went deep between the check and the key, which is _wake_and_wait's
        job. It also keeps this thread from running a /24 scan while the TV
        thread is running its own.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self.is_awake() is True:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(self.tv_confirm_poll_s, 1.0))

    def _tv_half(self, timeout_s: float) -> WakeResult:
        """
        The television's half of a fast wake, on its own thread.

        Touches ONLY self.cec_waker -- never _adb, never self.ip, never the
        cooldowns -- so it can run beside the box half without a lock. Wake-on-LAN
        is what brings a television out of DEEP standby; it does nothing to one
        in shallow standby, and the box's own wake (<Text View On> + <Active
        Source>) or the KEY_HDMI pair does that part, so this only has to get
        the set answering. Every exception becomes a result: a traceback on a
        worker thread must never take the turn down.
        """
        try:
            if not self.cec_waker.available:
                return WakeResult(False, self.cec_waker.unavailable_reason)
            if self.cec_waker.power_on_tv(timeout_s=timeout_s):
                return WakeResult(True, "TV answering")
            return WakeResult(False, "TV did not answer after Wake-on-LAN")
        except Exception as exc:  # noqa: BLE001 - reported, never raised across the join
            log.error("TV half of the wake failed: %s", exc)
            return WakeResult(False, f"TV half failed: {exc}")

    def _turn_on_fast(self, box_asleep: bool, since: str | None = None,
                      tv_dark: bool = False) -> bool:
        """
        Box half and TV half side by side, then confirm on the CEC bus.

        Thread invariant: during the parallel window the box thread is the only
        _adb caller and the TV thread never touches MediaService state, so _adb
        needs no lock and hdmi_state() is read only after both futures joined.
        If the box half fails (adbd suspended between the check and the key, or
        it never reported awake) this falls through to the CEC chain; the second
        Wake-on-LAN in there is a documented no-op.

        `tv_dark` is the pre-check's verdict that the television is in standby
        under an awake box. Nothing will re-ask its power then -- the box's own
        wake sequence is what asks, and the box is not waking -- so the proven
        KEY_HDMI pair goes out as soon as the set answers REST, and the confirm
        loop reads the enumeration traffic the TV emits as it comes up.
        """
        started = time.monotonic()
        budget = self.fast_wake_timeout_s

        def box_half() -> bool:
            if not box_asleep:
                return True
            return self._send_wakeup() and self._wait_for_awake(budget)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="wake") as pool:
            box_future = pool.submit(box_half)
            tv_future = pool.submit(self._tv_half, budget)
            box_ok = box_future.result()
            tv_result = tv_future.result()
        box_at = time.monotonic() - started

        if not box_ok:
            log.warning("Box did not wake over ADB in %.1fs, falling back to the CEC chain", box_at)
            return self._wake_and_wait()

        if not tv_result:
            log.warning("TV half did not complete: %s", tv_result.detail)

        self.last_input_result = None
        confirmed = self._confirm_tv_showing_box(self.tv_confirm_timeout_s, since=since,
                                                 nudge_first=tv_dark and bool(tv_result))
        elapsed = time.monotonic() - started
        needs_pairing = getattr(self.last_input_result, "needs_pairing", False) is True
        if confirmed is True:
            detail = f"box awake in {box_at:.1f}s, TV showing it at {elapsed:.1f}s"
        elif confirmed is False:
            detail = f"box awake in {box_at:.1f}s; TV not showing it after {elapsed:.1f}s"
        else:
            detail = f"box awake in {box_at:.1f}s; TV unconfirmed after {elapsed:.1f}s"
        if not tv_result:
            detail += f" ({tv_result.detail})"
        if needs_pairing:
            detail = f"{self.last_input_result.detail}; {detail}"
        self.last_wake_result = WakeResult(True, detail, needs_pairing=needs_pairing,
                                           tv_confirmed=confirmed)
        log.info("Fast wake: %s", detail)
        return True

    def _confirm_tv_showing_box(self, timeout_s: float, since: str | None = None,
                                nudge_first: bool = False) -> bool | None:
        """
        Watch the CEC bus until the television is on and showing the box.

        One `dumpsys hdmi_control` per pass. True when tv_power is "on" and the
        box is the active source. The input is selected ONLY on a definitive
        active_source False -- once the TV reports on, or once at the deadline if
        its power never became known -- never on None, because cycling from an
        unknown input is not idempotent. A television still reporting "standby"
        at the deadline is False; nothing heard either way is None.

        `since` hides evidence older than the wake, see _parse_tv_power.
        `nudge_first` sends the pair before the first read, for a television
        the caller already knows is dark. A parked input is left alone for
        otp_grace_s first: the box's own One Touch Play usually fixes it.
        """
        started = time.monotonic()
        deadline = started + timeout_s
        last = HdmiState(None, None)
        nudged = False
        if nudge_first and self.cec_waker.available:
            nudged = True
            result = self.cec_waker.press_input_pair()
            self.last_input_result = result
            if getattr(result, "needs_pairing", False) is True:
                return None
        while True:
            last = self.hdmi_state(since=since)
            if last.tv_power == "on":
                if last.active_source is True:
                    return True
                if last.active_source is False and time.monotonic() - started >= self.otp_grace_s:
                    return self.ensure_active_source() is True
            elif last.tv_power == "standby" and not nudged and self.cec_waker.available:
                # WoL cannot lift a shallow-standby TV; the proven pair can. The
                # box says the TV is dark, so an odd landing is not a risk here.
                nudged = True
                result = self.cec_waker.press_input_pair()
                self.last_input_result = result
                if getattr(result, "needs_pairing", False) is True:
                    return None
            if time.monotonic() >= deadline:
                break
            time.sleep(self.tv_confirm_poll_s)
        if last.tv_power == "standby":
            return False
        if last.active_source is False:
            return self.ensure_active_source() is True
        return None

    def _wake_and_wait(self) -> bool:
        """
        The deep-standby fallback: wake via the TV over CEC, then wait for the
        box to rejoin the network. ~52s measured 2026-09-18, most of it the box
        resuming.
        """
        self.last_wake_result = None
        for attempt in range(1, self.wake_attempts + 1):
            result = self.cec_waker.wake()
            self.last_wake_result = result
            if not result:
                log.warning("CEC wake did not run: %s", result.detail)
                # A rejected token will not fix itself on a retry, and it needs a
                # different fix from every other failure. Stop and say so.
                if getattr(result, "needs_pairing", False):
                    return False
                continue

            log.info("CEC wake sent (%s), waiting up to %.0fs for the box (attempt %d/%d)",
                     result.detail, self.wake_settle_s, attempt, self.wake_attempts)
            if self._wait_for_box():
                log.info("Box back on the network after CEC wake")
                return True

        log.warning("Box did not rejoin the network after %d CEC wake attempt(s)",
                    self.wake_attempts)
        return False

    def is_boot_completed(self) -> bool | None:
        """
        Has Android finished booting? True / False / None, and None is "cannot tell".

        `sys.boot_completed` flips to 1 when the system is actually up. This is
        NOT the same question as "is ADB answering": adbd comes up early in boot,
        so ensure_connected() can succeed against a box that cannot yet launch an
        app. Firing a Stremio deep link into that window is a launch that quietly
        does nothing.

        Verified present on the real box (Android 11, SDK 30).
        """
        ok, output = self._adb("shell getprop sys.boot_completed")
        if not ok:
            return None
        answer = (output or "").strip()
        if not answer:
            return None
        return answer == "1"

    def _wait_for_box(self) -> bool:
        # Every miss rediscovers, then retries. The loop used to suppress
        # discovery for the whole settle window on the theory that a booting box
        # comes back at its known address and a scan per poll is waste. Measured
        # 2026-09-11: the box came back on a NEW lease after a CEC wake, so the
        # loop polled a dead address for 47s and only found it in the one scan
        # allowed after the timeout. The scan costs ~1.7s against a 2s poll
        # interval -- cheaper than a single wasted poll, and it ends the wait the
        # moment the box is up anywhere on the LAN.
        deadline = time.monotonic() + self.wake_settle_s
        while True:
            # ensure_connected() stamps _last_fail_time on every miss and then
            # refuses to retry for _OFFLINE_COOLDOWN, and _rediscover_and_connect
            # refuses to rescan for rescan_cooldown_s. Both are correct for normal
            # operation and wrong here, where we are deliberately waiting out a
            # boot, so clear both before each pass.
            self._last_fail_time = 0
            self._last_discovery_t = 0
            # Reachable is not the same as ready. adbd answers early in boot,
            # so returning here on connection alone hands back a box that
            # cannot launch anything yet -- and settle_ms exists as a fixed
            # budget precisely because there was no better signal.
            #
            # `is not False` is the fail-open: an unreadable getprop must
            # never be worse than the old behaviour, which accepted the
            # connection by itself.
            if self.ensure_connected() and self.is_boot_completed() is not False:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.wake_poll_interval_s)

    def is_active_source(self) -> bool | None:
        """
        Is the box what the television is actually showing?

        True/False/None, and **None is not False**. None means "could not tell"
        (dumpsys unreadable, CEC disabled, box unreachable) and must never trigger
        an HDMI switch: switch_input falls back to *cycling* inputs, which is not
        idempotent, so guessing costs Master Miguel the picture he already had.

        The box turns the TV on and selects itself on wake, via CEC One Touch Play
        (<Text View On> + <Active Source>, observed on the real box). So this is
        only false in one real situation: the input was parked somewhere else --
        terrestrial TV, a console -- while the box stayed awake.
        """
        return self.hdmi_state().active_source

    def tv_power_status(self) -> str | None:
        """
        The TV's own power state, as the box last heard it over CEC.

        The television has no usable API for this -- its REST payload carries no
        PowerState field at all on this model, and it answers that endpoint in
        standby anyway. The box is the only oracle, because the TV reports its
        status on the CEC bus: <Report Power Status> 04:90:00 means source 0 (the
        TV) told the box "on"; :01 is standby.

        Returns "on" | "standby" | None.

        **Read the tail, never the head.** dumpsys keeps a capped ring of ~246
        entries, so the first matching line can be days old -- grepping the head
        during this work returned entries from two days earlier and looked exactly
        like "nothing happened".
        """
        return self.hdmi_state().tv_power

    def hdmi_state(self, since: str | None = None) -> HdmiState:
        """
        Both hdmi_control facts from ONE round trip.

        `since` (a CEC stamp from an earlier HdmiState.newest_stamp) makes the
        TV-power reading ignore older evidence; see _parse_tv_power.

        is_active_source() and tv_power_status() each ran their own `dumpsys
        hdmi_control` over identical output, so asking for both cost two of them.
        They now delegate here and keep their own names, signatures and None
        semantics, because ensure_active_source() and _ensure_playable() call
        them and must not change.
        """
        if not self.ensure_connected():
            return HdmiState(None, None)
        ok, output = self._adb("shell dumpsys hdmi_control")
        if not ok or not output:
            return HdmiState(None, None)
        return HdmiState(
            _parse_active_source(output),
            _parse_tv_power(output, since=since),
            _newest_cec_stamp(output),
        )

    def ensure_active_source(self, attempts: int = 3, settle_s: float = 2.0) -> bool | None:
        """
        Make the television actually show the box. Verified, not assumed.

        `switch_input` cannot verify itself: it sends KEY_HDMI<n> and falls back to
        cycling only if the *send* failed -- but the websocket accepts every key,
        so the send always "succeeds" while KEY_HDMI2 has no observable effect on
        this set (already noted in its own docstring). Measured 2026-09-05: parking
        the TV on HDMI1 and calling switch_hdmi(2) left it on HDMI1, with the CEC
        log showing <Set Stream Path> 0F:86:10:00 (0x1000 == HDMI 1) four times.

        The box is the only honest witness, so drive the loop from its
        mIsActiveSource rather than from what the TV said it accepted.

        Returns True (we are on screen), False (gave up), or None (cannot tell --
        never guess, cycling from an unknown input is not idempotent).
        """
        state = self.is_active_source()
        if state is not True:
            log.info("Box is not the active source (state=%s), selecting its input", state)
        if state is None or state is True:
            return state

        for attempt in range(1, attempts + 1):
            # Direct addressing first because it is a single deterministic press
            # when it works; then cycling, which is the mechanism actually proven
            # on this set. Do not "simplify" to direct-only -- the TV accepts
            # KEY_HDMI2 and ignores it, so that loop never converges.
            if attempt == 1:
                result = self.switch_hdmi(self.mibox_hdmi_port)
            else:
                result = self.cec_waker.cycle_input()
            self.last_input_result = result
            # Identity, not truthiness: a bare Mock's attribute is truthy.
            if getattr(result, "needs_pairing", False) is True:
                # Cycling will not fix a rejected token; stop and let the
                # dispatcher send Master Miguel to the screen.
                log.error("Input selection needs the TV re-paired: %s", result.detail)
                return False
            time.sleep(settle_s)
            state = self.is_active_source()
            if state is True:
                log.info("Box is on screen again after %d input change(s)", attempt)
                return True
            if state is None:
                return None
        log.warning("Could not select the box's HDMI input after %d attempts", attempts)
        return False

    def switch_hdmi(self, port: int):
        """Select an HDMI input on the television. Returns a WakeResult."""
        return self.cec_waker.switch_input(port)

    def power_toggle(self) -> bool:
        """State-aware. Never sends a bare KEYCODE_POWER -- see the note above."""
        return self.turn_off() if self.is_awake() is True else self.turn_on()

    # Back-compat aliases for the older action names.
    def sleep(self) -> bool:
        return self.turn_off()

    def wake(self) -> bool:
        return self.turn_on()

    # --- State awareness ---

    def get_current_app(self) -> str:
        """Return the package name of the foreground app."""
        if not self.ensure_connected():
            return "unknown (TV unreachable)"
        ok, output = self._adb("shell dumpsys window displays")
        if ok and output:
            try:
                return self._friendly_app(output) or "unknown"
            except Exception:
                return output
        return "unknown"

    def _friendly_app(self, dumpsys_output: str) -> str | None:
        """The foreground app under its configured name, else its package."""
        pkg = self._parse_focus_package(dumpsys_output)
        if not pkg:
            return None
        for name, package in self.apps.items():
            if package in pkg:
                return name
        return pkg

    def _parse_focus_package(self, dumpsys_output: str) -> str:
        """Best-effort foreground package extraction.

        Stremio shows ``mCurrentFocus=null`` while its splash screen is up even
        though ``mFocusedApp`` already points at ``com.stremio.one``. Try the
        window focus first, then fall back to the activity focus.
        """
        for line in dumpsys_output.splitlines():
            stripped = line.strip()
            if "mCurrentFocus=" not in stripped or "mCurrentFocus=null" in stripped:
                continue
            if "/" in stripped:
                return stripped.split("/")[0].split(" ")[-1]
        match = re.search(
            r"mFocusedApp=ActivityRecord\{[^}]*\s([\w.]+)/",
            dumpsys_output,
        )
        if match:
            return match.group(1)
        return ""

    def get_current_focus(self) -> str:
        """Return the raw foreground package/activity token when available."""
        if not self.ensure_connected():
            return ""
        ok, output = self._adb("shell dumpsys window displays")
        if not ok or not output:
            return ""
        for line in output.splitlines():
            if "mCurrentFocus=" not in line:
                continue
            stripped = line.strip()
            match = re.search(r"([A-Za-z0-9._$]+/[A-Za-z0-9._$]+)\}?\s*$", stripped)
            if match:
                return match.group(1)
            return stripped.split("mCurrentFocus=", 1)[-1].strip()
        return ""

    def is_playing(self) -> bool | None:
        """
        Is something playing right now? None means "could not tell".

        This replaced get_media_session(), which returned up to 15 raw lines of
        dumpsys straight into the LLM's context to be read out loud.
        """
        if not self.ensure_connected():
            return None
        ok, output = self._adb("shell dumpsys media_session")
        if not ok or not output:
            return None
        return _parse_playing(output)

    def room_status(self) -> RoomStatus:
        """
        Everything readable about the room, in as few round trips as possible.

        This is the ONE method allowed to read dumpsys without going back through
        ensure_connected(), and that is the whole reason it exists.
        ensure_connected() fires a live `adb shell echo ping` on EVERY call, so
        composing this from is_awake() + hdmi_state() + get_current_app() +
        is_playing() would cost eight round trips to answer one question. Here it
        is one ping and two to four dumpsys reads.

        A sleeping box short-circuits: there is nothing on screen to ask about.
        """
        if not self.ensure_connected():
            return RoomStatus(reachable=False)

        ok, power_dump = self._adb("shell dumpsys power")
        awake = _parse_wakefulness(power_dump) if ok else None

        ok, hdmi_dump = self._adb("shell dumpsys hdmi_control")
        tv_power = _parse_tv_power(hdmi_dump) if ok else None
        on_the_box = _parse_active_source(hdmi_dump) if ok else None

        if awake is False:
            return RoomStatus(True, awake, tv_power, on_the_box)

        ok, focus_dump = self._adb("shell dumpsys window displays")
        app = self._friendly_app(focus_dump) if ok else None

        ok, session_dump = self._adb("shell dumpsys media_session")
        # Scope playback to the app that is actually in front. Spotify holds
        # a session of its own even while idle, so an unscoped answer would
        # hand its state to whatever happens to be on screen.
        package = self.apps.get(app) if app else None
        playing = _parse_playing(session_dump, package) if ok else None
        session = (
            _session_for(_parse_media_sessions(session_dump), package) if ok else None
        )
        title = session.title if session is not None else None
        playback = _PLAYBACK_WORDS.get(session.state) if session is not None else None
        position_ms = session.position_ms if session is not None else None
        position_s = position_ms // 1000 if position_ms is not None else None

        ok, audio_dump = self._adb("shell dumpsys audio")
        volume = _parse_volume(audio_dump) if ok else None

        return RoomStatus(
            True, awake, tv_power, on_the_box, app, playing, title,
            playback, position_s,
            volume.level if volume else None,
            volume.maximum if volume else None,
            volume.muted if volume else None,
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        with open("config.yaml") as f:
            config = yaml.safe_load(f)
        svc = MediaService(config)
        print("Connecting to Mi BOX S...")
        if svc.connect():
            print("Connected. Lowering volume as test...")
            svc.volume_down(2)
            print("Done. Check your TV.")
        else:
            print("Failed. Is the BOX on? Is ADB enabled?")
