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

  - Wake-on-LAN *to the box*: no Ethernet. (Its Wi-Fi MAC 16:da:99:37:d0:89 is
    stable within an SSID -- see AGENTS.md -- WoL is dead for lack of a wired
    NIC, not because of the MAC. The radio is off in standby anyway.)
  - BLE: the box does not advertise at all. A 20s scan beside it, awake, sees
    nothing. `bleak` and the Govee transport are useless here.
  - Bluetooth Classic from Windows: AF_BTH Winsock `bind()` fails against the
    *local* radio with WSAEADDRNOTAVAIL, and WinRT `PairAsync` refuses an
    address it has not discovered -- which a TV box, never being in pairing
    mode, is not. Five approaches tried, all dead.

THE MECHANISM THAT WORKS (re-measured 2026-09-25)
1. Wake-on-LAN the Samsung TV. Needs no IP and no token -- it is a MAC
   broadcast -- which is what makes the whole chain recoverable.
2. That is all, when the TV was last on the box's input (HDMI 2), which is
   nearly always: once its own CEC side is up (~37s after a cold power-on) the
   TV announces the input it came up on and the box wakes. 42s end to end,
   measured twice with no key pressed at all.
3. If the box has still not appeared, select HDMI 2 through the Source menu
   (`select_box_input`). Only needed when the TV was left on another input.

**The blind KEY_HDMI pair this module used to send was the bug.** While the box
is asleep it sends no signal and the Samsung's cycle key SKIPS its input, so
"away then back" moved a TV that was already on HDMI 2 off it, onto HDMI 1, and
the box never woke: the old chain spent 3 x 45s failing (167s, 2026-09-25).
KEY_HDMI1/KEY_HDMI2 are accepted and ignored. The Source menu lists TV, HDMI 1,
HDMI 2 left to right and LEFT does not wrap, so LEFT x4 then RIGHT x<port>
lands on a port from anywhere, sleeping box or not.

