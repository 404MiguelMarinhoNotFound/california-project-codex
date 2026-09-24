"""
TP-Link Tapo light control over the LAN, via python-kasa.

Selected by `govee.transport: "tapo"` in config.yaml. The config block is still
named `govee` because that is what `GoveeService` reads; the service is really a
generic light service with pluggable transports, and renaming the block is a
separate change from adding this one.

**Why this transport exists at all: these bulbs can be READ.** The Govee strip's
characteristic is Write Without Response with no notify beside it, so
`services/light_shadow.py` has to answer "what are my lights doing" from memory
of the last command and say out loud that it is memory. A Tapo bulb answers.
`get_state()` is therefore the point of this module, not an afterthought -- power
and brightness come back as facts.

What it deliberately does NOT read back is colour. python-kasa reports hue and
saturation, and turning those into "warm white" would need an rgb-to-name map
this project does not have and should not grow (see light_shadow's docstring).
Power and brightness are unambiguous and speakable; hue 37 is neither.

**Local, but not credential-free.** Traffic stays on the LAN -- there is no cloud
round trip on the control path -- but the KLAP handshake authenticates with the
TP-Link *cloud* account, so TAPO_USERNAME / TAPO_PASSWORD must be set. That is a
real step down from the BLE transport, which needs no credentials at all, and it
is the price of a bulb that answers.

Connect-per-command, like BleTransport and DeebotService, for a reason specific
to asyncio rather than to radios: `asyncio.run` builds a fresh event loop every
call, and a cached `Device` holds a connection bound to the loop that made it.
Reusing one across calls is a use-after-free waiting to happen. The KLAP
handshake is two LAN round trips, which is cheap enough not to buy trouble for.

**python-kasa is only half the story.** Tapo firmware 1.4.2 (early 2026)
moved the bulbs to a new local protocol, TPAP, that python-kasa 0.10.2 refuses
outright (`UnsupportedDeviceError ... encrypt_type='TPAP'`), and upstream has
no working implementation. The living-room L530E is on that firmware.
`services/tapo_tpap.py` speaks it; `_with_device` probes each host once and
routes to whichever the bulb answers. Everything above this line -- the three
writes, `get_state`, the worker thread, the retry loop -- is protocol-blind.
"""

import asyncio
import colorsys
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from services.govee_service import GoveeCommandResult, clamp_percent

logger = logging.getLogger(__name__)

_MSG_MISSING_DEP = "Tapo light support isn't installed."
_MSG_NO_CREDENTIALS = "My TP-Link login isn't set up."
_MSG_UNREACHABLE = "I couldn't reach your lights on the network."


@dataclass
class LightReading:
    """
    A LIVE reading off the bulb, as opposed to light_shadow's memory of a send.

    Deliberately no `__bool__`. `GoveeCommandResult` has one and the scar tissue
    around it fills half of CLAUDE.md's Govee section; a reading is never a
    success flag, so `is None` is the only existence check and truthiness has no
    meaning here at all.

    `None` on a field means "the bulb did not tell me", never "off" or "zero" --
    the same contract `MediaService`'s readers use.
    """

    power: bool | None = None
    percent: int | None = None


def _ensure_kasa_importable() -> None:
    """
    Put `typing.ByteString` back on Python 3.14 so mashumaro can import.

    python-kasa depends on mashumaro, whose codegen references
    `typing.ByteString`. That alias was deprecated in 3.9 and **removed in 3.14**,
    which this project pins (`.python-version`) because deebot-client 18.5.0+
    requires it. The result is that `import kasa` raises
    `AttributeError: module 'typing' has no attribute 'ByteString'` before a
    single line of our code runs -- measured on mashumaro 3.22 / python-kasa
    0.10.2, both the newest releases as of 2026-09-16.

    So this is not defensive coding, it is the only way the dependency imports at
    all on the interpreter the rest of the project needs. The blast radius is
    small and checked: the alias is set only when genuinely absent, it resolves to
    `bytes` (what mashumaro's `issubclass` check wants), and nothing else in this
    tree references `typing.ByteString`.

    Delete this the moment mashumaro ships a 3.14-clean release. If `import kasa`
    starts working with the body of this function removed, it is dead code.
    """
    if sys.version_info < (3, 14):
        return
    import typing

    if not hasattr(typing, "ByteString"):
        typing.ByteString = bytes  # type: ignore[attr-defined]
        logger.debug("Patched typing.ByteString for mashumaro on Python 3.14+")


