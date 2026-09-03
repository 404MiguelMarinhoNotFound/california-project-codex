import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.youtube_http import fetch_text



def _extract_initial_data(body: str) -> dict:
    patterns = [
        r"var ytInitialData = (\{.*?\});",
        r"ytInitialData = (\{.*?\});",
        r"window\[['\"]ytInitialData['\"]\] = (\{.*?\});",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.DOTALL)
        if match:
            return json.loads(match.group(1))
    raise ValueError("Could not find ytInitialData in search response")


def _text_from_runs(value) -> str:
    if not isinstance(value, dict):
        return ""
    if "simpleText" in value:
        return value["simpleText"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(run.get("text", "") for run in runs if isinstance(run, dict))
    return ""


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def search(query: str, limit: int = 10):
    url = (
        "https://www.youtube.com/results?"
        f"search_query={quote_plus(query)}&sp=EgIQAw%253D%253D"
    )
    status, body = fetch_text(url)
    if status != 200:
        raise RuntimeError(f"YouTube search returned HTTP {status}")

    data = _extract_initial_data(body)
    results = []
    seen = set()
    for node in _walk(data):
        renderer = node.get("playlistRenderer")
        if not isinstance(renderer, dict):
            continue

        playlist_id = renderer.get("playlistId")
        title = _text_from_runs(renderer.get("title"))
        if not playlist_id or not title or playlist_id in seen:
            continue

        seen.add(playlist_id)
        results.append(
            {
                "playlist_id": playlist_id,
                "title": title,
                "url": f"https://www.youtube.com/playlist?list={playlist_id}",
            }
        )
        if len(results) >= limit:
            break

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(search(" ".join(args.query), args.limit), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
