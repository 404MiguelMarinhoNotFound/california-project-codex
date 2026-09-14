"""
Shared HTTP surface for the YouTube tools.

`validate_youtube_playlists.py`, `search_youtube_playlists.py` and
`search_youtube_videos.py` each carried a byte-identical copy of the spoofed
User-Agent and the Request/urlopen pair. This is that copy, once.

The fetch helpers return the status code instead of raising on it. That is the
whole point: for the validator a 404 is the ANSWER — the playlist is gone — not
an error to be swallowed into a generic "something went wrong" bucket.
"""

import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# One UA for the tools and the runtime resolver alike; the assistant scrapes
# the same results page at runtime now (services/youtube_search.py).
from services.youtube_search import HEADERS, USER_AGENT  # noqa: E402,F401

# Returned when the request never reached a server (DNS, timeout, TLS).
NO_RESPONSE = 0


def fetch_text(url: str, timeout: int = 20) -> tuple[int, str]:
    """
    GET `url` with the spoofed UA. Returns (status_code, body).

    An HTTP error status comes back as data, with whatever body the server sent.
    Only a transport-level failure yields NO_RESPONSE, and its message is
    returned as the body so callers can log it.
    """
    request = Request(url, headers=HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return exc.code, body
    except (URLError, TimeoutError, OSError) as exc:
        return NO_RESPONSE, str(exc)


def fetch_json(url: str, timeout: int = 20) -> tuple[int, str]:
    """
    Same as fetch_text, for endpoints that answer with JSON.

    The body stays a string on purpose — the caller classifies the response,
    and an unparseable body is itself a signal rather than an exception.
    """
    return fetch_text(url, timeout=timeout)
