"""
Govee Service — control for Govee smart lights.

Two transports, selected by `govee.transport` in config.yaml:

  "ble"   — direct Bluetooth LE from this machine. Works with models Govee does
            not expose over its cloud API, which includes every BLE-only strip.
  "cloud" — Govee Developer v2 HTTP API. Only works for models on Govee's
            published whitelist (https://developer.govee.com/docs/support-product-model)
            AND only for Wi-Fi devices owned by the account the key belongs to.

Master Miguel's attic strip is an H617E, which is BLE-only: it is absent from the
cloud whitelist, never appears in `GET /user/devices`, and does not join Wi-Fi at
all (its setup flow pairs over Bluetooth and never asks for an SSID). Hence "ble"
is the working default here. The cloud transport is kept for any future
whitelisted device.

BLE protocol (reverse-engineered, confirmed working on H617A/E):
  characteristic 00010203-0405-0607-0809-0a0b0c0d2b11, Write Without Response
  20-byte packets: 19 bytes of command, then a XOR checksum of those 19

Deliberately has nothing to do with the Mi Box or Surfshark, so no ADB checks and
no VPN preflight on this path.
"""

import asyncio
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from services.name_matcher import match_name

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openapi.api.govee.com"

# --- Cloud capability identifiers ---
CAPABILITY_ON_OFF = "devices.capabilities.on_off"
INSTANCE_POWER_SWITCH = "powerSwitch"
CAPABILITY_RANGE = "devices.capabilities.range"
INSTANCE_BRIGHTNESS = "brightness"
CAPABILITY_COLOR_SETTING = "devices.capabilities.color_setting"
INSTANCE_COLOR_RGB = "colorRgb"

# Spoken colour names -> RGB. Deliberately covers what someone actually says out
# loud; anything more exotic is handled by passing a hex value instead.
COLOR_NAMES = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "warm white": (255, 170, 80),
    "cool white": (200, 220, 255),
    "orange": (255, 100, 0),
    "yellow": (255, 220, 0),
    "amber": (255, 160, 20),
    "gold": (255, 200, 40),
    "lime": (150, 255, 0),
    "teal": (0, 200, 160),
    "cyan": (0, 255, 255),
    "turquoise": (0, 220, 200),
    "purple": (140, 0, 255),
    "violet": (170, 60, 255),
    "magenta": (255, 0, 255),
    "pink": (255, 80, 160),
    "coral": (255, 90, 60),
    "crimson": (200, 0, 40),
}

# --- BLE protocol constants ---
BLE_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b11"
BLE_PACKET_LEN = 20

_MSG_NOT_CONFIGURED = "Light control isn't set up right now."
_MSG_UNKNOWN_LIGHT = "I don't have that light saved."
_MSG_CLOUD_UNREACHABLE = "I couldn't reach Govee just now."
_MSG_BLE_UNREACHABLE = "I couldn't reach your lights over Bluetooth."
_MSG_BLE_MISSING_DEP = "Bluetooth light support isn't installed."

# Govee's docs quote different rate limits on different reference pages, so
# nothing here tries to pre-empt them. Handle the 429 and let the API decide.
_CLOUD_ERROR_MESSAGES = {
    400: "Govee rejected that light command.",
    401: "My Govee key isn't working.",
    403: "My Govee key isn't working.",
    404: "I can't find that light on your Govee account.",
    429: "Govee is rate limiting me, give it a second.",
}


@dataclass
class GoveeCommandResult:
    success: bool
    message: str = ""
    code: int | None = None

    def __bool__(self) -> bool:
        return self.success


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ble_packet(*payload: int) -> bytes:
    """Build a 20-byte Govee BLE packet: 19 command bytes then a XOR checksum."""
    body = bytes(payload) + bytes(BLE_PACKET_LEN - 1 - len(payload))
    checksum = 0
    for byte in body:
        checksum ^= byte
    return body + bytes([checksum])


BLE_POWER_ON = ble_packet(0x33, 0x01, 0x01)
BLE_POWER_OFF = ble_packet(0x33, 0x01, 0x00)


def ble_brightness(percent: int) -> bytes:
    """Brightness packet. The H617E takes 1-100 directly, not 0-255."""
    return ble_packet(0x33, 0x04, clamp_percent(percent))


def ble_color(red: int, green: int, blue: int) -> bytes:
    """
    Segmented RGB packet. The FF 7F at offsets 12-13 selects "all segments" and
    is required — without it the strip ignores the colour.
    """
    return ble_packet(0x33, 0x05, 0x15, 0x01, red, green, blue, 0, 0, 0, 0, 0, 0xFF, 0x7F)


