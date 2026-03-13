import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.media_service import MediaService
from services.surfshark_service import SurfsharkService


def load_config() -> dict:
    return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Run a Surfshark route on the TV.")
    parser.add_argument(
        "route",
        choices=["quick_connect", "restart_autoconnect"],
        help="Named Surfshark route to execute.",
    )
    parser.add_argument("--capture", action="store_true", help="Capture screenshots before launch and after route steps.")
    parser.add_argument("--capture-dir", help="Override the capture directory root.")
    parser.add_argument("--pause-ms", type=int, default=0, help="Pause after each Quick Connect key event.")
    parser.add_argument(
        "--no-force-restart",
        action="store_true",
        help="Skip the initial force-stop when debugging Quick Connect.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()
    media = MediaService(config)
    connected = media.connect()
    print(f"adb_connected={connected}")
    print(f"current_app={media.get_current_app()}")

    surfshark = SurfsharkService(config, media)
    result = surfshark.debug_route(
        route_name=args.route,
        capture=True if args.capture else None,
        capture_dir=args.capture_dir,
        pause_between_steps_s=max(0, args.pause_ms) / 1000,
        force_restart=not args.no_force_restart,
    )

    print(f"route={result['route']}")
    print(f"sequence_name={result['sequence_name']}")
    print(f"sequence={result['sequence']}")
    print(f"success={result['success']}")
    print(f"used_recovery={result['used_recovery']}")
    if result["captures"]:
        for path in result["captures"]:
            print(f"capture={path}")
    print(f"message={result['message']}")


if __name__ == "__main__":
    main()
