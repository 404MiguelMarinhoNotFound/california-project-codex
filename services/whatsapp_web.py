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

from services.whatsapp_groups import words

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
    # The chat-list filter chips: <button role=tab> with aria-selected.
    # Clicking one opens no chat, so nothing is marked read.
    #
    # Until 2026-10-02 these keyed on ids (#unread-filter, #group-filter). That
    # day the live page renamed them to positional `label_item_1`/`_3` -- only
    # #all-filter survived -- and unread quietly degraded to the ~20 rendered
    # "All" rows with no group detection. Positional ids are worse than labels,
    # so the label text is the fallback (English and Portuguese UI). Playwright's
    # :has-text is a case-insensitive substring, and "Groups\n2" (with its
    # unread badge) still matches.
    "filter_all": ["button#all-filter"],
    "filter_unread": [
        "button#unread-filter",
        "button[role='tab']:has-text(\"Unread\")",
        "button[role='tab']:has-text(\"Não lidas\")",
    ],
    "filter_groups": [
        "button#group-filter",
        "button[role='tab']:has-text(\"Groups\")",
        "button[role='tab']:has-text(\"Grupos\")",
    ],
    # The scrollable chat list. It is virtualised: only ~20 rows exist in the
    # DOM at a time, so reading every chat means scrolling it.
    "chat_list": ["#pane-side"],
    # The chat-list search box. Under the Groups chip it reads "Search group
    # chats" and searches group titles only (observed live 2026-10-02).
    "search": ["#side input[role='textbox']", "#side div[contenteditable='true'][role='textbox']"],
    # The open chat's name in its header -- checked before a group send, so a
    # click that opened the wrong chat never gets a message typed into it. NOT
    # `span[title]`: in a group header that is the "click here for group info"
    # tooltip (live 2026-10-02). The name's emoji are <img>s, so its text is
    # "☭flamingus unanounymous☭" for that title -- compared by words, not exactly.
    "chat_title": ["#main header span[dir='auto']"],
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


@dataclass
class GroupChat:
    """One group, as the chat list shows it. The title is its only handle."""

    name: str
    muted: bool = False


@dataclass
class GroupListResult:
    status: str  # READ_OK, NOT_LINKED, TIMEOUT or NO_GROUP_FILTER
    groups: list[GroupChat]

    def __bool__(self) -> bool:
        return self.status == READ_OK


# The "Groups" chip is the only thing that says a chat is a group -- nothing in
# a row marks one -- so without it there is no list to give.
NO_GROUP_FILTER = "no_group_filter"

# Runs in the page. Reads each chat row's name, preview, badge and muted marker.
# Selectors come in as arguments so SELECTORS stays the one table to update.
#
# `index` is the row's position in the WHOLE list, not in the ~20 rendered
# rows; it is what lets a scrolled read keep two chats with the same title
# apart. It comes from the wrapper the virtual list positions each row with,
# `transform: translateY(380px)` over `height: 76px` = row 5. NOT from
# data-testid="list-item-N": that is the recycled DOM slot, 0-19 at every
# scroll position (measured live 2026-10-02, and keying on it stopped a
# 216-group read at 20). null when no positioned wrapper is found.
_READ_ROWS_JS = r"""
([rowSel, badgeSel, mutedSel]) => [...document.querySelectorAll(rowSel)].map(r => {
  const titled = [...r.querySelectorAll("span[title]")].map(s => s.getAttribute("title") || "");
  const badge = r.querySelector(badgeSel);
  let count = null;
  if (badge) {
    const m = (badge.getAttribute("aria-label") || "").match(/\d+/);
    count = m ? parseInt(m[0], 10) : null;
  }
  let idx = NaN, top = null;
  for (let e = r, i = 0; e && i < 5 && Number.isNaN(idx); e = e.parentElement, i++) {
    const m = (e.style && e.style.transform || "").match(/translateY\((-?[\d.]+)px\)/);
    if (m) {
      const y = parseFloat(m[1]);
      const h = parseFloat(e.style.height) || e.offsetHeight;
      idx = h > 0 ? Math.round(y / h) : y;
      top = y;
    }
  }
  return {
    name: titled[0] || "",
    preview: titled[1] || "",
    unread: !!badge,
    count,
    muted: !!r.querySelector(mutedSel),
    index: Number.isNaN(idx) ? null : idx,
    top,
  };
})
"""

