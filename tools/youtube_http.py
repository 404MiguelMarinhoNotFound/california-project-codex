"""
Shared HTTP surface for the YouTube tools.

`validate_youtube_playlists.py`, `search_youtube_playlists.py` and
`search_youtube_videos.py` each carried a byte-identical copy of the spoofed
User-Agent and the Request/urlopen pair. This is that copy, once.

The fetch helpers return the status code instead of raising on it. That is the
whole point: for the validator a 404 is the ANSWER — the playlist is gone — not
an error to be swallowed into a generic "something went wrong" bucket.
"""

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

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
