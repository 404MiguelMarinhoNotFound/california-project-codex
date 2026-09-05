"""
Wake the Mi Box through the television, over HDMI-CEC.

WHY THE TV AND NOT THE BOX
The Mi Box suspends in standby and `adbd` suspends with it, so ADB can turn the
box off but never back on. Measured on the real box 2026-09-03:

    adb shell input keyevent KEYCODE_SLEEP   -> ok, mWakefulness=Asleep
    adb shell input keyevent KEYCODE_WAKEUP  -> error: closed
    adb connect 192.168.1.35:5555            -> WSA 10060, every time

**Ping is not a reachability test here.** The box answers ICMP intermittently in
standby (windows every 6-15s) because the Wi-Fi firmware's offload replies while
the CPU stays suspended. Twelve `adb connect` attempts fired the instant ICMP
replied all failed. `wifi_sleep_policy` is already 2 ("never sleep") and
`stay_on_while_plugged_in=3` does not prevent the suspend -- both tested.

Routes that are dead, with evidence, so nobody burns an evening re-deriving them:

  - Wake-on-LAN *to the box*: no Ethernet, and Android randomises the Wi-Fi MAC
    per network (16:da:99:37:d0:89 is locally-administered, unstable).
  - BLE: the box does not advertise at all. A 20s scan beside it, awake, sees
    nothing. `bleak` and the Govee transport are useless here.
  - Bluetooth Classic from Windows: AF_BTH Winsock `bind()` fails against the
    *local* radio with WSAEADDRNOTAVAIL, and WinRT `PairAsync` refuses an
    address it has not discovered -- which a TV box, never being in pairing
    mode, is not. Five approaches tried, all dead.

THE MECHANISM THAT WORKS
1. Wake-on-LAN the Samsung TV. Needs no IP and no token -- it is a MAC
   broadcast -- which is what makes the whole chain recoverable.
2. Toggle the TV's HDMI input over its WebSocket API. The TV then emits
   <Routing Change> and <Set Stream Path> on the CEC bus, and the box wakes.

**Step 2 is load-bearing.** In testing the box was still unreachable after the
TV powered on and only woke after the input toggle, so the pairing token is a
hard dependency, not a convenience. That is why token failure gets its own
result type instead of being flattened into a generic failure.
"""

import json
import logging
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_REST_TIMEOUT_S = 4


@dataclass
class WakeResult:
    """
    Mirrors GoveeCommandResult / StremioPlayResult: returned, never raised.

    Defines __bool__, so a failed result is falsy. Never truthiness-test one to
    check whether it *exists* -- always `is None` / `is not None`. See the
    GoveeService note in AGENTS.md for the three shapes of that bug.
    """

    ok: bool
    detail: str
    needs_pairing: bool = False

    def __bool__(self) -> bool:
        return self.ok