# The one rendered chat row whose title is exactly `title`, as an element, or
# null when there is not exactly one. The caller clicks THAT element with a
# real Playwright click:
# - a synthetic mousedown/mouseup/click dispatched in the page opened nothing
#   (live 2026-10-02), while a pointer click opens the chat in ~0.2s. The
#   role=dialog layer that defeats pointer clicks on the chips spares the rows;
# - an element, not a position: the search re-sorts the list under the click.
#   Clicking row N of a list read a moment earlier opened a different group
#   (live 2026-10-02, "Test group" sat near the top of the unfiltered list).
_FIND_ROW_JS = r"""
([rowSel, title, marks]) => {
  const strip = s => (s || "").replace(new RegExp(marks, "g"), "").trim();
  const rows = [...document.querySelectorAll(rowSel)].filter(r => {
    const t = r.querySelector("span[title]");
    return t && strip(t.getAttribute("title")) === title;
  });
  return rows.length === 1 ? rows[0] : null;
}
"""

# Scroll the chat list by `fraction` of its visible height (0 = to the top) and
# report where it ended up, so the caller knows when it has reached the bottom.
_SCROLL_LIST_JS = r"""
([listSel, fraction]) => {
  const p = document.querySelector(listSel);
  if (!p) return null;
  p.scrollTop = fraction > 0 ? p.scrollTop + p.clientHeight * fraction : 0;
  return {top: p.scrollTop, height: p.scrollHeight, client: p.clientHeight};
}
"""

# Bring a row (by its translateY offset) into the middle of the list's view.
_SCROLL_TO_JS = r"""
([listSel, top]) => {
  const p = document.querySelector(listSel);
  if (p) p.scrollTop = Math.max(0, top - p.clientHeight / 2);
}
"""

# A list of 216 groups took ~40 steps live (2026-10-02); this only bounds a
# list that never reports a bottom.
_MAX_SCROLL_STEPS = 400
# How far each step scrolls (a fraction of the visible height) and how long the
# rows get to re-render. Measured on 218 groups, 2026-10-02: 0.4s / 0.8 took
# 18.9s, 0.15s / 0.9 took 7.4s. Never above 1.0: rows past the rendered window
# would be skipped, which only the overscan was hiding at 1.5.
_SCROLL_FRACTION = 0.9
_SCROLL_SETTLE_S = 0.15
# What WhatsApp shows in a group row before the group's name has loaded. At the
# faster settle one row read "Group" and showed its real name 0.3s later (live
# 2026-10-02); such rows are revisited until the real name shows.
_PLACEHOLDER_TITLES = frozenset({"Group", "Grupo"})
_PLACEHOLDER_TRIES = 4
_PLACEHOLDER_WAIT_S = 0.3
# How long a group search may take to show the title before it counts as gone.
_SEARCH_WAIT_S = 6.0
# The search re-renders a beat after typing; rows count as settled once two
# reads this far apart agree.
_SEARCH_SETTLE_S = 0.15
# How long a clicked group may take to show its header before it counts as the
# wrong chat (and is retried once).
_OPEN_WAIT_S = 5.0
# A chat's history renders a beat after it opens; see _enter_and_confirm.
_HISTORY_SETTLE_S = 0.15
_HISTORY_WAIT_S = 3.0

_BIDI_MARKS = re.compile("[‪-‮⁦-⁩‎‏]")

