"""
TPAP client: the handshake maths, the encrypted channel, and the routing.

Nothing here touches a bulb. `FakeBulb` plays the device side of SPAKE2+ with
the module's own curve helpers and the same PBKDF2 derivation, so a wrong
constant, a wrong transcript layout or a wrong confirmation key fails the
handshake here the way it would against the real firmware -- the .NET port
this is taken from was proven against the living-room L530E, and this fake is
proven against this port, which is as close as an offline test gets.
`test_unit_tests_never_open_a_network_connection` pins that `requests.post`
is the only boundary.
"""

import base64
import hashlib
import json
import secrets
import socket
import struct
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from services import tapo_tpap
from services.tapo_tpap import (
    TpapClient,
    TpapDevice,
    TpapError,
    _credential_string,
    _decode_point,
    _encode_point,
    _hkdf,
    probe,
)
from services.tapo_transport import TapoTransport

_HOST = "bulb.invalid"  # RFC 2606: never resolves, so a broken patch cannot reach the LAN
_USER = "test@example.invalid"
_PASS = "not-a-password"
_MAC = "58D812234985"


class FakeBulb:
    """
    The firmware's half of the protocol, minus everything but the maths.

    Answers the three login sub-methods and the encrypted `/ds` endpoint the
    way the real L530E did on 2026-09-17: `pake: [2]`, `password_shadow`
    with `passwd_id: 2`, suite 1, aes_128_ccm, 3000 PBKDF2 iterations (cut to
    50 here; the count is the device's to choose and the test is not a
    benchmark).
    """

    def __init__(self, password=_PASS, *, iterations=50, pake=(2,), tls=0, dac=0):
        self.password = password
        self.iterations = iterations
        self.pake = list(pake)
        self.tls = tls
        self.dac = dac
        self.salt = secrets.token_bytes(16)
        self.dev_random = secrets.token_bytes(32)
        self.state = {"device_on": True, "brightness": 100, "model": "L530", "nickname": base64.b64encode(b"sala").decode()}
        self.commands = []
        self.stok = "session-token"
        self.start_seq = 1000
        self._w0 = self._w1 = None
        self._y = None
        self._dev_share = None
        self._key = self._base_nonce = None
        self.reply_plain_json = None  # set to a dict to answer /ds with plain JSON

    # -- the SPAKE2+ server side ----------------------------------------------

    def _derive(self):
        credential = hashlib.sha1(self.password.encode()).hexdigest()
        derived = hashlib.pbkdf2_hmac("sha256", credential.encode(), self.salt, self.iterations, 80)
        self._w0 = int.from_bytes(derived[:40], "big") % tapo_tpap._N
        self._w1 = int.from_bytes(derived[40:], "big") % tapo_tpap._N

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        response = Mock()
        response.status_code = 200
        if url.endswith("/ds"):
            response.content = self._ds(data)
            return response
        body = self._login(json["params"])
        response.json = Mock(return_value=body)
        response.content = b"{}"
        return response

    def _login(self, params):
        sub = params["sub_method"]
        if sub == "discover":
            return {"error_code": 0, "result": {"mac": _MAC, "tpap": {"tls": self.tls, "dac": self.dac, "pake": self.pake, "port": 80}}}
        if sub == "pake_register":
            self._derive()
            self._user_random = base64.b64decode(params["user_random"])
            self._y = secrets.randbelow(tapo_tpap._N - 2) + 1
            n_point = _decode_point(tapo_tpap._N_ENCODED)
            self._dev_share = tapo_tpap._add(tapo_tpap._mul(self._y, tapo_tpap._G), tapo_tpap._mul(self._w0, n_point))
            return {
                "error_code": 0,
                "result": {
                    "cipher_suites": 1,
                    "encryption": "aes_128_ccm",
                    "iterations": self.iterations,
                    "dev_salt": base64.b64encode(self.salt).decode(),
                    "dev_random": base64.b64encode(self.dev_random).decode(),
                    "dev_share": base64.b64encode(_encode_point(self._dev_share)).decode(),
                    "extra_crypt": {"type": "password_shadow", "params": {"passwd_id": 2}},
                },
            }
        if sub == "pake_share":
            m_point = _decode_point(tapo_tpap._M_ENCODED)
            n_point = _decode_point(tapo_tpap._N_ENCODED)
            user_share = _decode_point(base64.b64decode(params["user_share"]))
            unmasked = tapo_tpap._add(user_share, tapo_tpap._neg(tapo_tpap._mul(self._w0, m_point)))
            z_point = tapo_tpap._mul(self._y, unmasked)
            v_point = tapo_tpap._mul(self._y, tapo_tpap._mul(self._w1, tapo_tpap._G))
            context = hashlib.sha256(b"PAKE V1" + self._user_random + self.dev_random).digest()
            transcript = b"".join(
                tapo_tpap._len8(part)
                for part in (
                    context, b"", b"",
                    _encode_point(m_point), _encode_point(n_point),
                    _encode_point(user_share), _encode_point(self._dev_share),
                    _encode_point(z_point), _encode_point(v_point),
                    tapo_tpap._encode_w(self._w0),
                )
            )
            transcript_hash = hashlib.sha256(transcript).digest()
            confirm_keys = _hkdf(transcript_hash, b"", b"ConfirmationKeys", 64)
            shared = _hkdf(transcript_hash, b"", b"SharedKey", 32)
            import hmac as _hmac

            expected_user = _hmac.new(confirm_keys[:32], _encode_point(self._dev_share), hashlib.sha256).digest()
            if not _hmac.compare_digest(expected_user, base64.b64decode(params["user_confirm"])):
                return {"error_code": -2203, "error_info": {"remainAttempts": 4}}
            dev_confirm = _hmac.new(confirm_keys[32:], _encode_point(user_share), hashlib.sha256).digest()
            self._key = _hkdf(shared, b"tp-kdf-salt-aes128-key", b"tp-kdf-info-aes128-key", 16)
            self._base_nonce = _hkdf(shared, b"tp-kdf-salt-aes128-iv", b"tp-kdf-info-aes128-iv", 12)
            return {"error_code": 0, "result": {"dev_confirm": base64.b64encode(dev_confirm).decode(), "stok": self.stok, "start_seq": self.start_seq, "expired": 86400}}
        raise AssertionError(f"unexpected sub_method {sub}")

    def _ds(self, payload: bytes) -> bytes:
        if self.reply_plain_json is not None:
            return json.dumps(self.reply_plain_json).encode()
        seq = struct.unpack(">i", payload[:4])[0]
        nonce = self._base_nonce[:8] + struct.pack(">i", seq)
        request = json.loads(AESCCM(self._key, tag_length=16).decrypt(nonce, payload[4:], None))
        self.commands.append(request)
        if request["method"] == "get_device_info":
            reply = {"error_code": 0, "result": dict(self.state)}
        elif request["method"] == "set_device_info":
            self.state.update(request["params"])
            reply = {"error_code": 0}
        else:
            reply = {"error_code": -1}
        plain = json.dumps(reply).encode()
        return struct.pack(">i", seq) + AESCCM(self._key, tag_length=16).encrypt(nonce, plain, None)


