"""
WhatsApp messaging for the control_whatsapp tool.

Same shape as GoveeService and DeebotService: never raises at construction,
self-disables when the config flag is off / the dependency is missing / the
platform is wrong, returns WhatsAppCommandResult instead of raising, and runs
the blocking work on a daemon worker thread with one command in flight.

Three things make this service unlike the other three device services, and
they drive most of what looks unusual below.

- **It is browser automation, not an API.** There is no token and no
  request: auth is a WhatsApp Web session in a browser profile. Two backends,
  picked by `whatsapp.backend`:

  - `playwright` (default): services/whatsapp_web.py drives WhatsApp Web in
    California's own profile (`whatsapp.profile_dir`, linked once with
    tools/link_whatsapp.py). Enter goes to the compose box element, never the
    OS keyboard, the page is kept open between sends, and "sent" means a sent
    tick was seen. Runs anywhere Playwright does, the Pi included.
  - `keyboard`: the original port of Master Miguel's wa_send.py. Opens a
    Firefox tab, sleeps `wait_ms`, presses a real Enter with pyautogui. Takes
    the keyboard for ~13s, cannot confirm delivery, Windows only. Kept as a
    rollback while the Playwright backend proves itself.

  Both run on ONE long-lived daemon worker thread. Playwright's sync API is
  not thread-safe (its docs: one instance per thread), so the thread that
  launched the browser is the only one that may touch it again.

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
import queue
import quopri
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

from services.name_matcher import match_name, normalize_text

logger = logging.getLogger(__name__)

_MSG_NOT_CONFIGURED = "WhatsApp isn't set up right now."
_MSG_UNREACHABLE = "I couldn't send that WhatsApp message just now."
_MSG_BUSY = "I'm still sending the last WhatsApp message."
_MSG_NO_BROWSER = "I couldn't open Firefox to send that."
# Playwright outcomes. Each one names its own fix, because "scan the code",
# "check the number" and "try again" are three different things to do -- the
# same reason the TV has a needs_pairing line of its own.
_MSG_NEEDS_LINK = (
    "WhatsApp's logged out on the laptop. Run the link tool and scan the code with your phone."
)
_MSG_INVALID_NUMBER = "That number isn't on WhatsApp."
_MSG_UNCONFIRMED = (
    "It's in WhatsApp but hasn't gone out yet, so I can't say it's sent."
)

_MSG_READ_NEEDS_PLAYWRIGHT = "I can only check unread WhatsApps with the Playwright setup."
_MSG_READ_FAILED = "I couldn't check WhatsApp just now."

_BACKENDS = ("playwright", "keyboard")
# A Playwright send has no fixed wait to schedule around; this is only the
# floor for "too soon to schedule".
_PLAYWRIGHT_MIN_LEAD_S = 5.0
# On top of the send timeout: a cold browser launch plus WhatsApp Web's boot.
_PLAYWRIGHT_LAUNCH_MARGIN_S = 25.0

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


def _is_quoted_printable(line: str) -> bool:
    """Whether a property line's PARAMETERS (before the first ':') say QUOTED-PRINTABLE."""
    return "QUOTED-PRINTABLE" in line.split(":", 1)[0].upper()


