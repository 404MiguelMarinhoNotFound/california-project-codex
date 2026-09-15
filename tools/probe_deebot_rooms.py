"""
Reads the vacuum's live status and room list and prints the `deebot.rooms`
block config.yaml needs.

Room ids shift after a remap, so run this whenever rooms are renamed, split,
merged, or the map is rebuilt, and paste the printed block over the one in
config.yaml (keeping any aliases you added). `--check` compares the live ids
against config.yaml and exits nonzero on drift, so it can gate a commit.

Rooms still named "Default" on the robot are listed but never emitted: name
them in the ECOVACS HOME app (Map Management > Edit Map > Area Type) first.

  uv sync --extra default
  uv run python tools/probe_deebot_rooms.py
  uv run python tools/probe_deebot_rooms.py --check
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.deebot_service import DeebotService  # noqa: E402


def _yaml_block(live: dict[str, int], existing: dict) -> str:
    lines = ["deebot:", "  rooms:"]
    for name, room_id in sorted(live.items(), key=lambda kv: kv[1]):
        key = name.lower()
        aliases = (existing.get(key) or {}).get("aliases") or [f"the {key}"]
        alias_text = ", ".join(f'"{a}"' for a in aliases)
        lines.append(f"    {key}:")
        lines.append(f"      id: {room_id}")
        lines.append(f"      aliases: [{alias_text}]")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Exit 1 if config.yaml ids differ from the robot")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    config.setdefault("deebot", {})["enabled"] = True

    service = DeebotService(config)
    if not service.enabled:
        print("Vacuum control is not available. Check ECOVACS_EMAIL / ECOVACS_PASSWORD")
        print("in .env and that deebot-client is installed: uv sync --extra default")
        return 1

    status = service.status()
    if not status.available:
        print("The vacuum did not answer. Is it powered on and online?")
        return 1
    print(f"state: {status.state}  battery: {status.battery}%"
          + (f"  error: {status.error}" if status.error else ""))

    live = service.sync_rooms()
    if not live:
        print("No named rooms on the robot yet. Name them in the ECOVACS HOME app first.")
        return 1

    print("\nLive rooms (id -> name):")
    for name, room_id in sorted(live.items(), key=lambda kv: kv[1]):
        print(f"  {room_id:>2}  {name}")

    existing = (config.get("deebot") or {}).get("rooms") or {}
    if args.check:
        drift = service.check_room_drift()
        missing = [n.lower() for n in live if n.lower() not in existing]
        if not drift and not missing:
            print("\nconfig.yaml matches the robot.")
            return 0
        if drift:
            print(f"\nStale ids in config.yaml: {', '.join(drift)}")
        if missing:
            print(f"Rooms on the robot but not in config.yaml: {', '.join(missing)}")
        return 1

    print("\nPaste into config.yaml (keeps your existing aliases):\n")
    print(_yaml_block(live, existing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