# Outcomes of one send. Distinct because each needs a different spoken fix.
SENT = "sent"                      # bubble with a sent tick
UNCONFIRMED = "unconfirmed"        # bubble appeared, tick never did (still queued)
NOT_LINKED = "not_linked"          # QR code instead of the chat: run the link tool
INVALID_NUMBER = "invalid_number"  # WhatsApp's own "not on WhatsApp" dialog
TIMEOUT = "timeout"                # nothing recognisable within the budget
GROUP_NOT_FOUND = "group_not_found"  # no group with exactly that title (renamed, or left)
GROUP_AMBIGUOUS = "group_ambiguous"  # two groups with exactly that title: cannot tell which
WRONG_CHAT = "wrong_chat"          # the chat that opened is not the one clicked; nothing typed


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


def _open_chat_title(page) -> str:
    """The name in the open chat's header, or "" when there is none to read."""
    try:
        text = _any(page, "chat_title").first.inner_text() or ""
    except Exception:  # noqa: BLE001 - no chat open yet reads as "not this one"
        return ""
    return _BIDI_MARKS.sub("", text).strip()


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

    def unread_chats(self, known_groups=()) -> UnreadResult:
        """
        What is unread, read off the chat LIST. Never opens a chat.

        That is the whole privacy and etiquette story: opening a chat marks it
        read on his phone and sends the sender blue ticks. The list shows the
        sender, the unread count and the LAST message's preview, and reading it
        changes nothing on either side. The cost is that only the latest message
        per chat is visible, truncated -- the caller says so.

        Groups are identified through the "Groups" filter chip because nothing
        in a row reliably marks one. Only the rows that chip renders without
        scrolling are read here (a full scroll is ~18s), so `known_groups` --
        the cached list_groups() titles -- covers the rest. The "Unread" chip's
        list is scrolled to the end, so every unread chat is read, not just the
        first ~20. The list is put back on "All" afterwards.
        """
        state = self.warm()
        if state == "needs_link":
            return UnreadResult(NOT_LINKED, [])
        if state != "linked":
            return UnreadResult(TIMEOUT, [])
        page = self._page
        group_names = set(known_groups)
        group_names |= {c["name"] for c in (self._rows_under(page, "filter_groups") or [])}
        rows = self._all_rows_under(page, "filter_unread")
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

    def _click_filter(self, page, key: str, settle: bool = True) -> bool:
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
        if settle:
            # The rows re-render a beat after the chip flips. A caller that
            # polls the rows itself (a group send) passes settle=False: this
            # fixed half-second was ~1s of every send, measured 2026-10-02.
            time.sleep(0.5)
        return True

    def _rows_under(self, page, key: str) -> list[dict] | None:
        """Rows RENDERED under one filter chip (~20), or None when the chip is not there."""
        if not self._click_filter(page, key):
            return None
        return self._read_rows(page)

    def _all_rows_under(self, page, key: str) -> list[dict] | None:
        """Every row under one filter chip, scrolled for, or None when the chip is not there."""
        if not self._click_filter(page, key):
            return None
        return self._collect_rows(page)

    def _scroll_list(self, page, fraction: float) -> dict | None:
        return page.evaluate(_SCROLL_LIST_JS, [", ".join(SELECTORS["chat_list"]), fraction])

    def _collect_rows(self, page) -> list[dict]:
        """
        Every row of the current (virtualised) list, by scrolling it to the end.

        Rows are keyed by their position in the whole list when the markup
        gives one, so two chats with the same title stay two rows; by title
        otherwise. A row seen again keeps its LATEST name, since names load
        after rows do. Done when the list reports its bottom and a further read
        adds nothing; then any row still showing the "Group" placeholder is
        scrolled back to and re-read. The list is left at the top.
        """
        seen: dict = {}

        def _absorb() -> bool:
            added = False
            for r in self._read_rows(page):
                if not r.get("name"):
                    continue
                key = r["index"] if r.get("index") is not None else r["name"]
                if key not in seen:
                    added = True
                    seen[key] = r
                elif r.get("index") is not None and seen[key]["name"] != r["name"]:
                    seen[key] = r  # the real name arrived after the placeholder
            return added

        pos = self._scroll_list(page, 0)
        time.sleep(_SCROLL_SETTLE_S)
        _absorb()
        for _ in range(_MAX_SCROLL_STEPS):
            at_bottom = pos is None or pos["top"] + pos["client"] >= pos["height"] - 2
            pos = self._scroll_list(page, _SCROLL_FRACTION)
            time.sleep(_SCROLL_SETTLE_S)
            if not _absorb() and at_bottom:
                break
        else:
            logger.warning("Chat list never reported its bottom; read %d rows", len(seen))
        self._revisit_placeholders(page, seen, _absorb)
        self._scroll_list(page, 0)
        rows = list(seen.values())
        if all(r.get("index") is not None for r in rows):
            rows.sort(key=lambda r: r["index"])
        return rows

    def _revisit_placeholders(self, page, seen: dict, absorb) -> None:
        """Scroll back to rows still named like a placeholder until they load."""
        for _ in range(_PLACEHOLDER_TRIES):
            pending = [r for r in seen.values() if r["name"] in _PLACEHOLDER_TITLES and r.get("top") is not None]
            if not pending:
                return
            for r in pending:
                page.evaluate(_SCROLL_TO_JS, [", ".join(SELECTORS["chat_list"]), r["top"]])
                time.sleep(_PLACEHOLDER_WAIT_S)
                absorb()
        still = [r["index"] for r in seen.values() if r["name"] in _PLACEHOLDER_TITLES]
        if still:
            logger.info("Group rows %s still read as a placeholder; kept as they are", still)

    def list_groups(self) -> GroupListResult:
        """
        Every group this account is in, read off the chat list. Opens no chat.

        The "Groups" chip filters the list to groups and the list is scrolled to
        the end. The title is the only handle on a group: the page carries no
        group id (no @g.us anywhere in the list, checked live 2026-10-02), so a
        renamed group is found again only by reading the list again. Two
        groups can share a title, and both are returned.
        """
        state = self.warm()
        if state == "needs_link":
            return GroupListResult(NOT_LINKED, [])
        if state != "linked":
            return GroupListResult(TIMEOUT, [])
        page = self._page
        try:
            rows = self._all_rows_under(page, "filter_groups")
        finally:
            self._click_filter(page, "filter_all")
        if rows is None:
            return GroupListResult(NO_GROUP_FILTER, [])
        groups = [
            GroupChat(name=_BIDI_MARKS.sub("", r["name"]).strip(), muted=bool(r["muted"]))
            for r in rows
        ]
        return GroupListResult(READ_OK, [g for g in groups if g.name])

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

        return self._enter_and_confirm(page, message, deadline)

    def _enter_and_confirm(self, page, message: str, deadline: float) -> SendOutcome:
        """
        The text is in the compose box, or about to be: press Enter once and
        wait for the bubble and its tick. Shared by `send` and `send_group`.
        """
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
        # Count only once the chat's history has finished rendering. Counted
        # while it was still loading, an OLDER copy of the same text arriving
        # read as the new bubble: 0 -> 2 on a repeated "latency benchmark 2"
        # (live 2026-10-02). For "on my way", sent many times, that is a false
        # "sent". Two equal reads _HISTORY_SETTLE_S apart; capped, because an
        # empty chat is settled at once.
        before = bubbles.count()
        settle_until = min(deadline, time.monotonic() + _HISTORY_WAIT_S)
        while time.monotonic() < settle_until:
            time.sleep(_HISTORY_SETTLE_S)
            now = bubbles.count()
            if now == before:
                break
            before = now

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

    def send_group(self, title: str, message: str) -> SendOutcome:
        """
        Send `message` to the group whose title is exactly `title`.

        There is no link that opens a group (no id is exposed; see
        list_groups), so this goes the way a person would: Groups chip, type
        the title into the search -- which that chip scopes to groups -- and
        click the one row whose title matches EXACTLY. Zero rows or two rows
        with that title are refused before anything is clicked. After the
        click the open chat's header must carry the same title, or nothing is
        typed. Then the same one-Enter, wait-for-the-tick tail as `send`.

        Opening the group marks it read on his phone. That is what sending
        into a chat by hand does too.
        """
        state = self.warm()
        if state == "needs_link":
            return SendOutcome(NOT_LINKED)
        if state != "linked":
            return SendOutcome(TIMEOUT, "WhatsApp Web never loaded")
        page = self._page
        deadline = time.monotonic() + self.timeout_s
        try:
            if not self._click_filter(page, "filter_groups", settle=False):
                return SendOutcome(TIMEOUT, "no Groups filter")
            search = _any(page, "search").first
            args = [", ".join(SELECTORS["chat_row"]), title, _BIDI_MARKS.pattern]
            opened = False
            # Twice at most: a click that opened the wrong chat has typed
            # nothing, so searching again is safe.
            for attempt in range(2):
                search.fill("")
                # The longest emoji-free run of the title: what can be typed,
                # and still a substring of the title for WhatsApp's search.
                search.fill(_snippet(title) or title)
                found = self._settled_count(page, title, min(deadline, time.monotonic() + _SEARCH_WAIT_S))
                if found == 0 and attempt:
                    break  # the retry ran out of time: still the wrong-chat outcome
                if found == 0:
                    return SendOutcome(GROUP_NOT_FOUND)
                if found > 1:
                    return SendOutcome(GROUP_AMBIGUOUS)
                row = page.evaluate_handle(_FIND_ROW_JS, args).as_element()
                if row is None:
                    continue  # the list moved between the read and now
                row.click(timeout=5000)
                if self._wait_for_chat(page, title, min(deadline, time.monotonic() + _OPEN_WAIT_S)):
                    opened = True
                    break
                logger.warning(
                    "Group send: opened %r instead of %r (attempt %d)",
                    _open_chat_title(page), title, attempt + 1,
                )
            if not opened:
                return SendOutcome(WRONG_CHAT, "the group never opened")

            compose = _any(page, "compose").first
            compose.evaluate("e => e.focus()")
            # One line: a newline typed into the compose box would send early.
            page.keyboard.insert_text(" ".join(message.split()))
            return self._enter_and_confirm(page, message, deadline)
        finally:
            try:
                _any(page, "search").first.fill("")
            except Exception:  # noqa: BLE001 - tidying the search box must not mask the outcome
                pass
            self._click_filter(page, "filter_all", settle=False)

    def _settled_count(self, page, title: str, until: float) -> int:
        """
        How many rows carry exactly `title`, read once the search has settled:
        two reads `_SEARCH_SETTLE_S` apart that agree and contain the title.
        Agreement alone is not enough -- the unfiltered list is stable too.
        """
        previous = None
        while time.monotonic() < until:
            rows = [_BIDI_MARKS.sub("", r["name"]).strip() for r in self._read_rows(page)]
            if rows == previous and title in rows:
                return rows.count(title)
            previous = rows
            time.sleep(_SEARCH_SETTLE_S)
        return 0

    @staticmethod
    def _wait_for_chat(page, title: str, until: float) -> bool:
        """
        Whether the open chat becomes `title` before `until`. Compared by
        words: the header renders the title's emoji as images.
        """
        while time.monotonic() < until:
            if _visible(page, "compose") and words(_open_chat_title(page)) == words(title):
                return True
            time.sleep(0.1)
        return False


def default_channel() -> str:
    """Edge ships with Windows, so it needs no download; elsewhere, bundled Chromium."""
    return "msedge" if sys.platform == "win32" else "chromium"
