"""
The live UI: a small web page on the home Wi-Fi that shows what she is doing
right now, streamed from `core/event_bus.py` over Server-Sent Events.

Why SSE on the standard library: the page only watches (v1 is read-only), so a
one-way stream is the whole requirement. The browser's EventSource reconnects
by itself and resumes from `Last-Event-ID`, and `http.server` needs nothing
added to uv.lock, on the laptop or the Pi.

The token is in the PATH, not a query string: `http://<host>:8765/<token>/`.
By default the token is the word `california` (`http://<host>:8765/california/`),
easy to type and to read off a phone; it is a name, not a secret, so keep the
port on a trusted LAN or set a longer `CALIFORNIA_UI_TOKEN`.
That way the page's relative `app.js`, `app.css` and `events` URLs carry it
with no JavaScript, and anything without it gets a 404. That includes the bare
`/`, so a scan of the LAN learns nothing. The page shows what he said, phone
call transcripts and log lines, so it is never served without the token.
`CALIFORNIA_UI_TOKEN` in .env (or `ui.token`) overrides it.

Past calls are served from `logs/calls.jsonl` at `calls` (a list) and
`calls/<call_id>` (one, with its full timed transcript), behind the same token.

Same contract as every service: never raises at construction, `start()`
returns False and logs once when it cannot bind, and California carries on.
"""

import hmac
import json
import logging
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ui")

# The only files ever served. No path from the URL is joined onto a directory:
# the lookup is an exact dict match, so "vendor/three.min.js" needs no joining
# logic and "../" can never reach anything. An optional third element is the
# Cache-Control for that file; the default is no-store.
_NO_STORE = "no-store"
_IMMUTABLE = "public, max-age=86400, immutable"
_STATIC = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "halo.js": ("halo.js", "text/javascript; charset=utf-8"),
    # ~600 KB the stdlib server cannot gzip, and versioned by ui/vendor/README.md,
    # so a phone downloads it once a day rather than on every page load.
    "vendor/three.min.js": ("vendor/three.min.js", "text/javascript; charset=utf-8", _IMMUTABLE),
    # Geist, self-hosted (the CSP blocks font CDNs). The version is in the name,
    # so immutable is safe while app.css stays no-store; see ui/fonts/README.md.
    "fonts/geist-1.7.2.woff2": ("fonts/geist-1.7.2.woff2", "font/woff2", _IMMUTABLE),
    "fonts/geist-mono-1.7.2.woff2": ("fonts/geist-mono-1.7.2.woff2", "font/woff2", _IMMUTABLE),
}

DEFAULT_TOKEN = "california"
_HEARTBEAT_S = 15.0
_CALLS_MAX = 200
_SECURITY_HEADERS = (
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy",
     "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
     "style-src 'self'; script-src 'self'; worker-src 'none'; frame-ancestors 'none'"),
)


def _lan_address() -> str:
    """This machine's address on the LAN, for the URL in the boot log.

    A UDP connect picks the outbound interface without sending anything.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


class LiveUI:
    def __init__(self, config: dict, bus, ui_dir: str = UI_DIR):
        cfg = (config or {}).get("ui") or {}
        self.enabled = cfg.get("enabled", False) is not False
        self.host = str(cfg.get("host", "0.0.0.0"))
        self.port = int(cfg.get("port", 8765))
        # A plain word by default (2026-10-15, his call): a uuid-like path was hard
        # to read and type. Letters only, so WhatsApp still links the URL.
        self.token = os.environ.get("CALIFORNIA_UI_TOKEN") or str(cfg.get("token") or "") or DEFAULT_TOKEN
        log_cfg = (config or {}).get("logging") or {}
        phone_cfg = (config or {}).get("phone") or {}
        self.calls_path = str(phone_cfg.get("log_path") or os.path.join(log_cfg.get("dir", "logs"), "calls.jsonl"))
        self.bus = bus
        self.ui_dir = ui_dir
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._hello = _hello(config)

    @property
    def url(self) -> str:
        host = _lan_address() if self.host in ("0.0.0.0", "") else self.host
        port = self._server.server_address[1] if self._server else self.port
        return f"http://{host}:{port}/{self.token}/"

    def start(self) -> bool:
        if not self.enabled:
            return False
        try:
            server = ThreadingHTTPServer((self.host, self.port), _make_handler(self))
        except OSError as exc:
            logger.warning("Live UI disabled: cannot listen on %s:%s (%s)", self.host, self.port, exc)
            return False
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.5},
            name="live-ui", daemon=True,
        )
        self._thread.start()
        logger.info("Live UI: %s", self.url)
        return True

    def stop(self) -> None:
        self._stopping.set()
        server = self._server
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logger.debug("Live UI stop failed", exc_info=True)
        self._server = None

    def token_ok(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self.token.encode())


def read_calls(path: str, limit: int = _CALLS_MAX) -> list[dict]:
    """Logged calls, newest first. A missing file or a bad line is skipped, never raised."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
    except OSError:
        return []
    records = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def call_summary(record: dict) -> dict:
    """What the Calls list shows for one call: no transcript, no brief."""
    meta = record.get("meta") or {}
    return {
        "id": meta.get("call_id") or str(record.get("ts") or ""),
        "ts": record.get("ts"),
        "to": record.get("to"),
        "kind": record.get("kind"),
        "status": record.get("status"),
        "duration_s": record.get("duration_s"),
        "talk_s": meta.get("talk_s"),
        "turn": meta.get("turn"),
        "summary": (record.get("outcome") or {}).get("summary"),
        "has_transcript": bool(record.get("transcript")),
    }


