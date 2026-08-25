"""Smoke-test: open Stremio to Shrinking through the real play() flow.

Exercises StremioService.play() end-to-end so we verify the "open Stremio
directly, no VPN" path AND that the detail page is launched only once even
when multiple providers fail to start playback.

Usage (from the california/ directory):
    python tools/test_stremio_shrinking.py
    python tools/test_stremio_shrinking.py --title "Shrinking" --prep-app home --debug
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()

from services.media_service import MediaService
from services.stremio_service import StremioService
from services.surfshark_service import SurfsharkService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Shrinking")
    parser.add_argument("--media-type", choices=["series", "movie"], default="series")
    parser.add_argument(
        "--prep-app",
        choices=["youtube", "stremio", "surfshark", "spotify", "home"],
        help="Optional app to open first (simulate a cross-app launch).",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    media = MediaService(config)
    if not media.connect():
        print("TV not reachable over ADB.")
        return 1

    if args.prep_app:
        if args.prep_app == "home":
            media.go_home()
        else:
            media.launch_app(args.prep_app)
        time.sleep(1.5)

    surfshark = SurfsharkService(config, media)
    stremio = StremioService(config, media_service=media)

    print(f"vpn_routing_enabled={surfshark.enabled}")
    print(f"current_app_before={media.get_current_app()}")

    launch_calls = {"count": 0}
    original_launch_uri = stremio._launch_uri

    def counting_launch_uri(uri: str):
        launch_calls["count"] += 1
        print(f"launch_uri_call_{launch_calls['count']}: {uri}")
        return original_launch_uri(uri)

    stremio._launch_uri = counting_launch_uri

    t0 = time.monotonic()
    result = stremio.play(
        title=args.title,
        media_type=args.media_type,
        allow_unknown_source=True,
    )
    elapsed = time.monotonic() - t0

    print(f"launch_uri_call_count={launch_calls['count']}")
    print(f"play_result=success:{result.success} "
          f"requires_confirmation:{result.requires_confirmation} "
          f"played_source:{result.played_source} "
          f"target_mode:{result.target_mode} "
          f"message:{result.message}")
    print(f"elapsed_s={elapsed:.2f}")
    print(f"current_app_after={media.get_current_app()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
