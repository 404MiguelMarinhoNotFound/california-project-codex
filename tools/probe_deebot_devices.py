"""
Logs into the EcoVacs account and lists Deebot devices with what deebot-client
knows about them: the API-reported model name/class, and whether the library
resolved a model-specific capability profile for that class or fell back to
the generic one.

This is exploratory only, no vacuum control here. It exists to answer one
question before anything gets wired into control_tv/control_lights-style
dispatch: does deebot-client actually recognize the N8+ (or whatever is on
the account), or does it fall back to the generic profile (which still
covers most core commands, per deebot_client/hardware/deebot/fallback.py,
just not model-specific extras like mapping/mopping variants)?

Needs in .env:
  ECOVACS_EMAIL
  ECOVACS_PASSWORD
  ECOVACS_COUNTRY   two-letter code, e.g. PT, US, DE (default: PT)

  uv sync --extra deebot
  uv run python tools/probe_deebot_devices.py
  uv run python tools/probe_deebot_devices.py --json
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _patch_stale_app_version() -> None:
    """Work around Ecovacs rejecting deebot-client's hardcoded appVersion.

    deebot-client 6.0.2 (the newest release that still supports our pinned
    Python 3.11 -- upstream bumped to requires-python >=3.14 in 18.x) sends
    appVersion "1.6.3", which the Ecovacs API now rejects with
    {"code": "1013", "msg": "Please update to the latest version to
    continue."}. Upstream's real fix (closed issue #1702, PR #1703) is
    exactly this: bump appVersion to match the current EcoVacs HOME Android
    app (3.14.0, per Google Play / APKPure at the time of writing). It never
    shipped in a Python-3.11-compatible release, so it's patched here instead
    of vendoring a newer deebot-client.
    """
    from deebot_client import authentication

    authentication._META["appVersion"] = "3.14.0"


async def probe(email: str, password: str, country: str, as_json: bool) -> int:
    import aiohttp
    from deebot_client.api_client import ApiClient
    from deebot_client.authentication import Authenticator, create_rest_config
    from deebot_client.hardware import deebot as deebot_hardware
    from deebot_client.models import DeviceInfo
    from deebot_client.util import md5

    _patch_stale_app_version()

    device_id = md5(str(time.time()))
    password_hash = md5(password)

    async with aiohttp.ClientSession() as session:
        rest_config = create_rest_config(
            session, device_id=device_id, alpha_2_country=country
        )
        authenticator = Authenticator(rest_config, email, password_hash)
        api_client = ApiClient(authenticator)

        try:
            devices = await api_client.get_devices()
        except Exception as exc:  # noqa: BLE001 - report auth/API errors plainly
            print(f"Login or device fetch failed: {exc!r}")
            return 1

        if not devices:
            print("Login worked, but the account has no devices the API returned.")
            return 0

        fallback = deebot_hardware.DEVICES.get(deebot_hardware.FALLBACK)

        results = []
        for entry in devices:
            if isinstance(entry, DeviceInfo):
                api_info = entry.api
                is_fallback = entry.static is fallback
            else:
                # "eco-legacy" company devices come back as a bare dict, with
                # no static capability profile resolved at all.
                api_info = entry
                is_fallback = None

            results.append(
                {
                    "name": api_info.get("name"),
                    "nick": api_info.get("nick"),
                    "class": api_info.get("class"),
                    "deviceName": api_info.get("deviceName"),
                    "company": api_info.get("company"),
                    "is_fallback_profile": is_fallback,
                }
            )

        if as_json:
            print(json.dumps(results, indent=2, ensure_ascii=True))
            return 0

        print(f"{len(results)} device(s) returned by the EcoVacs API:\n")
        for entry in results:
            print(f"  name:          {entry['name']}")
            print(f"  nickname:      {entry['nick']}")
            print(f"  device name:   {entry['deviceName']}")
            print(f"  device class:  {entry['class']}")
            print(f"  company:       {entry['company']}")
            if entry["is_fallback_profile"] is None:
                print("  capabilities:  legacy (non-MQTT) device, deebot-client cannot drive this")
            elif entry["is_fallback_profile"]:
                print("  capabilities:  FALLBACK profile (generic, no model-specific extras)")
            else:
                print("  capabilities:  model-specific profile found")
            print()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print raw results as JSON")
    args = parser.parse_args()

    load_dotenv()
    email = os.getenv("ECOVACS_EMAIL")
    password = os.getenv("ECOVACS_PASSWORD")
    country = os.getenv("ECOVACS_COUNTRY", "PT")

    if not email or not password:
        print("Set ECOVACS_EMAIL and ECOVACS_PASSWORD in .env first.")
        return 1

    return asyncio.run(probe(email, password, country, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