def _hello(config: dict) -> dict:
    """Per-connection context the page needs to draw itself."""
    ww = (config or {}).get("wake_word") or {}
    logging_cfg = (config or {}).get("logging") or {}
    ui_cfg = (config or {}).get("ui") or {}
    threshold = ww.get("threshold")
    return {
        "threshold": threshold,
        "barge_in_threshold": ww.get("barge_in_threshold", threshold),
        "transcripts": logging_cfg.get("include_transcripts", True) is not False,
        "started": time.time(),
        # The halo (ui/halo.js). voice_offset_ms delays the page's animation of
        # a clip to match the laptop speaker's output latency.
        "halo": ui_cfg.get("halo", True) is not False,
        "voice_offset_ms": _int(ui_cfg.get("voice_offset_ms"), 120),
        "mood_hold_ms": _int(ui_cfg.get("mood_hold_ms"), 6000),
        "palette": str(ui_cfg.get("palette") or "dusk"),
    }


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _make_handler(ui: LiveUI):
    class Handler(BaseHTTPRequestHandler):
        server_version = "California"
        sys_version = ""

        def log_message(self, fmt, *args):  # the stdlib logs every request to stderr
            pass

        def _route(self) -> str | None:
            """The path after the token, or None when the token is wrong."""
            path = self.path.split("?", 1)[0]
            parts = path.lstrip("/").split("/", 1)
            if len(parts) < 2 or not ui.token_ok(parts[0]):
                return None
            return parts[1]

        def _not_found(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            rest = self._route()
            if rest is None:
                return self._not_found()
            if rest == "events":
                return self._events()
            if rest == "calls" or rest.startswith("calls/"):
                return self._calls(rest)
            entry = _STATIC.get(rest)
            if entry is None:
                return self._not_found()
            name, ctype = entry[0], entry[1]
            cache = entry[2] if len(entry) > 2 else _NO_STORE
            try:
                with open(os.path.join(ui.ui_dir, name), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._not_found()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in _SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _calls(self, rest: str):
            records = read_calls(ui.calls_path)
            if rest == "calls":
                payload = [call_summary(r) for r in records]
            else:
                wanted = rest.split("/", 1)[1]
                payload = next((r for r in records if call_summary(r)["id"] == wanted), None)
                if payload is None:
                    return self._not_found()
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in _SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _events(self):
            last_id = None
            header = self.headers.get("Last-Event-ID")
            if header and header.isdigit():
                last_id = int(header)
            backlog, q = ui.bus.subscribe(last_id)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self._send(None, "hello", ui._hello)
                for event in backlog:
                    self._send_event(event)
                # The end of the backlog. Everything before it already
                # happened; the page draws only what is still going on (a
                # tool still running, a read-back still waiting) instead of
                # replaying every finished tool call as if it were new.
                self._send(None, "replayed", {"ts": time.time(), "count": len(backlog)})
                self.wfile.flush()
                while not ui._stopping.is_set():
                    try:
                        event = q.get(timeout=_HEARTBEAT_S)
                    except queue.Empty:
                        # A real event, not an SSE comment: the page counts
                        # it, so a long tool call (no wake scores while she
                        # waits on a 50s CEC wake) never reads as a stall.
                        self._send(None, "ping", {"ts": time.time()})
                        self.wfile.flush()
                        continue
                    self._send_event(event)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ui.bus.unsubscribe(q)

        def _send_event(self, event: dict):
            self._send(event["id"], event["kind"], {"ts": event["ts"], **event["data"]})

        def _send(self, event_id, kind: str, data: dict):
            payload = json.dumps(data, ensure_ascii=False, default=str)
            lines = []
            if event_id is not None:
                lines.append(f"id: {event_id}")
            lines.append(f"event: {kind}")
            lines.append(f"data: {payload}")
            self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))

    return Handler
