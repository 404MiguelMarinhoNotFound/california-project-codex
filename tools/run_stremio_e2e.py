import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.media_service import MediaService
from services.stremio_service import StremioService
from services.surfshark_service import SurfsharkService


DEFAULT_TITLE = "Shrinking"
DEFAULT_MEDIA_TYPE = "series"


def load_config() -> dict:
    return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def stage_app(media: MediaService, app_name: str) -> bool:
    if app_name == "home":
        ok = media.go_home()
        time.sleep(1.0)
        return ok
    ok, _ = media.launch_app(app_name)
    if ok:
        time.sleep(1.5)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Run a fixed Stremio E2E flow through the live TV services.")
    parser.add_argument(
        "--title",
        default=DEFAULT_TITLE,
        help=f"Title to open on Stremio. Defaults to '{DEFAULT_TITLE}'.",
    )
    parser.add_argument(
        "--media-type",
        choices=["series", "movie"],
        default=DEFAULT_MEDIA_TYPE,
        help=f"Media type hint for Stremio. Defaults to '{DEFAULT_MEDIA_TYPE}'.",
    )
    parser.add_argument(
        "--prep-app",
        choices=["youtube", "stremio", "surfshark", "spotify", "home"],
        help="Optional app to open before the test so you can simulate same-app or cross-app routing.",
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
    if not connected:
        print("TV connection failed.")
        return 1

    if args.prep_app:
        staged = stage_app(media, args.prep_app)
        print(f"prep_app={args.prep_app}")
        print(f"prep_success={staged}")
        if not staged:
            print("Prep app launch failed.")
            return 1

    current_app = media.get_current_app()
    surfshark = SurfsharkService(config, media)
    stremio = StremioService(config, media_service=media)
    selected_route = surfshark.route_by_app.get("stremio")
    stremio_foreground = media.is_app_foreground("stremio")

    print(f"title={args.title}")
    print(f"media_type={args.media_type}")
    print(f"current_app_before={current_app}")
    print(f"selected_route={selected_route}")
    print(f"stremio_foreground={stremio_foreground}")

    if not stremio_foreground:
        vpn_result = surfshark.ensure_route(selected_route or "quick_connect")
        print(
            "vpn_result="
            f"success:{vpn_result.success} "
            f"target:{vpn_result.target_country} "
            f"current:{vpn_result.current_country} "
            f"switched:{vpn_result.switched} "
            f"message:{vpn_result.message}"
        )
    else:
        print("vpn_result=skipped because stremio is already foreground")

    result = stremio.play(
        title=args.title,
        media_type=args.media_type,
    )
    print(
        "stremio_result="
        f"success:{result.success} "
        f"requires_confirmation:{result.requires_confirmation} "
        f"played_source:{result.played_source} "
        f"message:{result.message}"
    )
    print(f"current_app_after={media.get_current_app()}")
    return 0 if result.success or result.requires_confirmation else 1


if __name__ == "__main__":
    raise SystemExit(main())
