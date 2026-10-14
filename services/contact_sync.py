"""
The phone's own contact book, read over ADB once a week into a local snapshot.

`contacts.vcf` was a hand export that went stale the day it was made. This
reads the Xiaomi's contacts provider read-only (`AdbPhone.query`, a
`content query`, nothing pushed, nothing written on the phone), folds the
per-account rows into one `Contact` per person, and writes
`phone_contacts.json` atomically for `WhatsAppService` to load in place of
the VCF.

Three rules carry the safety of it:

- **Zero contacts is never a book.** A phone with a contact book never
  answers with none, so `[]` is UNREADABLE and the snapshot is left alone.
- **A book that shrank below `min_keep_ratio` of the last one is not
  written** (SHRUNK). A half-synced account or a provider hiccup must not
  quietly delete half his people.
- **Logs and spoken lines carry counts only**, never a name or a number:
  the logs are shared when debugging, and the repository is public.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from services.phone_adb import AdbPhone
from services.whatsapp_service import Contact, normalize_phone

logger = logging.getLogger(__name__)

URI = "content://com.android.contacts/data/phones"
# display_name last: it is the one free-text column, and parse_content_rows
# can only take free text from the last key.
PROJECTION = ["contact_id", "data2", "is_super_primary", "account_type", "data1", "display_name"]

_SNAPSHOT_VERSION = 1
_DEFAULT_COUNTRY = "351"

_MESSAGES = {
    "SYNCED": "Contacts refreshed from your phone: {count}, {added} new, {removed} gone, {changed} changed.",
    "UNCHANGED": "Your contacts were already up to date: {count} from your phone.",
    "NO_PHONE": "I can't reach your phone right now, so I'm keeping the contacts from {age}.",
    "NO_PHONE_NO_BOOK": "I can't reach your phone right now, so I'm keeping the contact book I have.",
    "LOCKED_DOWN": "The phone has USB debugging (Security settings) switched off, so I can't read its contacts.",
    "UNREADABLE": "Your phone wouldn't hand over its contacts just now. I kept the ones I had.",
    "SHRUNK": "Your phone only gave me {count} contacts against {previous} last time, so I kept the old list.",
    "BUSY": "I'm already refreshing your contacts.",
    "DISABLED": "Contact sync isn't set up.",
}


@dataclass
class ContactSyncResult:
    status: str
    count: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    previous: int = 0
    message: str = ""

    def __bool__(self) -> bool:
        return self.status in ("SYNCED", "UNCHANGED")


# --------------------------------------------------------------------- rows


def _rank(row: dict) -> int:
    """Mirrors whatsapp_service._tel_priority: PREF 3, CELL 2, HOME 1."""
    score = 0
    if row.get("is_super_primary") == "1":
        score += 3
    if row.get("data2") == "2":  # Phone.TYPE_MOBILE
        score += 2
    if row.get("data2") == "1":  # Phone.TYPE_HOME
        score += 1
    return score


def contacts_from_rows(rows: list[dict], default_cc: str) -> list[Contact]:
    """
    One Contact per `contact_id`, in first-seen order.

    The same number appears once per account (Google, WhatsApp, Duo), so the
    numbers are ranked, stable-sorted and deduplicated. Two ids with the same
    name stay two contacts: the "which one?" question depends on it.
    """
    groups: dict[str, dict] = {}
    for r in rows:
        cid = r.get("contact_id")
        if cid is None:
            continue
        g = groups.setdefault(cid, {"name": "", "numbers": []})
        if not g["name"] and (r.get("display_name") or "").strip():
            g["name"] = r["display_name"].strip()
        raw = r.get("data1") or ""
        # A pause (',') or wait (';') starts DTMF digits for an extension.
        for sep in (",", ";"):
            raw = raw.split(sep, 1)[0]
        number = normalize_phone(raw, default_cc)
        if number is not None:
            g["numbers"].append((_rank(r), number))

    contacts: list[Contact] = []
    for g in groups.values():
        ranked = sorted(g["numbers"], key=lambda item: item[0], reverse=True)
        phones = list(dict.fromkeys(number for _, number in ranked))
        if g["name"] and phones:
            contacts.append(Contact(g["name"], phones))
    return contacts


# --------------------------------------------------------------------- snapshot


def load_snapshot(path: Path) -> tuple[list[Contact], datetime | None]:
    """The saved book and when it was synced; `([], None)` when missing or corrupt."""
    path = Path(path)
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _SNAPSHOT_VERSION:
            raise ValueError(f"unknown snapshot version {data.get('version')!r}")
        synced_at = datetime.fromisoformat(data["synced_at"])
        contacts = [Contact(str(c["name"]), [str(p) for p in c["phones"]]) for c in data["contacts"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("Contact snapshot %s is unreadable (%s); a sync is due", path, type(exc).__name__)
        return [], None
    return contacts, synced_at


def write_snapshot(path: Path, contacts: list[Contact], synced_at: datetime) -> None:
    """Writes `<path>.tmp` then replaces, so a crash never leaves half a book."""
    path = Path(path)
    data = {
        "version": _SNAPSHOT_VERSION,
        "synced_at": synced_at.isoformat(timespec="seconds"),
        "source": "adb",
        "contacts": [{"name": c.name, "phones": list(c.phones)} for c in contacts],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def diff_counts(old: list[Contact], new: list[Contact]) -> tuple[int, int, int]:
    """(added, removed, changed), keyed by casefolded name; same-name contacts form a multiset."""

    def keyed(contacts: list[Contact]) -> dict[str, list[tuple[str, ...]]]:
        out: dict[str, list[tuple[str, ...]]] = {}
        for c in contacts:
            out.setdefault(c.name.casefold(), []).append(tuple(c.phones))
        return {k: sorted(v) for k, v in out.items()}

    a, b = keyed(old), keyed(new)
    added = sum(1 for k in b if k not in a)
    removed = sum(1 for k in a if k not in b)
    changed = sum(1 for k in a if k in b and a[k] != b[k])
    return added, removed, changed


# --------------------------------------------------------------------- service


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _age_words(age_s: float) -> str:
    days = int(age_s // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


class ContactSync:
    def __init__(
        self,
        config: dict,
        phone=None,
        on_synced: Callable[[], object] | None = None,
        now: Callable[[], datetime] = _local_now,
    ):
        config = config or {}
        self._config = config
        cfg = config.get("contacts_sync", {}) or {}
        self._phone = phone
        self._on_synced = on_synced
        self._now = now
        self._lock = threading.Lock()

        self.snapshot_path = Path(str(cfg.get("snapshot_path") or "phone_contacts.json"))
        days = cfg.get("interval_days")
        self.interval_days = max(1, int(7 if days is None else days))
        self.check_interval_minutes = int(cfg.get("check_interval_minutes") or 360)
        self.min_keep_ratio = float(cfg.get("min_keep_ratio") if cfg.get("min_keep_ratio") is not None else 0.5)
        wa_cfg = config.get("whatsapp", {}) or {}
        phone_cfg = config.get("phone", {}) or {}
        self.default_cc = str(wa_cfg.get("default_country") or _DEFAULT_COUNTRY)

        adb_path = str((phone_cfg.get("adb", {}) or {}).get("path") or "adb")
        self.enabled = False
        if cfg.get("enabled") is not True:
            logger.info("Contact sync off (contacts_sync.enabled)")
        elif sys.platform != "win32":
            logger.info("Contact sync off: Windows only for now")
        elif not (shutil.which(adb_path) or os.path.isfile(adb_path)):
            logger.info("Contact sync off: adb not found at %r (phone.adb.path)", adb_path)
        elif not (wa_cfg.get("enabled") is True or phone_cfg.get("enabled") is True):
            logger.info("Contact sync off: neither WhatsApp nor phone is enabled, nothing would read it")
        else:
            self.enabled = True

    # ------------------------------------------------------------- timing

    def age_s(self) -> float | None:
        _, synced_at = load_snapshot(self.snapshot_path)
        if synced_at is None:
            return None
        return (self._now() - synced_at).total_seconds()

    def due(self) -> bool:
        if not self.enabled:
            return False
        age = self.age_s()
        return age is None or age >= self.interval_days * 86400

    # ------------------------------------------------------------- sync

    def _result(self, status: str, message_key: str = "", age: str = "", **counts) -> ContactSyncResult:
        result = ContactSyncResult(status, **counts)
        result.message = _MESSAGES[message_key or status].format(age=age, **counts)
        return result

    def sync(self) -> ContactSyncResult:
        if not self.enabled:
            return self._result("DISABLED")
        if not self._lock.acquire(blocking=False):
            return self._result("BUSY")
        try:
            return self._sync()
        finally:
            self._lock.release()

    def _sync(self) -> ContactSyncResult:
        started = time.monotonic()
        if self._phone is None:
            self._phone = AdbPhone.from_config(self._config)
        rows = self._phone.query(URI, PROJECTION)
        if rows is None:
            status = getattr(self._phone, "last_status", "")
            if status == "NO_PHONE":
                age = self.age_s()
                if age is None:
                    return self._result("NO_PHONE", message_key="NO_PHONE_NO_BOOK")
                return self._result("NO_PHONE", age=_age_words(age))
            if status == "LOCKED_DOWN":
                return self._result("LOCKED_DOWN")
            logger.info("Contact sync: phone unreadable (%s)", status or "no status")
            return self._result("UNREADABLE")

        contacts = contacts_from_rows(rows, self.default_cc)
        if not contacts:
            logger.info("Contact sync: %d row(s) but no contacts; keeping the snapshot", len(rows))
            return self._result("UNREADABLE")

        old, _ = load_snapshot(self.snapshot_path)
        count, previous = len(contacts), len(old)
        if previous > 0 and count < previous * self.min_keep_ratio:
            logger.warning("Contact sync: phone gave %d contacts against %d last time; not written", count, previous)
            return self._result("SHRUNK", count=count, previous=previous)

        added, removed, changed = diff_counts(old, contacts)
        write_snapshot(self.snapshot_path, contacts, self._now())
        if self._on_synced is not None:
            try:
                self._on_synced()
            except Exception as exc:  # a reload failure must not fail the sync
                logger.warning("Contact sync: on_synced failed: %s", type(exc).__name__)
        logger.info(
            "Contacts synced from phone: %d contacts (+%d -%d ~%d) in %.1fs",
            count, added, removed, changed, time.monotonic() - started,
        )
        status = "UNCHANGED" if (added, removed, changed) == (0, 0, 0) else "SYNCED"
        return self._result(status, count=count, added=added, removed=removed, changed=changed, previous=previous)
