"""
Discovers Govee lights and prints what config.yaml needs.

Which mode runs is driven by `govee.transport` in config.yaml, or forced with
--transport:

  ble    scans for Govee devices over Bluetooth and prints their MAC and signal
         strength. The MAC goes into `govee.lights.<room>.mac`.
  cloud  lists devices on the Govee account with their sku, device id, and
         capabilities. Those go into `sku` and `device`.

Note that BLE-only models (the H617E among them) never appear in the cloud
listing no matter how healthy the API key is, because they never reach Govee's
cloud at all. An empty cloud listing plus a successful BLE scan is the signature
of exactly that.

  uv run python tools/probe_govee_devices.py
  uv run python tools/probe_govee_devices.py --transport ble
  uv run python tools/probe_govee_devices.py --transport cloud --json
"""

import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.govee_service import GoveeService


def _signal_label(rssi: int) -> str:
    if rssi > -70:
        return "strong"
    if rssi > -85:
        return "usable"
    return "marginal, expect dropouts"


def probe_ble(service, as_json: bool) -> None:
    devices = service.transport.discover()
    if as_json:
        print(json.dumps(devices, indent=2, ensure_ascii=True))
        return

    if not devices:
        print("No Govee devices found over Bluetooth.")
        print()
        print("Check that the strip is powered, that this machine has Bluetooth on,")
        print("and that the Govee phone app is force-closed. BLE allows only one")
        print("connection at a time, so the app holding it will hide the device.")
        return

    print(f"{len(devices)} Govee device(s) in Bluetooth range:\n")
    for entry in devices:
        print(f"  name:   {entry['name']}")
        print(f"  mac:    {entry['mac']}")
        print(f"  signal: {entry['rssi']} dBm ({_signal_label(entry['rssi'])})")
        print()
    print("Put the mac into config.yaml under govee.lights.<room>.mac")


def probe_cloud(service, as_json: bool) -> None:
    devices = service.transport.list_devices()
    if as_json:
        print(json.dumps(devices, indent=2, ensure_ascii=True))
        return

    if not devices:
        # list_devices() raises on an API-level error, so reaching here means the
        # key authenticated fine and the account simply exposes nothing.
        print("The API key works, but this Govee account exposes no API-controllable devices.")
        print()
        print("Most likely causes, in order:")
        print("  1. The light is a Bluetooth-only model. Those never appear here.")
        print("     Try --transport ble instead.")
        print("  2. The key was issued from a different Govee account than the one")
        print("     the light is paired to. Check the email in Govee Home > profile.")
        print("  3. The model is not on Govee's supported list:")
        print("     https://developer.govee.com/docs/support-product-model")
        return

    print(f"{len(devices)} device(s) on this account:\n")
    for entry in devices:
        print(f"  name:   {entry.get('deviceName', '(unnamed)')}")
        print(f"  sku:    {entry.get('sku', '?')}")
        print(f"  device: {entry.get('device', '?')}")
        capabilities = entry.get("capabilities") or []
        print("  capabilities:")
        for capability in capabilities:
            print(f"    - {capability.get('type', '?')} / {capability.get('instance', '?')}")
        has_power = any(c.get("instance") == "powerSwitch" for c in capabilities)
        print(f"  powerSwitch: {'yes' if has_power else 'NO - control_lights cannot drive this device'}")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["ble", "cloud"], help="Override govee.transport")
    parser.add_argument("--json", action="store_true", help="Print the raw payload instead of a summary")
    args = parser.parse_args()

    load_dotenv()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    # The probe is useful even when govee.enabled is false in config.
    govee_cfg = config.setdefault("govee", {})
    govee_cfg["enabled"] = True
    if args.transport:
        govee_cfg["transport"] = args.transport

    service = GoveeService(config)
    if service.transport is None:
        print(f"Unknown govee.transport: {service.transport_name!r}")
        return
    if not service.transport.available:
        if service.transport_name == "ble":
            print("bleak is not installed. Install it with: uv sync --extra govee")
        else:
            print("GOVEE_API_KEY is not set. Put it in .env, or use --transport ble.")
        return

    print(f"transport: {service.transport_name}\n")
    if service.transport_name == "ble":
        probe_ble(service, args.json)
    else:
        probe_cloud(service, args.json)


if __name__ == "__main__":
    main()
