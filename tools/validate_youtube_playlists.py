import argparse
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import yaml


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _iter_playlist_entries(playlists: dict):
    for category, raw_value in (playlists or {}).items():
        if isinstance(raw_value, str):
            values = [raw_value]
        elif isinstance(raw_value, list):
            values = [item for item in raw_value if isinstance(item, str)]
        else:
            values = []

        for value in values:
            playlist_id = value.strip()
            if playlist_id:
                yield category, playlist_id


def _youtube_url(playlist_id: str) -> str:
    if playlist_id.startswith("RD"):
        query = urlencode({"v": playlist_id[2:], "list": playlist_id, "start_radio": "1"})
        return f"https://www.youtube.com/watch?{query}"
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _extract_title(page_html: str) -> str | None:
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
            if title:
                return title
    return None


def _fetch_title(url: str) -> str | None:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
    return _extract_title(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--id",
        dest="playlist_ids",
        action="append",
        default=[],
        help="Validate a specific playlist id, can be passed multiple times",
    )
    args = parser.parse_args()

    results = []
    entries = []
    if args.playlist_ids:
        entries = [("manual", playlist_id) for playlist_id in args.playlist_ids]
    else:
        config_path = Path(args.config)
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        playlists = config.get("youtube_playlists", {})
        entries = list(_iter_playlist_entries(playlists))

    for category, playlist_id in entries:
        url = _youtube_url(playlist_id)
        try:
            title = _fetch_title(url)
            status = "ok" if title else "no-title"
        except Exception as exc:
            title = None
            status = f"error: {exc}"
        results.append(
            {
                "category": category,
                "playlist_id": playlist_id,
                "url": url,
                "title": title,
                "status": status,
            }
        )

    print(json.dumps(results, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
