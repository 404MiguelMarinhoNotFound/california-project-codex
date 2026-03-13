import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.media_service import MediaService
from services.surfshark_service import SurfsharkService


DEFAULT_PLAYLIST_KEY = "70s 80s 90s hits"


def load_config() -> dict:
    return yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))


def pick_playlist_id(config: dict, playlist_key: str) -> tuple[str, str]:
    playlists = config.get("youtube_playlists", {})
    value = playlists.get(playlist_key)
    if isinstance(value, str) and value.strip():
        return playlist_key, value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return playlist_key, item.strip()
    raise ValueError(f"No playlist IDs configured for '{playlist_key}'.")


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
    parser = argparse.ArgumentParser(description="Run a fixed YouTube playlist E2E flow through the live TV services.")
    parser.add_argument(
        "--playlist-key",
        default=DEFAULT_PLAYLIST_KEY,
        help=f"YouTube playlist bucket to test. Defaults to '{DEFAULT_PLAYLIST_KEY}'.",
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
    playlist_key, playlist_id = pick_playlist_id(config, args.playlist_key)

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
    selected_route = surfshark.route_by_app.get("youtube")
    youtube_foreground = media.is_app_foreground("youtube")

    print(f"playlist_key={playlist_key}")
    print(f"playlist_id={playlist_id}")
    print(f"current_app_before={current_app}")
    print(f"selected_route={selected_route}")
    print(f"youtube_foreground={youtube_foreground}")

    if not youtube_foreground:
        vpn_result = surfshark.ensure_route(selected_route or "restart_autoconnect")
        print(
            "vpn_result="
            f"success:{vpn_result.success} "
            f"target:{vpn_result.target_country} "
            f"current:{vpn_result.current_country} "
            f"switched:{vpn_result.switched} "
            f"message:{vpn_result.message}"
        )
        stopped = media.force_stop_app("youtube")
        print(f"youtube_force_stopped={stopped}")
    else:
        print("vpn_result=skipped because youtube is already foreground")

    opened = media.youtube_playlist(playlist_id)
    print(f"playlist_opened={opened}")
    print(f"current_app_after={media.get_current_app()}")
    return 0 if opened else 1


if __name__ == "__main__":
    raise SystemExit(main())
