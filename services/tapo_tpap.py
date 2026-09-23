"""
TPAP: the local protocol Tapo firmware 1.4.2+ speaks, which python-kasa cannot.

**Why this module exists.** The living-room L530E(EU) on firmware
`1.4.2 Build 260113` answers discovery with
`encrypt_type='TPAP', http_port=80, lv=2`, and python-kasa 0.10.2 -- the
newest release -- raises `UnsupportedDeviceError` on it. Upstream has an open
issue (python-kasa#1590) and two unmerged PRs, both stuck on a handshake that
their authors believed needed cloud-issued certificates. It does not, at least
not for `tls: 0, dac: 0` bulbs like this one: KasaTapoClient (.NET, MIT,
github.com/oznetmaster/KasaTapoClient) has it working locally with the L530E
on its confirmed list, and this is a port of its `TpapTransport`. Verified
against the real bulb 2026-09-17: handshake, read, brightness, colour.

**What the protocol is.** Three plain-JSON POSTs to `http://<host>/`, then an
encrypted channel:

1. `login/discover` -- unauthenticated. Returns the MAC and a `tpap` block
   saying which PAKE mode the device wants (`pake: [2]` = account password).
2. `login/pake_register` -- we send a random, the device answers with its
   SPAKE2+ share, a PBKDF2 salt and iteration count, and `extra_crypt`
   saying how to pre-hash the password (`password_shadow` / `passwd_id: 2`
   on this bulb = SHA-1 hex of the password).
3. `login/pake_share` -- SPAKE2+ (RFC 9383 shape, P-256, the M/N points
   below) with the PBKDF2-derived w0/w1. We send our share and a
   confirmation MAC; the device answers with its own, which we check, plus a
   session token and a starting sequence number.
4. `POST /stok=<token>/ds` with `[seq:4 BE][AES-128-CCM(nonce=base[:8]+seq)]`
   carrying the ordinary smart-protocol JSON (`get_device_info`,
   `set_device_info`) -- the same commands python-kasa would send over KLAP.

The EC arithmetic is a plain affine P-256 in Python rather than a dependency:
five scalar multiplications per handshake, ~0.2s total on the laptop, and
`cryptography` deliberately exposes no point arithmetic to build on. Every
constant here is copied from the .NET port and must not be "tidied": the
`PAKE V1` context tag, the eight-byte little-endian length prefixes in the
transcript, the sign-byte encoding of w, and the HKDF salt/info strings for
the session key and nonce are all what the firmware computes on its side.

**Sessions are cached per host.** The reason `TapoTransport` reconnects per
command -- a kasa `Device` is bound to the event loop that made it -- does not
apply here: this is synchronous `requests` with no loop involved. A session
dies on the device's side eventually (`expired` comes back in `pake_share`,
and a dead one answers `/ds` with plain JSON instead of ciphertext); that is
raised as a retryable `TpapError`, the caller drops the session and the next
attempt handshakes afresh.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
import uuid

import requests

logger = logging.getLogger(__name__)

# --- P-256 (secp256r1) -------------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)

# The SPAKE2+ M and N points for suite 1 (P-256 / SHA-256 / HMAC), as the
# firmware uses them. These are the RFC 9383 P-256 constants.
_M_ENCODED = bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f")
_N_ENCODED = bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49")

_PAKE_CONTEXT_TAG = b"PAKE V1"
_TAG_LENGTH = 16
_NONCE_LENGTH = 12

# Error codes off the .NET port. Authentication ones invalidate the session
# and are not retried; retryable ones mean "handshake again and resend".
_AUTH_ERRORS = {-1501, -2202, -2203, -2101}
_RETRYABLE_ERRORS = {9999, 1002, -40401, -40413}


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        lam = (3 * p[0] * p[0] + _A) * _inv(2 * p[1]) % _P
    else:
        lam = (q[1] - p[1]) * _inv(q[0] - p[0]) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return (x, (lam * (p[0] - x) - p[1]) % _P)


def _mul(k: int, p):
    result = None
    while k:
        if k & 1:
            result = _add(result, p)
        p = _add(p, p)
        k >>= 1
    return result


def _neg(p):
    return (p[0], (-p[1]) % _P)


def _encode_point(p) -> bytes:
    return b"\x04" + p[0].to_bytes(32, "big") + p[1].to_bytes(32, "big")


def _decode_point(raw: bytes):
    if raw[0] == 0x04:
        return (int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:65], "big"))
    if raw[0] not in (0x02, 0x03):
        raise ValueError("unsupported EC point encoding")
    x = int.from_bytes(raw[1:33], "big")
    y_sq = (x * x * x + _A * x + _B) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if (y & 1) != (raw[0] & 1):
        y = _P - y
    return (x, y)


# --- KDFs and encodings ------------------------------------------------------


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _len8(value: bytes) -> bytes:
    return struct.pack("<Q", len(value)) + value


def _encode_w(value: int) -> bytes:
    """
    w as the firmware hashes it: unsigned big-endian with a sign byte.

    The .NET port's `EncodeW`: when the unsigned encoding has an odd length
    and its top bit is set, a zero byte is prepended; otherwise it is used as
    is. Not "pad to even length" -- `0x01` stays one byte.
    """
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") or b"\0"
    if len(raw) % 2 == 0:
        return raw
    return (b"\0" + raw) if raw[0] & 0x80 else raw


def _credential_string(extra_crypt, username: str, password: str, mac: str) -> str:
    """
    The password as the device wants it pre-hashed, from `extra_crypt`.

    Only `password_shadow`/`passwd_id: 2` (SHA-1 hex) has been seen on a real
    bulb; the other branches are carried over from the .NET port so a bulb
    that asks differently is not dead on arrival.
    """
    if not extra_crypt:
        return f"{username}/{password}" if username else password
    kind = str(extra_crypt.get("type") or "").lower()
    params = extra_crypt.get("params") or {}
    if kind == "password_shadow":
        passwd_id = params.get("passwd_id", 0)
        if passwd_id == 2:
            return hashlib.sha1(password.encode()).hexdigest()
        if passwd_id == 3 and username and len(mac) == 12:
            colon_mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2)).upper()
            return hashlib.sha1((hashlib.md5(username.encode()).hexdigest() + "_" + colon_mac).encode()).hexdigest()
        return password
    if kind == "password_sha_with_salt":
        salt_b64 = params.get("sha_salt")
        if salt_b64 and params.get("sha_name") is not None:
            salt = base64.b64decode(salt_b64).decode()
            hint = "admin" if params.get("sha_name") == 0 else "user"
            return hashlib.sha256((hint + salt + password).encode()).hexdigest()
        return password
    return f"{username}/{password}" if username else password


# --- the client --------------------------------------------------------------


class TpapError(Exception):
    """A device-reported error. `authentication` means the credentials were rejected."""

    def __init__(self, message: str, error_code: int = 0, *, retryable: bool = False, authentication: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.authentication = authentication


def probe(host: str, timeout_s: float = 2.0) -> bool:
    """
    True when `host` speaks TPAP. One unauthenticated POST, no credentials.

    A KLAP/AES device does not have this endpoint's shape and a bulb that is
    off does not answer at all; either is False, never an exception, because
    this is the branch point between TPAP and python-kasa and a raise here
    would take both paths down.
    """
    try:
        response = requests.post(
            f"http://{host}/",
            json={"method": "login", "params": {"sub_method": "discover"}},
            timeout=timeout_s,
        )
        body = response.json()
    except Exception:  # noqa: BLE001 - see docstring
        return False
    return isinstance(body, dict) and isinstance((body.get("result") or {}).get("tpap"), dict)


class TpapClient:
    """One bulb, one cached session. Synchronous; call it from one thread."""

    def __init__(self, host: str, username: str, password: str, timeout_s: float = 5.0):
        self.host = host
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        self._stok = None
        self._seq = 0
        self._key = None
        self._base_nonce = None

    @property
    def established(self) -> bool:
        return self._stok is not None

    def invalidate(self) -> None:
        self._stok = None
        self._key = None
        self._base_nonce = None

    # -- login steps ----------------------------------------------------------

    def _post_login(self, params: dict, step: str) -> dict:
        response = requests.post(
            f"http://{self.host}/",
            json={"method": "login", "params": params},
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise TpapError(f"TPAP {step} failed for {self.host}: HTTP {response.status_code}")
        body = response.json()
        self._raise_for_error(body, step)
        result = body.get("result")
        if not isinstance(result, dict):
            raise TpapError(f"TPAP {step} for {self.host} returned no result")
        return result

    def _raise_for_error(self, body: dict, step: str) -> None:
        code = body.get("error_code", -100000)
        if code == 0:
            return
        message = f"TPAP {step} failed for {self.host}: {code}"
        info = body.get("error_info")
        if isinstance(info, dict):
            # The device reports a lockout budget alongside failed PAKE attempts.
            # Say it, because a wrong password locks the bulb out for minutes.
            details = []
            if "remainAttempts" in info:
                details.append(f"{info['remainAttempts']} attempts left before lockout")
            if info.get("lockedMinute"):
                details.append(f"locked for {info['lockedMinute']} min")
            if details:
                message += " (" + "; ".join(details) + ")"
        auth = code in _AUTH_ERRORS
        if auth:
            message += " -- the device rejected the credentials"
            self.invalidate()
        raise TpapError(message, code, retryable=code in _RETRYABLE_ERRORS, authentication=auth)

    def handshake(self) -> None:
        self.invalidate()
        discover = self._post_login({"sub_method": "discover"}, "discover")
        tpap = discover.get("tpap") or {}
        mac = str(discover.get("mac") or "").replace(":", "").replace("-", "")
        pake = tpap.get("pake") or []
        if tpap.get("tls") not in (0, None):
            raise TpapError(f"TPAP over TLS (tls={tpap.get('tls')}) is not supported on {self.host}")
        if tpap.get("dac") == 1:
            raise TpapError(f"TPAP DAC certification is not supported on {self.host}")
        if 0 in pake or 3 in pake or not any(mode in pake for mode in (1, 2, 5)):
            # 0 is a MAC-derived default passcode and 3 a shared token; the
            # bulbs seen so far all want the account password. Say which so a
            # future device is a one-line fix rather than a mystery.
            raise TpapError(f"TPAP passcode modes {pake} on {self.host} are not supported (want userpw)")

        register_user = (
            hashlib.sha256(b"admin").hexdigest().upper()
            if tpap.get("user_hash_type") == 1
            else hashlib.md5(b"admin").hexdigest()
        )
        user_random = secrets.token_bytes(32)
        register = self._post_login(
            {
                "sub_method": "pake_register",
                "username": register_user,
                "user_random": base64.b64encode(user_random).decode(),
                "cipher_suites": [1],
                "encryption": ["aes_128_ccm"],
                "passcode_type": "userpw",
                "stok": None,
            },
            "pake_register",
        )
        if register.get("cipher_suites") != 1:
            raise TpapError(f"TPAP suite {register.get('cipher_suites')} on {self.host} is not supported")
        cipher = str(register.get("encryption") or "").lower().replace("-", "_")
        if cipher != "aes_128_ccm":
            raise TpapError(f"TPAP cipher {cipher!r} on {self.host} is not supported")
        iterations = int(register.get("iterations") or 0)
        if iterations <= 0:
            raise TpapError(f"TPAP register on {self.host} gave no PBKDF2 iteration count")

        credential = _credential_string(register.get("extra_crypt"), self.username, self.password, mac)
        derived = hashlib.pbkdf2_hmac("sha256", credential.encode(), base64.b64decode(register["dev_salt"]), iterations, 80)
        w0 = int.from_bytes(derived[:40], "big") % _N
        w1 = int.from_bytes(derived[40:], "big") % _N

        m_point = _decode_point(_M_ENCODED)
        n_point = _decode_point(_N_ENCODED)
        x = secrets.randbelow(_N - 2) + 1
        our_share = _add(_mul(x, _G), _mul(w0, m_point))
        dev_share = _decode_point(base64.b64decode(register["dev_share"]))
        unmasked = _add(dev_share, _neg(_mul(w0, n_point)))
        z_point = _mul(x, unmasked)
        v_point = _mul(w1, unmasked)

        context = hashlib.sha256(_PAKE_CONTEXT_TAG + user_random + base64.b64decode(register["dev_random"])).digest()
        transcript = b"".join(
            _len8(part)
            for part in (
                context,
                b"",
                b"",
                _encode_point(m_point),
                _encode_point(n_point),
                _encode_point(our_share),
                _encode_point(dev_share),
                _encode_point(z_point),
                _encode_point(v_point),
                _encode_w(w0),
            )
        )
        transcript_hash = hashlib.sha256(transcript).digest()
        confirm_keys = _hkdf(transcript_hash, b"", b"ConfirmationKeys", 64)
        shared_key = _hkdf(transcript_hash, b"", b"SharedKey", 32)
        user_confirm = hmac.new(confirm_keys[:32], _encode_point(dev_share), hashlib.sha256).digest()
        expected_dev_confirm = hmac.new(confirm_keys[32:], _encode_point(our_share), hashlib.sha256).digest()

        share = self._post_login(
            {
                "sub_method": "pake_share",
                "user_share": base64.b64encode(_encode_point(our_share)).decode(),
                "user_confirm": base64.b64encode(user_confirm).decode(),
            },
            "pake_share",
        )
        dev_confirm = base64.b64decode(share.get("dev_confirm") or "")
        if not hmac.compare_digest(dev_confirm, expected_dev_confirm):
            raise TpapError(f"TPAP confirmation from {self.host} did not verify", authentication=True)
        stok = share.get("sessionId") or share.get("stok")
        if not stok:
            raise TpapError(f"TPAP share on {self.host} returned no session token")

        self._key = _hkdf(shared_key, b"tp-kdf-salt-aes128-key", b"tp-kdf-info-aes128-key", 16)
        self._base_nonce = _hkdf(shared_key, b"tp-kdf-salt-aes128-iv", b"tp-kdf-info-aes128-iv", _NONCE_LENGTH)
        self._seq = int(share["start_seq"])
        self._stok = stok

    # -- the encrypted channel ------------------------------------------------

    def _nonce(self, seq: int) -> bytes:
        return self._base_nonce[: _NONCE_LENGTH - 4] + struct.pack(">i", seq)

    def request(self, method: str, params=None) -> dict:
        """
        Send one smart-protocol command and return the device's reply body.

        The reply is the decrypted JSON as the device sent it: `error_code`
        plus, for reads, `result`. A nonzero `error_code` raises.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM

        if not self.established:
            self.handshake()
        body = {
            "method": method,
            "request_time_milis": int(time.time() * 1000),
            "terminal_uuid": base64.b64encode(uuid.uuid4().bytes).decode(),
        }
        if params is not None:
            body["params"] = params
        plaintext = json.dumps(body, separators=(",", ":")).encode()
        seq = self._seq
        self._seq += 1
        aead = AESCCM(self._key, tag_length=_TAG_LENGTH)
        payload = struct.pack(">i", seq) + aead.encrypt(self._nonce(seq), plaintext, None)

        response = requests.post(
            f"http://{self.host}/stok={self._stok}/ds",
            data=payload,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.timeout_s,
        )
        raw = response.content
        if response.status_code != 200:
            self.invalidate()
            raise TpapError(f"TPAP request to {self.host} failed: HTTP {response.status_code}", retryable=True)
        if raw[:1] == b"{":
            # Plain JSON on the encrypted endpoint is the device saying the
            # session is gone (expired, or it rebooted). Never ciphertext.
            self.invalidate()
            try:
                self._raise_for_error(json.loads(raw), method)
            except TpapError:
                raise
            raise TpapError(f"TPAP session on {self.host} was rejected", retryable=True)
        if len(raw) < 4 + _TAG_LENGTH:
            self.invalidate()
            raise TpapError(f"TPAP reply from {self.host} was too short", retryable=True)
        reply_seq = struct.unpack(">i", raw[:4])[0]
        plain = aead.decrypt(self._nonce(reply_seq or seq), raw[4:], None)
        reply = json.loads(plain)
        self._raise_for_error(reply, method)
        return reply


