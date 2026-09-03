"""
Check that every saved YouTube playlist ID in config.yaml still exists.

    uv run python tools/validate_youtube_playlists.py
    uv run python tools/validate_youtube_playlists.py --json --strict-title
    uv run python tools/validate_youtube_playlists.py --id RDgQRtAnPL6HM

Exits 0 when everything resolves, 1 when any playlist is gone, 2 when a result
was inconclusive. That makes it usable as a gate; the old version always exited 0.

WHY THIS IS NOT JUST A TITLE SCRAPE
-----------------------------------
A deleted or private playlist still answers with HTTP 200 and a full HTML page.
The previous version looked only for "did a title-shaped string come back", found
the page chrome string "Visit source", and reported `ok`. Six genuinely dead IDs
in this repo's config passed that check. The authoritative signal is YouTube's
oembed endpoint, which answers 400/401/403/404 for content that is gone.

WHAT THIS CANNOT TELL YOU
-------------------------
* An `RD...` id is an auto-generated mix seeded from a video. Every check here
  runs against the SEED VIDEO, so `ok` means the seed still exists — not that the
  mix still sounds like the category. YouTube regenerates those mixes and they
  drift. No HTTP check can see that; a human has to listen. RD ids are used on
  purpose (see "Current Playlist Strategy" in AGENTS.md), so this is a limitation
  to live with, not a reason to drop them.
* Region and age gating are invisible from here. This asks as an anonymous
  crawler, not as the Mi Box's signed-in session.
* `--strict-title` is advisory and off by default, because for RD ids oembed
  returns the seed video's title: samba/RDc4XeTP11EI8 is "Grupo Revelacao -
  Deixa Acontecer", which shares no word with "samba". Turning it on by default
  would flag most healthy entries.
"""

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.youtube_http import NO_RESPONSE, fetch_json, fetch_text  # noqa: E402

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TITLE_MISMATCH = "title_mismatch"
STATUS_FETCH_ERROR = "fetch_error"

# oembed answers with one of these when the content is deleted, private or the
# id is malformed. A bogus video id gives 400 rather than 404, hence the spread.
UNAVAILABLE_CODES = {400, 401, 403, 404}

# Markers that appear in the HTML of a dead page that still returns 200.
UNAVAILABLE_MARKERS = (
    '"status":"ERROR"',
    "UNPLAYABLE",
    "This video isn't available",
    "This video is unavailable",
    "This playlist does not exist",
    "The playlist does not exist",
)

# Page chrome that the title regexes happily match on a dead page. "Visit source"
# is the exact string that made six dead playlists report ok.
CHROME_TITLES = {"visit source", "undefined", "youtube", "- youtube"}

# Words too generic to prove a title matches its category.
STOP_TOKENS = {"songs", "hits", "music", "the", "and", "mix", "playlist"}


# --------------------------------------------------------------------------
# Pure functions. No I/O, so tests/test_youtube_validator.py can drive them.
# --------------------------------------------------------------------------

def playlist_url(playlist_id: str) -> str:
    """
    The canonical page for one saved ID.

    The RD branch is load-bearing, including for oembed: `/playlist?list=RD...`
    returns 404 even for a perfectly healthy mix. An RD id must be asked about
    through its seed video's watch URL.
    """
    if playlist_id.startswith("RD"):
        query = urlencode({"v": playlist_id[2:], "list": playlist_id, "start_radio": "1"})
        return f"https://www.youtube.com/watch?{query}"
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def oembed_url(target_url: str) -> str:
    return "https://www.youtube.com/oembed?" + urlencode({"url": target_url, "format": "json"})