class CecWaker:
    """
    Turns the TV on and makes it wake the Mi Box over CEC.

    Self-disables rather than raising when it cannot work. Construction never
    touches the network.
    """

    def __init__(self, config: dict):
        cfg = ((config.get("media") or {}).get("cec_wake") or {})
        self.tv_mac = str(cfg.get("tv_mac") or "").strip()
        self.tv_duid = str(cfg.get("tv_duid") or "").strip()
        self.tv_ip_hint = str(cfg.get("tv_ip") or "").strip()
        self.tv_port = int(cfg.get("tv_port", 8002))
        self.token_path = str(cfg.get("token_path") or "samsung_token.txt")
        self.state_path = Path(str(cfg.get("tv_state_path") or "tv_state.json"))
        self.input_key = str(cfg.get("input_key") or "KEY_HDMI")
        self.key_delay_s = max(0.1, int(cfg.get("key_delay_ms", 6000)) / 1000)
        self.wol_attempts = max(1, int(cfg.get("wol_attempts", 5)))
        self.tv_boot_timeout_s = max(1, int(cfg.get("tv_boot_timeout_ms", 40000))) / 1000
        self.hdmi_ports = {int(k): str(v) for k, v in (cfg.get("hdmi_ports") or {}).items()}

        self._tv_ip: str | None = None
        self.unavailable_reason = self._resolve_availability(bool(cfg.get("enabled", False)))
        if self.unavailable_reason:
            log.warning("CEC wake disabled: %s", self.unavailable_reason)

    def _resolve_availability(self, enabled: bool) -> str:
        if not enabled:
            return "media.cec_wake.enabled is false"
        if not self.tv_mac:
            return "media.cec_wake.tv_mac is not set"
        if not self.tv_duid:
            return (
                "media.cec_wake.tv_duid is not set. Read it with: "
                "curl http://<tv-ip>:8001/api/v2/"
            )
        try:
            import samsungtvws  # noqa: F401
        except ImportError:
            return "samsungtvws not installed. Install it with: uv sync --extra cec"
        return ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    # ------------------------------------------------------------ discovery ---

    def _probe(self, ip: str) -> bool:
        """True when `ip` answers the Samsung REST endpoint AS THIS TV."""
        try:
            with urllib.request.urlopen(
                f"http://{ip}:8001/api/v2/", timeout=_REST_TIMEOUT_S
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return False
        # The duid check is the point. Without it a stale IP that some *other*
        # device has since taken would answer 200 and be used happily.
        return str(payload.get("device", {}).get("duid", "")) == self.tv_duid

    def _cached_ip(self) -> str:
        try:
            return str(json.loads(self.state_path.read_text("utf-8")).get("tv_ip") or "")
        except Exception:
            return ""

    def _remember_ip(self, ip: str) -> None:
        try:
            self.state_path.write_text(json.dumps({"tv_ip": ip}, indent=2), "utf-8")
        except OSError as exc:
            log.debug("Could not write %s: %s", self.state_path, exc)

    def _arp_lookup(self) -> str:
        """Find the TV's current IP by MAC, after nudging the ARP cache."""
        wanted = self.tv_mac.lower().replace(":", "-")
        base = ".".join((self._cached_ip() or self.tv_ip_hint or "192.168.1.1").split(".")[:3])

        # `arp -a` only lists recently-contacted hosts, so provoke the subnet first.
        flag = "-n" if sys.platform == "win32" else "-c"
        with ThreadPoolExecutor(max_workers=64) as pool:
            pool.map(
                lambda host: subprocess.run(
                    ["ping", flag, "1", "-w", "300", f"{base}.{host}"],
                    capture_output=True, timeout=5,
                ),
                range(1, 255),
            )
        try:
            table = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                                   errors="replace", timeout=15).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""
        for line in table.splitlines():
            if wanted in line.lower().replace(":", "-"):
                for token in line.split():
                    if token.count(".") == 3 and token.replace(".", "").isdigit():
                        return token
        return ""

    def _sweep(self) -> str:
        base = ".".join((self._cached_ip() or self.tv_ip_hint or "192.168.1.1").split(".")[:3])
        candidates = [f"{base}.{h}" for h in range(1, 255)]
        with ThreadPoolExecutor(max_workers=48) as pool:
            for ip, hit in zip(candidates, pool.map(self._probe, candidates)):
                if hit:
                    return ip
        return ""

    def resolve_tv_ip(self, force: bool = False) -> str:
        """
        Cached hint -> ARP by MAC -> subnet probe. Verified by duid at every step.

        The happy path is one HTTP GET; discovery only runs on a miss, which is
        why a DHCP move self-heals instead of surfacing as "couldn't turn the
        TV on".
        """
        if self._tv_ip and not force:
            return self._tv_ip
        for candidate in (self._cached_ip(), self.tv_ip_hint):
            if candidate and self._probe(candidate):
                self._tv_ip = candidate
                return candidate
        log.info("TV not at its cached address, rediscovering by MAC %s", self.tv_mac)
        for finder in (self._arp_lookup, self._sweep):
            found = finder()
            if found and self._probe(found):
                log.info("TV rediscovered at %s", found)
                self._remember_ip(found)
                self._tv_ip = found
                return found
        return ""

    # ----------------------------------------------------------------- wake ---

    def _tv_is_up(self) -> bool:
        """
        Probe the TV, and remember where it answered.

        The assignment is load-bearing. `power_on_tv` returns early on this, so
        without it `self._tv_ip` stays None on the happy path and `_remote()`
        builds SamsungTVWS(host=None) -- "Can't build URL with port but without
        host". That failure only hides while the cached IP is *stale*, because
        the resulting rediscovery sets `_tv_ip` as a side effect.
        """
        ip = self._tv_ip or self._cached_ip() or self.tv_ip_hint
        if not ip or not self._probe(ip):
            return False
        self._tv_ip = ip
        return True

    def _send_wol(self) -> None:
        raw = bytes.fromhex(self.tv_mac.replace(":", "").replace("-", ""))
        packet = b"\xff" * 6 + raw * 16
        base = ".".join((self._cached_ip() or self.tv_ip_hint or "192.168.1.1").split(".")[:3])
        for target in ("255.255.255.255", f"{base}.255"):
            for port in (9, 7):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.sendto(packet, (target, port))
                    sock.close()
                except OSError as exc:
                    log.debug("WoL to %s:%s failed: %s", target, port, exc)

    def power_on_tv(self) -> bool:
        """
        Wake-on-LAN the TV and wait for it to answer. Needs no token.

        **"Answers REST" is NOT "powered on", and this used to assume it was.**
        This set has two standby depths, measured 2026-09-05:

          shallow -- :8001/api/v2/ still answers while the screen is off
          deep    -- the endpoint stops answering entirely

        In shallow standby the old `if self._tv_is_up(): return True` short-circuit
        skipped Wake-on-LAN, which is the only step that would have powered the TV
        on -- so wake() reported success against a television that stayed dark.
        Three WoL bursts did nothing while it answered REST; one burst woke it once
        it had dropped into deep standby.

        The set exposes no PowerState field at all, so `_probe` cannot be taught to
        tell on from off -- it verifies the *address*, nothing more. WoL is a
        broadcast and a no-op against a TV that is already on, so the cheapest
        correct thing is to always send it and let the probe confirm reachability.
        """
        # Resolve first: the address is still needed, and this populates _tv_ip
        # for _remote(). What we must NOT do is treat the answer as "powered on".
        reachable = self._tv_is_up()
        log.info("WoL to %s (TV %s at its address)", self.tv_mac,
                 "answers" if reachable else "does not answer")
        self._send_wol()
        if reachable:
            return True
        for attempt in range(1, self.wol_attempts + 1):
            log.info("WoL to %s (attempt %d/%d)", self.tv_mac, attempt, self.wol_attempts)
            self._send_wol()
            deadline = time.monotonic() + (self.tv_boot_timeout_s / self.wol_attempts)
            while time.monotonic() < deadline:
                time.sleep(3)
                if self.resolve_tv_ip(force=True):
                    log.info("TV is up at %s", self._tv_ip)
                    return True
        return False

    def _remote(self):
        from samsungtvws import SamsungTVWS

        return SamsungTVWS(host=self._tv_ip, port=self.tv_port,
                           token_file=self.token_path, name="California", timeout=30)

    def _send_keys(self, keys: list[str]) -> WakeResult:
        try:
            remote = self._remote()
            for index, key in enumerate(keys):
                remote.send_key(key)
                if index < len(keys) - 1:
                    time.sleep(self.key_delay_s)
        except Exception as exc:
            # A revoked token and an unplugged TV both surface as exceptions
            # here, and they need opposite fixes -- one wants a human at the
            # screen, the other wants the remote. Separate them.
            if _looks_like_auth_failure(exc):
                log.error("TV rejected the stored token: %s", exc)
                return WakeResult(False, f"TV pairing token rejected: {exc}", needs_pairing=True)
            log.error("TV remote failed: %s", exc)
            return WakeResult(False, f"TV remote failed: {exc}")
        return WakeResult(True, f"sent {', '.join(keys)}")

    def wake(self) -> WakeResult:
        """Power the TV, then toggle its input so CEC wakes the box."""
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        if not self.power_on_tv():
            return WakeResult(False, "TV did not come up after Wake-on-LAN")
        # Away, then back: the return press is what emits Set Stream Path at the
        # box's physical address.
        return self._send_keys([self.input_key, self.input_key])

    def switch_input(self, port: int) -> WakeResult:
        """
        Select an HDMI port on the TV.

        Direct addressing (KEY_HDMI<n>) is NOT proven on this set -- KEY_HDMI2
        showed no observable effect in testing. Try it, then fall back to
        cycling, which is proven. The caller verifies via CEC active source
        where it can; that only works while the box is reachable.
        """
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        if not self.resolve_tv_ip():
            return WakeResult(False, "TV not reachable")
        direct = self._send_keys([f"{self.input_key}{port}"])
        if direct:
            return WakeResult(True, f"selected HDMI {port}")
        return direct if direct.needs_pairing else self._send_keys([self.input_key])

    def cycle_input(self) -> WakeResult:
        """
        Advance the TV to its next input. Proven on this set, unlike KEY_HDMI<n>.

        `switch_input`'s fallback to this is unreachable in practice: the
        websocket accepts KEY_HDMI2 and reports success while the TV ignores it,
        so `direct` is always truthy. Verified 2026-09-05 -- three switch_input(2)
        calls left the set on HDMI 1, with the CEC log showing
        <Set Stream Path> 0F:86:10:00 (0x1000 == HDMI 1) each time.

        Cycling is only safe with an external check on where it landed, because
        it is not idempotent. MediaService.ensure_active_source owns that loop --
        it can see the box's own mIsActiveSource, which is the only honest witness.
        """
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        if not self.resolve_tv_ip():
            return WakeResult(False, "TV not reachable")
        return self._send_keys([self.input_key])


def _looks_like_auth_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in
               ("unauthor", "forbidden", "token", "denied", "ms.channel.unauthorized"))
