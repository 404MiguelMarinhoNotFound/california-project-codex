"""
Shared EcoVacs auth session helpers.

Every deebot script here used to build a brand-new Authenticator with no
memory of the last login, which forces a full password login (the same
endpoint Ecovacs gates with device verification) on every single run.
That's the actual reason verification kept re-triggering constantly rather
than roughly every ~7 days (the real token lifetime). This module persists
the device id and the login token/credentials locally so a still-valid
token gets reused instead, skipping the gated endpoint entirely.

Files written (both gitignored, both sensitive -- the credentials file
holds a live session token):
  .deebot_device_id        stable device id, must not change across runs
  .deebot_credentials.json cached {token, user_id, expires_at}
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICE_ID_PATH = ROOT / ".deebot_device_id"
CREDENTIALS_PATH = ROOT / ".deebot_credentials.json"

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def stable_device_id() -> str:
    from deebot_client.util import md5

    if DEVICE_ID_PATH.exists():
        return DEVICE_ID_PATH.read_text(encoding="utf-8").strip()
    device_id = md5(os.urandom(16).hex())
    DEVICE_ID_PATH.write_text(device_id, encoding="utf-8")
    return device_id


def load_cached_credentials():
    from deebot_client.models import Credentials

    if not CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        return Credentials(
            token=data["token"],
            user_id=data["user_id"],
            expires_at=data["expires_at"],
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _save_credentials(credentials) -> None:
    CREDENTIALS_PATH.write_text(
        json.dumps(
            {
                "token": credentials.token,
                "user_id": credentials.user_id,
                "expires_at": credentials.expires_at,
            }
        ),
        encoding="utf-8",
    )


async def build_authenticator(session, *, device_id: str, country: str, email: str, password: str):
    """Build an Authenticator wired to persist/reuse credentials across runs."""
    from deebot_client.authentication import Authenticator, create_rest_config
    from deebot_client.util import md5

    rest_config = create_rest_config(session, device_id=device_id, alpha_2_country=country)
    authenticator = Authenticator(rest_config, email, md5(password))

    async def _on_credentials_changed(credentials) -> None:
        _save_credentials(credentials)

    authenticator.subscribe(_on_credentials_changed)

    cached = load_cached_credentials()
    if cached is not None:
        # Preload so authenticate() finds a still-valid token and skips the
        # gated password-login endpoint entirely. There's no public setter
        # for this -- _set_credentials is the only way in, and it also arms
        # the library's own refresh timer against the real expiry.
        authenticator._set_credentials(cached)  # noqa: SLF001

    return authenticator


async def authenticate(authenticator, *, verification_code_env: str = "ECOVACS_VERIFICATION_CODE") -> bool:
    """Authenticate, handling device verification if it's (still) required.

    Verification order when a code is needed:
      1. verification_code_env, if already set (e.g. supplied by a human)
      2. GMAIL_APP_PASSWORD auto-read (if ECOVACS_EMAIL/GMAIL_APP_PASSWORD are
         set) -- requests a fresh code, then polls that inbox over IMAP and
         extracts it, no human involved
      3. otherwise, request a code and tell the caller to supply one

    Returns True once authenticated, False if a code was needed and neither
    (1) nor (2) could produce one.
    """
    import asyncio

    from deebot_client.exceptions import DeviceVerificationRequiredError

    try:
        await authenticator.authenticate()
        return True
    except DeviceVerificationRequiredError:
        pass

    code = os.getenv(verification_code_env)
    if code:
        print(f"Verifying with {verification_code_env}...")
        await authenticator.verify_device(code.strip())
        return True

    gmail_address = os.getenv("ECOVACS_EMAIL")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    if gmail_address and app_password:
        import gmail_verification_code as gmail

        print("Device verification required. Reading the code from Gmail...")
        since_uid = await asyncio.to_thread(gmail.latest_uid, gmail_address, app_password)
        await authenticator.request_device_verification_code()
        code = await asyncio.to_thread(
            gmail.fetch_new_code, gmail_address, app_password, since_uid=since_uid
        )
        if code:
            print(f"Found code {code} in Gmail, verifying...")
            await authenticator.verify_device(code)
            return True
        print("No verification email arrived within the timeout. Try again, or")
        print(f"check {gmail_address} manually and set {verification_code_env}.")
        return False

    await authenticator.request_device_verification_code()
    print("Ecovacs wants this device verified again. Check email, then re-run with")
    print(f"{verification_code_env}=xxxxxx set.")
    return False
