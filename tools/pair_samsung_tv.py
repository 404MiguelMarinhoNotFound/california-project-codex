"""
Pair California with the Samsung TV, or re-pair after a revoked token.

The CEC wake needs the TV's WebSocket API, and that needs a pairing token. The
token is written once, on approval of an on-screen prompt, and survives until a
factory reset, a firmware update, or Master Miguel clearing devices in the TV's
Device Connection Manager.

That is why this tool exists rather than a docs paragraph: the recovery has to
happen with the TV ON and a person in front of it, and it is easy to get stuck
on the ordering. Wake-on-LAN needs no token, so this powers the TV on first and
only then asks for approval.

    uv run python tools/pair_samsung_tv.py

Prints the resolved IP and duid so a changed address or a replaced television is
obvious immediately, instead of surfacing later as "couldn't turn the TV on".
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.cec_wake import CecWaker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pair with the Samsung TV for CEC wake.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = yaml.safe_load(Path(args.config).read_text("utf-8"))
    waker = CecWaker(config)
    if not waker.available:
        print(f"CEC wake is disabled: {waker.unavailable_reason}")
        return 1

    print(f"TV MAC   : {waker.tv_mac}")
    print(f"TV duid  : {waker.tv_duid}")
    print("Powering the TV on (Wake-on-LAN needs no token)...")
    if not waker.power_on_tv():
        print("\nThe TV did not come up. Check it is plugged in, and that its")
        print("Network Standby / 'Turn on with mobile' setting is enabled.")
        return 1

    ip = waker.resolve_tv_ip()
    print(f"TV is up at {ip}")
    if ip != waker.tv_ip_hint:
        print(f"NOTE: this differs from media.cec_wake.tv_ip ({waker.tv_ip_hint!r}).")
        print("      That is fine, it self-heals, but a DHCP reservation is tidier.")

    token_file = Path(waker.token_path)
    if token_file.exists():
        print(f"\nRemoving the existing token at {token_file} to force a fresh prompt.")
        token_file.unlink()

    print("\n>>> ACCEPT THE PROMPT ON THE TV SCREEN <<<\n")
    result = waker._send_keys([waker.input_key])
    if not result:
        print(f"Pairing failed: {result.detail}")
        if result.needs_pairing:
            print("The prompt was declined or timed out. Run this again and accept it.")
        return 1

    if not token_file.exists():
        print("No token file was written. The prompt was probably not accepted.")
        return 1

    print(f"Paired. Token written to {token_file}")
    print("\nVerify the whole path with:")
    print("  uv run python -c \"import yaml; from services.media_service import "
          "MediaService; print(MediaService(yaml.safe_load("
          "open('config.yaml',encoding='utf-8'))).turn_on())\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
