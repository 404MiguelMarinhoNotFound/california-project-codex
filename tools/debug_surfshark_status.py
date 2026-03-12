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
    parser = argparse.ArgumentParser(description="Inspect Surfshark routing status on the TV.")
    parser.add_argument("--ensure", help="Ensure a target country after printing current status.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached vpn_state.json.")
    parser.add_argument("--force-ensure", action="store_true", help="Bypass matching cache and always try the switch path.")
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
    status = surfshark.get_status(force_refresh=args.force_refresh)
    print(
        "status="
        f"connected:{status.connected} "
        f"country:{status.country} "
        f"source:{status.source} "
        f"cache_source:{status.cache_source} "
        f"updated_at:{status.updated_at}"
    )

    if args.ensure:
        result = surfshark.ensure_country(args.ensure, force_switch=args.force_ensure)
        print(
            "ensure_result="
            f"success:{result.success} "
            f"target:{result.target_country} "
            f"current:{result.current_country} "
            f"switched:{result.switched} "
            f"skipped:{result.skipped} "
            f"message:{result.message}"
        )


if __name__ == "__main__":
    main()
