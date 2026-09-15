"""
Reads the Ecovacs device-verification email straight out of Gmail over IMAP,
so tools/deebot_session.py can complete verification with zero manual steps.

Standalone: no MCP, no Claude tool access, just imaplib + email from the
standard library. Runs entirely on this machine.

Needs a Gmail **App Password** (not the account password) in
GMAIL_APP_PASSWORD -- generate one at
https://myaccount.google.com/apppasswords, which needs 2-Step Verification
turned on first. An app password doesn't expire on its own (unlike an OAuth
refresh token for an unverified/testing app, which Google caps at 7 days --
exactly the problem this exists to avoid), so this is a one-time setup.
"""

from __future__ import annotations

import email
import imaplib
import re
import time

IMAP_HOST = "imap.gmail.com"
SENDER = "noreply@support.ecovacs.com"
# Real email body (captured 2026-09-16): "...Your verification code is
# &nbsp;&nbsp;045689&nbsp;.&nbsp;This code will expire in 24 hours..."
CODE_RE = re.compile(r"verification code is\D*(\d{6})", re.IGNORECASE)


def _connect(gmail_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(gmail_address, app_password.replace(" ", ""))
    imap.select("INBOX")
    return imap




def _sender_uids(imap: imaplib.IMAP4_SSL) -> list[int]:
    status, data = imap.uid("search", None, f'(FROM "{SENDER}")')
    if status != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


def _extract_code(imap: imaplib.IMAP4_SSL, uid: int) -> str | None:
    status, msg_data = imap.uid("fetch", str(uid).encode(), "(RFC822)")
    if status != "OK" or not msg_data or not msg_data[0]:
        return None
    msg = email.message_from_bytes(msg_data[0][1])
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")
    match = CODE_RE.search(body)
    return match.group(1) if match else None


def latest_uid(gmail_address: str, app_password: str) -> int:
    """Highest UID of an existing Ecovacs verification email right now.

    Call this BEFORE requesting a new code, then pass the result as
    `since_uid` to `fetch_new_code` so a stale email already in the inbox
    can never be mistaken for the fresh one.
    """
    imap = _connect(gmail_address, app_password)
    try:
        uids = _sender_uids(imap)
        return max(uids) if uids else 0
    finally:
        imap.logout()


def fetch_new_code(
    gmail_address: str,
    app_password: str,
    *,
    since_uid: int,
    timeout_s: int = 90,
    poll_interval_s: int = 3,
) -> str | None:
    """Poll the inbox for a verification email newer than `since_uid`.

    Returns the 6-digit code, or None if nothing arrived within timeout_s.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        imap = _connect(gmail_address, app_password)
        try:
            new_uids = [u for u in _sender_uids(imap) if u > since_uid]
            if new_uids:
                code = _extract_code(imap, max(new_uids))
                if code:
                    return code
        finally:
            imap.logout()
        time.sleep(poll_interval_s)
    return None