def rgb_to_hsv(red: int, green: int, blue: int) -> tuple[int, int]:
    """
    Turn RGB into the (hue degrees, saturation percent) pair Tapo takes.

    The brightness component of HSV is thrown away on purpose. `set_hsv`'s third
    argument is the bulb's brightness, so passing it would make "make it red"
    silently a dimmer command -- `#404040` would drop the room to 25%. Colour and
    brightness are separate actions in the tool schema and stay separate here,
    which also matches the BLE transport, where a colour packet never touched
    brightness.
    """
    hue, saturation, _value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return round(hue * 360), round(saturation * 100)


class TapoTransport:
    """TP-Link Tapo over the LAN. Reads as well as writes."""

    name = "tapo"
    required_fields = ("host",)

    def __init__(self, govee_cfg: dict):
        tapo_cfg = govee_cfg.get("tapo", {}) or {}

        self.username = os.getenv("TAPO_USERNAME") or tapo_cfg.get("username") or ""
        self.password = os.getenv("TAPO_PASSWORD") or tapo_cfg.get("password") or ""
        self.timeout_s = max(1, _as_int(tapo_cfg.get("request_timeout_ms"), 5000)) / 1000
        self.retries = max(1, _as_int(tapo_cfg.get("retries"), 2))

        # One dedicated worker, same shape as BleTransport. The COM apartment
        # dance there is bleak-specific and not needed here, but the thread is:
        # the orchestrator is synchronous and threaded, and asyncio.run must
        # never be called on whatever thread happens to be holding the audio
        # stack.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tapo")

        # Per-host protocol decision and TPAP session cache, both only ever
        # touched on the worker thread. See _with_device.
        self._protocols: dict[str, str] = {}
        self._tpap_clients: dict = {}

        self._dependency_ok = False
        try:
            _ensure_kasa_importable()
            import kasa  # noqa: F401

            self._dependency_ok = True
        except Exception as exc:  # noqa: BLE001 - an import error must not crash boot
            logger.warning(
                "python-kasa is not usable, Tapo light control disabled (%s). "
                "Install it with: uv sync --extra default",
                exc,
            )

        if self._dependency_ok and not (self.username and self.password):
            logger.warning(
                "TAPO_USERNAME/TAPO_PASSWORD are not set, Tapo light control disabled. "
                "Local control still authenticates against the TP-Link account."
            )

        self.available = bool(self._dependency_ok and self.username and self.password)

    # --- writes -------------------------------------------------------------

    def set_power(self, light: dict, on: bool) -> GoveeCommandResult:
        async def action(device, _light_module):
            await (device.turn_on() if on else device.turn_off())

        return self._run(light["host"], action)

    def set_brightness(self, light: dict, percent: int) -> GoveeCommandResult:
        value = clamp_percent(percent)

        async def action(_device, light_module):
            await light_module.set_brightness(value)

        return self._run(light["host"], action)

    def set_color(self, light: dict, rgb: tuple) -> GoveeCommandResult:
        hue, saturation = rgb_to_hsv(*rgb)

        async def action(_device, light_module):
            # value=None leaves brightness alone -- see rgb_to_hsv.
            await light_module.set_hsv(hue, saturation, None)

        return self._run(light["host"], action)

    # --- the read that justifies the module ---------------------------------

    def get_state(self, light: dict) -> LightReading | None:
        """The bulb's live power and brightness, or None if it could not be read."""
        if not self.available:
            return None

        async def action(device, light_module):
            percent = None
            # A bulb without a brightness feature is not an error, it is a bulb
            # with no dimmer. Ask before reading rather than catching afterwards.
            if light_module is not None and light_module.has_feature("brightness"):
                percent = light_module.brightness
            return LightReading(power=device.is_on, percent=percent)

        try:
            return self._run_async(lambda: self._with_device(light["host"], action))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tapo read failed for %s: %s", light.get("host"), exc)
            return None

    # --- plumbing -----------------------------------------------------------

    def _run(self, host: str, action) -> GoveeCommandResult:
        if not self._dependency_ok:
            return GoveeCommandResult(False, _MSG_MISSING_DEP)
        if not self.available:
            return GoveeCommandResult(False, _MSG_NO_CREDENTIALS)

        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                self._run_async(lambda: self._with_device(host, action))
                return GoveeCommandResult(True)
            except Exception as exc:  # noqa: BLE001 - never let kasa errors escape
                last_error = exc
                logger.warning("Tapo attempt %d/%d failed for %s: %s", attempt, self.retries, host, exc)

        logger.warning("Tapo command gave up on %s: %s", host, last_error)
        return GoveeCommandResult(False, _MSG_UNREACHABLE)

    def _run_async(self, coro_factory):
        """Run one kasa coroutine on the dedicated worker thread and wait."""
        return self._executor.submit(lambda: asyncio.run(coro_factory())).result()

    async def _with_device(self, host: str, action):
        """
        Connect, update, hand the caller the device and its Light module, disconnect.

        Two protocols behind one boundary. Firmware 1.4.2+ speaks TPAP, which
        python-kasa cannot (see `services/tapo_tpap.py`); older firmware speaks
        KLAP/AES, which it can. The first call to a host asks it which with one
        unauthenticated POST (`tpap_tpap.probe`) and remembers the answer for
        the life of the process -- a bulb does not change protocol without a
        firmware update, and the probe costs a round trip per command otherwise.

        On the TPAP path the session is cached per host and `disconnect()` is a
        no-op; a failed command drops the session (`TpapClient.invalidate`) so
        `_run`'s retry handshakes afresh. On the kasa path the `update()` is not
        optional even for a write: python-kasa populates the module list from
        the device's own component negotiation, so `device.modules` is empty
        until it has run.
        """
        if self._protocol_for(host) == "tpap":
            from services import tapo_tpap

            client = self._tpap_clients.get(host)
            if client is None:
                client = tapo_tpap.TpapClient(host, self.username, self.password, self.timeout_s)
                self._tpap_clients[host] = client
            device = tapo_tpap.TpapDevice(client)
            try:
                await device.update()
                return await action(device, device.light)
            except Exception:
                client.invalidate()
                raise

        from kasa import Credentials, Device, DeviceConfig, Module

        config = DeviceConfig(
            host=host,
            credentials=Credentials(username=self.username, password=self.password),
            timeout=self.timeout_s,
        )
        device = await Device.connect(config=config)
        try:
            await device.update()
            result = await action(device, device.modules.get(Module.Light))
        finally:
            await device.disconnect()
        self._protocols[host] = "kasa"
        return result

    def _protocol_for(self, host: str) -> str:
        """
        `"tpap"` or `"kasa"`, probed per host and remembered once it is known.

        Only a positive answer is cached from the probe itself: a bulb that is
        off at the wall fails the probe too, and remembering that as "kasa"
        would send every later command down the wrong path after it came back.
        The kasa answer is cached by the kasa branch once a command succeeds.
        """
        protocol = self._protocols.get(host)
        if protocol is None:
            from services import tapo_tpap

            if tapo_tpap.probe(host, min(self.timeout_s, 2.0)):
                self._protocols[host] = protocol = "tpap"
                logger.info("Tapo %s speaks TPAP", host)
            else:
                protocol = "kasa"
        return protocol


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
