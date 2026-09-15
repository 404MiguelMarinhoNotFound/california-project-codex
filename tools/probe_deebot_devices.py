"""
Logs into the EcoVacs account and lists Deebot devices with what deebot-client
knows about them: the API-reported model name/class, and whether the library
resolved a model-specific capability profile for that class.

This is exploratory only, no vacuum control here. It exists to answer one
question before anything gets wired into control_tv/control_lights-style
dispatch: does deebot-client actually recognize the N8+ (or whatever is on
the account)?

Ecovacs now requires one-time device verification (an emailed code) before
a device id logs in, and it does not reliably stay verified: this
library's own internal token refresh re-runs a full password login, which
re-checks verification, and Ecovacs doesn't always honor "already verified"
on that recheck (see tools/deebot_session.py and the upstream issue linked
there). The mitigation is to never do a cold password login when a
still-valid cached token exists -- tools/deebot_session.py persists both
the device id and the login token locally and reuses them, so this only
needs a fresh emailed code roughly when the cached token actually expires
(~7 days), not on every run.

Needs in .env:
  ECOVACS_EMAIL
  ECOVACS_PASSWORD
  ECOVACS_COUNTRY   two-letter code, e.g. PT, US, DE (default: PT)

  uv sync --extra default --extra deebot
  uv run python tools/probe_deebot_devices.py
  uv run python tools/probe_deebot_devices.py --json
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import deebot_session  # noqa: E402


async def probe(email: str, password: str, country: str, as_json: bool) -> int:
    import aiohttp
    from deebot_client.api_client import ApiClient

    device_id = deebot_session.stable_device_id()

    async with aiohttp.ClientSession() as session:
        authenticator = await deebot_session.build_authenticator(
            session, device_id=device_id, country=country, email=email, password=password
        )

        try:
            if not await deebot_session.authenticate(authenticator):
                return 1
            api_client = ApiClient(authenticator)
            devices = await api_client.get_devices()
        except Exception as exc:  # noqa: BLE001 - report auth/API errors plainly
            print(f"Login or device fetch failed: {exc!r}")
            return 1

        if not devices.mqtt and not devices.xmpp and not devices.not_supported:
            print("Login worked, but the account has no devices at all.")
            return 0

        results = []
        for entry in devices.mqtt:
            results.append(
                {
                    "name": entry.api.get("name"),
                    "nick": entry.api.get("nick"),
                    "class": entry.api.get("class"),
                    "deviceName": entry.api.get("deviceName"),
                    "company": entry.api.get("company"),
                    "protocol": "mqtt",
                    "supported": True,
                }
            )
        for entry in devices.xmpp:
            results.append(
                {
                    "name": entry.get("name"),
                    "nick": entry.get("nick"),
                    "class": entry.get("class"),
                    "deviceName": entry.get("deviceName"),
                    "company": entry.get("company"),
                    "protocol": "xmpp (legacy, deebot-client cannot drive this)",
                    "supported": False,
                }
            )
        for entry in devices.not_supported:
            results.append(
                {
                    "name": entry.get("name"),
                    "nick": entry.get("nick"),
                    "class": entry.get("class"),
                    "deviceName": entry.get("deviceName"),
                    "company": entry.get("company"),
                    "protocol": "unrecognized class, no capability profile",
                    "supported": False,
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
            print(f"  protocol:      {entry['protocol']}")
            print(f"  supported:     {'yes' if entry['supported'] else 'NO'}")
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
