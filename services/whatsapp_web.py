"""
WhatsApp Web driven by Playwright, for WhatsAppService's "playwright" backend.

This replaces the keyboard backend's three worst properties at once:

- **It never touches the OS keyboard or raises a window.** Enter is dispatched
  to the compose box element inside the page (`Locator.press`), not synthesised
  at whatever the laptop has focused, so the machine stays usable during a send.
- **It waits for the page, not a clock.** The keyboard backend slept a fixed
  `wait_ms` (12s) and pressed Enter into whatever was there. Here every step
  waits on the element it needs, so a warm page sends in a second or two.
- **"Sent" means it saw the message go.** After Enter it waits for a new
  outgoing bubble carrying the text and a sent tick. A bubble stuck on the
  clock icon is `unconfirmed`, never `sent` -- the same rule as the TV, where
  playback that `media_session` does not confirm is never reported as playing.

Two constraints from Playwright's docs shape everything here:

- The sync API is **not thread-safe**; one Playwright instance per thread. A
  driver is therefore owned by exactly one thread for its whole life --
  WhatsAppService's worker -- and never touched from anywhere else.
- Automating a **default** Chrome/Edge profile is unsupported. The driver runs
  in its own `profile_dir`, linked once with tools/link_whatsapp.py.

Every selector lives in `SELECTORS`. WhatsApp redesigns its web client without
notice, and when it does this is the one table to update; the failure mode
until then is `timeout`/`unconfirmed`, never a false "sent".
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

WHATSAPP_URL = "https://web.whatsapp.com/"

# One place for everything that depends on WhatsApp Web's current markup.
# Each entry is a list of alternatives joined into one CSS selector.
#
# Verified against the live page on 2026-09-23 (Edge 153, headless, English UI).
# WhatsApp Web now ships generated class names (x78zum5 ...), so nothing here
# keys on a class: the old `div.message-out` and `msg-check` icon selectors
# that every tutorial uses are gone. Roles and accessible labels survived.
SELECTORS: dict[str, list[str]] = {
    # The chat list pane: present only once a linked session has loaded.
    "linked": ["#pane-side"],
    # The QR code shown to an unlinked (or logged-out) profile. Its canvas is
    # labelled "Scan this QR code to link a device!".
    "qr": ["canvas[aria-label]", "div[data-ref] canvas"],
    # The message box in an open chat: role=textbox, labelled "Type a message
    # to +351 ...". `/send?text=` pre-fills it.
    "compose": ["footer div[contenteditable='true']"],
    # WhatsApp's modal for a number that is not on WhatsApp. There is ALWAYS a
    # role=dialog node in the DOM, so the text is what identifies this one --
    # English UI; add the Portuguese wording here if the UI language changes.
    "invalid_dialog": ["div[role='dialog']:has-text(\"isn't on WhatsApp\")"],
    # Every message row in the OPEN conversation, incoming or outgoing. The
    # driver counts rows carrying the message text, so direction needs no
    # selector of its own. Scoped to #main because the chat list items are
    # role=row too, and the list previews the last message -- unscoped, one
    # send matched twice (measured live 2026-09-23).
    "outgoing": ["#main div[role='row']"],
    # The delivery status on an outgoing bubble is an aria-label with padding
    # spaces (" Read " observed live). Pending (clock) is deliberately absent:
    # a row that only ever shows Pending is `unconfirmed`, not `sent`.
    "tick_sent": [
        "[aria-label=' Sent ']",
        "[aria-label=' Delivered ']",
        "[aria-label=' Read ']",
        "[aria-label='Sent']",
        "[aria-label='Delivered']",
        "[aria-label='Read']",
    ],
    # --- the chat list, for reading what is unread without opening anything ---
    # A chat row in the left pane. Its first span[title] is the chat's name,
    # the second the last message's preview (wrapped in U+202A/U+202C marks).
    "chat_row": ["#pane-side div[role='row']"],
    # "1 unread message" / "3 unread messages" (observed live 2026-09-23).
    "unread_badge": ["span[aria-label*='unread message']"],
    "muted": ["[aria-label*='muted' i]", "[data-icon*='muted']"],
    # The chat-list filter chips: <button role=tab id=...> with aria-selected.
    # Clicking one opens no chat, so nothing is marked read. The ids are
    # language-independent, unlike the labels.
    "filter_all": ["button#all-filter"],
    "filter_unread": ["button#unread-filter"],
    "filter_groups": ["button#group-filter"],
}

# Unread-read outcomes.
READ_OK = "ok"


@dataclass
class UnreadChat:
    """One chat with unread messages, as the chat LIST shows it."""

    name: str
    count: int | None  # None: marked unread by hand, no number on the badge
    preview: str  # the LAST message only, as the list truncates it
    is_group: bool = False
    muted: bool = False


@dataclass
class UnreadResult:
    status: str  # READ_OK, NOT_LINKED or TIMEOUT
    chats: list[UnreadChat]

    def __bool__(self) -> bool:
        return self.status == READ_OK


# Runs in the page. Reads each chat row's name, preview, badge and muted marker.
# Selectors come in as arguments so SELECTORS stays the one table to update.
_READ_ROWS_JS = r"""
([rowSel, badgeSel, mutedSel]) => [...document.querySelectorAll(rowSel)].map(r => {
  const titled = [...r.querySelectorAll("span[title]")].map(s => s.getAttribute("title") || "");
  const badge = r.querySelector(badgeSel);
  let count = null;
  if (badge) {
    const m = (badge.getAttribute("aria-label") || "").match(/\d+/);
    count = m ? parseInt(m[0], 10) : null;
  }
  return {
    name: titled[0] || "",
    preview: titled[1] || "",
    unread: !!badge,
    count,
    muted: !!r.querySelector(mutedSel),
  };
})
"""

_BIDI_MARKS = re.compile("[‪-‮⁦-⁩‎‏]")

# Outcomes of one send. Distinct because each needs a different spoken fix.
SENT = "sent"                      # bubble with a sent tick
UNCONFIRMED = "unconfirmed"        # bubble appeared, tick never did (still queued)
NOT_LINKED = "not_linked"          # QR code instead of the chat: run the link tool
INVALID_NUMBER = "invalid_number"  # WhatsApp's own "not on WhatsApp" dialog
TIMEOUT = "timeout"                # nothing recognisable within the budget


# Anything WhatsApp may render as an image instead of text: astral-plane
# characters (most emoji), the BMP symbol blocks, variation selectors and ZWJ.
_EMOJI_SPLIT = re.compile("[\U00010000-\U0010ffff←-⯿⌀-⏿︎️‍]+")


def _any(page, key: str):
    """A locator matching any of SELECTORS[key]."""
    return page.locator(", ".join(SELECTORS[key]))


def _visible(page, key: str) -> bool:
    try:
        return _any(page, key).first.is_visible()
    except Exception:  # noqa: BLE001 - a detached/navigating page reads as "not yet"
        return False


def _snippet(message: str) -> str:
    """
    The part of a message used to recognise its bubble.

    The longest emoji-free run of the first line, capped. Three things make the
    full body an unreliable substring of the bubble's text:

    - WhatsApp renders emoji as <img alt="...">, so they are ABSENT from the
      row's text: "Test from California 🌴 (Playwright)" reads back as
      "Test from California  (Playwright)". Measured live 2026-09-23; searching
      for the literal body timed out on a message that had been read.
    - Newlines render as separate blocks.
    - Long text is collapsed behind "Read more".
    """
    line = (message.strip().splitlines() or [""])[0]
    pieces = [p.strip() for p in _EMOJI_SPLIT.split(line)]
    return max(pieces, key=len, default="")[:60]


@dataclass
class SendOutcome:
    status: str
    detail: str = ""


class WhatsAppWebDriver:
    """
    One browser, one page, owned by one thread.

    Lazily launched: `open_page()` starts the persistent context on first use and
    reuses it after, so consecutive sends skip the browser launch and the
    WhatsApp Web boot. `close()` tears it down (idle timeout, shutdown).
    """

    def __init__(
        self,
        profile_dir: Path,
        channel: str | None,
        headless: bool,
        timeout_s: float = 30.0,
    ):
        self.profile_dir = Path(profile_dir)
        self.channel = channel
        self.headless = headless
        self.timeout_s = timeout_s
        self._pw = None
        self._context = None
        self._page = None

    @classmethod
    def from_config(cls, cfg: dict, headless: bool | None = None) -> "WhatsAppWebDriver":
        channel = str(cfg.get("browser_channel") or "").strip() or default_channel()
        if channel == "chromium":
            channel = None  # Playwright's bundled build
        return cls(
            profile_dir=Path(str(cfg.get("profile_dir") or ".whatsapp_profile")),
            channel=channel,
            headless=bool(cfg.get("headless", True)) if headless is None else headless,
            timeout_s=max(5.0, float(cfg.get("send_timeout_ms") or 30000) / 1000),
        )

    # ---------------------------------------------------------------- lifecycle

    @property
    def is_open(self) -> bool:
        return self._page is not None

    def open_page(self):
        """Launch the persistent context if needed; return the single page."""
        if self._page is not None and not self._page.is_closed():
            return self._page
        self.close()
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        t0 = time.monotonic()
        self._context = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            channel=self.channel,
            headless=self.headless,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(self.timeout_s * 1000)
        logger.info(
            "WhatsApp Web browser up in %.1fs (%s, headless=%s)",
            time.monotonic() - t0,
            self.channel or "chromium",
            self.headless,
        )
        return self._page

    def close(self) -> None:
        """Tear everything down. Safe to call repeatedly and after a crash."""
        for obj, name in ((self._context, "context"), (self._pw, "playwright")):
            if obj is None:
                continue
            try:
                obj.close() if name == "context" else obj.stop()
            except Exception:  # noqa: BLE001 - closing a dead browser must not raise
                logger.debug("WhatsApp Web %s close failed", name, exc_info=True)
        self._pw = self._context = self._page = None

    def warm(self) -> str:
        """Open WhatsApp Web and wait for it to settle. Returns the session state."""
        page = self.open_page()
        if not page.url.startswith(WHATSAPP_URL):
            page.goto(WHATSAPP_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + self.timeout_s
        state = "loading"
        while time.monotonic() < deadline:
            state = self.session_state(page)
            if state != "loading":
                break
            time.sleep(0.25)
        return state

    # --------------------------------------------------------------------- read

    @staticmethod
    def session_state(page) -> str:
        """'linked', 'needs_link', or 'loading' (neither has rendered yet)."""
        if _visible(page, "linked") or _visible(page, "compose"):
            return "linked"
        if _visible(page, "qr"):
            return "needs_link"
        return "loading"

    # --------------------------------------------------------------------- read

    def unread_chats(self) -> UnreadResult:
        """
        What is unread, read off the chat LIST. Never opens a chat.

        That is the whole privacy and etiquette story: opening a chat marks it
        read on his phone and sends the sender blue ticks. The list shows the
        sender, the unread count and the LAST message's preview, and reading it
        changes nothing on either side. The cost is that only the latest message
        per chat is visible, truncated -- the caller says so.

        Groups are identified through the "Groups" filter chip because nothing
        in a row reliably marks one. The "Unread" chip lists every unread chat,
        not just the ~20 rows the virtualised "All" list has rendered. The list
        is put back on "All" afterwards.
        """
        state = self.warm()
        if state == "needs_link":
            return UnreadResult(NOT_LINKED, [])
        if state != "linked":
            return UnreadResult(TIMEOUT, [])
        page = self._page
        group_names = {c["name"] for c in (self._rows_under(page, "filter_groups") or [])}
        rows = self._rows_under(page, "filter_unread")
        if rows is None:
            # No filter chips (older UI or a redesign): fall back to the rows
            # the "All" list has rendered, which still carry their badges.
            rows = self._read_rows(page)
        self._click_filter(page, "filter_all")

        chats = [
            UnreadChat(
                name=r["name"],
                count=r["count"],
                preview=_BIDI_MARKS.sub("", r["preview"]).strip(),
                is_group=r["name"] in group_names,
                muted=r["muted"],
            )
            for r in rows
            if r["name"] and r["unread"]
        ]
        return UnreadResult(READ_OK, chats)

    def _read_rows(self, page) -> list[dict]:
        args = [", ".join(SELECTORS[k]) for k in ("chat_row", "unread_badge", "muted")]
        return page.evaluate(_READ_ROWS_JS, args)

    def _click_filter(self, page, key: str) -> bool:
        chip = _any(page, key).first
        try:
            if not chip.is_visible():
                return False
            # A DOM click on the element, not a pointer click: WhatsApp keeps a
            # role=dialog layer in the page, and a pointer click waited out its
            # whole 30s actionability timeout on every chip (97s for one
            # unread read, measured 2026-09-23) without ever landing.
            chip.evaluate("b => b.click()")
            # The chip reports its own state; wait for it rather than a clock.
            deadline = time.monotonic() + 3
            while chip.get_attribute("aria-selected") != "true":
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.1)
        except Exception:  # noqa: BLE001 - a missing chip is a fallback, not a failure
            return False
        time.sleep(0.5)  # the rows re-render a beat after the chip flips
        return True

    def _rows_under(self, page, key: str) -> list[dict] | None:
        """Rows shown under one filter chip, or None when the chip is not there."""
        if not self._click_filter(page, key):
            return None
        return self._read_rows(page)

    # --------------------------------------------------------------------- send

    def send(self, phone: str, message: str) -> SendOutcome:
        """
        Open the chat with `message` pre-filled, press Enter in the compose box,
        and wait for the bubble and its tick. Never raises for an expected
        failure; every one becomes a SendOutcome.
        """
        page = self.open_page()
        number = phone.lstrip("+")
        url = f"{WHATSAPP_URL}send?phone={number}&text={quote(message)}"
        page.goto(url, wait_until="domcontentloaded")

        # Wait for whichever of the three screens this turns into.
        deadline = time.monotonic() + self.timeout_s
        while True:
            if _visible(page, "compose"):
                break
            if _visible(page, "qr"):
                return SendOutcome(NOT_LINKED)
            # The chat list stays on screen behind this dialog, so it must not
            # be gated on "linked" being absent (measured live 2026-09-23).
            if _visible(page, "invalid_dialog"):
                return SendOutcome(INVALID_NUMBER)
            if time.monotonic() > deadline:
                return SendOutcome(TIMEOUT, "chat never opened")
            time.sleep(0.2)

        compose = _any(page, "compose").first
        snippet = _snippet(message)

        # The text arrives a beat after the box does; Enter on an empty box
        # sends nothing and would read as a timeout below. An emoji-only
        # message has no text to look for, so it waits for the box to be
        # non-empty instead (the emoji are images in there too).
        def _prefilled() -> bool:
            text = (compose.inner_text() or "").strip()
            return snippet.split()[0] in text if snippet else bool(text) or compose.locator("img").count() > 0

        while not _prefilled():
            if time.monotonic() > deadline:
                return SendOutcome(TIMEOUT, "text never reached the compose box")
            time.sleep(0.1)

        rows = _any(page, "outgoing")
        bubbles = rows.filter(has_text=snippet) if snippet else rows
        before = bubbles.count()

        # Exactly one Enter, on the element -- see WhatsAppService._press_send
        # for why a second one is harmful (it lands on the record button).
        compose.press("Enter")

        while bubbles.count() <= before:
            if time.monotonic() > deadline:
                return SendOutcome(TIMEOUT, "no bubble after Enter")
            time.sleep(0.2)

        newest = bubbles.last
        tick = newest.locator(", ".join(SELECTORS["tick_sent"]))
        while tick.count() == 0:
            if time.monotonic() > deadline:
                return SendOutcome(UNCONFIRMED)
            time.sleep(0.2)
        return SendOutcome(SENT)


def default_channel() -> str:
    """Edge ships with Windows, so it needs no download; elsewhere, bundled Chromium."""
    return "msedge" if sys.platform == "win32" else "chromium"
