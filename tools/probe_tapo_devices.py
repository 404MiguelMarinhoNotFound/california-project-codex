"""
Discovers TP-Link Tapo lights and prints what config.yaml needs.

  uv run python tools/probe_tapo_devices.py
  uv run python tools/probe_tapo_devices.py --host 192.168.1.42
  uv run python tools/probe_tapo_devices.py --json

**This exists because python-kasa's own `kasa` CLI does not run on this
project.** It imports kasa before anything can patch `typing.ByteString` back
in, so on our pinned Python 3.14 it dies with the same AttributeError described
in `services/tapo_transport._ensure_kasa_importable`. Going through the
transport's shim is the only way to talk to these bulbs from this repo.

Discovery is a UDP broadcast, so it only finds bulbs on the same subnet as this
machine. `--host` skips it and queries one address directly, which is what to
use across VLANs or when broadcast is filtered.

Needs TAPO_USERNAME / TAPO_PASSWORD in .env -- local control still authenticates
against the TP-Link account the bulbs were onboarded with.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.tapo_transport import TapoTransport


async def _describe(device) -> dict:
    """One row per bulb: what it is, where it is, and what it is doing."""
    from kasa import Module

    await device.update()
    light = device.modules.get(Module.Light)
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


async def _discover(transport: TapoTransport, timeout: float) -> list[dict]:
    from kasa import Credentials, Discover

    found = await Discover.discover(
        credentials=Credentials(username=transport.username, password=transport.password),
        discovery_timeout=timeout,
    )
    rows = []
    for device in found.values():
        try:
            rows.append(await _describe(device))
        except Exception as exc:  # noqa: BLE001 - one bad bulb must not hide the rest
            rows.append({"host": device.host, "error": str(exc)})
        finally:
            await device.disconnect()
    return rows


async def _one(transport: TapoTransport, host: str) -> list[dict]:
    from kasa import Credentials, Device, DeviceConfig

    config = DeviceConfig(
        host=host,
        credentials=Credentials(username=transport.username, password=transport.password),
        timeout=transport.timeout_s,
    )
    device = await Device.connect(config=config)
    try:
        return [await _describe(device)]
    finally:
        await device.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Query one address instead of broadcasting.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Discovery seconds.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args()

    load_dotenv()
    transport = TapoTransport({"tapo": {}})
    if not transport.available:
        print(
            "Tapo is not usable. Set TAPO_USERNAME and TAPO_PASSWORD in .env, and "
            "make sure python-kasa is installed: uv sync --extra default",
            file=sys.stderr,
        )
        return 2

    work = _one(transport, args.host) if args.host else _discover(transport, args.timeout)
    try:
        rows = transport._run_async(lambda: work)
    except Exception as exc:  # noqa: BLE001
        print(f"Probe failed: {exc}", file=sys.stderr)
        return 1

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
        print(f"{row['alias']} ({row['model']}) at {row['host']} -- {state}")

    print("\nPut the address into config.yaml under govee.lights.<room>.host,")
    print('and set govee.transport to "tapo".')
    return 0


if __name__ == "__main__":
    sys.exit(main())
