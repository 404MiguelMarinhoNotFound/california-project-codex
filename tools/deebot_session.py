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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICE_ID_PATH = ROOT / ".deebot_device_id"
CREDENTIALS_PATH = ROOT / ".deebot_credentials.json"


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

    Returns True once authenticated. Returns False and prints instructions
    if a fresh emailed code is needed and none was supplied via env.
    """
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

    await authenticator.request_device_verification_code()
    print("Ecovacs wants this device verified again. Check email, then re-run with")
    print(f"{verification_code_env}=xxxxxx set.")
    return False
