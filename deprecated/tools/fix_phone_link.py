"""
Bring Phone Link's calling link back, so California can place calls again.

Phone Link's Bluetooth calling link drops on its own; the Calls tab then says
"We weren't able to connect to your mobile device" and every call fails with
PHONE_NOT_CONNECTED. This does what that screen asks, from the laptop side:

  1. checks the Calls tab (nothing is touched if calling already works)
  2. presses Try again
  3. turns the laptop's Bluetooth off and on (Bluetooth headphones reconnect
     too), then presses Try again until the dial pad is back

If it still fails, toggle Bluetooth on the PHONE as well and run it again.

    uv run python tools/fix_phone_link.py            # repair if needed
    uv run python tools/fix_phone_link.py --check    # only report

Exit code 0 when calls work, 1 when they do not.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.phone_link import calls_state, repair_calling_link


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only report the state, change nothing")
    parser.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the phone after the toggle")
    args = parser.parse_args()

    if args.check:
        state = calls_state()
        print(f"Phone Link calls: {state or 'no answer from PowerShell'}")
        return 0 if state == "READY" else 1

    ready, steps = repair_calling_link(wait_s=args.wait)
    for step in steps:
        print(" -", step)
    print("Calls are working." if ready else "Calls are still down.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
