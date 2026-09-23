"""
Discovers TP-Link Tapo lights and prints what config.yaml needs.

  uv run python tools/probe_tapo_devices.py
  uv run python tools/probe_tapo_devices.py --host 192.168.1.74
  uv run python tools/probe_tapo_devices.py --json

**This exists because python-kasa's own `kasa` CLI does not run on this
project.** It imports kasa before anything can patch `typing.ByteString` back
in, so on our pinned Python 3.14 it dies with the same AttributeError described
in `services/tapo_transport._ensure_kasa_importable`. Going through the
transport's shim is the only way to talk to these bulbs from this repo.

**It also has to see bulbs python-kasa cannot talk to.** Firmware 1.4.2+
speaks TPAP; python-kasa's discovery hears those bulbs and then drops them as
unsupported, so a plain `Discover.discover()` reports "nothing found" with a
bulb answering on the LAN (that is exactly what happened 2026-09-17). The
`on_unsupported` hook collects them, and every device -- supported or not --
is described through `TapoTransport._with_device`, which routes TPAP bulbs to
`services/tapo_tpap.py`.

Discovery is a UDP broadcast, so it only finds bulbs on the same subnet as this
machine. It is sent to 255.255.255.255 AND to the directed broadcast of every
local IPv4 /24, because on a Windows box with a second (virtual) adapter the
global broadcast leaves on the wrong one and hears nothing -- measured here:
255.255.255.255 found nothing four runs out of four, 192.168.1.255 found the
bulb every time. `--target` overrides that list; `--host` skips discovery and
queries one address directly, which is what to use across VLANs or when
broadcast is filtered.

Needs TAPO_USERNAME / TAPO_PASSWORD in .env -- local control still authenticates
against the TP-Link account.
"""

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.tapo_transport import TapoTransport


async def _describe(device, light) -> dict:
    """One row per bulb: what it is, where it is, and what it is doing."""
    brightness = None
    if light is not None and light.has_feature("brightness"):
        brightness = light.brightness
    return {
        "alias": device.alias,
        "host": device.host,
        "model": getattr(device, "model", ""),
        "on": device.is_on,
        "brightness": brightness,
    }


def _describe_host(transport: TapoTransport, host: str) -> dict:
    try:
        row = transport._run_async(lambda: transport._with_device(host, _describe))
    except Exception as exc:  # noqa: BLE001 - one bad bulb must not hide the rest
        return {"host": host, "error": str(exc)}
    row["protocol"] = transport._protocols.get(host, "kasa")
    return row


def _broadcast_targets() -> list[str]:
    """The global broadcast plus each local /24's directed one. See the docstring."""
    targets = ["255.255.255.255"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith("127."):
                continue
            directed = ".".join(address.split(".")[:3]) + ".255"
            if directed not in targets:
                targets.append(directed)
    except OSError:
        pass
    return targets


def _discover_hosts(transport: TapoTransport, timeout: float, targets: list[str]) -> list[str]:
    """Every address that answered a broadcast, whether python-kasa likes it or not."""
    from kasa import Credentials, Discover

    hosts: list[str] = []

    async def on_unsupported(exc) -> None:
        # TPAP bulbs land here. The host is on the exception; the discovery
        # result carries the model, but describing it through the transport
        # is the real test, so only the address is kept.
        if exc.host and exc.host not in hosts:
            hosts.append(exc.host)

    async def run() -> None:
        for target in targets:
            found = await Discover.discover(
                target=target,
                credentials=Credentials(username=transport.username, password=transport.password),
                discovery_timeout=timeout,
                on_unsupported=on_unsupported,
            )
            for host, device in found.items():
                if host not in hosts:
                    hosts.append(host)
                await device.disconnect()

    transport._run_async(run)
    return hosts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Query one address instead of broadcasting.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Discovery seconds per target.")
    parser.add_argument(
        "--target",
        action="append",
        help="Broadcast address to use (repeatable). Default: 255.255.255.255 plus every local /24.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    transport = TapoTransport({"tapo": {}})
    if not transport.available:
        print(
            "Tapo is not usable. Set TAPO_USERNAME and TAPO_PASSWORD in .env, and "
            "make sure python-kasa is installed: uv sync --extra default",
            file=sys.stderr,
        )
        return 2

    try:
        if args.host:
            hosts = [args.host]
        else:
            hosts = _discover_hosts(transport, args.timeout, args.target or _broadcast_targets())
    except Exception as exc:  # noqa: BLE001
        print(f"Probe failed: {exc}", file=sys.stderr)
        return 1
    rows = [_describe_host(transport, host) for host in hosts]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0 if rows else 1

    if not rows:
        print("No Tapo devices found. Try --host with the address from the Tapo app.")
        return 1

    for row in rows:
        if "error" in row:
            print(f"{row['host']}: unreadable ({row['error']})")
            continue
        state = "on" if row["on"] else "off"
        if row["on"] and row["brightness"] is not None:
            state += f" at {row['brightness']}%"
        print(f"{row['alias']} ({row['model']}, {row['protocol']}) at {row['host']} -- {state}")

    print("\nPut the address into config.yaml under govee.lights.<room>.host,")
    print('and set govee.transport to "tapo".')
    return 0


if __name__ == "__main__":
    sys.exit(main())
