import logging
import re
import subprocess
import time
import yaml
from pathlib import Path
from urllib.parse import quote_plus

from services.cec_wake import CecWaker
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
        self.ui_dump_retry_count = max(1, int(media_cfg.get("ui_dump_retry_count", 2)))
        self.ui_dump_retry_delay_s = max(0, int(media_cfg.get("ui_dump_retry_delay_ms", 700)) / 1000)
        self.ui_dump_timeout_s = max(1, int(media_cfg.get("ui_dump_timeout_ms", 6000))) / 1000

        # Wake. ADB can put the box to sleep but can never bring it back, so
        # turn_on goes through the television over CEC. See services/cec_wake.py.
        wake_cfg = media_cfg.get("cec_wake", {}) or {}
        self.wake_settle_s = max(0, int(wake_cfg.get("settle_ms", 25000))) / 1000
        self.wake_poll_interval_s = max(0.1, int(wake_cfg.get("poll_interval_ms", 2000)) / 1000)
        self.wake_attempts = max(1, int(wake_cfg.get("wake_attempts", 3)))
        self.mibox_hdmi_port = int(wake_cfg.get("mibox_hdmi_port", 2))
        self.cec_waker = cec_waker if cec_waker is not None else CecWaker(config)

        # Set by _wake_and_wait so the dispatcher can tell a rejected pairing
        # token (needs a human at the screen) from any other wake failure.
        self.last_wake_result = None

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
        self._discovery_suppressed = False
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
        if not self.discovery_enabled or self._discovery_suppressed:
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

    # --- Power ---
    #
    # Turning the box on and turning it off are NOT symmetric, and nothing here
    # should pretend they are. Off is one ADB keyevent. On is a Bluetooth pair
    # request, because the box leaves Wi-Fi entirely in standby and ADB cannot
    # reach it at all. That asymmetry is why KEYCODE_POWER is never sent blind:
    # it is a toggle, so firing it at an already-awake box turns the TV off and
    # ends every other form of control until someone picks up the remote.

    def is_awake(self) -> bool | None:
        """
        True if awake, False if asleep but reachable, None if unreachable.

        None is the interesting one: it means the box is in standby with its
        Wi-Fi radio down, which is the only state that needs the Bluetooth path.
        """
        if not self.ensure_connected():
            return None
        ok, output = self._adb("shell dumpsys power")
        if not ok or not output:
            return None
        match = _WAKEFULNESS_RE.search(output)
        if not match:
            return None
        return match.group(1).strip().lower() == "awake"

    def turn_on(self) -> bool:
        state = self.is_awake()
        if state is True:
            return True  # Already on. Sending anything here risks turning it off.
        if state is False:
            # Reachable but asleep: the screen is off, the radio is not.
            # KEYCODE_WAKEUP is a no-op when already awake, so it is the safe
            # choice over KEYCODE_POWER even though we checked the state first.
            return self._adb("shell input keyevent KEYCODE_WAKEUP")[0]
        return self._wake_and_wait()

    def turn_off(self) -> bool:
        if self.is_awake() is None:
            return True  # Unreachable already means off.
        return self._adb("shell input keyevent KEYCODE_SLEEP")[0]

    def _wake_and_wait(self) -> bool:
        """Wake via the TV over CEC, then wait for the box to rejoin the network."""
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

    def _wait_for_box(self) -> bool:
        # The box is booting at an address we already know, so a subnet scan on
        # every poll would be pure waste. Suppress discovery for the duration --
        # as an instance flag, not a parameter, because the loop must keep calling
        # the *public* ensure_connected() that tests patch by name.
        self._discovery_suppressed = True
        deadline = time.monotonic() + self.wake_settle_s
        try:
            while time.monotonic() < deadline:
                # ensure_connected() stamps _last_fail_time on every miss and then
                # refuses to retry for _OFFLINE_COOLDOWN. That cooldown is correct
                # for normal operation and wrong here, where we are deliberately
                # waiting out a boot, so clear it on each pass.
                self._last_fail_time = 0
                if self.ensure_connected():
                    return True
                time.sleep(self.wake_poll_interval_s)
        finally:
            self._discovery_suppressed = False
        # The wait timed out. A box that rebooted may have come back on a new
        # lease, and this is the one moment that is likely -- so rediscover
        # exactly once, after the wait, never during it.
        self._last_fail_time = 0
        return self._rediscover_and_connect()

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
        if not self.ensure_connected():
            return None
        ok, output = self._adb("shell dumpsys hdmi_control")
        if not ok or not output:
            return None
        match = _ACTIVE_SOURCE_RE.search(output)
        if not match:
            return None
        return match.group(1).strip().lower() == "true"

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
        if not self.ensure_connected():
            return None
        ok, output = self._adb("shell dumpsys hdmi_control")
        if not ok or not output:
            return None
        last = None
        for match in _TV_POWER_RE.finditer(output):
            last = match.group(1)
        if last is None:
            return None
        return {"00": "on", "01": "standby"}.get(last)

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
                self.switch_hdmi(self.mibox_hdmi_port)
            else:
                self.cec_waker.cycle_input()
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
                pkg = self._parse_focus_package(output)
                if not pkg:
                    return "unknown"
                # Reverse-lookup friendly name
                for name, package in self.apps.items():
                    if package in pkg:
                        return name
                return pkg
            except Exception:
                return output
        return "unknown"

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

    def get_media_session(self) -> str:
        """Return active media session info (track/show if the app exposes it)."""
        if not self.ensure_connected():
            return "TV unreachable"
        ok, output = self._adb("shell dumpsys media_session")
        if ok and output:
            # Extract the useful bits — metadata and playback state
            lines = output.splitlines()
            relevant = []
            capture = False
            for line in lines:
                if "metadata:" in line.lower() or "state=" in line.lower():
                    capture = True
                if capture:
                    relevant.append(line.strip())
                    if len(relevant) > 15:
                        break
                if capture and line.strip() == "":
                    capture = False
            return "\n".join(relevant) if relevant else "no active media session"
        return "couldn't query media session"


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
