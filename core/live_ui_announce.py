"""
Tell Master Miguel where the live page is, once per run.

When California boots with the live UI up, a daemon thread (1) sends the page's
URL to his own WhatsApp number and (2) opens it in Firefox on this laptop unless
a Firefox tab already shows it. Neither step matters to the boot, so both run in
the background and every failure is a log line, never an exception.

The number is NOT in config.yaml: the repository is public, so it comes from
`CALIFORNIA_UI_NOTIFY_NUMBER` in .env, the same reason contacts.vcf is gitignored.

"Already open" is read from Firefox's own session file,
`<profile>/sessionstore-backups/recovery.jsonlz4`, which lists every open tab in
every window (the window title only shows the active tab). It is Mozilla's LZ4
variant, decoded below with no new dependency. Firefox rewrites it about every
15s, and keeps it after a quit, so it only counts while firefox.exe is running.
With no `CALIFORNIA_UI_TOKEN` the URL is new each run and never matches an old
tab; that old tab is dead anyway (its token 404s), so a fresh one is right.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
_MOZLZ4_MAGIC = b"mozLz40\0"
_BUSY_RETRIES = 3
_BUSY_WAIT_S = 20.0


# ----------------------------------------------------------------- Firefox


def lz4_block_decompress(src: bytes, size: int) -> bytes:
    """Decode one raw LZ4 block (the format inside a .jsonlz4 file)."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                b = src[i]
                i += 1
                literal += b
                if b != 255:
                    break
        out += src[i:i + literal]
        i += literal
        if i >= n:
            break  # the last sequence is literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("lz4: zero offset")
        match = token & 15
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(out) - offset
        if start < 0:
            raise ValueError("lz4: offset before start")
        if offset >= match:
            out += out[start:start + match]
        else:  # overlapping copy, byte by byte
            for k in range(match):
                out.append(out[start + k])
    if len(out) != size:
        raise ValueError(f"lz4: decoded {len(out)} bytes, header says {size}")
    return bytes(out)


def read_mozlz4(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:8] != _MOZLZ4_MAGIC:
        raise ValueError(f"{path} is not a mozlz4 file")
    size = int.from_bytes(data[8:12], "little")
    return lz4_block_decompress(data[12:], size)


def _session_files(profiles_dir: Path) -> list[Path]:
    return sorted(profiles_dir.glob("*/sessionstore-backups/recovery.jsonlz4"))


def open_tab_urls(profiles_dir: Path) -> set[str]:
    """Every URL open in a tab, across every Firefox profile's session file."""
    urls: set[str] = set()
    for path in _session_files(profiles_dir):
        try:
            session = json.loads(read_mozlz4(path))
        except (OSError, ValueError) as exc:
            logger.debug("Could not read Firefox session %s: %s", path, exc)
            continue
        for window in session.get("windows", []):
            for tab in window.get("tabs", []):
                for entry in tab.get("entries", []):
                    url = entry.get("url")
                    if url:
                        urls.add(url)
    return urls


def firefox_running() -> bool | None:
    """True/False from tasklist; None when it cannot tell (not Windows, no tasklist)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq firefox.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "firefox.exe" in out.lower()


def _default_profiles_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"


def open_in_firefox(url: str, firefox_path: str, profiles_dir: Path | None = None) -> str:
    """
    Open `url` in a new Firefox tab unless one already shows it.
    Returns "already_open", "opened" or "failed".
    """
    profiles_dir = profiles_dir or _default_profiles_dir()
    if firefox_running() is True and url in open_tab_urls(profiles_dir):
        logger.info("Live UI already open in Firefox, not opening another tab")
        return "already_open"
    if not Path(firefox_path).is_file():
        logger.warning("Live UI: Firefox not found at %s, not opening the page", firefox_path)
        return "failed"
    try:
        # Hands the URL to the running instance (or starts one) and returns.
        subprocess.Popen([firefox_path, "-new-tab", url])
    except OSError as exc:
        logger.warning("Live UI: could not start Firefox: %s", exc)
        return "failed"
    logger.info("Live UI opened in Firefox")
    return "opened"


# ---------------------------------------------------------------- WhatsApp


def send_link(whatsapp_service, number: str, url: str) -> bool:
    """
    WhatsApp the link to `number`. Playwright backend only: the keyboard backend
    would take over the keyboard and screen at boot, with nobody asking for it.
    """
    if getattr(whatsapp_service, "enabled", False) is not True:
        logger.info("Live UI: WhatsApp is off, link not sent")
        return False
    if getattr(whatsapp_service, "backend", "") != "playwright":
        logger.info("Live UI: link not sent, the keyboard WhatsApp backend would take the keyboard")
        return False
    match = whatsapp_service.resolve_contact(number)
    if not match.certain or not match.phone:
        logger.warning("Live UI: CALIFORNIA_UI_NOTIFY_NUMBER is not a usable phone number")
        return False

    message = f"California is up ({datetime.now():%H:%M}). Live UI: {url}"
    from services.whatsapp_service import _MSG_BUSY

    for attempt in range(_BUSY_RETRIES):
        # Queued behind the boot warm-up on the WhatsApp worker. Only a refusal
        # as busy is retried: a timeout may still send later, and a retry then
        # would send the link twice.
        result = whatsapp_service.send(match, message)
        if result:
            logger.info("Live UI link sent on WhatsApp")
            return True
        if getattr(result, "message", "") != _MSG_BUSY or attempt == _BUSY_RETRIES - 1:
            logger.warning("Live UI: WhatsApp link not sent: %s", getattr(result, "message", result))
            return False
        time.sleep(_BUSY_WAIT_S)
    return False


# -------------------------------------------------------------------- entry


def announce(url: str, whatsapp_service, config: dict) -> None:
    """Both steps, each on its own; one failing never stops the other."""
    cfg = ((config or {}).get("ui") or {}).get("announce") or {}
    if cfg.get("open_firefox", True):
        try:
            firefox = os.getenv("FIREFOX_PATH") or str(cfg.get("firefox_path") or "") or _DEFAULT_FIREFOX
            open_in_firefox(url, firefox)
        except Exception:  # noqa: BLE001 - background nicety, never fatal
            logger.exception("Live UI: opening Firefox failed")
    if cfg.get("whatsapp", True):
        number = (os.getenv("CALIFORNIA_UI_NOTIFY_NUMBER") or "").strip()
        if not number:
            logger.info("Live UI: CALIFORNIA_UI_NOTIFY_NUMBER not set, link not sent on WhatsApp")
            return
        try:
            send_link(whatsapp_service, number, url)
        except Exception:  # noqa: BLE001
            logger.exception("Live UI: sending the link on WhatsApp failed")


def announce_in_background(url: str, whatsapp_service, config: dict) -> threading.Thread:
    thread = threading.Thread(
        target=announce, args=(url, whatsapp_service, config),
        name="live-ui-announce", daemon=True,
    )
    thread.start()
    return thread
