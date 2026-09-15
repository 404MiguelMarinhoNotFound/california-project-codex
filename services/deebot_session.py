"""
EcoVacs auth session helpers shared by DeebotService and the deebot tools.

Every deebot script used to build a brand-new Authenticator with no memory of
the last login, which forces a full password login (the same endpoint Ecovacs
gates with device verification) on every single run. That's the actual reason
verification kept re-triggering constantly rather than roughly every ~7 days
(the real token lifetime). This module persists the device id and the login
token locally so a still-valid token gets reused instead, skipping the gated
endpoint entirely.

When Ecovacs does demand verification again, `authenticate()` reads the emailed
code itself over IMAP (services/gmail_verification_code.py) when
GMAIL_APP_PASSWORD is set, so nobody has to be asked.

Files written under `state_dir` (project root by default; both gitignored, both
sensitive -- the credentials file holds a live session token):
  .deebot_device_id        stable device id, must not change across runs
  .deebot_credentials.json cached {token, user_id, expires_at}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEVICE_ID_FILE = ".deebot_device_id"
CREDENTIALS_FILE = ".deebot_credentials.json"

# Reasons returned by authenticate(), so callers can log which path fired.
REASON_CACHED = "cached"
REASON_PASSWORD = "password"
REASON_VERIFIED_ENV = "verified_via_env"
REASON_VERIFIED_GMAIL = "verified_via_gmail"
REASON_NEEDS_CODE = "needs_code"
REASON_GMAIL_TIMEOUT = "gmail_timeout"


def _device_id_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / DEVICE_ID_FILE


def _credentials_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / CREDENTIALS_FILE


def stable_device_id(state_dir: Path | str = ROOT) -> str:
    from deebot_client.util import md5

    path = _device_id_path(state_dir)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    device_id = md5(os.urandom(16).hex())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device_id, encoding="utf-8")
    return device_id


def load_cached_credentials(state_dir: Path | str = ROOT):
    from deebot_client.models import Credentials

    path = _credentials_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Credentials(
            token=data["token"],
            user_id=data["user_id"],
            expires_at=data["expires_at"],
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def clear_cached_credentials(state_dir: Path | str = ROOT) -> None:
    """Drop the cached token so the next authenticate() starts over."""
    path = _credentials_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _save_credentials(credentials, state_dir: Path | str) -> None:
    path = _credentials_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "token": credentials.token,
                "user_id": credentials.user_id,
                "expires_at": credentials.expires_at,
            }
        ),
        encoding="utf-8",
    )


async def build_authenticator(
    session,
    *,
    device_id: str,
    country: str,
    email: str,
    password: str,
    state_dir: Path | str = ROOT,
):
    """Build an Authenticator wired to persist/reuse credentials across runs."""
    from deebot_client.authentication import Authenticator, create_rest_config
    from deebot_client.util import md5

    rest_config = create_rest_config(session, device_id=device_id, alpha_2_country=country)
    authenticator = Authenticator(rest_config, email, md5(password))

    async def _on_credentials_changed(credentials) -> None:
        _save_credentials(credentials, state_dir)

    authenticator.subscribe(_on_credentials_changed)

    cached = load_cached_credentials(state_dir)
    if cached is not None:
        # Preload so authenticate() finds a still-valid token and skips the
        # gated password-login endpoint entirely. There's no public setter
        # for this -- _set_credentials is the only way in, and it also arms
        # the library's own refresh timer against the real expiry.
        authenticator._set_credentials(cached)  # noqa: SLF001

    return authenticator


async def authenticate(
    authenticator,
    *,
    verification_code_env: str = "ECOVACS_VERIFICATION_CODE",
    gmail_address: str | None = None,
    gmail_app_password: str | None = None,
    verification_email_timeout_s: int = 90,
) -> tuple[bool, str]:
    """Authenticate, handling device verification if it's (still) required.

    Order when a code is needed:
      1. `verification_code_env`, if set (a human supplied it)
      2. Gmail auto-read, if `gmail_address` + `gmail_app_password` are given
         (default: ECOVACS_EMAIL + GMAIL_APP_PASSWORD from the environment):
         request a fresh code, poll that inbox over IMAP, extract it
      3. otherwise request a code and report that one is needed

    Returns (ok, reason). `reason` is one of the REASON_* constants.
    """
    import asyncio

    from deebot_client.exceptions import DeviceVerificationRequiredError

    had_cached = authenticator._credentials is not None  # noqa: SLF001
    try:
        await authenticator.authenticate()
        return True, REASON_CACHED if had_cached else REASON_PASSWORD
    except DeviceVerificationRequiredError:
        pass

    code = os.getenv(verification_code_env)
    if code:
        logger.info("Deebot: verifying device with %s", verification_code_env)
        await authenticator.verify_device(code.strip())
        return True, REASON_VERIFIED_ENV

    gmail_address = gmail_address or os.getenv("ECOVACS_EMAIL")
    gmail_app_password = gmail_app_password or os.getenv("GMAIL_APP_PASSWORD")
    if gmail_address and gmail_app_password:
        from services import gmail_verification_code as gmail

        logger.info("Deebot: device verification required, reading the code from Gmail")
        since_uid = await asyncio.to_thread(gmail.latest_uid, gmail_address, gmail_app_password)
        await authenticator.request_device_verification_code()
        code = await asyncio.to_thread(
            gmail.fetch_new_code,
            gmail_address,
            gmail_app_password,
            since_uid=since_uid,
            timeout_s=verification_email_timeout_s,
        )
        if code:
            await authenticator.verify_device(code)
            logger.info("Deebot: verified via Gmail")
            return True, REASON_VERIFIED_GMAIL
        logger.warning(
            "Deebot: no verification email arrived within %ss", verification_email_timeout_s
        )
        return False, REASON_GMAIL_TIMEOUT

    await authenticator.request_device_verification_code()
    logger.warning(
        "Deebot: device verification required. Check email, then re-run with %s=xxxxxx set.",
        verification_code_env,
    )
    return False, REASON_NEEDS_CODE