class TpapLightModule:
    """The slice of python-kasa's Light module that TapoTransport's actions use."""

    def __init__(self, device: "TpapDevice"):
        self._device = device

    def has_feature(self, name: str) -> bool:
        return name == "brightness" and "brightness" in self._device.info

    @property
    def brightness(self) -> int:
        return int(self._device.info.get("brightness"))

    async def set_brightness(self, percent: int) -> None:
        self._device.client.request("set_device_info", {"brightness": int(percent)})

    async def set_hsv(self, hue: int, saturation: int, value=None) -> None:
        # Same body python-kasa sends: color_temp 0 hands precedence to hue/sat,
        # and brightness is only included when the caller asked for it.
        params = {"color_temp": 0, "hue": int(hue), "saturation": int(saturation)}
        if value is not None:
            params["brightness"] = int(value)
        self._device.client.request("set_device_info", params)


class TpapDevice:
    """
    A TPAP bulb wearing the python-kasa `Device` surface the transport uses.

    `update()` is a `get_device_info`; `is_on` reads the last update. The
    async methods are async only so the transport's actions -- written against
    kasa's coroutines -- work unchanged; the network calls inside are
    synchronous `requests` on the transport's own worker thread.
    """

    def __init__(self, client: TpapClient):
        self.client = client
        self.info: dict = {}
        self.light = TpapLightModule(self)

    @property
    def host(self) -> str:
        return self.client.host

    async def update(self) -> None:
        reply = self.client.request("get_device_info")
        self.info = reply.get("result") or {}

    @property
    def is_on(self) -> bool:
        return bool(self.info.get("device_on"))

    @property
    def alias(self) -> str:
        raw = self.info.get("nickname") or ""
        try:
            return base64.b64decode(raw).decode() or raw
        except Exception:  # noqa: BLE001 - a nickname is display text, never worth a failure
            return raw

    @property
    def model(self) -> str:
        return str(self.info.get("model") or "")

    async def turn_on(self) -> None:
        self.client.request("set_device_info", {"device_on": True})

    async def turn_off(self) -> None:
        self.client.request("set_device_info", {"device_on": False})

    async def disconnect(self) -> None:
        """No-op: the session is cached on the client, see the module docstring."""