**TV power is read off the UPnP port, never the REST one.** :9197 closes within
6s of standby; :8001 keeps answering for ~15s (shallow standby) and only then
closes (deep). So :9197 open is "on", :8001 alone is "standby" (KEY_POWER lifts
it; WoL does not), and neither is "off" (WoL lifts it).
"""

import json
import logging
import socket
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from services.device_finder import (
    DeviceFinder,
    arp_table_candidates,
    port_open,
    tcp_port_candidates,
)

log = logging.getLogger(__name__)

# The Samsung REST endpoint. Only reached once `port_open` says something is
# listening, so this timeout is for a host that accepts TCP and then stalls --
# a TV part-way through boot -- not for a TV that is off. Off never gets here.
_REST_PORT = 8001
_REST_TIMEOUT_S = 2

# UPnP RenderingControl (services/tv_volume.py talks to it). Open only while the
# screen is on, which makes it the one network-visible power reading this set
# has. It also flakes -- one connect refused a moment after another succeeded --
# so "not on" needs more than one refused probe.
_UPNP_PORT = 9197
_UPNP_PROBES = 3
_UPNP_PROBE_GAP_S = 0.3


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
    # Set by MediaService.turn_on on the fast path: True when the CEC bus
    # confirmed the TV is on and showing the box, False when it definitely is
    # not, None when the box came up but the television could not be confirmed
    # either way. `ok` alone is "the box is awake", which is a weaker claim.
    tv_confirmed: bool | None = None

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
        self.input_key = str(cfg.get("input_key") or "KEY_HDMI")
        # Pause between keys of one sequence. samsungtvws sleeps a hidden 1s
        # after every key by default (key_press_delay); _send_keys turns that off
        # and uses this instead, which took the Source-menu sequence from 11.3s
        # to 2.5s on the real set.
        self.key_delay_s = max(0.0, int(cfg.get("key_delay_ms", 250)) / 1000)
        # The Source menu needs a moment to open before it takes navigation.
        self.source_menu_open_s = max(0.0, int(cfg.get("source_menu_open_ms", 800)) / 1000)
        # Source menu order on this set, left to right: TV, HDMI 1, HDMI 2.
        # RIGHT presses from the leftmost item to reach an HDMI port is therefore
        # the port number; override if a set lists more before them.
        self.source_menu_left_presses = max(1, int(cfg.get("source_menu_left_presses", 4)))
        self.mibox_hdmi_port = int(cfg.get("mibox_hdmi_port", 2))
        self.wol_attempts = max(1, int(cfg.get("wol_attempts", 5)))
        self.tv_boot_timeout_s = max(1, int(cfg.get("tv_boot_timeout_ms", 40000))) / 1000
        # Same key MediaService reads for the box: how often to re-check a device
        # that is booting. One rediscovery per miss costs ~2s, so polling faster
        # than this buys nothing.
        self.poll_interval_s = max(0.1, int(cfg.get("poll_interval_ms", 2000)) / 1000)
        self.hdmi_ports = {int(k): str(v) for k, v in (cfg.get("hdmi_ports") or {}).items()}

        # The same ladder the box uses -- memory -> cache -> hint -> ARP by MAC ->
        # TCP scan -- with the duid probe as `verify`. This replaced a private
        # copy that HTTP-probed with a 4s timeout, tried cached and hint in series,
        # ping-flooded the /24 and then HTTP-probed all 254 hosts: ~35s per miss
        # on a TV that was off, measured 2026-09-11. Port-gated, a miss is ~2s.
        disc_cfg = ((config.get("media") or {}).get("discovery") or {})
        self.port_probe_timeout_s = max(0.05, int(disc_cfg.get("port_probe_timeout_ms", 300)) / 1000)
        state_path = cfg.get("tv_state_path") or disc_cfg.get("state_path") or "device_state.json"
        self._finder = DeviceFinder(
            key="tv",
            label="TV",
            mac=self.tv_mac,
            hint=self.tv_ip_hint,
            cache_path=Path(str(state_path)),
            # Late-bound on purpose: tests patch CecWaker._probe on the class.
            verify=lambda ip: self._probe(ip),
            candidate_sources=[
                arp_table_candidates(self.tv_mac),
                tcp_port_candidates(
                    _REST_PORT,
                    timeout_s=self.port_probe_timeout_s,
                    workers=max(1, int(disc_cfg.get("scan_workers", 64))),
                ),
            ],
        )
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

    @property
    def _tv_ip(self) -> str | None:
        """Where the TV last verified. `_remote()` needs this to be non-empty."""
        return self._finder.ip or None

    # ------------------------------------------------------------ discovery ---

    def _probe(self, ip: str) -> bool:
        """
        True when `ip` answers the Samsung REST endpoint AS THIS TV.

        Port probe first, then HTTP -- the same ordering as the box's adb verify,
        for the same reason: a host that is off does not refuse quickly, it stays
        silent until the timeout. 0.3s to learn that instead of 4s, on every rung.
        """
        if not ip or not port_open(ip, _REST_PORT, self.port_probe_timeout_s):
            return False
        try:
            with urllib.request.urlopen(
                f"http://{ip}:{_REST_PORT}/api/v2/", timeout=_REST_TIMEOUT_S
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return False
        # The duid check is the point. Without it a stale IP that some *other*
        # device has since taken would answer 200 and be used happily.
        return str(payload.get("device", {}).get("duid", "")) == self.tv_duid

    def resolve_tv_ip(self, force: bool = False, discover: bool = True) -> str:
        """
        Cached hint -> ARP by MAC -> TCP scan on :8001. Verified by duid at every step.

        The happy path is one probe. Discovery only runs on a miss, which is
        why a DHCP move self-heals instead of surfacing as "couldn't turn the
        TV on". Delegates to the shared DeviceFinder; see its docstring.
        """
        return self._finder.resolve(force=force, discover=discover)

    # ----------------------------------------------------------------- wake ---

    def _tv_is_up(self) -> bool:
        """
        Probe the TV at its known addresses, and remember where it answered.

        Known addresses only, no scan: this runs BEFORE Wake-on-LAN, and a TV that
        is off cannot be found by scanning -- spending ~2s on one just delays the
        packet that turns it on. The scan happens in `power_on_tv`'s poll loop,
        where the TV is booting and may well come up on a new lease.

        The finder records the address as a side effect, which `_remote()` needs:
        `power_on_tv` returns early on this, and without it SamsungTVWS was built
        with host=None -- "Can't build URL with port but without host".
        """
        return bool(self.resolve_tv_ip(force=True, discover=False))

    def _send_wol(self) -> None:
        raw = bytes.fromhex(self.tv_mac.replace(":", "").replace("-", ""))
        packet = b"\xff" * 6 + raw * 16
        base = self._finder.subnet()
        for target in ("255.255.255.255", f"{base}.255"):
            for port in (9, 7):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.sendto(packet, (target, port))
                    sock.close()
                except OSError as exc:
                    log.debug("WoL to %s:%s failed: %s", target, port, exc)

    def power_on_tv(self, timeout_s: float | None = None) -> bool:
        """
        Wake-on-LAN the TV and wait for it to answer. Needs no token.

        `timeout_s` caps the whole wait; the default is the configured
        tv_boot_timeout. The fast path in MediaService passes its own budget so
        a television that is unplugged cannot hold a wake hostage for 40s while
        the box itself came up in one.

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
        budget_s = self.tv_boot_timeout_s if timeout_s is None else max(0.0, float(timeout_s))
        for attempt in range(1, self.wol_attempts + 1):
            log.info("WoL to %s (attempt %d/%d)", self.tv_mac, attempt, self.wol_attempts)
            self._send_wol()
            # Every miss rediscovers, then retries: the TV came back from deep
            # standby on a new lease (.34 -> .59, 2026-09-11) and only a scan per
            # poll finds that the moment it happens. Same rule as _wait_for_box.
            deadline = time.monotonic() + (budget_s / self.wol_attempts)
            while time.monotonic() < deadline:
                time.sleep(self.poll_interval_s)
                if self.resolve_tv_ip(force=True):
                    log.info("TV is up at %s", self._tv_ip)
                    return True
        return False

    def _remote(self):
        from samsungtvws import SamsungTVWS

        return SamsungTVWS(host=self._tv_ip, port=self.tv_port,
                           token_file=self.token_path, name="California", timeout=30)

    def _send_keys(self, keys: list[str], first_pause_s: float | None = None) -> WakeResult:
        """
        Send `keys` over ONE websocket connection, `key_delay_s` apart.

        `key_press_delay=0` matters: samsungtvws otherwise sleeps a full second
        after every key, which is where most of the old 6s "pair" went.
        `first_pause_s` replaces the gap after the first key, for a menu that
        needs time to open before it takes navigation.
        """
        remote = None
        try:
            remote = self._remote()
            for index, key in enumerate(keys):
                remote.send_key(key, key_press_delay=0)
                if index < len(keys) - 1:
                    first = index == 0 and first_pause_s is not None
                    time.sleep(first_pause_s if first else self.key_delay_s)
        except Exception as exc:
            # A revoked token and an unplugged TV both surface as exceptions
            # here, and they need opposite fixes -- one wants a human at the
            # screen, the other wants the remote. Separate them.
            if _looks_like_auth_failure(exc):
                log.error("TV rejected the stored token: %s", exc)
                return WakeResult(False, f"TV pairing token rejected: {exc}", needs_pairing=True)
            log.error("TV remote failed: %s", exc)
            return WakeResult(False, f"TV remote failed: {exc}")
        finally:
            close = getattr(remote, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing a dead socket is not news
                    pass
        return WakeResult(True, f"sent {', '.join(keys)}")

    # ---------------------------------------------------------------- power ---

    def tv_power(self, discover: bool = False) -> str | None:
        """
        "on" | "standby" | "off", read off the network. None when unavailable.

          on       :9197 (UPnP) answers -- only ever open with the screen on
          standby  :8001 answers as this TV but :9197 does not (~15s after the
                   set goes dark). KEY_POWER lifts it; Wake-on-LAN does not.
          off      nothing answers: deep standby, or unplugged. WoL lifts it.

        Measured 2026-09-25: :9197 closed within 6s of standby while :8001 stayed
        up ~15s, then both closed. "on" is a positive reading; the other two are
        only ever concluded after `_UPNP_PROBES` refusals, because :9197 flakes.

        `discover=False` probes known addresses only, which is right before a
        power-on: a TV that is off cannot be found by scanning.
        """
        if not self.available:
            return None
        ip = self.resolve_tv_ip(force=True, discover=discover)
        if not ip:
            return "off"
        for attempt in range(_UPNP_PROBES):
            if port_open(ip, _UPNP_PORT, self.port_probe_timeout_s):
                return "on"
            if attempt < _UPNP_PROBES - 1:
                time.sleep(_UPNP_PROBE_GAP_S)
        return "standby"

    def ensure_tv_on(self, timeout_s: float | None = None) -> WakeResult:
        """
        Get the screen on, by whichever route its standby depth needs.

        standby -> KEY_POWER, a toggle, so only behind a "standby" reading taken
        twice: a set part-way through powering on answers :8001 about a second
        before :9197 (1.5s vs 2.6s measured), and one early read would switch it
        straight back off. off -> Wake-on-LAN, resent while it boots. Success is
        :9197 answering, never "REST came back", which is also true in standby.
        """
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        budget_s = self.tv_boot_timeout_s if timeout_s is None else max(0.0, float(timeout_s))
        started = time.monotonic()
        state = self.tv_power()
        if state == "on":
            return WakeResult(True, "TV already on")
        if state == "standby":
            time.sleep(1.5)
            state = self.tv_power()
            if state == "on":
                return WakeResult(True, "TV came on by itself")
        if state == "standby":
            sent = self._send_keys(["KEY_POWER"])
            if not sent:
                return sent
            how = "KEY_POWER"
        else:
            self._send_wol()
            how = "Wake-on-LAN"
        log.info("TV was %s; sent %s", state, how)

        deadline = started + budget_s
        last_wol = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if how == "Wake-on-LAN" and time.monotonic() - last_wol >= 3:
                self._send_wol()
                last_wol = time.monotonic()
            # Rediscover on every miss once WoL is out: the set can come back on a
            # new lease (.34 -> .59, 2026-09-11), and only a scan finds that.
            if self.tv_power(discover=how == "Wake-on-LAN") == "on":
                elapsed = time.monotonic() - started
                log.info("TV on after %s in %.1fs", how, elapsed)
                return WakeResult(True, f"TV on after {how} in {elapsed:.1f}s")
        return WakeResult(False, f"TV did not come on after {how}")

    def standby_tv(self, tv_confirmed_on: bool) -> WakeResult:
        """
        Put the television into standby WITHOUT sleeping the box.

        `tv_confirmed_on` must be a FRESH positive reading -- `tv_power() == "on"`
        or the CEC bus saying on -- and it is required rather than advisory.
        KEY_POWEROFF, Samsung's discrete off key, would need no such proof -- but
        this UE49M5505 ignores it outright (measured 2026-09-21: two presses,
        nothing on the bus, TV stayed on). What works is KEY_POWER, and that is a
        toggle, so sending it against an unknown state is the same class of bug
        as KEYCODE_POWER on the box: it would turn a dark television back on.
        Never pass a REST probe: this set answers :8001 in standby.
        """
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        if not tv_confirmed_on:
            return WakeResult(False, "TV power unconfirmed; KEY_POWER is a toggle")
        if not self.resolve_tv_ip():
            return WakeResult(False, "TV not reachable")
        return self._send_keys(["KEY_POWER"])

    # ----------------------------------------------------------------- wake ---

    def wake(self) -> WakeResult:
        """
        Power the TV so that its own CEC wakes the box. No input keys.

        The TV announces the input it comes up on once its CEC side is ready,
        and the box wakes on that when it is HDMI 2 -- which is where this TV
        sits nearly always. Sending input keys here is what used to break it; see
        the module docstring. `select_box_input` is the rescue for the rest.
        """
        return self.ensure_tv_on()

    def select_input(self, port: int) -> WakeResult:
        """
        Select HDMI `port` through the Source menu. Works whatever is selected now.

        KEY_HDMI<n> is accepted and ignored by this set, and the KEY_HDMI cycle
        skips an input with no signal -- a sleeping box -- so neither can select
        the box reliably. The menu lists every input, dark or not: TV, HDMI 1,
        HDMI 2, and LEFT stops at the first. So LEFT to the start, RIGHT x port.
        Measured 2026-09-25 from HDMI 1 with the box asleep: box on the LAN 7.9s
        after Enter.
        """
        if not self.available:
            return WakeResult(False, self.unavailable_reason)
        if not self.resolve_tv_ip():
            return WakeResult(False, "TV not reachable")
        keys = (["KEY_SOURCE"] + ["KEY_LEFT"] * self.source_menu_left_presses
                + ["KEY_RIGHT"] * max(0, int(port)) + ["KEY_ENTER"])
        result = self._send_keys(keys, first_pause_s=self.source_menu_open_s)
        if result:
            return WakeResult(True, f"selected HDMI {port}")
        return result

    def select_box_input(self) -> WakeResult:
        """The Source-menu route to the box's own input."""
        return self.select_input(self.mibox_hdmi_port)

    def switch_input(self, port: int) -> WakeResult:
        """Select an HDMI port on the TV. See select_input for why not KEY_HDMI<n>."""
        return self.select_input(port)


def _looks_like_auth_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in
               ("unauthor", "forbidden", "token", "denied", "ms.channel.unauthorized"))
