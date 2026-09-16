"""
Deebot vacuum control (Ecovacs N8+) for the control_vacuum tool.

Same shape as GoveeService: never raises at construction, self-disables when
the config flag is off, the dependency is missing, or the credentials are
absent, returns DeebotCommandResult instead of raising, and resolves spoken
room names through services/name_matcher.

Three things about how it talks to the robot, all measured on the real N8+:

- **REST only, no MQTT.** deebot-client sends every JSON command over
  `api/iot/devmanager.do` and the reply comes back in the same HTTP response;
  MQTT is only for push events. Battery, state, error and the room list all
  arrive via the REST refresh (spike 2026-09-16: 3.1s for all five reads, ~2s
  for a status). Skipping MqttClient also skips aiomqtt's Windows
  `add_reader` NotImplementedError noise entirely.
- **Connect-per-command on a daemon worker thread**, like BleTransport. The
  orchestrator is threaded and synchronous; every public method runs one
  coroutine on a fresh daemon thread and waits. Never `asyncio.run` from
  orchestrator code. One command at a time: a timed-out future cannot be
  cancelled, so a second command is refused rather than queued behind a worker
  that is still talking to the robot. The wait is `command_timeout_ms` PLUS
  `verification_email_timeout_ms`, because a single command may have to
  complete a device verification on the way through.
- **Auth first, every command, with one retry.** `_with_auth` reuses the
  cached token (services/deebot_session), and when Ecovacs rejects it
  mid-command it drops the token, re-authenticates (reading the emailed code
  from Gmail when GMAIL_APP_PASSWORD is set) and retries the command exactly
  once. No human in the loop. Rejection is not always an auth error: the
  portal answers an unauthorised request with HTTP 200 and no `devices` key,
  which arrives here as `LookupError`, so that counts as a stale token too --
  but only when the token came from the cache.

Room ids: `deebot.rooms` in config.yaml caches `name -> id` so a room command
is one REST call. A room with no cached id, or a cached id the robot rejects
(ids shift after a remap), falls back to the live room list and matches the
config key against the live names. Rooms still named `Default` on the robot
are ignored on purpose -- they can't be asked for by voice anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path

from services import deebot_session
from services.name_matcher import match_name

logger = logging.getLogger(__name__)

_MSG_NOT_CONFIGURED = "Vacuum control isn't set up right now."
_MSG_UNREACHABLE = "I couldn't reach the vacuum just now."
_MSG_NEEDS_CODE = (
    "The vacuum login needs a fresh verification code and I couldn't read one from email."
)
_MSG_BAD_LOGIN = "The vacuum login was rejected. The Ecovacs email or password looks wrong."
_MSG_BUSY = "I'm still working on the last vacuum command."
_MSG_UNKNOWN_ROOM = "I don't have that room on the vacuum's map."
_MSG_REJECTED = "The vacuum didn't accept that command."

_UNNAMED_ROOM = "default"
_LIVE_ROOMS_TTL_S = 300


@dataclass
class DeebotCommandResult:
    success: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.success


@dataclass
class VacuumStatus:
    available: bool
    state: str | None = None  # idle | cleaning | returning | docked | error | paused
    battery: int | None = None
    error: str | None = None
    # Why it is unavailable, when we know something better than "unreachable".
    # "needs a verification code" and "can't see it on the network" are opposite
    # fixes, so the dispatcher has to be able to tell them apart -- the same
    # reason MediaService carries unreachable_reason for the box.
    message: str = ""


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ok(raw: dict | None) -> bool:
    """A devmanager reply is good when ret == ok and the body code is 0."""
    if not isinstance(raw, dict) or raw.get("ret") != "ok":
        return False
    body = (raw.get("resp") or {}).get("body") or {}
    return _as_int(body.get("code"), 0) == 0


class DeebotService:
    def __init__(self, config: dict):
        cfg = config.get("deebot", {}) or {}

        self.email = os.getenv("ECOVACS_EMAIL") or str(cfg.get("email") or "").strip()
        self.password = os.getenv("ECOVACS_PASSWORD") or str(cfg.get("password") or "").strip()
        self.country = (
            os.getenv("ECOVACS_COUNTRY") or str(cfg.get("country") or "").strip() or "PT"
        ).upper()
        self.gmail_app_password = os.getenv("GMAIL_APP_PASSWORD") or str(
            cfg.get("gmail_app_password") or ""
        ).strip()

        self.state_dir = Path(cfg.get("state_dir") or deebot_session.ROOT)
        # Floor the SECONDS, not the milliseconds. max(5, ms) / 1000 reads like a
        # 5s minimum and is a 5ms one, so command_timeout_ms: 3 used to expire
        # every command instantly and verification_email_timeout_ms: 500 used to
        # give fetch_new_code a 0s budget, whose poll loop never runs at all.
        self.command_timeout_s = max(5.0, _as_int(cfg.get("command_timeout_ms"), 30000) / 1000)
        self.verification_email_timeout_s = max(
            10, _as_int(cfg.get("verification_email_timeout_ms"), 90000) // 1000
        )
        # One command may have to complete a device verification on the way
        # through (request a code, then poll Gmail for it), so the wait has to
        # cover both budgets. At 30s vs 90s the old wait gave up while the
        # verification was still succeeding, reported "unreachable", and left
        # the worker running so the next command timed out too. This is a
        # ceiling, not a cost: every step underneath has its own timeout, so an
        # unreachable robot still answers in seconds.
        self._wait_timeout_s = self.command_timeout_s + self.verification_email_timeout_s

        self.rooms = self._build_rooms(cfg.get("rooms") or {})

        try:
            import deebot_client  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False
            logger.warning(
                "deebot-client is not installed, vacuum control disabled. "
                "Install it with: uv sync --extra default"
            )

        configured = bool(cfg.get("enabled", False))
        has_credentials = bool(self.email and self.password)
        if configured and not has_credentials:
            logger.warning(
                "deebot.enabled is true but ECOVACS_EMAIL/ECOVACS_PASSWORD are not set, "
                "vacuum control disabled"
            )
        self.enabled = bool(configured and self.available and has_credentials)

        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._device_info = None
        self._live_rooms: dict[str, int] = {}
        self._live_rooms_at = 0.0

        if self.enabled:
            logger.info(
                "Deebot initialized: %d room(s), country=%s", len(self.rooms), self.country
            )

    # ------------------------------------------------------------------ rooms

    @staticmethod
    def _build_rooms(raw: dict) -> dict:
        rooms = {}
        for key, value in (raw or {}).items():
            name = str(key or "").strip()
            if not name or name.lower() == _UNNAMED_ROOM:
                logger.warning("Deebot room %r has no usable name, skipping", key)
                continue
            value = value if isinstance(value, dict) else {}
            room_id = value.get("id")
            rooms[name] = {
                "id": int(room_id) if isinstance(room_id, int) and not isinstance(room_id, bool) else None,
                "aliases": [
                    str(alias).strip()
                    for alias in (value.get("aliases") or [])
                    if str(alias).strip()
                ],
            }
        return rooms

    def resolve_room(self, hint: str = "") -> tuple[str | None, dict | None]:
        """Turn a spoken room name into a configured room. No default room on purpose."""
        cleaned = (hint or "").strip()
        if not cleaned or not self.rooms:
            return None, None
        candidates = {key: [key, *value["aliases"]] for key, value in self.rooms.items()}
        matched = match_name(cleaned, candidates)
        if not matched:
            return None, None
        return matched, self.rooms[matched]

    def _resolve_live_ids(self, keys: list[str], live: dict[str, int]) -> dict[str, int | None]:
        """Match config keys against the robot's live room names."""
        candidates = {name: [name] for name in live}
        out = {}
        for key in keys:
            matched = match_name(key, candidates)
            out[key] = live[matched] if matched else None
        return out

    # --------------------------------------------------------------- plumbing

    def close(self) -> None:
        """Stop accepting commands. Safe to call more than once."""
        with self._lock:
            self._closed = True

    def _run(self, coro_factory):
        """Run one command coroutine on a worker thread with a timeout.

        One command at a time, and never more than one in flight. A timed-out
        future cannot be cancelled, so the worker keeps going; queueing behind
        it just means the next command burns its own budget waiting in line and
        reports a failure that never reached the robot. Refusing outright says
        something true instead.

        The worker is a daemon thread on purpose. A ThreadPoolExecutor's workers
        are not, and concurrent.futures joins them from an atexit hook, so a
        command still polling Gmail would hold the whole process open on Ctrl-C.
        """
        with self._lock:
            if self._closed:
                return DeebotCommandResult(False, _MSG_NOT_CONFIGURED)
            if self._busy:
                logger.warning("Deebot: a command is still running, refusing a second")
                return DeebotCommandResult(False, _MSG_BUSY)
            self._busy = True

        future: Future = Future()

        def _work():
            try:
                future.set_result(asyncio.run(coro_factory()))
            except BaseException as exc:  # noqa: BLE001 - handed to the caller below
                future.set_exception(exc)
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=_work, name="deebot", daemon=True).start()
        try:
            return future.result(timeout=self._wait_timeout_s)
        except FutureTimeoutError:
            logger.warning("Deebot command timed out after %.0fs", self._wait_timeout_s)
            return DeebotCommandResult(False, _MSG_UNREACHABLE)
        except Exception:  # noqa: BLE001 - never let a device error reach the orchestrator
            logger.exception("Deebot command failed")
            return DeebotCommandResult(False, _MSG_UNREACHABLE)

    async def _authenticate(self, authenticator) -> tuple[bool, str]:
        ok, reason = await deebot_session.authenticate(
            authenticator,
            gmail_address=self.email,
            gmail_app_password=self.gmail_app_password or None,
            verification_email_timeout_s=self.verification_email_timeout_s,
        )
        if ok and reason != deebot_session.REASON_CACHED:
            logger.info("Deebot: authenticated via %s", reason)
        return ok, reason

    async def _device(self, authenticator):
        from deebot_client.api_client import ApiClient
        from deebot_client.device import Device

        if self._device_info is None:
            devices = await ApiClient(authenticator).get_devices()
            if not devices.mqtt:
                raise LookupError("no supported Deebot on this account")
            self._device_info = devices.mqtt[0]
            logger.info(
                "Deebot: using %s (%s)",
                self._device_info.api.get("deviceName"),
                self._device_info.api.get("name"),
            )
        return Device(self._device_info, authenticator)

    async def _with_auth(self, session, fn):
        """Auth check first; on a stale token mid-command, re-auth and retry once."""
        from deebot_client.exceptions import (
            ApiError,
            ApiTimeoutError,
            AuthenticationError,
            DeviceVerificationRequiredError,
            InvalidAuthenticationError,
        )

        authenticator = await deebot_session.build_authenticator(
            session,
            device_id=deebot_session.stable_device_id(self.state_dir),
            country=self.country,
            email=self.email,
            password=self.password,
            state_dir=self.state_dir,
        )
        bot = None
        try:
            ok, reason = await self._authenticate(authenticator)
            if not ok:
                return DeebotCommandResult(False, _MSG_NEEDS_CODE)
            # Authenticator.authenticate() short-circuits on an unexpired cached
            # token without contacting Ecovacs at all, so this call may have
            # proved nothing about whether the token is still good.
            used_cached_token = reason == deebot_session.REASON_CACHED
            try:
                bot = await self._device(authenticator)
                return await fn(bot)
            except (
                DeviceVerificationRequiredError,
                InvalidAuthenticationError,
                LookupError,
            ) as exc:
                # LookupError is here because a rejected token does not always
                # arrive as an auth error. The portal answers an unauthorised
                # request with HTTP 200 and a body carrying no "devices" key,
                # which ApiClient turns into an empty device list and _device
                # into LookupError. Retrying a cached token costs one login;
                # retrying a token minted this call cannot help, and would hit
                # the gated login endpoint for nothing.
                if isinstance(exc, LookupError) and not used_cached_token:
                    raise
                logger.info(
                    "Deebot: token rejected mid-command (%r), re-authenticating once", exc
                )
                if bot is not None:
                    await bot.teardown()
                    bot = None
                deebot_session.clear_cached_credentials(self.state_dir)
                authenticator._credentials = None  # noqa: SLF001
                self._device_info = None
                ok, _ = await self._authenticate(authenticator)
                if not ok:
                    return DeebotCommandResult(False, _MSG_NEEDS_CODE)
                bot = await self._device(authenticator)
                return await fn(bot)
        except DeviceVerificationRequiredError as exc:
            # Must precede AuthenticationError, which it subclasses.
            logger.warning("Deebot still needs device verification: %r", exc)
            return DeebotCommandResult(False, _MSG_NEEDS_CODE)
        except AuthenticationError as exc:
            # Not an ApiError -- they are siblings under DeebotError, so without
            # this branch a wrong password escaped to _run's blanket handler and
            # logged a traceback every turn under a generic "unreachable" line.
            logger.warning("Deebot login rejected: %r", exc)
            return DeebotCommandResult(False, _MSG_BAD_LOGIN)
        except (ApiTimeoutError, ApiError, LookupError, OSError, asyncio.TimeoutError) as exc:
            logger.warning("Deebot unreachable: %r", exc)
            return DeebotCommandResult(False, _MSG_UNREACHABLE)
        finally:
            if bot is not None:
                try:
                    await bot.teardown()
                except Exception:  # noqa: BLE001
                    pass

    async def _command(self, fn):
        import aiohttp

        async with aiohttp.ClientSession() as session:
            return await self._with_auth(session, fn)

    @staticmethod
    async def _await_event(bot, event_type, timeout_s: float = 6.0, settle_s: float = 0.0):
        """Read one event type off a fresh Device.

        Subscribing as the first listener makes the event bus run that event's
        refresh commands itself (deebot_client/event_bus.py `subscribe`), over
        REST, so no explicit get-command is needed. `settle_s` keeps listening
        after the first event and returns the last one: StateEvent is fed by two
        commands (GetChargeState, then GetCleanInfo) and only the pair together
        tells docked from idle.
        """
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        got = []

        async def _on_event(event):
            got.append(event)
            if not first.done():
                first.set_result(None)

        unsubscribe = bot.events.subscribe(event_type, _on_event)
        try:
            await asyncio.wait_for(first, timeout=timeout_s)
            if settle_s:
                await asyncio.sleep(settle_s)
            return got[-1]
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()

    async def _fetch_live_rooms(self, bot) -> dict[str, int]:
        from deebot_client.events import RoomsEvent

        event = await self._await_event(bot, RoomsEvent, timeout_s=10.0)
        if event is None:
            return {}
        live = {
            room.name.strip(): room.id
            for room in event.rooms
            if room.name and room.name.strip().lower() != _UNNAMED_ROOM
        }
        self._live_rooms = live
        self._live_rooms_at = time.monotonic()
        return live

    async def _live_rooms_cached(self, bot) -> dict[str, int]:
        if self._live_rooms and time.monotonic() - self._live_rooms_at < _LIVE_ROOMS_TTL_S:
            return self._live_rooms
        return await self._fetch_live_rooms(bot)

    # ----------------------------------------------------------------- public

    def status(self) -> VacuumStatus:
        if not self.enabled:
            return VacuumStatus(available=False, message=_MSG_NOT_CONFIGURED)

        async def _status(bot) -> VacuumStatus:
            from deebot_client.events import BatteryEvent, ErrorEvent, StateEvent

            battery = await self._await_event(bot, BatteryEvent)
            if battery is None:
                return VacuumStatus(available=False)

            state = await self._await_event(bot, StateEvent, settle_s=1.5)
            error_event = await self._await_event(bot, ErrorEvent)

            word = state.state.name.lower() if state is not None else None
            error = None
            if error_event is not None and _as_int(error_event.code, 0) != 0:
                error = error_event.description or f"error code {error_event.code}"
                word = "error"
            return VacuumStatus(
                available=True, state=word, battery=int(battery.value), error=error
            )

        result = self._run(lambda: self._command(_status))
        if isinstance(result, VacuumStatus):
            return result
        # Carry the failure's own line through. Flattening it here used to make
        # _MSG_NEEDS_CODE unspeakable on every path: the dispatcher gates each
        # clean on status(), so "needs a verification code" always came out as
        # "I couldn't reach the vacuum" -- a network answer to a login problem.
        message = result.message if isinstance(result, DeebotCommandResult) else ""
        return VacuumStatus(available=False, message=message)

    def clean_all(self) -> DeebotCommandResult:
        return self._simple("clean_all")

    def stop(self) -> DeebotCommandResult:
        return self._simple("stop")

    def dock(self) -> DeebotCommandResult:
        return self._simple("dock")

    def _simple(self, what: str) -> DeebotCommandResult:
        if not self.enabled:
            return DeebotCommandResult(False, _MSG_NOT_CONFIGURED)

        async def _do(bot) -> DeebotCommandResult:
            from deebot_client.commands.json.charge import Charge
            from deebot_client.commands.json.clean import Clean
            from deebot_client.models import CleanAction

            command = {
                "clean_all": lambda: Clean(CleanAction.START),
                "stop": lambda: Clean(CleanAction.STOP),
                "dock": lambda: Charge(),
            }[what]()
            raw = await bot.execute_command(command)
            if _ok(raw):
                return DeebotCommandResult(True)
            logger.warning("Deebot %s rejected: %r", what, raw)
            return DeebotCommandResult(False, _MSG_REJECTED)

        return self._run(lambda: self._command(_do))

    def clean_rooms(self, keys: list[str]) -> DeebotCommandResult:
        """Clean configured rooms by key. Cached ids first, live names as fallback."""
        if not self.enabled:
            return DeebotCommandResult(False, _MSG_NOT_CONFIGURED)
        keys = [k for k in keys if k in self.rooms]
        if not keys:
            return DeebotCommandResult(False, _MSG_UNKNOWN_ROOM)

        async def _do(bot) -> DeebotCommandResult:
            from deebot_client.commands.json.clean import CleanArea
            from deebot_client.models import CleanMode

            ids: dict[str, int | None] = {k: self.rooms[k]["id"] for k in keys}
            missing = [k for k, v in ids.items() if v is None]
            if missing:
                live = await self._live_rooms_cached(bot)
                ids.update(self._resolve_live_ids(missing, live))
                still_missing = [k for k in missing if ids[k] is None]
                if still_missing:
                    logger.warning("Deebot: no live room matches %s", still_missing)
                    return DeebotCommandResult(False, _MSG_UNKNOWN_ROOM)

            raw = await bot.execute_command(
                CleanArea(CleanMode.SPOT_AREA, [ids[k] for k in keys])
            )
            if _ok(raw):
                return DeebotCommandResult(True)

            # A cached id the robot rejects is what a remap looks like from here.
            # Re-resolve every key against the live names and try exactly once more.
            logger.warning("Deebot: room clean rejected (%r), re-resolving live ids", raw)
            live = await self._fetch_live_rooms(bot)
            fresh = self._resolve_live_ids(keys, live)
            if any(v is None for v in fresh.values()) or fresh == ids:
                return DeebotCommandResult(False, _MSG_REJECTED)
            raw = await bot.execute_command(
                CleanArea(CleanMode.SPOT_AREA, [fresh[k] for k in keys])
            )
            if _ok(raw):
                logger.warning(
                    "Deebot: cached room ids are stale, update deebot.rooms in config.yaml "
                    "(uv run python tools/probe_deebot_rooms.py)"
                )
                return DeebotCommandResult(True)
            return DeebotCommandResult(False, _MSG_REJECTED)

        return self._run(lambda: self._command(_do))

    def sync_rooms(self) -> dict[str, int]:
        """Live `name -> id` from the robot. Empty when disabled or unreachable."""
        if not self.enabled:
            return {}

        async def _do(bot) -> dict[str, int]:
            return await self._fetch_live_rooms(bot)

        result = self._run(lambda: self._command(_do))
        return result if isinstance(result, dict) else {}

    def check_room_drift(self) -> list[str]:
        """Config keys whose cached id no longer matches the live room of that name."""
        live = self.sync_rooms()
        if not live:
            return []
        resolved = self._resolve_live_ids(list(self.rooms), live)
        drifted = []
        for key, room in self.rooms.items():
            cached = room["id"]
            live_id = resolved.get(key)
            if cached is not None and live_id is not None and cached != live_id:
                drifted.append(key)
        if drifted:
            logger.warning(
                "Deebot: cached room ids differ from the robot for %s. "
                "Update deebot.rooms in config.yaml: uv run python tools/probe_deebot_rooms.py",
                ", ".join(drifted),
            )
        return drifted
