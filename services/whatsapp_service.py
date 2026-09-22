"""
WhatsApp messaging for the control_whatsapp tool.

Same shape as GoveeService and DeebotService: never raises at construction,
self-disables when the config flag is off / the dependency is missing / the
platform is wrong, returns WhatsAppCommandResult instead of raising, and runs
the blocking work on a daemon worker thread with one command in flight.

Three things make this service unlike the other three device services, and
they drive most of what looks unusual below.

- **It is GUI automation, not an API.** The send path opens
  `web.whatsapp.com/send?phone=...&text=...` in a new Firefox tab, waits for
  the page to put the text in the compose box, and presses Enter once. There
  is no token and no request: auth is whatever Firefox profile is already
  signed in to WhatsApp Web. So it needs a desktop session, it steals
  keyboard focus for ~13s, and it exists on Windows only. Everywhere else
  the service disables itself, exactly like services/bt_wake.py being Linux
  only. Ported from Master Miguel's standalone wa_send.py.

- **It messages real people, so a fuzzy name is confirmed before it sends.**
  The TV, the lights and the vacuum are all recoverable; a WhatsApp message
  is not. Whisper mistranscribes names in this project -- the vacuum's
  nickname has come back as "SirSoxalot" and "Sir Soxalot" -- and the contact
  book is full of first-name collisions. An explicit phone number or an
  exact/prefix/first-name-token hit sends straight away; anything weaker is
  read back first. See `_classify` and `send`.

- **The confirmation is a server-side token, not a promise the model keeps.**
  `confirm=True` on its own does not send. A fuzzy send stores
  `(key, message, deadline)` in `self._pending` and returns the read-back;
  only a later call whose resolved contact AND message text match that
  record, inside `confirm_timeout_ms`, is allowed through. A model that sets
  `confirm: True` on the very first call therefore still gets the read-back
  rather than a delivered message.

The contact book is a local VCF export (`whatsapp.contacts_path`). It is NOT
injected into the system prompt: at 400+ cards that is unaffordable on every
turn, so the model passes the spoken name through and resolution happens here.
Only the small `whatsapp.aliases` map is advertised, and that goes through the
shared services/name_matcher like the light rooms do.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from services.name_matcher import match_name

logger = logging.getLogger(__name__)

_MSG_NOT_CONFIGURED = "WhatsApp isn't set up right now."
_MSG_UNREACHABLE = "I couldn't send that WhatsApp message just now."
_MSG_BUSY = "I'm still sending the last WhatsApp message."
_MSG_NO_BROWSER = "I couldn't open Firefox to send that."

# Windows default. Overridable from config for a non-standard install.
_DEFAULT_FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
_DEFAULT_COUNTRY = "351"  # Portugal, matches most numbers in this book

# Score bands out of resolve_contact(). At or above this the match is good
# enough to send without asking: an exact name, a prefix, or an exact
# first-name token. Below it ("ana" found inside "Joana", or a difflib ratio)
# the recipient is read back first.
_CERTAIN_SCORE = 80.0

# How close the runner-up has to be before a match counts as ambiguous.
_AMBIGUOUS_MARGIN = 5.0

# WhatsApp Web needs a floor of load time before the compose box holds the
# text; below this the Enter lands on an empty box.
_MIN_WAIT_S = 6.0
# On top of the page wait, to cover the browser launch and the focus dance.
_SEND_MARGIN_S = 10.0


@dataclass
class WhatsAppCommandResult:
    success: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.success


@dataclass
class Contact:
    name: str
    phones: list[str] = field(default_factory=list)
    org: str = ""

    @property
    def phone(self) -> str | None:
        return self.phones[0] if self.phones else None


@dataclass
class ContactMatch:
    """
    The outcome of resolving a spoken name or number to someone to message.

    `certain` is the whole safety story: True means send, False means read it
    back to Master Miguel first. `candidates` is populated only when two
    contacts scored close enough that picking either would be a coin flip, and
    in that case `key` and `phone` are empty -- there is nothing to confirm,
    only a question to ask.
    """

    key: str = ""
    phone: str = ""
    certain: bool = False
    candidates: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.phone)


# --------------------------------------------------------------------- VCF


def _unfold_vcf(text: str) -> str:
    """RFC 6350 soft line breaks: a newline followed by space/tab continues the line."""
    return re.sub(r"\r?\n[ \t]", "", text)


def normalize_phone(raw: str, default_cc: str = _DEFAULT_COUNTRY) -> str | None:
    """
    Turn a VCF TEL line or a spoken number into an E.164-ish "+<digits>".

    Returns None for anything that cannot be a WhatsApp number, which includes
    the short service codes this book is full of ("111", "12055").
    """
    s = raw.strip()
    s = re.sub(r"^TEL[^:]*:", "", s, flags=re.I)
    s = s.replace("tel:", "").strip()
    # Keep a leading +; strip everything else that is not a digit.
    digits = re.sub(r"[^\d+]", "", s)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits:
        return None
    if digits.startswith("+"):
        body = re.sub(r"\D", "", digits)
        return f"+{body}" if len(body) >= 8 else None
    body = re.sub(r"\D", "", digits)
    if not body or len(body) < 7:
        return None
    # Short service codes are not reachable on WhatsApp.
    if len(body) < 9:
        return None
    # Portuguese mobile (9xx) / landline (2xx) written without the country code.
    if len(body) == 9 and body[0] in "92":
        return f"+{default_cc}{body}"
    if body.startswith(default_cc) and len(body) >= 11:
        return f"+{body}"
    return f"+{body}"


def _tel_priority(tel_line: str) -> int:
    """Rank a TEL line so the mobile Master Miguel actually messages wins."""
    u = tel_line.upper()
    score = 0
    if "PREF" in u:
        score += 3
    if "CELL" in u or "MOBILE" in u:
        score += 2
    if "HOME" in u:
        score += 1
    return score


def load_vcf(path: Path, default_cc: str = _DEFAULT_COUNTRY) -> list[Contact]:
    """Parse a VCF export into contacts. Cards with no name or no usable number are dropped."""
    text = _unfold_vcf(path.read_text(encoding="utf-8", errors="replace"))
    contacts: list[Contact] = []

    for block in re.split(r"BEGIN:VCARD", text, flags=re.I):
        if "END:VCARD" not in block.upper():
            continue
        fn = ""
        org = ""
        n_parts: list[str] = []
        tels: list[tuple[int, str]] = []

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("FN"):
                fn = line.split(":", 1)[-1].strip()
            elif upper.startswith("ORG"):
                org = line.split(":", 1)[-1].strip()
            elif upper.startswith("N:") or upper.startswith("N;"):
                raw = line.split(":", 1)[-1]
                # N:Last;First;...
                n_parts = [p.strip() for p in raw.split(";") if p.strip()]
            elif upper.startswith("TEL"):
                phone = normalize_phone(line, default_cc)
                if phone:
                    tels.append((_tel_priority(line), phone))

        name = fn
        if not name and n_parts:
            name = f"{n_parts[1]} {n_parts[0]}".strip() if len(n_parts) >= 2 else n_parts[0]
        if not name:
            name = org
        if not name or not tels:
            continue

        # Highest-priority number first, duplicates dropped.
        tels.sort(key=lambda t: -t[0])
        phones: list[str] = []
        for _, p in tels:
            if p not in phones:
                phones.append(p)

        contacts.append(Contact(name=name, phones=phones, org=org))

    return contacts


def score_contacts(query: str, contacts: list[Contact], limit: int = 8) -> list[tuple[float, Contact]]:
    """
    Rank contacts against a spoken name, best first.

    The bands matter, because _classify reads them: 100 exact, 90 prefix either
    way, 80-85 an exact name token ("marta" in "Marta Zuka"), 60 a bare
    substring, and below that a difflib ratio. Everything from 80 up is safe to
    send without asking; everything under it gets read back.

    This is deliberately NOT services/name_matcher.match_name. That cascade's
    substring tier runs in both directions with no score at all, which is right
    for six light rooms and wrong for four hundred people -- "ana" would match
    "Joana" as confidently as it matches "Ana", and the failure mode here is
    messaging a stranger.
    """
    q = query.strip().lower()
    if not q:
        return []

    scored: list[tuple[float, Contact]] = []
    for c in contacts:
        name = c.name.lower()
        tokens = re.split(r"\W+", name)
        if name == q:
            score = 100.0
        elif name.startswith(q) or q.startswith(name):
            score = 90.0
        elif q in tokens:
            # An exact first-name token, e.g. "marta" in "Marta Zuka".
            score = 80.0 + (5.0 if tokens[0] == q else 0.0)
        elif q in name:
            score = 60.0
        else:
            ratio = difflib.SequenceMatcher(None, q, name).ratio()
            if ratio < 0.55:
                continue
            score = 40.0 * ratio
        scored.append((score, c))

    scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
    return scored[:limit]


def looks_like_phone(value: str) -> bool:
    """True when the hint is a number Master Miguel said rather than a name."""
    t = (value or "").strip()
    if t.startswith("+") and sum(c.isdigit() for c in t) >= 8:
        return True
    digits = re.sub(r"\D", "", t)
    return len(digits) >= 9 and t[:1].isdigit()


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WhatsAppService:
    def __init__(self, config: dict):
        cfg = config.get("whatsapp", {}) or {}

        self.default_country = (
            str(cfg.get("default_country") or "").strip() or _DEFAULT_COUNTRY
        )
        self.firefox_path = Path(
            os.getenv("FIREFOX_PATH") or str(cfg.get("firefox_path") or "").strip() or _DEFAULT_FIREFOX
        )
        self.contacts_path = Path(str(cfg.get("contacts_path") or "contacts.vcf"))

        # Floor the SECONDS, not the milliseconds -- the same trap called out in
        # deebot_service: max(6, ms) / 1000 reads like a 6s minimum and is a 6ms
        # one, so wait_ms: 3 would fire Enter into a blank page.
        self.wait_s = max(_MIN_WAIT_S, _as_int(cfg.get("wait_ms"), 12000) / 1000)
        self.confirm_timeout_s = max(
            10.0, _as_int(cfg.get("confirm_timeout_ms"), 120000) / 1000
        )
        # The browser launch and the focus dance sit on top of the page wait.
        self._wait_timeout_s = self.wait_s + _SEND_MARGIN_S

        self.aliases = self._build_aliases(cfg.get("aliases") or {})

        self.contacts: list[Contact] = []
        self._load_contacts()

        self.available = self._probe_platform()

        configured = bool(cfg.get("enabled", False))
        if configured and self.available and not self.contacts:
            logger.warning(
                "whatsapp.enabled is true but no contacts loaded from %s, "
                "only explicit phone numbers will resolve",
                self.contacts_path,
            )
        self.enabled = bool(configured and self.available)

        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._pending: tuple[str, str, float] | None = None
        self._timers: list[threading.Timer] = []

        if self.enabled:
            logger.info(
                "WhatsApp initialized: %d contact(s), %d alias(es)",
                len(self.contacts),
                len(self.aliases),
            )

    # ------------------------------------------------------------ self-disable

    def _probe_platform(self) -> bool:
        """
        Whether this machine can actually drive WhatsApp Web.

        Never raises. Each failing factor logs its own line naming the fix,
        because "wrong OS", "missing package" and "Firefox is somewhere else"
        are three different problems with three different answers.
        """
        if sys.platform != "win32":
            logger.warning(
                "WhatsApp control is Windows only (this is %s), disabled. "
                "The send path drives WhatsApp Web in Firefox with pyautogui "
                "and needs a desktop session.",
                sys.platform,
            )
            return False

        try:
            import pyautogui  # noqa: F401
        except Exception:  # noqa: BLE001 - pyautogui raises on a headless box, not just ImportError
            logger.warning(
                "pyautogui is not usable, WhatsApp control disabled. "
                "Install it with: uv sync --extra default"
            )
            return False

        if not self.firefox_path.is_file():
            logger.warning(
                "Firefox not found at %s, WhatsApp control disabled. "
                "Set whatsapp.firefox_path in config.yaml.",
                self.firefox_path,
            )
            return False

        return True

    # --------------------------------------------------------------- contacts

    @staticmethod
    def _build_aliases(raw: dict) -> dict:
        """Spoken nickname -> the exact contact name it means. Blank entries skipped."""
        aliases = {}
        for key, value in (raw or {}).items():
            spoken = str(key or "").strip()
            target = str(value or "").strip()
            if not spoken or not target:
                logger.warning("WhatsApp alias %r has no target, skipping", key)
                continue
            aliases[spoken] = target
        return aliases

    def _load_contacts(self) -> None:
        """Read the VCF. A missing or unreadable book is a warning, never a raise."""
        if not self.contacts_path.is_file():
            logger.warning(
                "WhatsApp contact book not found at %s, name lookup unavailable",
                self.contacts_path,
            )
            self.contacts = []
            return
        try:
            self.contacts = load_vcf(self.contacts_path, self.default_country)
        except Exception:  # noqa: BLE001 - a malformed export must not take the assistant down
            logger.exception("Could not parse %s, WhatsApp name lookup unavailable", self.contacts_path)
            self.contacts = []

    def reload_contacts(self) -> int:
        """Re-read the VCF so a refreshed export does not need a restart."""
        self._load_contacts()
        return len(self.contacts)

    def resolve_contact(self, hint: str) -> ContactMatch:
        """
        Turn a spoken name or number into someone to message.

        No default recipient, on purpose -- the same reason DeebotService has no
        default room, except the cost of guessing here is a message to a
        stranger. Order: an explicit number, then the configured aliases, then
        the contact book.
        """
        cleaned = (hint or "").strip()
        if not cleaned:
            return ContactMatch()

        if looks_like_phone(cleaned):
            phone = normalize_phone(cleaned, self.default_country)
            if not phone:
                return ContactMatch()
            # He said the digits, so there is nothing for him to confirm.
            return ContactMatch(key=phone, phone=phone, certain=True)

        # Aliases are a handful of hand-written nicknames, so the shared
        # matcher's cascade is the right tool here -- same as the light rooms.
        if self.aliases:
            matched = match_name(cleaned, {key: [key] for key in self.aliases})
            if matched:
                target = self.aliases[matched]
                exact = [c for c in self.contacts if c.name.lower() == target.lower()]
                if exact and exact[0].phone:
                    return ContactMatch(key=exact[0].name, phone=exact[0].phone, certain=True)
                if looks_like_phone(target):
                    phone = normalize_phone(target, self.default_country)
                    if phone:
                        return ContactMatch(key=matched, phone=phone, certain=True)
                logger.warning(
                    "WhatsApp alias %r points at %r, which is not in the contact book",
                    matched,
                    target,
                )

        return self._classify(score_contacts(cleaned, self.contacts))

    @staticmethod
    def _classify(scored: list[tuple[float, Contact]]) -> ContactMatch:
        """Turn a ranked candidate list into send / confirm / ask-which."""
        if not scored:
            return ContactMatch()

        best_score, best = scored[0]
        if not best.phone:
            return ContactMatch()

        # Two people scored close enough that picking either is a coin flip.
        # An exact hit (100) is never ambiguous -- a second contact cannot also
        # be an exact match on the same name without being the same name.
        if (
            len(scored) > 1
            and best_score < 100
            and scored[1][0] >= best_score - _AMBIGUOUS_MARGIN
        ):
            return ContactMatch(candidates=[c.name for _, c in scored if c.phone][:5])

        return ContactMatch(
            key=best.name,
            phone=best.phone,
            certain=best_score >= _CERTAIN_SCORE,
        )

    def find_contacts(self, hint: str, limit: int = 5) -> list[tuple[str, str]]:
        """(name, phone) pairs for a lookup that must never send anything."""
        cleaned = (hint or "").strip()
        if not cleaned:
            return []
        if looks_like_phone(cleaned):
            phone = normalize_phone(cleaned, self.default_country)
            return [(phone, phone)] if phone else []
        return [
            (c.name, c.phone)
            for _, c in score_contacts(cleaned, self.contacts, limit=limit)
            if c.phone
        ][:limit]

    # --------------------------------------------------------------- plumbing

    def close(self) -> None:
        """
        Stop accepting commands and drop any scheduled send.

        Cancelling the timers is the point: a scheduled send is a daemon thread
        that would otherwise wake up after shutdown, raise a Firefox window and
        press Enter at a machine nobody is watching. Safe to call more than once.
        """
        with self._lock:
            self._closed = True
            timers, self._timers = self._timers, []
        for timer in timers:
            timer.cancel()

    def _run(self, work):
        """
        Run one blocking send on a worker thread with a timeout.

        One at a time, refused rather than queued -- DeebotService._run's
        reasoning applies unchanged: a timed-out future cannot be cancelled, so
        the worker keeps driving the keyboard, and a second send queued behind
        it would burn its own budget waiting in line and then report a failure
        that never happened. Refusing says something true instead.

        The worker is a daemon thread for the same reason it is there: a
        ThreadPoolExecutor's workers are not, and concurrent.futures joins them
        from an atexit hook, so a send still waiting on WhatsApp Web would hold
        the whole process open on Ctrl-C.
        """
        with self._lock:
            if self._closed:
                return WhatsAppCommandResult(False, _MSG_NOT_CONFIGURED)
            if self._busy:
                logger.warning("WhatsApp: a send is still running, refusing a second")
                return WhatsAppCommandResult(False, _MSG_BUSY)
            self._busy = True

        future: Future = Future()

        def _work():
            try:
                future.set_result(work())
            except BaseException as exc:  # noqa: BLE001 - handed to the caller below
                future.set_exception(exc)
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=_work, name="whatsapp", daemon=True).start()
        try:
            return future.result(timeout=self._wait_timeout_s)
        except FutureTimeoutError:
            logger.warning("WhatsApp send timed out after %.0fs", self._wait_timeout_s)
            return WhatsAppCommandResult(False, _MSG_UNREACHABLE)
        except Exception:  # noqa: BLE001 - never let a browser error reach the orchestrator
            logger.exception("WhatsApp send failed")
            return WhatsAppCommandResult(False, _MSG_UNREACHABLE)

    # ------------------------------------------------------------- the browser

    def _open_chat(self, phone: str, message: str) -> None:
        """
        Open the chat with the message already in the compose box.

        A direct Popen rather than the webbrowser module: webbrowser picks
        whatever the OS default is, and this path is calibrated for Firefox
        with a signed-in WhatsApp Web session.
        """
        number = phone if phone.startswith("+") else f"+{phone}"
        url = f"https://web.whatsapp.com/send?phone={number}&text={quote(message)}"
        subprocess.Popen(
            [str(self.firefox_path), "-new-tab", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _focus_firefox() -> None:
        """
        Bring an existing Firefox window to the front.

        SetForegroundWindow only. Never ShowWindow or a restore call: those made
        the window flash and minimise instead of coming forward.
        """
        try:
            import win32gui

            targets: list[int] = []

            def enum_handler(hwnd: int, _: object) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                if "Firefox" in title or "WhatsApp" in title:
                    targets.append(hwnd)

            win32gui.EnumWindows(enum_handler, None)
            if targets:
                win32gui.SetForegroundWindow(targets[0])
                time.sleep(0.3)
                return
        except Exception:  # noqa: BLE001 - fall through to the pygetwindow path
            pass

        try:
            import pygetwindow as gw

            for title in ("WhatsApp", "Mozilla Firefox", "Firefox"):
                wins = gw.getWindowsWithTitle(title)
                if wins:
                    wins[0].activate()
                    time.sleep(0.3)
                    return
        except Exception:  # noqa: BLE001 - focus is best effort, the Enter may still land
            pass

    def _press_send(self) -> None:
        """
        Keyboard-only send.

        Exactly ONE Enter. The compose box already holds the text and already
        has focus when the URL loads, so one press sends it -- and after it
        sends, focus moves to the record-audio button, where a second Enter
        starts a voice recording instead of doing nothing. Do not add a retry
        press here.

        A centre click (what pywhatkit does) is worse than useless: it often
        unfocuses the input, and then Enter never sends at all.
        """
        import pyautogui as pg

        # Set on use, not at import: FAILSAFE at module scope would arm a
        # corner-of-screen abort for the whole assistant.
        pg.FAILSAFE = False
        pg.PAUSE = 0.15

        self._focus_firefox()
        time.sleep(0.3)
        pg.press("enter")

    def _send_now(self, phone: str, message: str) -> WhatsAppCommandResult:
        """Open the chat, wait out the page load, press Enter once."""
        try:
            self._open_chat(phone, message)
        except OSError:
            logger.exception("Could not launch Firefox at %s", self.firefox_path)
            return WhatsAppCommandResult(False, _MSG_NO_BROWSER)

        time.sleep(self.wait_s)
        self._press_send()
        time.sleep(1)
        logger.info("WhatsApp message sent to %s", phone)
        return WhatsAppCommandResult(True)

    # ----------------------------------------------------------------- sending

    def _confirmed(self, key: str, message: str) -> bool:
        """
        Whether this exact send was read back and is still inside its window.

        The check is the whole reason `confirm` cannot be short-circuited: the
        model can set the flag, but it cannot manufacture a pending record, so
        a first-call `confirm=True` finds nothing here and still gets the
        read-back. One shot -- the record is dropped whether or not it matched,
        so a second message to the same person is read back again.
        """
        with self._lock:
            pending, self._pending = self._pending, None
        if not pending:
            return False
        p_key, p_message, deadline = pending
        if time.monotonic() > deadline:
            logger.info("WhatsApp confirmation for %s expired, asking again", p_key)
            return False
        return p_key == key and p_message == message

    def _arm_confirmation(self, key: str, message: str) -> None:
        with self._lock:
            self._pending = (key, message, time.monotonic() + self.confirm_timeout_s)

    def send(self, match: ContactMatch, message: str, confirm: bool = False) -> WhatsAppCommandResult:
        """
        Send a message, or ask for confirmation first.

        Returns a falsy result carrying the read-back line when the recipient
        was only a fuzzy match and no confirmation is pending. The dispatcher
        speaks that line and waits; a later call with confirm=True and the same
        recipient and text goes through.
        """
        if not self.enabled:
            return WhatsAppCommandResult(False, _MSG_NOT_CONFIGURED)
        if not match or not message:
            return WhatsAppCommandResult(False, _MSG_UNREACHABLE)

        if not match.certain and not (confirm and self._confirmed(match.key, message)):
            self._arm_confirmation(match.key, message)
            return WhatsAppCommandResult(
                False,
                f"I've got {match.key} at {match.phone}. Say send it and I will.",
            )

        return self._run(lambda: self._send_now(match.phone, message))

    def schedule(
        self, match: ContactMatch, message: str, at: str, confirm: bool = False
    ) -> WhatsAppCommandResult:
        """
        Arm a send for a local HH:MM today (or tomorrow if that time has passed).

        A timer thread, not a sleep inside the worker: the worker allows one
        command in flight, so sleeping there would block every other WhatsApp
        command until the send fired. The timer's callback goes through the
        same `_run` gate when it wakes.

        Two things this deliberately does not do, both stated back to Master
        Miguel in the spoken line rather than engineered around: it does not
        survive a restart (close() cancels it), and at fire time it raises a
        Firefox window and takes the keyboard whether or not anyone is there.
        """
        if not self.enabled:
            return WhatsAppCommandResult(False, _MSG_NOT_CONFIGURED)

        delay = _seconds_until(at)
        if delay is None:
            return WhatsAppCommandResult(
                False, "I need a time like nine thirty or seventeen hundred for that."
            )
        if delay < self.wait_s:
            return WhatsAppCommandResult(
                False, "That's too soon to schedule, I'd have to send it right now."
            )

        if not match.certain and not (confirm and self._confirmed(match.key, message)):
            self._arm_confirmation(match.key, message)
            return WhatsAppCommandResult(
                False,
                f"I've got {match.key} at {match.phone}. Say send it and I'll set it for {at}.",
            )

        phone, key = match.phone, match.key

        def _fire():
            with self._lock:
                self._timers = [t for t in self._timers if t.is_alive()]
                closed = self._closed
            if closed:
                return
            logger.info("Scheduled WhatsApp send to %s firing now", key)
            self._run(lambda: self._send_now(phone, message))

        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        with self._lock:
            if self._closed:
                return WhatsAppCommandResult(False, _MSG_NOT_CONFIGURED)
            self._timers.append(timer)
        timer.start()
        logger.info("WhatsApp send to %s scheduled for %s (%.0fs away)", key, at, delay)
        return WhatsAppCommandResult(True)


def _seconds_until(at: str) -> float | None:
    """Seconds from now until the next local HH:MM. None when it will not parse."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:h.]?\s*(\d{2})\s*", at or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None

    now = time.localtime()
    target = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1)
    )
    delay = target - time.time()
    if delay <= 0:
        delay += 86400  # already gone today, so he means tomorrow
    return delay