class CurveTests(unittest.TestCase):
    def test_the_spake_points_are_on_p256(self):
        for encoded in (tapo_tpap._M_ENCODED, tapo_tpap._N_ENCODED):
            x, y = _decode_point(encoded)
            self.assertEqual((y * y) % tapo_tpap._P, (x ** 3 + tapo_tpap._A * x + tapo_tpap._B) % tapo_tpap._P)

    def test_compressed_and_uncompressed_encodings_round_trip(self):
        point = tapo_tpap._mul(12345, tapo_tpap._G)
        self.assertEqual(_decode_point(_encode_point(point)), point)
        compressed = bytes([2 | (point[1] & 1)]) + point[0].to_bytes(32, "big")
        self.assertEqual(_decode_point(compressed), point)

    def test_w_gets_a_sign_byte_only_when_odd_length_with_the_high_bit_set(self):
        # BigInteger.ToByteArrayUnsigned semantics as the .NET port hashes it:
        # a leading zero only when the length is odd AND the top bit is set.
        self.assertEqual(tapo_tpap._encode_w(0x01), b"\x01")
        self.assertEqual(tapo_tpap._encode_w(0x0102), b"\x01\x02")
        self.assertEqual(tapo_tpap._encode_w(0x80), b"\x00\x80")
        self.assertEqual(tapo_tpap._encode_w(0x8000), b"\x80\x00")


