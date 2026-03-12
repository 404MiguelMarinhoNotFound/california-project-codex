import argparse
import json
import re
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


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
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")

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
