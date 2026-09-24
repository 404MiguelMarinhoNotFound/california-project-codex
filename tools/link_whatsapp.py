"""
Link California's own browser profile to WhatsApp, or relink after a logout.

The Playwright backend drives WhatsApp Web in a dedicated profile folder
(`whatsapp.profile_dir`), never the everyday browser profile -- Playwright's
docs say automating a default Chrome/Edge profile is unsupported. That folder
starts empty, so it has to be linked once as a WhatsApp linked device, and that
needs a phone and a person: open WhatsApp on the phone, Settings -> Linked
devices -> Link a device, and scan the QR code this opens.

    uv run python tools/link_whatsapp.py

It opens a visible window, waits for the chat list to load, and exits 0 once
the profile is linked (or already was). Run it again whenever California says
WhatsApp needs relinking -- a linked device drops after roughly two weeks with
the phone offline, or when it is removed from the phone's device list.
"""

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.whatsapp_web import WHATSAPP_URL, WhatsAppWebDriver  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Link California's WhatsApp Web profile.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for the scan")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("whatsapp", {}) or {}

    # Always visible: there is a QR code to scan.
    driver = WhatsAppWebDriver.from_config(cfg, headless=False)
    print(f"Profile: {driver.profile_dir.resolve()}  (browser: {driver.channel})", flush=True)

    try:
        page = driver.open_page()
        page.goto(WHATSAPP_URL)
        deadline = time.monotonic() + args.timeout
        announced = False
        while time.monotonic() < deadline:
            state = driver.session_state(page)
            if state == "linked":
                print("Linked. California can send WhatsApp messages from this profile.", flush=True)
                return 0
            if state == "needs_link" and not announced:
                print("Scan the QR code: phone -> WhatsApp -> Settings -> Linked devices -> Link a device.", flush=True)
                announced = True
            time.sleep(1)
        print(f"Not linked after {args.timeout}s. Run it again to retry.", flush=True)
        return 1
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