class CredentialTests(unittest.TestCase):
    def test_password_shadow_id_2_is_sha1_of_the_password(self):
        self.assertEqual(
            _credential_string({"type": "password_shadow", "params": {"passwd_id": 2}}, _USER, _PASS, _MAC),
            hashlib.sha1(_PASS.encode()).hexdigest(),
        )

    def test_no_extra_crypt_is_user_slash_password(self):
        self.assertEqual(_credential_string(None, _USER, _PASS, _MAC), f"{_USER}/{_PASS}")

    def test_sha_with_salt_hashes_hint_salt_and_password(self):
        salt = base64.b64encode(b"s4lt").decode()
        crypt = {"type": "password_sha_with_salt", "params": {"sha_name": 0, "sha_salt": salt}}
        self.assertEqual(_credential_string(crypt, _USER, _PASS, _MAC), hashlib.sha256(("admin" + "s4lt" + _PASS).encode()).hexdigest())


class ProbeTests(unittest.TestCase):
    def test_a_tpap_block_means_tpap(self):
        response = Mock(json=Mock(return_value={"error_code": 0, "result": {"tpap": {"tls": 0}}}))
        with patch.object(tapo_tpap.requests, "post", return_value=response):
            self.assertTrue(probe(_HOST))

    def test_anything_else_is_not_tpap_and_never_raises(self):
        with patch.object(tapo_tpap.requests, "post", side_effect=OSError("no route")):
            self.assertFalse(probe(_HOST))
        response = Mock(json=Mock(return_value={"error_code": -1}))
        with patch.object(tapo_tpap.requests, "post", return_value=response):
            self.assertFalse(probe(_HOST))


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.bulb = FakeBulb()
        patcher = patch.object(tapo_tpap.requests, "post", side_effect=self.bulb.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TpapClient(_HOST, _USER, _PASS)

    def test_the_handshake_completes_and_the_channel_round_trips(self):
        reply = self.client.request("get_device_info")

        self.assertTrue(self.client.established)
        self.assertEqual(reply["result"]["brightness"], 100)
        self.assertEqual(self.bulb.commands[0]["method"], "get_device_info")

    def test_the_sequence_advances_per_request(self):
        self.client.request("get_device_info")
        self.client.request("set_device_info", {"brightness": 40})

        self.assertEqual(self.client._seq, self.bulb.start_seq + 2)
        self.assertEqual(self.bulb.state["brightness"], 40)

    def test_a_wrong_password_is_an_authentication_error_with_the_lockout_budget(self):
        client = TpapClient(_HOST, _USER, "wrong")

        with self.assertRaises(TpapError) as caught:
            client.request("get_device_info")

        self.assertTrue(caught.exception.authentication)
        self.assertIn("4 attempts left", str(caught.exception))
        self.assertFalse(client.established)

    def test_a_forged_device_confirm_is_rejected(self):
        real_login = self.bulb._login

        def forged(params):
            body = real_login(params)
            if params["sub_method"] == "pake_share":
                body["result"]["dev_confirm"] = base64.b64encode(b"\0" * 32).decode()
            return body

        with patch.object(self.bulb, "_login", side_effect=forged):
            with self.assertRaises(TpapError) as caught:
                self.client.request("get_device_info")
        self.assertTrue(caught.exception.authentication)

    def test_plain_json_on_the_encrypted_endpoint_drops_the_session_as_retryable(self):
        self.client.request("get_device_info")
        self.bulb.reply_plain_json = {"error_code": -40401}

        with self.assertRaises(TpapError) as caught:
            self.client.request("get_device_info")

        self.assertTrue(caught.exception.retryable)
        self.assertFalse(self.client.established)

    def test_an_unsupported_pake_mode_is_named_rather_than_guessed(self):
        self.bulb.pake = [0]
        with self.assertRaises(TpapError) as caught:
            self.client.request("get_device_info")
        self.assertIn("[0]", str(caught.exception))


class DeviceSurfaceTests(unittest.TestCase):
    """TpapDevice quacks like the kasa Device the transport's actions expect."""

    def setUp(self):
        self.bulb = FakeBulb()
        patcher = patch.object(tapo_tpap.requests, "post", side_effect=self.bulb.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = TpapDevice(TpapClient(_HOST, _USER, _PASS))

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_update_reads_power_brightness_alias_and_model(self):
        self._run(self.device.update())

        self.assertTrue(self.device.is_on)
        self.assertTrue(self.device.light.has_feature("brightness"))
        self.assertEqual(self.device.light.brightness, 100)
        self.assertEqual((self.device.alias, self.device.model), ("sala", "L530"))

    def test_colour_sends_the_same_body_python_kasa_does_and_leaves_brightness_alone(self):
        self._run(self.device.light.set_hsv(240, 100, None))

        self.assertEqual(self.bulb.commands[-1]["params"], {"color_temp": 0, "hue": 240, "saturation": 100})
        self.assertEqual(self.bulb.state["brightness"], 100)

    def test_power_writes_device_on(self):
        self._run(self.device.turn_off())
        self.assertFalse(self.bulb.state["device_on"])
        self._run(self.device.turn_on())
        self.assertTrue(self.bulb.state["device_on"])


class TransportRoutingTests(unittest.TestCase):
    def _transport(self):
        with patch.dict("os.environ", {"TAPO_USERNAME": _USER, "TAPO_PASSWORD": _PASS}, clear=False):
            return TapoTransport({"tapo": {"retries": 1}})

    def test_a_tpap_bulb_is_driven_without_ever_touching_kasa(self):
        bulb = FakeBulb()
        transport = self._transport()
        with patch.object(tapo_tpap.requests, "post", side_effect=bulb.post):
            with patch.dict("sys.modules", {"kasa": None}):
                self.assertTrue(transport.set_brightness({"host": _HOST}, 30))
                reading = transport.get_state({"host": _HOST})

        self.assertEqual(bulb.state["brightness"], 30)
        self.assertEqual((reading.power, reading.percent), (True, 30))
        self.assertEqual(transport._protocols[_HOST], "tpap")

    def test_the_session_is_reused_across_commands(self):
        bulb = FakeBulb()
        transport = self._transport()
        with patch.object(tapo_tpap.requests, "post", side_effect=bulb.post) as post:
            transport.set_power({"host": _HOST}, True)
            calls_after_first = post.call_count
            transport.set_power({"host": _HOST}, False)

        # First command: probe + discover + register + share + update + set = 6
        # posts. Second: update + set only, no probe and no login traffic.
        self.assertEqual(calls_after_first, 6)
        self.assertEqual(post.call_count, calls_after_first + 2)

    def test_a_failed_probe_is_not_remembered(self):
        # A bulb off at the wall fails the probe too. Caching that as "kasa"
        # would send every later command down the wrong path once it is back.
        transport = self._transport()
        with patch.object(tapo_tpap, "probe", return_value=False):
            self.assertEqual(transport._protocol_for(_HOST), "kasa")
        self.assertNotIn(_HOST, transport._protocols)

    def test_a_failed_tpap_command_drops_the_session_so_the_retry_handshakes(self):
        bulb = FakeBulb()
        transport = self._transport()
        with patch.object(tapo_tpap.requests, "post", side_effect=bulb.post):
            transport.set_power({"host": _HOST}, True)
            client = transport._tpap_clients[_HOST]
            bulb.reply_plain_json = {"error_code": -40401}
            result = transport.set_power({"host": _HOST}, True)

        self.assertFalse(result)
        self.assertFalse(client.established)


class NetworkGuardTests(unittest.TestCase):
    def test_unit_tests_never_open_a_network_connection(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("a unit test tried to open a real connection")

        bulb = FakeBulb()
        with patch.object(socket.socket, "connect", explode):
            with patch.object(tapo_tpap.requests, "post", side_effect=bulb.post):
                TpapClient(_HOST, _USER, _PASS).request("get_device_info")


if __name__ == "__main__":
    unittest.main()
