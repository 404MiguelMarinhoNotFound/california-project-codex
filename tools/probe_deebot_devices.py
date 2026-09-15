"""
Logs into the EcoVacs account and lists Deebot devices with what deebot-client
knows about them: the API-reported model name/class, and whether the library
resolved a model-specific capability profile for that class.

This is exploratory only, no vacuum control here. It exists to answer one
question before anything gets wired into control_tv/control_lights-style
dispatch: does deebot-client actually recognize the N8+ (or whatever is on
the account)?

Ecovacs now requires one-time device verification (an emailed code) the
first time a given device id logs in, and that device id must then stay
STABLE across future logins -- a fresh random id every run re-triggers
verification forever. This script persists its device id to
.deebot_device_id (gitignored) for exactly that reason.

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

DEVICE_ID_PATH = ROOT / ".deebot_device_id"


def _stable_device_id() -> str:
    from deebot_client.util import md5

    if DEVICE_ID_PATH.exists():
        return DEVICE_ID_PATH.read_text(encoding="utf-8").strip()

    device_id = md5(os.urandom(16).hex())
    DEVICE_ID_PATH.write_text(device_id, encoding="utf-8")
    return device_id


async def _authenticate(authenticator) -> None:
    from deebot_client.exceptions import DeviceVerificationRequiredError

    try:
        await authenticator.authenticate()
        return
    except DeviceVerificationRequiredError:
        pass

    print("Ecovacs wants this device verified before it will log in.")
    print(f"Device id {DEVICE_ID_PATH.read_text(encoding='utf-8').strip()!r} is now")
    print("saved locally and will be reused on future runs, so this should only")
    print("happen once per device id.\n")

    code = os.getenv("ECOVACS_VERIFICATION_CODE")
    if code:
        # A code from a previous run's email is already in hand -- verify
        # with it directly. Do NOT also request a new one here: Ecovacs only
        # keeps the latest code valid, so requesting again would invalidate
        # the one we were just handed.
        print("Using ECOVACS_VERIFICATION_CODE from the environment.")
        await authenticator.verify_device(code.strip())
        return

    await authenticator.request_device_verification_code()
    print("Check your email for the code, then re-run with:")
    print("  ECOVACS_VERIFICATION_CODE=123456 uv run python tools/probe_deebot_devices.py")
    raise SystemExit(1)


async def probe(email: str, password: str, country: str, as_json: bool) -> int:
    import aiohttp
    from deebot_client.api_client import ApiClient
    from deebot_client.authentication import Authenticator, create_rest_config
    from deebot_client.util import md5

    device_id = _stable_device_id()
    password_hash = md5(password)

    async with aiohttp.ClientSession() as session:
        rest_config = create_rest_config(
            session, device_id=device_id, alpha_2_country=country
        )
        authenticator = Authenticator(rest_config, email, password_hash)

        try:
            await _authenticate(authenticator)
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
