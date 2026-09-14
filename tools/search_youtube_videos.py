import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The scrape itself lives in services/youtube_search.py since 2026-09-14,
# because the assistant now resolves "search for X and play it" the same way
# at runtime. This is the curation CLI over it.
from services.youtube_search import search_videos  # noqa: E402


def search(query: str, limit: int = 10):
    return [
        {
            "video_id": video.video_id,
            "title": video.title,
            "channel": video.channel,
            "url": video.url,
            "radio_playlist_id": video.radio_playlist_id,
        }
        for video in search_videos(query, limit=limit)
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(search(" ".join(args.query), args.limit), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
