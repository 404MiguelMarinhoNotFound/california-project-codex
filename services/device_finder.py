"""
Find a device on the LAN by MAC, prove it is the right one, and cache where it was.

WHY THIS EXISTS
`media.mibox_ip` was a static config literal. The box moved 192.168.1.35 -> .40 on a
plain DHCP renewal -- no reboot, 9.6 days of uptime -- and every control_tv call
started returning "TV is off or unreachable right now". The address had to be edited
by hand. The television already self-healed by MAC + duid; the box had nothing.

So an address is never configuration here. It is a *hint* that seeds a cache, and the
identity that matters is the pair (MAC to find it, an identity probe to prove it).

THE 21-SECOND RULE
Measured on the real box, 2026-09-05:

    adb connect  -> host with 5555 CLOSED   21.1s   (adb's own timeout, not tunable)
    adb connect + getprop ro.serialno       0.37s
    raw TCP connect scan of a whole /24     1.55s   (-> exactly 1 candidate)
    `arp -a` grep for a known MAC, warm     0.16s

A sweep that hands unverified hosts to `adb connect` therefore costs 254 * 21s. That
is why `tcp_port_candidates` exists and why every adb-based `verify` must gate itself
behind a cheap socket probe first. Do not "simplify" that gate away.

WHAT IS INJECTED, AND WHY THERE IS NO `if device == "tv"`
The two devices differ in exactly two things, and both are values rather than
behaviour, so they are constructor arguments:

    TV   verify: HTTP GET :8001/api/v2/ -> device.duid matches
         find:   warm ARP -> ping+ARP -> probe every host
    Box  verify: TCP 5555 open -> adb connect -> ro.serialno matches
         find:   warm ARP -> TCP scan on 5555

The ladder, the cache, the subnet derivation and the trace are the shared parts.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

# A candidate source is handed the subnet base ("192.168.1") and yields addresses
# to try, cheapest-first. It never decides identity -- `verify` does that.
CandidateSource = Callable[[str], Iterable[str]]

_DEFAULT_SUBNET_BASE = "192.168.1"


@dataclass(frozen=True)
class TraceStep:
    """One rung of a resolve(), for the debug tools and the failure classifier."""

    rung: str
    ip: str
    verdict: str  # "verified" | "rejected" | "skipped"
    elapsed_s: float


def normalize_mac(mac: str) -> str:
    """Lowercase, '-'-separated. Windows `arp -a` prints 54-bd-79-..., Linux 54:bd:79."""
    return (mac or "").strip().lower().replace(":", "-")


def subnet_base(*hints: str) -> str:
    """
    First three octets of the first hint that looks like an IPv4 address.

    Takes hints in priority order rather than reaching for a cached value itself, so
    the caller decides precedence. cec_wake.py repeated this expression verbatim in
    three places; this is that expression, once.
    """
    for hint in hints:
        parts = (hint or "").strip().split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return ".".join(parts[:3])
    return _DEFAULT_SUBNET_BASE


def port_open(ip: str, port: int, timeout_s: float) -> bool:
    """
    Cheap reachability gate. THIS IS WHAT KEEPS `adb connect` OFF UNKNOWN HOSTS.

    See the 21-second rule in the module docstring: never raise `timeout_s` toward
    adb's own connect timeout to "be safe" -- that reintroduces the exact cost this
    exists to avoid.
    """
    try:
        with socket.create_connection((ip, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _read_arp_table() -> str:
    try:
        return subprocess.run(
            ["arp", "-a"], capture_output=True, text=True,
            errors="replace", timeout=15,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _ips_for_mac(table: str, mac: str) -> list[str]:
    wanted = normalize_mac(mac)
    if not wanted:
        return []
    found = []
    for line in table.splitlines():
        if wanted not in normalize_mac(line):
            continue
        for token in line.split():
            # Linux prints the address as "? (192.168.1.41) at ..." -- the
            # parentheses are part of the token and must come off before the
            # digit check, or this parses on Windows and silently returns
            # nothing on the Pi.
            token = token.strip("()[],")
            if token.count(".") == 3 and token.replace(".", "").isdigit():
                found.append(token)
                break
    return found


def arp_table_candidates(mac: str) -> CandidateSource:
    """
    `arp -a` only -- no pings. 0.16s when the router table is warm, which it is
    right after a DHCP renewal. This is the rung that catches the common case.
    """

    def source(_base: str) -> Iterable[str]:
        return _ips_for_mac(_read_arp_table(), mac)

    return source


def arp_ping_candidates(mac: str) -> CandidateSource:
    """
    Ping the whole /24 to populate the ARP cache, then look the MAC up.

    Costs ~4.6s and 254 spawned processes. Only worth it for a device with no other
    cheap signal -- the box uses `tcp_port_candidates` instead, whose SYNs populate
    ARP as a side effect for free.
    """

    def source(base: str) -> Iterable[str]:
        flag = "-n" if sys.platform == "win32" else "-c"
        with ThreadPoolExecutor(max_workers=64) as pool:
            list(pool.map(
                lambda host: _ping_quietly(f"{base}.{host}", flag),
                range(1, 255),
            ))
        return _ips_for_mac(_read_arp_table(), mac)

    return source


def _ping_quietly(ip: str, flag: str) -> None:
    try:
        subprocess.run(["ping", flag, "1", "-w", "300", ip],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def tcp_port_candidates(port: int, timeout_s: float = 0.3, workers: int = 64) -> CandidateSource:
    """
    Every host on the /24 with `port` open. Measured at 1.55s for a whole subnet.

    Faster AND more selective than a ping sweep, because "answers on 5555" is already
    almost the answer. The SYNs also warm the ARP table, which is what lets the
    failure classifier tell "box is off" from "box is up but ADB is disabled".
    """

    def source(base: str) -> Iterable[str]:
        hosts = [f"{base}.{h}" for h in range(1, 255)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            hits = pool.map(lambda ip: (ip, port_open(ip, port, timeout_s)), hosts)
            return [ip for ip, is_open in hits if is_open]

    return source


def all_hosts_candidates() -> CandidateSource:
    """Every host on the /24, letting `verify` do all the work. The last resort."""

    def source(base: str) -> Iterable[str]:
        return [f"{base}.{h}" for h in range(1, 255)]

    return source


@dataclass
class DeviceFinder:
    """
    Resolve one device's address: memory -> cache -> hint -> candidate sources.

    Every rung is confirmed by `verify` before it is believed, which is the whole
    point: a stale address that some other device has since been given would answer
    perfectly well, and be wrong.

    Never raises. `resolve()` returns "" when the device cannot be found, matching
    the CecWaker convention of degrading rather than throwing.
    """

    key: str
    label: str
    mac: str
    hint: str
    cache_path: Path
    verify: Callable[[str], bool]
    candidate_sources: Sequence[CandidateSource]

    _ip: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    last_trace: list[TraceStep] = field(default_factory=list)
    last_verdict: str = ""

    # ------------------------------------------------------------------ cache ---

    def _load_doc(self) -> dict:
        try:
            doc = json.loads(Path(self.cache_path).read_text("utf-8"))
            return doc if isinstance(doc, dict) else {}
        except Exception:
            return {}

    def cached_entry(self) -> dict:
        entry = (self._load_doc().get("devices") or {}).get(self.key)
        if not isinstance(entry, dict):
            return {}
        # A cached address is only meaningful for the MAC it was found under. When
        # the configured MAC changes -- new SSID, factory reset -- the stored IP is
        # stale for exactly the same reason, so drop it rather than probing it.
        stored = normalize_mac(str(entry.get("mac") or ""))
        if stored and self.mac and stored != normalize_mac(self.mac):
            log.info("%s: cached entry was for MAC %s, config says %s -- discarding",
                     self.label, stored, normalize_mac(self.mac))
            return {}
        return entry

    def cached_ip(self) -> str:
        return str(self.cached_entry().get("ip") or "")

    def cached_or_hint(self) -> str:
        """Best guess without touching the network. Safe to call at construction."""
        return self.cached_ip() or self.hint

    def remember(self, ip: str) -> None:
        """
        Read-modify-write, so two devices can share one file.

        The old cec_wake._remember_ip wrote {"tv_ip": ip} over the whole file. Point
        two devices at that and each rediscovery erases the other's entry.
        """
        try:
            doc = self._load_doc()
            doc.setdefault("version", 1)
            doc.setdefault("devices", {})[self.key] = {
                "ip": ip,
                "mac": self.mac,
                "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            path = Path(self.cache_path)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(doc, indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            # Degrade to "rediscovers every restart", never to a crash.
            log.debug("Could not write %s: %s", self.cache_path, exc)

    def forget(self) -> None:
        self._ip = ""

    # ------------------------------------------------------------------ probe ---

    def _try(self, rung: str, ip: str) -> bool:
        started = time.monotonic()
        ok = bool(ip) and bool(self.verify(ip))
        self.last_trace.append(TraceStep(
            rung=rung, ip=ip,
            verdict="verified" if ok else "rejected",
            elapsed_s=time.monotonic() - started,
        ))
        return ok

    def subnet(self) -> str:
        return subnet_base(self._ip, self.cached_ip(), self.hint)

    def resolve(self, force: bool = False) -> str:
        """
        Cached -> hint -> candidate sources, verified at every rung.

        The happy path is one probe. Discovery only runs on a miss, which is what
        makes a DHCP move self-heal instead of surfacing as "unreachable".
        """
        with self._lock:
            if self._ip and not force:
                return self._ip

            self.last_trace = []
            self.last_verdict = ""

            for rung, candidate in (("cache", self.cached_ip()), ("hint", self.hint)):
                if candidate and self._try(rung, candidate):
                    self._ip = candidate
                    # Persist a hint hit too. The old code only wrote on a discovery
                    # hit, so the cache stayed empty until something moved.
                    if candidate != self.cached_ip():
                        self.remember(candidate)
                    self.last_verdict = "verified"
                    return candidate

            log.info("%s not at its known address, rediscovering by MAC %s",
                     self.label, self.mac)
            base = self.subnet()
            for source in self.candidate_sources:
                name = getattr(source, "__qualname__", "source").split(".")[0]
                try:
                    candidates = list(source(base))
                except Exception as exc:
                    log.debug("%s: candidate source %s failed: %s", self.label, name, exc)
                    continue
                for candidate in candidates:
                    if self._try(name, candidate):
                        log.info("%s rediscovered at %s", self.label, candidate)
                        self.remember(candidate)
                        self._ip = candidate
                        self.last_verdict = "verified"
                        return candidate

            self.last_verdict = "not-found"
            return ""
