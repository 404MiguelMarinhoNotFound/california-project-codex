import logging
import subprocess
import time
import yaml
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

# How long to wait before retrying after a failed connection (seconds)
_OFFLINE_COOLDOWN = 30


class MediaService:
    def __init__(self, config: dict):
        media_cfg = config["media"]
        self.ip   = media_cfg["mibox_ip"]
        self.port = media_cfg["adb_port"]
        self.apps = media_cfg["apps"]
        self.app_launch_components = media_cfg.get("app_launch_components", {})
        self.app_launch_categories = media_cfg.get("app_launch_categories", {})
        self.target = f"{self.ip}:{self.port}"
        self.adb_path = media_cfg.get("adb_path", "adb")
        self.volume_max_steps = media_cfg.get("volume_max_steps", 15)
        self.youtube_warm_launch_delay_s = media_cfg.get("youtube_warm_launch_delay_ms", 1500) / 1000
        self.youtube_profile_select_on_cold_start = media_cfg.get("youtube_profile_select_on_cold_start", True)
        self.youtube_profile_select_delay_s = media_cfg.get("youtube_profile_select_delay_ms", 1200) / 1000
        self.ui_dump_retry_count = max(1, int(media_cfg.get("ui_dump_retry_count", 3)))
        self.ui_dump_retry_delay_s = max(0, int(media_cfg.get("ui_dump_retry_delay_ms", 700)) / 1000)

        # Connection state tracking
        self._connected = False
        self._last_fail_time: float = 0  # monotonic timestamp of last failed reconnect

    def _adb(self, command: str, use_target: bool = True) -> tuple[bool, str]:
        adb = self.adb_path
        cmd = f'"{adb}" -s {self.target} {command}' if use_target else f'"{adb}" {command}'
        log.debug(f"ADB exec: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            ok = result.returncode == 0
            if stderr:
                log.debug(f"ADB stderr: {stderr}")
            if not ok:
                log.warning(f"ADB failed (rc={result.returncode}): cmd={cmd} | stdout={stdout} | stderr={stderr}")
            else:
                log.debug(f"ADB ok: {stdout}")
            return ok, stdout
        except subprocess.TimeoutExpired:
            log.warning(f"ADB command timed out: {cmd}")
            return False, "timeout"

    def connect(self) -> bool:
        # adb connect doesn't need -s, it's a global command
        _, output = self._adb(f"connect {self.target}", use_target=False)
        success = "connected" in output.lower()
        self._connected = success
        if not success:
            self._last_fail_time = time.monotonic()
        log.info(f"ADB connect -> '{output}' (success={success})")
        return success

    def ensure_connected(self) -> bool:
        # If we recently failed, don't waste time retrying — fail fast
        if not self._connected and self._last_fail_time:
            elapsed = time.monotonic() - self._last_fail_time
            if elapsed < _OFFLINE_COOLDOWN:
                log.debug(f"TV offline, skipping reconnect ({_OFFLINE_COOLDOWN - elapsed:.0f}s cooldown remaining)")
                return False

        ok, output = self._adb("shell echo ping")
        if ok:
            self._connected = True
            return True

        log.info(f"ADB ping failed (output='{output}'), reconnecting...")
        return self.connect()

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
            return ok, f"Opening {normalized_name}" if ok else f"Couldn't open {normalized_name}"

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
        return self._adb(f"shell am force-stop {package}")[0]

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
            ok, dump_output = self._adb(f"shell uiautomator dump --compressed {remote_path}")
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

    # --- YouTube ---

    def _youtube_package(self) -> str:
        return self._app_package("youtube") or "com.google.android.youtube.tv"

    def _youtube_is_foreground(self) -> bool:
        return self.is_app_foreground("youtube")

    def _prepare_youtube_launch(self) -> bool:
        if self._youtube_is_foreground():
            return True

        ok, _ = self.launch_app("youtube")
        if not ok:
            return False

        if self.youtube_warm_launch_delay_s > 0:
            time.sleep(self.youtube_warm_launch_delay_s)

        if self.youtube_profile_select_on_cold_start:
            self.keyevent("DPAD_CENTER")
            if self.youtube_profile_select_delay_s > 0:
                time.sleep(self.youtube_profile_select_delay_s)

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

    def power_toggle(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_POWER")[0]

    def sleep(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_SLEEP")[0]

    def wake(self) -> bool:
        return self.ensure_connected() and self._adb(
            "shell input keyevent KEYCODE_WAKEUP")[0]

    # --- State awareness ---

    def get_current_app(self) -> str:
        """Return the package name of the foreground app."""
        if not self.ensure_connected():
            return "unknown (TV unreachable)"
        ok, output = self._adb("shell dumpsys window displays")
        if ok and output:
            try:
                focus_line = next(
                    (line.strip() for line in output.splitlines() if "mCurrentFocus=" in line),
                    "",
                )
                if not focus_line:
                    return "unknown"
                pkg = focus_line.split("/")[0].split(" ")[-1]
                # Reverse-lookup friendly name
                for name, package in self.apps.items():
                    if package in pkg:
                        return name
                return pkg
            except Exception:
                return output
        return "unknown"

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