def _join_qp_soft_breaks(lines: list[str]) -> list[str]:
    """
    vCard 2.1 quoted-printable wraps a long value with a trailing '=' and
    continues on the next line with NO leading space, so _unfold_vcf cannot see
    it. Only lines whose own parameters say QUOTED-PRINTABLE are joined: a
    base64 PHOTO line can end in '=' padding too, and joining that one would
    swallow the next property.
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_quoted_printable(line):
            while line.endswith("=") and i + 1 < len(lines):
                i += 1
                line = line[:-1] + lines[i].strip()
        out.append(line)
        i += 1
    return out


def _prop_value(line: str) -> str:
    """
    A property's value, decoded.

    Android's vCard 2.1 export stores any name that is not plain ASCII -- an
    accent ("João") or an emoji ("ana 🫰🏽") -- as
    `FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=6D=61...`. Read raw, those
    names are a run of =XX codes that no spoken name can ever match: 41 cards
    in the real book (2026-09-23). One of them shared a first name with
    another contact, so "message <first name>" saw only one candidate and sent
    straight to the other person instead of asking which.
    """
    params, _, value = line.partition(":")
    if not _is_quoted_printable(line):
        return value.strip()
    charset = "utf-8"
    m = re.search(r"CHARSET=([\w-]+)", params, flags=re.I)
    if m:
        charset = m.group(1)
    raw = quopri.decodestring(value.encode("ascii", errors="ignore"))
    try:
        return raw.decode(charset, errors="replace").strip()
    except LookupError:  # an unknown charset name
        return raw.decode("utf-8", errors="replace").strip()


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

        for line in _join_qp_soft_breaks([l.strip() for l in block.splitlines()]):
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("FN"):
                fn = _prop_value(line)
            elif upper.startswith("ORG"):
                org = _prop_value(line)
            elif upper.startswith("N:") or upper.startswith("N;"):
                raw = _prop_value(line)
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
        elif name.startswith(q + " ") or q.startswith(name + " "):
            # A prefix only counts at a word boundary. Unbounded, a surname was a
            # prefix-match (90, certain) for a contact named just "Z", and
            # "mar" was certain for any full name starting "Mar" (2026-09-23).
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


def load_alias_config(cfg: dict) -> dict:
    """
    The raw alias map: `whatsapp.aliases` from config.yaml, then the local file
    at `whatsapp.aliases_path` merged over it.

    The real nicknames live in that file, gitignored, because they are a map of
    who Master Miguel's family, partner and friends are and the repository is
    public -- the same reason contacts.vcf is gitignored. config.yaml keeps only
    a made-up example. LLMService calls this too, so the nicknames the prompt
    advertises are exactly the ones resolve_contact accepts.

    Never raises: a missing or broken file is a warning and the config aliases.
    """
    merged = dict(cfg.get("aliases") or {})
    path = Path(str(cfg.get("aliases_path") or "").strip() or "whatsapp_aliases.yaml")
    if not path.is_file():
        return merged
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a typo in a local file must not take the boot down
        logger.warning("Could not read WhatsApp aliases from %s, ignoring it", path, exc_info=True)
        return merged
    if not isinstance(data, dict):
        logger.warning("%s should be a mapping of nickname -> contact, ignoring it", path)
        return merged
    merged.update(data)
    return merged


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

        self.backend = str(cfg.get("backend") or "keyboard").strip().lower()
        if self.backend not in _BACKENDS:
            logger.warning(
                "Unknown whatsapp.backend %r, expected one of %s; using keyboard",
                self.backend,
                ", ".join(_BACKENDS),
            )
            self.backend = "keyboard"

        self.confirm_timeout_s = max(
            10.0, _as_int(cfg.get("confirm_timeout_ms"), 120000) / 1000
        )
        self.keep_warm = bool(cfg.get("keep_warm", True))
        # 0 means never: the laptop has the memory, the Pi may not.
        self.idle_close_s = max(0.0, _as_int(cfg.get("idle_close_minutes"), 30) * 60.0)

        self._driver = None
        if self.backend == "playwright":
            from services.whatsapp_web import WhatsAppWebDriver

            # Constructing it launches nothing; open_page() does, on the worker.
            self._driver = WhatsAppWebDriver.from_config(cfg)
            self.wait_s = _PLAYWRIGHT_MIN_LEAD_S
            self._wait_timeout_s = self._driver.timeout_s + _PLAYWRIGHT_LAUNCH_MARGIN_S
        else:
            # Floor the SECONDS, not the milliseconds -- the same trap called out
            # in deebot_service: max(6, ms) / 1000 reads like a 6s minimum and is
            # a 6ms one, so wait_ms: 3 would fire Enter into a blank page.
            self.wait_s = max(_MIN_WAIT_S, _as_int(cfg.get("wait_ms"), 12000) / 1000)
            # The browser launch and the focus dance sit on top of the page wait.
            self._wait_timeout_s = self.wait_s + _SEND_MARGIN_S

        self.aliases, self.alias_prefer = self._build_aliases(load_alias_config(cfg))

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
        # The one thread every send runs on. Started lazily by _submit, so
        # constructing the service (and every unit test) starts nothing.
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

        if self.enabled:
            logger.info(
                "WhatsApp initialized (%s backend): %d contact(s), %d alias(es)",
                self.backend,
                len(self.contacts),
                len(self.aliases),
            )

    def start(self) -> None:
        """
        Warm the browser in the background so the first send skips the launch.

        Deliberately not done in __init__: the orchestrator calls this once at
        boot, and a unit test that only constructs the service must never start
        a browser. Non-blocking -- the warm-up is a job on the worker thread,
        and a send that arrives meanwhile simply queues behind it.
        """
        if not (self.enabled and self._driver is not None and self.keep_warm):
            return

        def _warm():
            try:
                state = self._driver.warm()
            except Exception:  # noqa: BLE001 - a failed warm-up costs the first send a launch, nothing more
                logger.warning("WhatsApp Web warm-up failed", exc_info=True)
                self._driver.close()
                return None
            if state == "needs_link":
                logger.warning(
                    "WhatsApp Web is not linked in %s. Run: uv run python tools/link_whatsapp.py",
                    self._driver.profile_dir,
                )
            else:
                logger.info("WhatsApp Web warm: %s", state)
            return state

        self._submit(_warm)

    # ------------------------------------------------------------ self-disable

    def _probe_platform(self) -> bool:
        """
        Whether this machine can actually drive WhatsApp Web.

        Never raises. Each failing factor logs its own line naming the fix,
        because "wrong OS", "missing package" and "Firefox is somewhere else"
        are three different problems with three different answers.
        """
        if self.backend == "playwright":
            return self._probe_playwright()

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

    def _probe_playwright(self) -> bool:
        """
        The Playwright backend needs the package and nothing else to be offered.

        An unlinked profile does NOT disable the tool: disabling would hide
        WhatsApp from the model entirely, and "it's logged out, run the link
        tool" is far more useful to hear than silence. The first send reports
        it through _MSG_NEEDS_LINK; this only warns at boot.
        """
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:  # noqa: BLE001
            logger.warning(
                "playwright is not installed, WhatsApp control disabled. "
                "Install it with: uv sync --extra default"
            )
            return False
        if not self._driver.profile_dir.is_dir():
            logger.warning(
                "WhatsApp profile %s does not exist yet. Link it with: "
                "uv run python tools/link_whatsapp.py",
                self._driver.profile_dir,
            )
        return True

    # --------------------------------------------------------------- contacts

    @staticmethod
    def _build_aliases(raw: dict) -> tuple[dict, dict]:
        """
        Spoken nickname -> the exact contact name it means, plus nickname ->
        preferred number prefix. Blank entries skipped.

        A value is either the contact name, or a mapping
        `{contact: "Maria Silva", prefer: "+351"}` for a contact with several numbers
        where the one he messages is not the one the card ranks first. `prefer`
        is a number prefix, which keeps the number itself out of config.yaml.
        """
        aliases, prefer = {}, {}
        for key, value in (raw or {}).items():
            spoken = str(key or "").strip()
            wanted = ""
            if isinstance(value, dict):
                wanted = str(value.get("prefer") or "").strip()
                value = value.get("contact")
            target = str(value or "").strip()
            if not spoken or not target:
                logger.warning("WhatsApp alias %r has no target, skipping", key)
                continue
            aliases[spoken] = target
            if wanted:
                prefer[spoken] = wanted
        return aliases, prefer

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

        book = self._classify(score_contacts(cleaned, self.contacts))

        # Aliases go through the shared matcher, whose substring tier is right
        # for six light rooms and dangerous here: with an `ana` alias for one
        # Ana, both "Ana Costa" and "Rui Pai Ana" (someone merely TAGGED with
        # the name, as phone books do) matched it, so a message meant for them
        # would have gone to the aliased Ana (measured on the real book
        # 2026-09-23, before this ordering). So:
        #   1. an EXACT alias ("ana", "gf") wins, even over a tie in the book
        #      -- that is what the alias is for;
        #   2. otherwise a certain book match wins ("Ana Costa" is Ana Costa);
        #   3. only then may a loose alias match ("the mum") apply.
        if self.aliases:
            spoken = normalize_text(cleaned)
            exact_alias = next(
                (key for key in self.aliases if normalize_text(key) == spoken), None
            )
            if exact_alias is not None:
                hit = self._alias_match(exact_alias)
                if hit:
                    return hit
            if not book.certain:
                loose = match_name(cleaned, {key: [key] for key in self.aliases})
                if loose:
                    hit = self._alias_match(loose)
                    if hit:
                        return hit

        return book

    def _alias_match(self, alias: str) -> ContactMatch | None:
        """The contact an alias points at, or None (logged) when it points nowhere."""
        target = self.aliases[alias]
        exact = [c for c in self.contacts if c.name.lower() == target.lower()]
        if exact and exact[0].phone:
            contact = exact[0]
            phone = contact.phone
            wanted = self.alias_prefer.get(alias, "")
            if wanted:
                picked = next((p for p in contact.phones if p.startswith(wanted)), None)
                if picked:
                    phone = picked
                else:
                    # Say so, but still reach her on the number she has.
                    logger.warning(
                        "WhatsApp alias %r prefers %s but %r has no such number; using %s",
                        alias, wanted, contact.name, phone,
                    )
            return ContactMatch(key=contact.name, phone=phone, certain=True)
        if looks_like_phone(target):
            phone = normalize_phone(target, self.default_country)
            if phone:
                return ContactMatch(key=alias, phone=phone, certain=True)
        logger.warning(
            "WhatsApp alias %r points at %r, which is not in the contact book", alias, target
        )
        return None

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
            # Only the contenders, not everyone the query touched: for "Ana"
            # the tie is "Ana Costa" vs "ana 🫰🏽" at 90, and the 80s below
            # them ("Rui Pai Ana") are other people tagged with
            # her name. Offering those in "which one?" is noise.
            floor = best_score - _AMBIGUOUS_MARGIN
            return ContactMatch(
                candidates=[c.name for s, c in scored if c.phone and s >= floor][:5]
            )

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
            worker = self._worker
        for timer in timers:
            timer.cancel()
        # The browser can only be closed from the thread that opened it, so
        # shutdown is a job too: the worker closes the driver and exits.
        if worker is not None and worker.is_alive():
            self._jobs.put(None)

    # ----------------------------------------------------------------- worker

    def _submit(self, work) -> Future:
        """Queue `work` on the single worker thread, starting it if needed."""
        future: Future = Future()
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="whatsapp", daemon=True
                )
                self._worker.start()
        self._jobs.put((work, future))
        return future

    def _worker_loop(self) -> None:
        """
        Run jobs forever on this one thread; close an idle browser.

        The idle close is here rather than on a timer because only this thread
        may touch the driver. A `get` that times out with the browser open is
        the idle signal.
        """
        while True:
            driver_open = self._driver is not None and self._driver.is_open
            timeout = self.idle_close_s if (driver_open and self.idle_close_s) else None
            try:
                item = self._jobs.get(timeout=timeout)
            except queue.Empty:
                logger.info("WhatsApp Web idle for %.0f min, closing the browser", self.idle_close_s / 60)
                self._driver.close()
                continue
            if item is None:
                if self._driver is not None:
                    self._driver.close()
                return
            work, future = item
            try:
                future.set_result(work())
            except BaseException as exc:  # noqa: BLE001 - handed to the caller in _run
                future.set_exception(exc)

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

        def _work():
            try:
                return work()
            finally:
                with self._lock:
                    self._busy = False

        future = self._submit(_work)
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
        """Send through whichever backend is configured. Runs on the worker."""
        if self.backend == "playwright":
            return self._send_playwright(phone, message)
        return self._send_keyboard(phone, message)

    def _send_playwright(self, phone: str, message: str) -> WhatsAppCommandResult:
        from services import whatsapp_web as web

        try:
            outcome = self._driver.send(phone, message)
        except Exception:  # noqa: BLE001 - a crashed browser is relaunched by the next send
            logger.exception("WhatsApp Web send to %s failed", phone)
            self._driver.close()
            return WhatsAppCommandResult(False, _MSG_UNREACHABLE)

        if outcome.status == web.SENT:
            logger.info("WhatsApp message sent to %s (tick seen)", phone)
            return WhatsAppCommandResult(True)
        logger.warning("WhatsApp send to %s: %s %s", phone, outcome.status, outcome.detail)
        return WhatsAppCommandResult(
            False,
            {
                web.NOT_LINKED: _MSG_NEEDS_LINK,
                web.INVALID_NUMBER: _MSG_INVALID_NUMBER,
                web.UNCONFIRMED: _MSG_UNCONFIRMED,
            }.get(outcome.status, _MSG_UNREACHABLE),
        )

    def _send_keyboard(self, phone: str, message: str) -> WhatsAppCommandResult:
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

    # ------------------------------------------------------------------ reading

    def unread(self):
        """
        What is unread, off the chat list. Returns the driver's UnreadResult on
        success and a falsy WhatsAppCommandResult (with its spoken line) on any
        failure. Never opens a chat, so nothing is marked read.

        Playwright only: the keyboard backend drives a browser it cannot see.
        """
        if not self.enabled:
            return WhatsAppCommandResult(False, _MSG_NOT_CONFIGURED)
        if self.backend != "playwright" or self._driver is None:
            return WhatsAppCommandResult(False, _MSG_READ_NEEDS_PLAYWRIGHT)

        from services import whatsapp_web as web

        def _work():
            try:
                result = self._driver.unread_chats()
            except Exception:  # noqa: BLE001 - a crashed browser is relaunched next time
                logger.exception("WhatsApp unread read failed")
                self._driver.close()
                return WhatsAppCommandResult(False, _MSG_READ_FAILED)
            if result.status == web.NOT_LINKED:
                return WhatsAppCommandResult(False, _MSG_NEEDS_LINK)
            if not result:
                return WhatsAppCommandResult(False, _MSG_READ_FAILED)
            return result

        return self._run(_work)

    def find_unread_chat(self, hint: str, chats: list):
        """
        The unread chat he asked about, or None.

        Chat titles are the phone's contact names, so the same resolution as a
        send applies first -- "mum" is the alias, the alias is a
        contact name, the contact name is the chat title. Only then a direct
        match on the titles, which covers groups and unsaved numbers.
        """
        if not hint or not chats:
            return None
        by_title = {c.name.lower(): c for c in chats}
        match = self.resolve_contact(hint)
        if match.key and match.key.lower() in by_title:
            return by_title[match.key.lower()]
        for name in match.candidates:
            if name.lower() in by_title:
                return by_title[name.lower()]
        title = match_name(hint, {c.name: [c.name] for c in chats})
        return next((c for c in chats if c.name == title), None) if title else None

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