def extract_title(page_html: str) -> str | None:
    patterns = [
        r'<meta\s+property="og:title"\s+content="([^"]+)"',
        r'<meta\s+name="title"\s+content="([^"]+)"',
        r'"title":"([^"]+)"',
        r"<title>(.*?)</title>",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = html.unescape(match.group(1)).strip()
            title = re.sub(r"\s*-\s*YouTube.*$", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\s+", " ", title).strip()
            if title and title.lower() not in CHROME_TITLES:
                return title
    return None


def classify_oembed(status_code: int, body: str) -> tuple[str, str | None]:
    """The authoritative check. oembed is honest about deleted content."""
    if status_code in UNAVAILABLE_CODES:
        return STATUS_UNAVAILABLE, None
    if status_code != 200:
        return STATUS_FETCH_ERROR, None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return STATUS_FETCH_ERROR, None
    title = (payload.get("title") or "").strip() if isinstance(payload, dict) else ""
    if not title:
        return STATUS_FETCH_ERROR, None
    return STATUS_OK, title


def classify_page(status_code: int, body: str) -> tuple[str, str | None]:
    """
    HTML fallback, used only when oembed was inconclusive.

    Markers are checked BEFORE the title, because a dead page returns 200 with a
    scrapeable chrome title. Getting that order wrong is the original bug.
    """
    if status_code in UNAVAILABLE_CODES:
        return STATUS_UNAVAILABLE, None
    if status_code != 200:
        return STATUS_FETCH_ERROR, None
    if any(marker in body for marker in UNAVAILABLE_MARKERS):
        return STATUS_UNAVAILABLE, None
    title = extract_title(body)
    if not title:
        return STATUS_FETCH_ERROR, None
    return STATUS_OK, title


def title_matches_category(title: str, category: str) -> bool:
    """Advisory only. See the module docstring on why this is noisy for RD ids."""
    haystack = re.sub(r"[^a-z0-9\s]", " ", (title or "").lower())
    words = set(haystack.split())
    tokens = {t for t in re.split(r"\s+", (category or "").lower()) if len(t) > 2} - STOP_TOKENS
    if not tokens:
        return True
    return bool(tokens & words)


# --------------------------------------------------------------------------
# Network layer
# --------------------------------------------------------------------------

def check_playlist(category: str, playlist_id: str, strict_title: bool = False) -> dict:
    url = playlist_url(playlist_id)

    status, title = classify_oembed(*fetch_json(oembed_url(url)))

    # Only an inconclusive oembed falls through to the page. A confirmed
    # `unavailable` is never upgraded to ok by the weaker check.
    if status == STATUS_FETCH_ERROR:
        page_status, page_title = classify_page(*fetch_text(url))
        if page_status != STATUS_FETCH_ERROR:
            status, title = page_status, page_title

    if status == STATUS_OK and strict_title and not title_matches_category(title, category):
        status = STATUS_TITLE_MISMATCH

    return {
        "category": category,
        "playlist_id": playlist_id,
        "url": url,
        "title": title,
        "status": status,
    }


def iter_playlist_entries(playlists: dict):
    for category, raw_value in (playlists or {}).items():
        values = [raw_value] if isinstance(raw_value, str) else raw_value
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                yield category, value.strip()


def main() -> int:
    # Six real entries have emoji titles that blow up on a cp1252 console.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Path to config.yaml")
    parser.add_argument("--id", dest="playlist_ids", action="append", default=[],
                        help="Check one specific playlist ID, repeatable")
    parser.add_argument("--json", action="store_true", help="Emit the raw JSON array")
    parser.add_argument("--strict-title", action="store_true",
                        help="Also flag playlists whose title looks unrelated to the category")
    parser.add_argument("--fail-on-mismatch", action="store_true",
                        help="Count title_mismatch toward the exit code")
    parser.add_argument("--delay-ms", type=int, default=200, help="Pause between requests")
    args = parser.parse_args()

    if args.playlist_ids:
        entries = [("manual", playlist_id) for playlist_id in args.playlist_ids]
    else:
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        entries = list(iter_playlist_entries(config.get("youtube_playlists", {})))

    results = []
    for index, (category, playlist_id) in enumerate(entries):
        if index and args.delay_ms > 0:
            time.sleep(args.delay_ms / 1000)
        results.append(check_playlist(category, playlist_id, strict_title=args.strict_title))

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=True))
    else:
        width = max((len(r["category"]) for r in results), default=8)
        for result in results:
            flag = " " if result["status"] == STATUS_OK else "!"
            print(f"{flag} {result['category']:<{width}}  {result['playlist_id']:<36} "
                  f"{result['status']:<14} {result['title'] or ''}")

    counts = {status: sum(1 for r in results if r["status"] == status)
              for status in (STATUS_UNAVAILABLE, STATUS_FETCH_ERROR, STATUS_TITLE_MISMATCH)}
    print(f"\n{len(results)} checked, {counts[STATUS_UNAVAILABLE]} unavailable, "
          f"{counts[STATUS_FETCH_ERROR]} errors, {counts[STATUS_TITLE_MISMATCH]} title mismatches")

    if counts[STATUS_UNAVAILABLE] or (args.fail_on_mismatch and counts[STATUS_TITLE_MISMATCH]):
        return 1
    if counts[STATUS_FETCH_ERROR]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
