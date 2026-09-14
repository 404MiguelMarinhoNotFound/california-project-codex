"""
Turn a spoken YouTube query into one video id, so the box can be told to PLAY
it rather than shown a results page.

Why this exists. The results deep link (`/results?search_query=`) stops at the
results page and never plays on its own -- measured 2026-09-14, 20s on the page
with the media session untouched -- and pressing into it blind is not
reliable: the top result is sometimes a video (one OK plays it), sometimes a
playlist (OK opens the playlist page, a second OK is needed), sometimes a
channel or a Mix, and the YouTube TV app is a Cobalt web view that gives
`uiautomator` nothing to read. A second OK is also play/pause inside the
player, so a blind retry paused the very thing the first press started.

`watch?v=<id>` has none of that: it plays directly, publishes the title in the
session ~2s later, and restarts from zero even when that video is already on.
So the search is resolved HERE, over the public results page -- the same
`ytInitialData` scrape the curation tools have used since day one -- and the
box gets a video id. No API key, no account, no quota.

Order is YouTube's own search ranking for the web, which is close to but not
identical with the TV app's; "the first thing" means the first VIDEO result,
so a playlist or channel sitting at the top is skipped rather than opened.

The web page is the single point of failure and it fails for reasons the box
cannot fix (no internet on the laptop, a consent interstitial, a markup
change), so `top_video` never raises: None means "could not resolve", and the
dispatcher falls back to opening the results page the old way.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

_INITIAL_DATA_PATTERNS = (
    r"var ytInitialData = (\{.*?\});",
    r"ytInitialData = (\{.*?\});",
    r"window\[['\"]ytInitialData['\"]\] = (\{.*?\});",
)


@dataclass(frozen=True)
class VideoResult:
    video_id: str
    title: str
    channel: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def radio_playlist_id(self) -> str:
        return f"RD{self.video_id}"


def results_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def fetch_results_page(query: str, timeout_s: float = 5.0) -> str:
    """The raw HTML of the web results page. Raises on transport or HTTP error."""
    request = Request(results_url(query), headers=HEADERS)
    with urlopen(request, timeout=timeout_s) as response:
        if response.status != 200:
            raise HTTPError(request.full_url, response.status, "bad status", response.headers, None)
        return response.read().decode("utf-8", errors="replace")


def extract_initial_data(body: str) -> dict:
    for pattern in _INITIAL_DATA_PATTERNS:
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


def parse_videos(body: str, limit: int = 10) -> list[VideoResult]:
    """
    The video results on a results page, in page order.

    Only `videoRenderer` nodes count. Playlists, channels, Mixes and Shorts
    shelves are different renderers and are skipped -- see the module doc for
    why "the first thing" has to be a video.
    """
    data = extract_initial_data(body)
    results: list[VideoResult] = []
    seen: set[str] = set()
    for node in _walk(data):
        renderer = node.get("videoRenderer")
        if not isinstance(renderer, dict):
            continue
        video_id = renderer.get("videoId")
        title = _text_from_runs(renderer.get("title"))
        channel = _text_from_runs(renderer.get("ownerText"))
        if not video_id or not title or video_id in seen:
            continue
        seen.add(video_id)
        results.append(VideoResult(video_id, title, channel))
        if len(results) >= limit:
            break
    return results


def search_videos(query: str, limit: int = 10, timeout_s: float = 20.0) -> list[VideoResult]:
    """Fetch and parse. Raises, for the curation tool that wants the reason."""
    return parse_videos(fetch_results_page(query, timeout_s=timeout_s), limit=limit)


def top_video(query: str, timeout_s: float = 5.0) -> VideoResult | None:
    """
    The first video result, or None. Never raises: a dispatcher that is
    holding the microphone open needs an answer, not a traceback, and the
    fallback (open the results page) is decided by the caller.
    """
    query = (query or "").strip()
    if not query:
        return None
    try:
        results = search_videos(query, limit=1, timeout_s=timeout_s)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("YouTube search resolve failed for %r: %s", query, exc)
        return None
    if not results:
        log.info("YouTube search resolve: no video results for %r", query)
        return None
    return results[0]


_BRACKETED_RE = re.compile(r"\s*[\[(][^\])]*[\])]")
_SPLIT_RE = re.compile(r"\s*[|•]\s*")


def speakable_title(title: str, max_chars: int = 70) -> str:
    """
    A YouTube title as she should say it, not as it was typed.

    Uploaders stuff titles with SEO -- "The Best of J. Cole | DJ set | Greatest
    Hits - sounds by Winter", "AudioFood : J. Cole Edition [J. COLE MIX 2024] |
    BEST J. COLE SONGS". Read out whole, that is a paragraph. Keep the segment
    before the first pipe, drop bracketed tags, and cap the length; the full
    title is still what the box publishes if he asks what is on.
    """
    text = (title or "").strip()
    if not text:
        return ""
    head = _SPLIT_RE.split(text, maxsplit=1)[0]
    head = _BRACKETED_RE.sub("", head).strip(" -:,")
    head = re.sub(r"\s+", " ", head)
    if not head:
        head = text
    if len(head) > max_chars:
        cut = head[:max_chars].rsplit(" ", 1)[0]
        head = (cut or head[:max_chars]).rstrip(" -:,")
    return head
