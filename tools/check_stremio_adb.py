"""Layer-by-layer health check for the Stremio path.

Read-only by default: it connects, inspects and reports, but never starts
playback and never writes watch_state.json unless you ask for it. The point is
to tell "the box is unreachable" from "the box is fine and Stremio's deep link
handler is gone" from "the deep link fired but nothing plays" -- those need
completely different fixes, and a single play attempt cannot distinguish them.

    uv run python tools/check_stremio_adb.py
    uv run python tools/check_stremio_adb.py --title Shrinking --sync
    uv run python tools/check_stremio_adb.py --title Shrinking --launch --debug

Exit code is the number of failed checks, so it can gate a script.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.media_service import MediaService
from services.stremio_service import StremioService

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

_ICON = {PASS: "[ok  ]", FAIL: "[FAIL]", WARN: "[warn]", SKIP: "[skip]"}


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, section: str, name: str, status: str, detail: str = "") -> str:
        self.rows.append((section, name, status, detail))
        print(f"  {_ICON[status]} {name}" + (f" -- {detail}" if detail else ""))
        return status

    @property
    def failures(self) -> int:
        return sum(1 for _, _, status, _ in self.rows if status == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for _, _, status, _ in self.rows if status == WARN)

    def as_json(self) -> str:
        return json.dumps(
            [
                {"section": s, "check": n, "status": st, "detail": d}
                for s, n, st, d in self.rows
            ],
            indent=2,
        )


def section(title: str):
    print(f"\n{title}")
    print("-" * len(title))


def check_local(report: Report, config: dict) -> None:
    section("1. Local configuration")

    media_cfg = config.get("media", {}) or {}
    if media_cfg.get("enabled"):
        report.add("local", "media.enabled", PASS)
    else:
        report.add(
            "local",
            "media.enabled",
            FAIL,
            "media.enabled is false -- control_tv is not offered to the LLM at all",
        )

    adb_path = media_cfg.get("adb_path", "adb")
    if os.path.sep in adb_path or (len(adb_path) > 1 and adb_path[1] == ":"):
        if Path(adb_path).exists():
            report.add("local", "adb binary", PASS, adb_path)
        else:
            report.add("local", "adb binary", FAIL, f"{adb_path} does not exist")
    else:
        from shutil import which

        resolved = which(adb_path)
        if resolved:
            report.add("local", "adb binary", PASS, f"{adb_path} -> {resolved}")
        else:
            report.add("local", "adb binary", FAIL, f"'{adb_path}' not on PATH")

    target = f"{media_cfg.get('mibox_ip')}:{media_cfg.get('adb_port')}"
    report.add("local", "adb target", PASS, target)

    if media_cfg.get("apps", {}).get("stremio"):
        report.add("local", "stremio package configured", PASS, media_cfg["apps"]["stremio"])
    else:
        report.add("local", "stremio package configured", FAIL, "media.apps.stremio is missing")

    if config.get("media", {}).get("vpn_routing_enabled"):
        report.add(
            "local",
            "vpn routing",
            WARN,
            "enabled -- a Surfshark preflight runs before Stremio and can mask a failure",
        )
    else:
        report.add("local", "vpn routing", PASS, "disabled, Stremio dispatches directly")


def check_credentials(report: Report, stremio: StremioService, config: dict) -> None:
    section("2. Credentials")

    if stremio.can_sync():
        report.add("creds", "STREMIO_EMAIL / STREMIO_PASSWORD", PASS, stremio.email or "")
    else:
        report.add(
            "creds",
            "STREMIO_EMAIL / STREMIO_PASSWORD",
            FAIL,
            "missing -- library sync, resume and progress lookup are all dead without these",
        )

    tmdb = config.get("tmdb", {}) or {}
    if os.getenv("TMDB_API_KEY") or os.getenv("TMDB_READ_ACCESS_TOKEN") or tmdb.get("api_key") or tmdb.get("read_access_token"):
        report.add("creds", "TMDB credentials", PASS)
    else:
        report.add(
            "creds",
            "TMDB credentials",
            WARN,
            "missing -- titles already in watch_state.json still work, new ones cannot resolve",
        )


def check_stremio_api(report: Report, stremio: StremioService, do_sync: bool) -> None:
    section("3. Stremio API and watch state")

    if not stremio.can_sync():
        report.add("api", "Stremio login", SKIP, "no credentials")
    else:
        try:
            key = stremio._authenticate()
            report.add("api", "Stremio login", PASS, f"authKey {key[:8]}...")
        except Exception as exc:
            report.add("api", "Stremio login", FAIL, f"{type(exc).__name__}: {exc}")

    if do_sync:
        if not stremio.can_sync():
            report.add("api", "library sync", SKIP, "no credentials")
        else:
            try:
                synced = stremio.sync_library()
                report.add(
                    "api",
                    "library sync",
                    PASS if synced else FAIL,
                    "watch_state.json rewritten" if synced else "sync_library() returned False",
                )
            except Exception as exc:
                report.add("api", "library sync", FAIL, f"{type(exc).__name__}: {exc}")
    else:
        report.add("api", "library sync", SKIP, "pass --sync to refresh watch_state.json")

    state = stremio._load_watch_state()
    if state:
        sample = ", ".join(sorted(e.get("title", k) for k, e in state.items())[:5])
        report.add("api", "watch_state.json", PASS, f"{len(state)} entries: {sample}")
    else:
        report.add(
            "api",
            "watch_state.json",
            WARN,
            "empty -- every request will fall through to TMDB and no resume target exists",
        )


def check_adb(report: Report, media: MediaService, config: dict) -> bool:
    section("4. ADB transport and the box")

    connected = media.connect()
    if not connected:
        report.add(
            "adb",
            "adb connect",
            FAIL,
            "box unreachable. In standby its Wi-Fi radio is down and ICMP replies "
            "are firmware offload, not the OS -- wake it before retrying.",
        )
        return False
    report.add("adb", "adb connect", PASS, media.target)

    awake = media.is_awake()
    if awake is True:
        report.add("adb", "power state", PASS, "awake")
    elif awake is False:
        report.add("adb", "power state", WARN, "asleep but reachable -- KEYCODE_WAKEUP would fix it")
    else:
        report.add("adb", "power state", FAIL, "unreachable while connected -- dumpsys power gave nothing")

    ok, out = media._adb("shell getprop ro.product.model")
    report.add("adb", "device identity", PASS if ok else FAIL, out or "no response")

    package = media.apps.get("stremio", "com.stremio.one")
    ok, out = media._adb(f"shell pm list packages {package}")
    if ok and package in (out or ""):
        report.add("adb", "Stremio installed", PASS, package)
    else:
        report.add("adb", "Stremio installed", FAIL, f"{package} not present on the box")

    # The whole playback path is one VIEW intent on a stremio:/// URI. If no
    # activity claims that scheme, every deep link silently no-ops and the box
    # just sits on whatever was already on screen.
    ok, out = media._adb(
        'shell cmd package resolve-activity -a android.intent.action.VIEW '
        '-d "stremio:///detail/series/tt0903747/tt0903747"'
    )
    if ok and package in (out or ""):
        report.add("adb", "stremio:// deep link handler", PASS, "claimed by " + package)
    elif ok and out:
        report.add(
            "adb",
            "stremio:// deep link handler",
            FAIL,
            f"resolved to something else: {out.splitlines()[0][:80]}",
        )
    else:
        report.add("adb", "stremio:// deep link handler", FAIL, out or "no resolver output")

    report.add("adb", "foreground app", PASS, media.get_current_app())

    ok, out = media._adb("shell dumpsys media_session")
    if ok:
        playing = "state=3" in (out or "").lower()
        report.add(
            "adb",
            "media_session readable",
            PASS,
            "something is playing (state=3)" if playing else "readable, nothing playing",
        )
    else:
        report.add(
            "adb",
            "media_session readable",
            FAIL,
            "playback verification is blind without this -- every play would report the fallback line",
        )

    # Source-list scraping (_extract_candidates_from_ui_xml) is entirely built
    # on this dump. It fails on some builds while everything else works.
    xml = media.dump_ui_hierarchy()
    if xml and "<hierarchy" in xml:
        report.add("adb", "uiautomator dump", PASS, f"{len(xml)} bytes")
    else:
        report.add(
            "adb",
            "uiautomator dump",
            WARN,
            "no hierarchy -- provider preference (comet/mediafusion/torrent) cannot be scraped, "
            "playback falls back to blind OK presses",
        )
    return True


def check_resolution(
    report: Report, stremio: StremioService, title: str, media_type: str | None
) -> None:
    section(f"5. Title resolution and deep link for '{title}'")

    try:
        imdb_id, resolved_type = stremio.resolve_imdb_id(title, media_type)
        report.add("resolve", "IMDb id", PASS, f"{imdb_id} ({resolved_type})")
    except Exception as exc:
        report.add("resolve", "IMDb id", FAIL, f"{type(exc).__name__}: {exc}")
        return

    progress = stremio.get_progress(title)
    season = episode = None
    if progress:
        season, episode = progress.get("season"), progress.get("episode")
        report.add(
            "resolve",
            "tracked progress",
            PASS,
            f"S{season}E{episode} finished_last={progress.get('finished_last')} "
            f"source={progress.get('last_successful_source')}",
        )
    else:
        report.add(
            "resolve",
            "tracked progress",
            WARN,
            "none -- a plain request opens the series detail page instead of resuming",
        )

    uri, mode = stremio.build_deep_link(imdb_id, resolved_type, season, episode)
    report.add("resolve", "deep link (not launched)", PASS, f"{mode}: {uri}")


def check_launch(report: Report, stremio: StremioService, media: MediaService, title: str, media_type: str | None) -> None:
    section(f"6. Live playback of '{title}' (--launch)")

    started = time.monotonic()
    result = stremio.play(title=title, media_type=media_type)
    elapsed = time.monotonic() - started

    detail = (
        f"success={result.success} confirm={result.requires_confirmation} "
        f"source={result.played_source} mode={result.target_mode} "
        f"in {elapsed:.1f}s -- {result.message or 'no message'}"
    )
    if result.success:
        report.add("launch", "stremio.play", PASS, detail)
    elif result.requires_confirmation:
        report.add("launch", "stremio.play", WARN, detail)
    else:
        report.add("launch", "stremio.play", FAIL, detail)

    report.add("launch", "foreground after", PASS, media.get_current_app())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", default="Shrinking", help="Title to resolve (and play with --launch).")
    parser.add_argument("--media-type", choices=["series", "movie"], default=None)
    parser.add_argument("--sync", action="store_true", help="Refresh watch_state.json from the Stremio API.")
    parser.add_argument("--launch", action="store_true", help="Actually start playback. Not read-only.")
    parser.add_argument("--skip-adb", action="store_true", help="Offline checks only, no TV needed.")
    parser.add_argument("--json", action="store_true", help="Print a JSON summary at the end.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    report = Report()

    check_local(report, config)

    media = None if args.skip_adb else MediaService(config)
    stremio = StremioService(config, media_service=media)

    check_credentials(report, stremio, config)
    check_stremio_api(report, stremio, args.sync)

    if media is None:
        section("4. ADB transport and the box")
        adb_ok = report.add("adb", "all ADB checks", SKIP, "--skip-adb") == PASS
    else:
        adb_ok = check_adb(report, media, config)

    check_resolution(report, stremio, args.title, args.media_type)

    if args.launch:
        if not adb_ok:
            section(f"6. Live playback of '{args.title}' (--launch)")
            report.add("launch", "stremio.play", SKIP, "the box is not reachable")
        else:
            check_launch(report, stremio, media, args.title, args.media_type)

    print(
        f"\n{len(report.rows)} checks: {report.failures} failed, "
        f"{report.warnings} warnings."
    )
    if args.json:
        print(report.as_json())
    return report.failures


if __name__ == "__main__":
    raise SystemExit(main())