def clamp_percent(percent) -> int:
    """Clamp to 1-100. Zero is not a brightness, it's what light_off is for."""
    return max(1, min(100, _as_int(percent, 100)))


def resolve_color(value: str) -> tuple[int, int, int] | None:
    """
    Turn a spoken colour or a hex string into RGB.

    Accepts "red", "warm white", "#ff0000", "ff0000" or "0xff0000". Names go
    through the same fuzzy matcher as light rooms, so "warmwhite" still lands.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None

    candidate = cleaned.lower().removeprefix("#").removeprefix("0x")
    if len(candidate) == 6:
        try:
            packed = int(candidate, 16)
        except ValueError:
            pass
        else:
            return (packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF

    # Offer a despaced alias for every name so "warmwhite" matches exactly on the
    # first tier. Without it the substring tier fires instead and "white", being a
    # substring of "warmwhite", wins — the wrong colour entirely.
    candidates = {name: [name, name.replace(" ", "")] for name in COLOR_NAMES}
    matched = match_name(cleaned, candidates)
    return COLOR_NAMES[matched] if matched else None


class BleTransport:
    """
    Direct Bluetooth LE control.

    Connects per command rather than holding a session open. That is not a
    compromise: measured on this hardware a warm reconnect costs about 0.2s
    while a persistent connection with the documented 2s heartbeat timed out
    outright. Simpler and more reliable both.
    """

    name = "ble"
    required_fields = ("mac",)

    def __init__(self, govee_cfg: dict):
        ble_cfg = govee_cfg.get("ble", {}) or {}
        self.scan_timeout_s = max(1, _as_int(ble_cfg.get("scan_timeout_ms"), 15000)) / 1000
        self.connect_timeout_s = max(1, _as_int(ble_cfg.get("connect_timeout_ms"), 20000)) / 1000
        self.retries = max(1, _as_int(ble_cfg.get("retries"), 3))
        self._device_cache: dict = {}

        # Single dedicated worker so every BLE call lands on a thread we own.
        # See _worker for why that matters. max_workers=1 also serialises access,
        # which is what we want for one radio talking to one strip.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="govee-ble")

        try:
            import bleak  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False
            logger.warning(
                "bleak is not installed, Bluetooth light control disabled. "
                "Install it with: uv sync --extra govee"
            )

    def set_power(self, light: dict, on: bool) -> GoveeCommandResult:
        return self._write(light["mac"], BLE_POWER_ON if on else BLE_POWER_OFF)

    def set_brightness(self, light: dict, percent: int) -> GoveeCommandResult:
        return self._write(light["mac"], ble_brightness(percent))

    def set_color(self, light: dict, rgb: tuple) -> GoveeCommandResult:
        return self._write(light["mac"], ble_color(*rgb))

    def _run_async(self, coro_factory):
        """Run a BLE coroutine on the dedicated MTA worker thread."""
        return self._executor.submit(self._worker, coro_factory).result()

    @staticmethod
    def _worker(coro_factory):
        """
        Entry point for the BLE worker thread.

        bleak's WinRT backend fails with "Thread is configured for Windows GUI
        but callbacks are not working" whenever it runs on a thread in an STA
        COM apartment. The orchestrator's audio stack (sounddevice/PortAudio)
        puts its thread into STA, which is why calling asyncio.run() inline
        worked in standalone scripts and failed inside the running assistant.
        Hopping onto a thread we create ourselves, explicitly in MTA, fixes it.
        """
        if sys.platform == "win32":
            try:
                import ctypes

                # 0 = COINIT_MULTITHREADED. Returns S_FALSE if this thread is
                # already MTA, which is fine; only a mode change would fail.
                ctypes.windll.ole32.CoInitializeEx(None, 0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("CoInitializeEx(MTA) failed, continuing anyway: %s", exc)
        return asyncio.run(coro_factory())

    def _write(self, mac: str, payload: bytes) -> GoveeCommandResult:
        if not self.available:
            return GoveeCommandResult(False, _MSG_BLE_MISSING_DEP)
        try:
            return self._run_async(lambda: self._async_write(mac, payload))
        except Exception as exc:  # noqa: BLE001 - never let BLE stack errors escape
            logger.warning("BLE write failed for %s: %s", mac, exc)
            return GoveeCommandResult(False, _MSG_BLE_UNREACHABLE)

    async def _async_write(self, mac: str, payload: bytes) -> GoveeCommandResult:
        from bleak import BleakClient

        device = self._device_cache.get(mac)
        for attempt in range(1, self.retries + 1):
            if device is None:
                device = await self._find(mac)
                if device is None:
                    logger.warning("BLE scan %d/%d did not find %s", attempt, self.retries, mac)
                    continue
                self._device_cache[mac] = device
            try:
                async with BleakClient(device, timeout=self.connect_timeout_s) as client:
                    await client.write_gatt_char(BLE_CHARACTERISTIC, payload, response=False)
                return GoveeCommandResult(True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("BLE attempt %d/%d failed for %s: %s", attempt, self.retries, mac, exc)
                # A stale cached handle is the usual cause, so force a rescan.
                self._device_cache.pop(mac, None)
                device = None

        return GoveeCommandResult(False, _MSG_BLE_UNREACHABLE)

    async def _find(self, mac: str):
        """
        Locate a device by MAC using a detection callback.

        Not BleakScanner.find_device_by_address: on Windows that returned "not
        found" three times running while the strip was advertising steadily at
        -82 dBm, which a callback scan picked up immediately every time.
        """
        from bleak import BleakScanner

        target = mac.upper()
        future = asyncio.get_running_loop().create_future()

        def on_detect(device, _advertisement):
            if device.address.upper() == target and not future.done():
                future.set_result(device)

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        try:
            return await asyncio.wait_for(future, timeout=self.scan_timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            await scanner.stop()

    def discover(self) -> list[dict]:
        """Every Govee-looking BLE device in range. Used by the probe tool."""
        if not self.available:
            raise ValueError("bleak is not installed. Install it with: uv sync --extra govee")
        return self._run_async(self._async_discover)

    async def _async_discover(self) -> list[dict]:
        from bleak import BleakScanner

        seen: dict = {}

        def on_detect(device, advertisement):
            name = device.name or advertisement.local_name or ""
            if "govee" in name.lower() or "ihoment" in name.lower():
                previous = seen.get(device.address)
                if previous is None or advertisement.rssi > previous["rssi"]:
                    seen[device.address] = {
                        "name": name,
                        "mac": device.address,
                        "rssi": advertisement.rssi,
                    }

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        await asyncio.sleep(self.scan_timeout_s)
        await scanner.stop()
        return sorted(seen.values(), key=lambda d: -d["rssi"])


class CloudTransport:
    """Govee Developer v2 HTTP API. Only reaches whitelisted Wi-Fi models."""

    name = "cloud"
    required_fields = ("sku", "device")

    def __init__(self, govee_cfg: dict):
        self.api_key = os.getenv("GOVEE_API_KEY") or govee_cfg.get("api_key")
        self.base_url = str(govee_cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = max(1, _as_int(govee_cfg.get("request_timeout_ms"), 10000)) / 1000
        self.available = bool(self.api_key)
        if not self.available:
            logger.warning("GOVEE_API_KEY is not set, cloud light control disabled")

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "Govee-API-Key": self.api_key}

    def set_power(self, light: dict, on: bool) -> GoveeCommandResult:
        return self._control(
            light["sku"],
            light["device"],
            CAPABILITY_ON_OFF,
            INSTANCE_POWER_SWITCH,
            1 if on else 0,
        )

    def set_brightness(self, light: dict, percent: int) -> GoveeCommandResult:
        return self._control(
            light["sku"],
            light["device"],
            CAPABILITY_RANGE,
            INSTANCE_BRIGHTNESS,
            clamp_percent(percent),
        )

    def set_color(self, light: dict, rgb: tuple) -> GoveeCommandResult:
        red, green, blue = rgb
        return self._control(
            light["sku"],
            light["device"],
            CAPABILITY_COLOR_SETTING,
            INSTANCE_COLOR_RGB,
            (red << 16) + (green << 8) + blue,
        )

    def _control(self, sku, device, capability_type, instance, value) -> GoveeCommandResult:
        if not self.available:
            return GoveeCommandResult(False, _MSG_NOT_CONFIGURED)

        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {
                "sku": sku,
                "device": device,
                "capability": {"type": capability_type, "instance": instance, "value": value},
            },
        }
        try:
            response = requests.post(
                f"{self.base_url}/router/api/v1/device/control",
                headers=self._headers(),
                json=body,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            logger.warning("Govee control request failed: %s", exc)
            return GoveeCommandResult(False, _MSG_CLOUD_UNREACHABLE)

        return self._interpret(response)

    @staticmethod
    def _interpret(response) -> GoveeCommandResult:
        status = getattr(response, "status_code", 200)
        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}

        code = _as_int(payload.get("code", status), status)
        if status == 200 and code == 200:
            return GoveeCommandResult(True, code=200)

        logger.warning("Govee control rejected: http=%s code=%s body=%s", status, code, payload)
        return GoveeCommandResult(
            False, _CLOUD_ERROR_MESSAGES.get(code, _MSG_CLOUD_UNREACHABLE), code=code
        )

    def list_devices(self) -> list[dict]:
        if not self.available:
            raise ValueError(
                "Govee cloud is not configured. Set GOVEE_API_KEY in .env and enable govee in config.yaml."
            )
        response = requests.get(
            f"{self.base_url}/router/api/v1/user/devices",
            headers=self._headers(),
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json() or {}

        # Govee reports auth and quota failures in the body with HTTP 200, so
        # raise_for_status alone would turn a real error into an empty list.
        code = _as_int(payload.get("code", response.status_code), response.status_code)
        if code != 200:
            detail = payload.get("message") or payload.get("msg") or "no detail"
            raise ValueError(f"Govee API returned code {code}: {detail}")

        return payload.get("data") or []


_TRANSPORTS = {"ble": BleTransport, "cloud": CloudTransport}


class GoveeService:
    def __init__(self, config: dict):
        govee_cfg = config.get("govee", {}) or {}

        self.transport_name = str(govee_cfg.get("transport") or "ble").strip().lower()
        self.default_light = str(govee_cfg.get("default_light") or "").strip()

        transport_cls = _TRANSPORTS.get(self.transport_name)
        if transport_cls is None:
            logger.error(
                "Unknown govee.transport '%s'. Choose from: %s",
                self.transport_name,
                ", ".join(sorted(_TRANSPORTS)),
            )
            self.transport = None
            self.lights = {}
        else:
            # Never raise here. The orchestrator builds every service at startup
            # and a misconfigured light must not take the whole assistant down.
            self.transport = transport_cls(govee_cfg)
            self.lights = self._build_lights(
                govee_cfg.get("lights", {}), transport_cls.required_fields
            )

        configured = bool(govee_cfg.get("enabled", False))
        self.enabled = bool(configured and self.transport and self.transport.available)

        if self.enabled:
            logger.info(
                "Govee initialized: transport=%s, %d light(s), default=%s",
                self.transport_name,
                len(self.lights),
                self.default_light or "none",
            )

    @staticmethod
    def _build_lights(raw: dict, required_fields: tuple) -> dict:
        lights = {}
        for key, value in (raw or {}).items():
            if not isinstance(value, dict):
                logger.warning("Govee light '%s' is not a mapping, skipping", key)
                continue
            entry = {}
            missing = []
            for field in required_fields:
                field_value = str(value.get(field) or "").strip()
                if not field_value:
                    missing.append(field)
                entry[field] = field_value
            if missing:
                logger.warning(
                    "Govee light '%s' is missing %s for this transport, skipping",
                    key,
                    " and ".join(missing),
                )
                continue
            entry["aliases"] = [
                str(alias).strip()
                for alias in (value.get("aliases") or [])
                if str(alias).strip()
            ]
            lights[str(key)] = entry
        return lights

    def resolve_light(self, hint: str = "") -> tuple[str | None, dict | None]:
        """
        Turn a spoken room name into a configured light.

        An empty hint falls back to `govee.default_light`, or to the only
        configured light when there is exactly one.
        """
        if not self.lights:
            return None, None

        cleaned = (hint or "").strip()
        if not cleaned:
            if self.default_light in self.lights:
                return self.default_light, self.lights[self.default_light]
            if len(self.lights) == 1:
                key = next(iter(self.lights))
                return key, self.lights[key]
            return None, None

        candidates = {key: [key, *value["aliases"]] for key, value in self.lights.items()}
        matched_key = match_name(cleaned, candidates)
        if not matched_key:
            return None, None
        return matched_key, self.lights[matched_key]

    def _light_for(self, light_key: str) -> tuple[dict | None, GoveeCommandResult | None]:
        light = self.lights.get(light_key)
        if not light:
            return None, GoveeCommandResult(False, _MSG_UNKNOWN_LIGHT)
        if not self.enabled:
            return None, GoveeCommandResult(False, _MSG_NOT_CONFIGURED)
        return light, None

    # NOTE: these all test `error is not None`, never `if error`. A failed
    # GoveeCommandResult is falsy by design, so a truthiness check here would
    # fall through to the transport with light=None.
    def set_power(self, light_key: str, on: bool) -> GoveeCommandResult:
        light, error = self._light_for(light_key)
        if error is not None:
            return error
        return self.transport.set_power(light, on)

    def set_brightness(self, light_key: str, percent: int) -> GoveeCommandResult:
        light, error = self._light_for(light_key)
        if error is not None:
            return error
        return self.transport.set_brightness(light, percent)

    def set_color(self, light_key: str, rgb: tuple) -> GoveeCommandResult:
        light, error = self._light_for(light_key)
        if error is not None:
            return error
        return self.transport.set_color(light, rgb)
