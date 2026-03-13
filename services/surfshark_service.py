import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


log = logging.getLogger(__name__)


@dataclass
class VpnStatus:
    connected: bool
    country: str | None
    source: str
    updated_at: str | None = None
    cache_source: str | None = None


@dataclass
class EnsureVpnResult:
    success: bool
    target_country: str
    current_country: str | None = None
    switched: bool = False
    skipped: bool = False
    message: str | None = None


@dataclass
class SurfsharkUiNode:
    text: str
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass
class SurfsharkRouteResult:
    success: bool
    route_name: str
    sequence: list[str]
    used_recovery: bool = False
    message: str | None = None


@dataclass
class SurfsharkRouteDefinition:
    name: str
    aliases: list[str]
    sequence: list[str]
    assumed_country: str | None = None
    launch_mode: str = "package"
    launch_delay_s: float = 0.0
    force_stop_before_launch: bool = False
    wait_for_ready: bool = False
    pre_sequence_wait_s: float = 0.0
    key_delay_s: float = 0.0
    post_sequence_wait_s: float = 0.0
    settle_wait_s: float = 0.0
    retry_force_stop_on_failure: bool = False


class SurfsharkService:
    def __init__(self, config: dict, media_service):
        self.config = config
        self.media_service = media_service

        media_cfg = config.get("media", {})
        self.enabled = bool(media_cfg.get("vpn_routing_enabled", False))
        self.vpn_state_path = Path(media_cfg.get("vpn_state_path", "vpn_state.json"))
        self.cache_max_age = timedelta(
            minutes=int(media_cfg.get("vpn_status_cache_max_age_minutes", 5))
        )
        self.failure_policy = str(media_cfg.get("vpn_failure_policy", "open_anyway")).strip().lower()
        self.route_by_app = {
            self._normalize_text(app_name): self._normalize_text(route_name)
            for app_name, route_name in (
                media_cfg.get(
                    "vpn_route_by_app",
                    {
                        "youtube": "restart_autoconnect",
                        "stremio": "quick_connect",
                    },
                ) or {}
            ).items()
            if self._normalize_text(app_name) and self._normalize_text(route_name)
        }

        self.surfshark_launch_delay_s = int(media_cfg.get("surfshark_launch_delay_ms", 2500)) / 1000
        self.surfshark_connect_timeout_s = int(media_cfg.get("surfshark_connect_timeout_ms", 15000)) / 1000
        self.surfshark_status_poll_interval_s = int(
            media_cfg.get("surfshark_status_poll_interval_ms", 1000)
        ) / 1000
        self.surfshark_pre_sequence_wait_s = int(
            media_cfg.get("surfshark_pre_sequence_wait_ms", 1200)
        ) / 1000
        self.surfshark_key_delay_s = int(media_cfg.get("surfshark_key_delay_ms", 350)) / 1000
        self.surfshark_post_sequence_wait_s = int(
            media_cfg.get("surfshark_post_sequence_wait_ms", 2500)
        ) / 1000
        self.surfshark_restart_autoconnect_wait_s = int(
            media_cfg.get("surfshark_restart_autoconnect_wait_ms", 4000)
        ) / 1000
        self.surfshark_ready_timeout_s = int(
            media_cfg.get("surfshark_ready_timeout_ms", 8000)
        ) / 1000
        self.surfshark_ready_poll_interval_s = int(
            media_cfg.get("surfshark_ready_poll_interval_ms", 500)
        ) / 1000
        self.surfshark_ready_stable_polls = max(
            1, int(media_cfg.get("surfshark_ready_stable_polls", 2))
        )
        self.surfshark_ready_settle_s = int(
            media_cfg.get("surfshark_ready_settle_ms", 1500)
        ) / 1000
        self.surfshark_retry_count = max(0, int(media_cfg.get("surfshark_retry_count", 1)))
        self.surfshark_debug_capture_enabled = bool(
            media_cfg.get("surfshark_debug_capture_enabled", False)
        )
        self.surfshark_debug_capture_dir = Path(
            media_cfg.get("surfshark_debug_capture_dir", "debug/surfshark")
        )
        configured_route_path = Path(
            media_cfg.get("surfshark_route_table_path", "surfshark_routes.json")
        )
        if configured_route_path.is_absolute():
            self.route_table_path = configured_route_path
        else:
            project_relative = Path(__file__).resolve().parents[1] / configured_route_path
            self.route_table_path = (
                project_relative if project_relative.exists() else configured_route_path
            )

        self.country_aliases = self._build_country_aliases(
            media_cfg.get(
                "surfshark_country_aliases",
                {
                    "albania": ["albania"],
                    "portugal": ["portugal"],
                },
            )
        )
        self.connected_markers = self._normalize_markers(
            media_cfg.get("surfshark_connected_markers", ["connected", "protected"])
        )
        self.disconnected_markers = self._normalize_markers(
            media_cfg.get("surfshark_disconnected_markers", ["disconnected", "not connected", "unprotected"])
        )
        self.route_definitions = self._load_route_definitions(media_cfg)

    def _normalize_text(self, value: str | None) -> str:
        return re.sub(r"\s+", " ", (value or "").strip().lower())

    def _normalize_key_name(self, key_name: str | None) -> str:
        normalized = self._normalize_text(key_name).replace(" ", "_")
        if normalized.startswith("keycode_"):
            normalized = normalized[len("keycode_"):]
        return normalized.upper()

    def _normalize_markers(self, markers: list[str] | tuple[str, ...]) -> list[str]:
        normalized = []
        for marker in markers or []:
            value = self._normalize_text(marker)
            if value and value not in normalized:
                normalized.append(value)
        return normalized

    def _normalize_sequence(self, sequence: list[str] | tuple[str, ...]) -> list[str]:
        normalized = []
        for item in sequence or []:
            key_name = self._normalize_key_name(item)
            if key_name:
                normalized.append(key_name)
        return normalized

    def _build_country_aliases(self, configured_aliases: dict) -> dict[str, list[str]]:
        aliases = {}
        for country, values in (configured_aliases or {}).items():
            normalized_country = self._normalize_text(country)
            if not normalized_country:
                continue
            normalized_values = []
            for value in list(values or []) + [country]:
                normalized = self._normalize_text(value)
                if normalized and normalized not in normalized_values:
                    normalized_values.append(normalized)
            aliases[normalized_country] = normalized_values
        return aliases

    def _milliseconds_to_seconds(self, value: int | float | None, default_s: float) -> float:
        if value is None:
            return default_s
        try:
            return max(0.0, float(value) / 1000)
        except (TypeError, ValueError):
            return default_s

    def _legacy_route_defaults(self, media_cfg: dict) -> dict[str, SurfsharkRouteDefinition]:
        return {
            "restart_autoconnect": SurfsharkRouteDefinition(
                name="restart_autoconnect",
                aliases=self._normalize_markers(
                    media_cfg.get(
                        "surfshark_restart_autoconnect_aliases",
                        ["restart_autoconnect", "restart autoconnect", "autoconnect", "albania"],
                    )
                ),
                sequence=[],
                assumed_country="albania",
                launch_mode="package",
                launch_delay_s=0.0,
                force_stop_before_launch=True,
                wait_for_ready=False,
                settle_wait_s=self.surfshark_restart_autoconnect_wait_s,
                retry_force_stop_on_failure=False,
            ),
            "quick_connect": SurfsharkRouteDefinition(
                name="quick_connect",
                aliases=self._normalize_markers(
                    media_cfg.get(
                        "surfshark_quick_connect_aliases",
                        ["quick_connect", "quick connect", "fastest", "fastest location"],
                    )
                ),
                sequence=self._normalize_sequence(
                    media_cfg.get(
                        "surfshark_quick_connect_sequence",
                        ["DPAD_CENTER", "DPAD_DOWN", "DPAD_DOWN", "DPAD_CENTER"],
                    )
                ),
                assumed_country="portugal",
                launch_mode="package",
                launch_delay_s=self.surfshark_launch_delay_s,
                force_stop_before_launch=False,
                wait_for_ready=True,
                pre_sequence_wait_s=self.surfshark_pre_sequence_wait_s,
                key_delay_s=self.surfshark_key_delay_s,
                post_sequence_wait_s=self.surfshark_post_sequence_wait_s,
                settle_wait_s=0.0,
                retry_force_stop_on_failure=True,
            ),
        }

    def _parse_route_definition(
        self,
        route_name: str,
        payload: dict,
        base: SurfsharkRouteDefinition,
    ) -> SurfsharkRouteDefinition:
        aliases = self._normalize_markers(payload.get("aliases", base.aliases))
        if route_name not in aliases:
            aliases.append(route_name)

        return SurfsharkRouteDefinition(
            name=route_name,
            aliases=aliases,
            sequence=self._normalize_sequence(payload.get("sequence", base.sequence)),
            assumed_country=self._normalize_text(payload.get("assumed_country", base.assumed_country)) or None,
            launch_mode=self._normalize_text(payload.get("launch_mode", base.launch_mode)) or base.launch_mode,
            launch_delay_s=self._milliseconds_to_seconds(
                payload.get("launch_delay_ms"),
                base.launch_delay_s,
            ),
            force_stop_before_launch=bool(
                payload.get("force_stop_before_launch", base.force_stop_before_launch)
            ),
            wait_for_ready=bool(payload.get("wait_for_ready", base.wait_for_ready)),
            pre_sequence_wait_s=self._milliseconds_to_seconds(
                payload.get("pre_sequence_wait_ms"),
                base.pre_sequence_wait_s,
            ),
            key_delay_s=self._milliseconds_to_seconds(
                payload.get("key_delay_ms"),
                base.key_delay_s,
            ),
            post_sequence_wait_s=self._milliseconds_to_seconds(
                payload.get("post_sequence_wait_ms"),
                base.post_sequence_wait_s,
            ),
            settle_wait_s=self._milliseconds_to_seconds(
                payload.get("settle_wait_ms"),
                base.settle_wait_s,
            ),
            retry_force_stop_on_failure=bool(
                payload.get("retry_force_stop_on_failure", base.retry_force_stop_on_failure)
            ),
        )

    def _load_route_definitions(self, media_cfg: dict) -> dict[str, SurfsharkRouteDefinition]:
        definitions = self._legacy_route_defaults(media_cfg)
        if not self.route_table_path.exists():
            return definitions

        try:
            payload = json.loads(self.route_table_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed loading Surfshark route table %s: %s", self.route_table_path, exc)
            return definitions

        route_items = payload.get("routes", payload)
        if not isinstance(route_items, dict):
            log.warning("Invalid Surfshark route table format in %s", self.route_table_path)
            return definitions

        for route_name, route_payload in route_items.items():
            normalized_name = self._normalize_text(route_name)
            if not normalized_name or not isinstance(route_payload, dict):
                continue
            base = definitions.get(
                normalized_name,
                SurfsharkRouteDefinition(
                    name=normalized_name,
                    aliases=[normalized_name],
                    sequence=[],
                    launch_delay_s=self.surfshark_launch_delay_s,
                ),
            )
            definitions[normalized_name] = self._parse_route_definition(
                normalized_name,
                route_payload,
                base,
            )

        return definitions

    def _route_definition(self, route_name: str | None) -> SurfsharkRouteDefinition | None:
        normalized = self._normalize_text(route_name)
        if not normalized:
            return None
        return self.route_definitions.get(normalized)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _is_cache_fresh(self, status: VpnStatus) -> bool:
        timestamp = self._parse_timestamp(status.updated_at)
        if timestamp is None:
            return False
        return (datetime.now(timezone.utc) - timestamp) <= self.cache_max_age

    def _is_authoritative_cache(self, status: VpnStatus | None) -> bool:
        if not status or status.source != "cache":
            return False
        return status.cache_source == "surfshark_ui"

    def _resolve_route_name(self, target_route: str | None) -> str | None:
        normalized = self._normalize_text(target_route)
        if not normalized:
            return None
        for route_name, route_definition in self.route_definitions.items():
            if normalized == route_name or normalized in route_definition.aliases:
                return route_name
        return None

    def get_status(self, force_refresh: bool = False) -> VpnStatus:
        if not self.enabled or not self.media_service:
            return VpnStatus(
                connected=False,
                country=None,
                source="disabled",
                updated_at=self._now_iso(),
            )

        cached = self._load_cached_status()
        if (
            cached
            and not force_refresh
            and self._is_cache_fresh(cached)
            and self._is_authoritative_cache(cached)
        ):
            log.debug("Surfshark authoritative cache hit: %s", cached)
            return cached

        return self._refresh_status_from_ui(launch=True)

    def ensure_route(self, target_route: str, force_switch: bool = False) -> EnsureVpnResult:
        normalized_route = self._resolve_route_name(target_route)
        if not normalized_route:
            return EnsureVpnResult(
                success=False,
                target_country=target_route,
                message=f"I don't have a Surfshark route for {target_route}.",
            )

        if not self.enabled or not self.media_service:
            return EnsureVpnResult(
                success=True,
                target_country=normalized_route,
                skipped=True,
                message="VPN routing is disabled.",
            )

        route_definition = self._route_definition(normalized_route)
        if normalized_route == "restart_autoconnect":
            result = self._run_restart_autoconnect_route()
            return EnsureVpnResult(
                success=result.success,
                target_country=normalized_route,
                current_country=route_definition.assumed_country if route_definition and result.success else None,
                switched=result.success,
                message=result.message,
            )

        result = self._run_quick_connect_route(force_restart=bool(force_switch))
        return EnsureVpnResult(
            success=result.success,
            target_country=normalized_route,
            current_country=route_definition.assumed_country if route_definition and result.success else None,
            switched=result.success,
            message=result.message,
        )

    def ensure_country(self, target_country: str, force_switch: bool = False) -> EnsureVpnResult:
        return self.ensure_route(target_country, force_switch=force_switch)

    def debug_route(
        self,
        route_name: str,
        capture: bool | None = None,
        capture_dir: str | Path | None = None,
        pause_between_steps_s: float = 0.0,
        force_restart: bool = False,
    ) -> dict:
        normalized_route = self._resolve_route_name(route_name)
        if not normalized_route:
            return {
                "success": False,
                "route": self._normalize_text(route_name),
                "sequence_name": None,
                "sequence": [],
                "captures": [],
                "used_recovery": False,
                "message": f"I don't have a Surfshark route for {route_name}.",
            }

        capture_enabled = self.surfshark_debug_capture_enabled if capture is None else bool(capture)
        capture_paths: list[str] = []
        capture_hook: Callable[[str], None] | None = None

        if capture_enabled:
            capture_root = Path(capture_dir) if capture_dir else self.surfshark_debug_capture_dir
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            run_dir = capture_root / f"{normalized_route}_{timestamp}"

            def capture_stage(stage_name: str):
                stage_path = run_dir / f"{stage_name}.png"
                if self.media_service.capture_screenshot(stage_path):
                    capture_paths.append(str(stage_path))
                    log.info("Surfshark debug capture saved to %s", stage_path)

            capture_hook = capture_stage

        if normalized_route == "restart_autoconnect":
            result = self._run_restart_autoconnect_route(capture_hook=capture_hook)
        else:
            result = self._run_quick_connect_route(
                capture_hook=capture_hook,
                pause_between_steps_s=max(0.0, pause_between_steps_s),
                force_restart=force_restart,
            )
        route_definition = self._route_definition(normalized_route)

        return {
            "success": result.success,
            "route": normalized_route,
            "sequence_name": result.route_name,
            "sequence": list(route_definition.sequence) if route_definition else [],
            "captures": capture_paths,
            "used_recovery": result.used_recovery,
            "message": result.message,
        }

    def debug_sequence(
        self,
        route_name: str,
        capture: bool | None = None,
        capture_dir: str | Path | None = None,
        pause_between_steps_s: float = 0.0,
        force_restart: bool = False,
    ) -> dict:
        return self.debug_route(
            route_name=route_name,
            capture=capture,
            capture_dir=capture_dir,
            pause_between_steps_s=pause_between_steps_s,
            force_restart=force_restart,
        )

    def _load_cached_status(self) -> VpnStatus | None:
        if not self.vpn_state_path.exists():
            return None
        try:
            payload = json.loads(self.vpn_state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        return VpnStatus(
            connected=bool(payload.get("connected", False)),
            country=self._normalize_text(payload.get("country")) or None,
            source="cache",
            updated_at=payload.get("updated_at"),
            cache_source=self._normalize_text(payload.get("source")) or None,
        )

    def _write_cached_status(self, status: VpnStatus) -> None:
        try:
            self.vpn_state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "connected": bool(status.connected),
                "country": self._normalize_text(status.country) or None,
                "updated_at": status.updated_at or self._now_iso(),
                "source": status.cache_source or status.source,
            }
            self.vpn_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Failed writing Surfshark cache %s: %s", self.vpn_state_path, exc)

    def _write_diagnostic_status(self, route_name: str, assumed_country: str | None) -> None:
        self._write_cached_status(
            VpnStatus(
                connected=True,
                country=assumed_country,
                source="surfshark_route",
                updated_at=self._now_iso(),
                cache_source=f"surfshark_route_{route_name}",
            )
        )

    def _open_surfshark(
        self,
        route_definition: SurfsharkRouteDefinition | None = None,
        delay_s: float | None = None,
    ) -> tuple[bool, str]:
        launch_mode = route_definition.launch_mode if route_definition else "configured"
        if launch_mode == "package" and hasattr(self.media_service, "launch_package"):
            ok, message = self.media_service.launch_package("surfshark")
        else:
            ok, message = self.media_service.launch_app("surfshark")
        log.info("Surfshark launch result: mode=%s ok=%s message=%s", launch_mode, ok, message)
        launch_delay_s = (
            route_definition.launch_delay_s
            if route_definition is not None and delay_s is None
            else self.surfshark_launch_delay_s if delay_s is None else max(0.0, delay_s)
        )
        if ok and launch_delay_s > 0:
            time.sleep(launch_delay_s)
        return ok, message

    def _run_restart_autoconnect_route(
        self,
        capture_hook: Callable[[str], None] | None = None,
    ) -> SurfsharkRouteResult:
        route_definition = self._route_definition("restart_autoconnect")
        if capture_hook:
            capture_hook("attempt_1_before_launch")

        if route_definition and route_definition.force_stop_before_launch:
            stopped = self.media_service.force_stop_app("surfshark")
            log.info("Surfshark restart_autoconnect force-stop result: %s", stopped)
            if not stopped:
                return SurfsharkRouteResult(
                    success=False,
                    route_name="restart_autoconnect",
                    sequence=[],
                    message="I couldn't restart Surfshark.",
                )
            time.sleep(0.5)

        ok, message = self._open_surfshark(route_definition=route_definition)
        if not ok:
            return SurfsharkRouteResult(
                success=False,
                route_name="restart_autoconnect",
                sequence=[],
                message=message or "I couldn't open Surfshark.",
            )

        if capture_hook:
            capture_hook("attempt_1_after_launch")

        log.info("Surfshark restart_autoconnect skipping readiness poll and continuing in background")

        if route_definition and route_definition.settle_wait_s > 0:
            time.sleep(route_definition.settle_wait_s)

        if capture_hook:
            capture_hook("attempt_1_after_restart_wait")

        self._write_diagnostic_status(
            "restart_autoconnect",
            route_definition.assumed_country if route_definition else "albania",
        )
        return SurfsharkRouteResult(
            success=True,
            route_name="restart_autoconnect",
            sequence=[],
        )

    def _run_quick_connect_route(
        self,
        capture_hook: Callable[[str], None] | None = None,
        pause_between_steps_s: float = 0.0,
        force_restart: bool = False,
    ) -> SurfsharkRouteResult:
        route_definition = self._route_definition("quick_connect")
        if not route_definition:
            return SurfsharkRouteResult(
                success=False,
                route_name="quick_connect",
                sequence=[],
                message="Surfshark Quick Connect is not configured.",
            )

        attempts = 1 + (max(0, self.surfshark_retry_count) if route_definition.retry_force_stop_on_failure else 0)
        last_message = "Surfshark Quick Connect failed."

        for attempt in range(1, attempts + 1):
            stage_prefix = f"attempt_{attempt}"
            if capture_hook:
                capture_hook(f"{stage_prefix}_before_launch")

            should_restart = force_restart or route_definition.force_stop_before_launch or attempt > 1
            if should_restart:
                stopped = self.media_service.force_stop_app("surfshark")
                log.info("Surfshark quick_connect force-stop on attempt %d: %s", attempt, stopped)
                time.sleep(0.5)

            ok, message = self._open_surfshark(route_definition=route_definition)
            if not ok:
                last_message = message or "I couldn't open Surfshark."
                continue

            if capture_hook:
                capture_hook(f"{stage_prefix}_after_launch")

            if route_definition.wait_for_ready:
                ready = self._wait_for_surfshark_ready()
                log.info("Surfshark quick_connect readiness gate on attempt %d: %s", attempt, ready)
                if not ready:
                    last_message = "Surfshark did not reach its TV home screen in time."
                    continue

            if route_definition.pre_sequence_wait_s > 0:
                time.sleep(route_definition.pre_sequence_wait_s)
            if capture_hook:
                capture_hook(f"{stage_prefix}_after_pre_wait")

            dispatch_error = self._dispatch_sequence(
                route_name="quick_connect",
                sequence=route_definition.sequence,
                attempt=attempt,
                key_delay_s=route_definition.key_delay_s,
                capture_hook=capture_hook,
                pause_between_steps_s=pause_between_steps_s,
            )
            if dispatch_error:
                last_message = dispatch_error
                continue

            if route_definition.post_sequence_wait_s > 0:
                time.sleep(route_definition.post_sequence_wait_s)
            if capture_hook:
                capture_hook(f"{stage_prefix}_after_post_wait")

            self._write_diagnostic_status("quick_connect", route_definition.assumed_country)
            return SurfsharkRouteResult(
                success=True,
                route_name="quick_connect",
                sequence=list(route_definition.sequence),
                used_recovery=attempt > 1,
            )

        return SurfsharkRouteResult(
            success=False,
            route_name="quick_connect",
            sequence=list(route_definition.sequence),
            used_recovery=attempts > 1,
            message=last_message,
        )

    def _wait_for_surfshark_ready(self) -> bool:
        deadline = time.monotonic() + max(self.surfshark_ready_timeout_s, 0)
        stable_hits = 0
        last_focus = ""

        while time.monotonic() <= deadline:
            focus = self._normalize_text(
                getattr(self.media_service, "get_current_focus", lambda: "")() or ""
            )
            if focus:
                log.info("Surfshark readiness focus poll: %s", focus)
            is_main = "surfshark" in focus and "tvmainactivity" in focus
            if is_main:
                if focus == last_focus:
                    stable_hits += 1
                else:
                    last_focus = focus
                    stable_hits = 1
                if stable_hits >= self.surfshark_ready_stable_polls:
                    if self.surfshark_ready_settle_s > 0:
                        time.sleep(self.surfshark_ready_settle_s)
                    return True
            else:
                stable_hits = 0
                last_focus = focus

            if self.surfshark_ready_poll_interval_s > 0:
                time.sleep(self.surfshark_ready_poll_interval_s)

        return False

    def _dispatch_sequence(
        self,
        route_name: str,
        sequence: list[str],
        attempt: int,
        key_delay_s: float = 0.0,
        capture_hook: Callable[[str], None] | None = None,
        pause_between_steps_s: float = 0.0,
    ) -> str | None:
        for index, key_name in enumerate(sequence, start=1):
            if not self.media_service.keyevent(key_name):
                log.warning(
                    "Surfshark route %s failed on attempt %d while sending %s",
                    route_name,
                    attempt,
                    key_name,
                )
                return f"I couldn't send Surfshark key {key_name}."
            if capture_hook:
                capture_hook(f"attempt_{attempt}_step_{index}_{key_name.lower()}")
            if pause_between_steps_s > 0:
                time.sleep(pause_between_steps_s)
            elif key_delay_s > 0 and index < len(sequence):
                time.sleep(key_delay_s)
        return None

    def _refresh_status_from_ui(self, launch: bool = False) -> VpnStatus:
        if launch:
            ok, message = self._open_surfshark()
            if not ok:
                status = VpnStatus(
                    connected=False,
                    country=None,
                    source="surfshark_ui",
                    updated_at=self._now_iso(),
                    cache_source="surfshark_ui",
                )
                log.warning("Surfshark UI refresh launch failed: %s", message)
                return status

        xml = self.media_service.dump_ui_hierarchy()
        if not xml:
            status = VpnStatus(
                connected=False,
                country=None,
                source="surfshark_ui",
                updated_at=self._now_iso(),
                cache_source="surfshark_ui",
            )
            self._write_cached_status(status)
            return status

        status = self._parse_status_from_xml(xml)
        status.updated_at = self._now_iso()
        status.source = "surfshark_ui"
        status.cache_source = "surfshark_ui"
        self._write_cached_status(status)
        return status

    def _parse_status_from_xml(self, xml: str) -> VpnStatus:
        nodes = self._extract_nodes(xml)
        normalized_texts = [self._normalize_text(node.text) for node in nodes if self._normalize_text(node.text)]
        if not nodes:
            return VpnStatus(connected=False, country=None, source="surfshark_ui")

        max_y = max(node.y2 for node in nodes)
        status_cutoff = max_y * 0.45
        status_nodes = [node for node in nodes if node.y1 <= status_cutoff]
        status_texts = [
            self._normalize_text(node.text) for node in status_nodes if self._normalize_text(node.text)
        ]
        all_texts = normalized_texts or status_texts

        has_connected_marker = any(marker in text for text in all_texts for marker in self.connected_markers)
        has_disconnected_marker = any(
            marker in text for text in all_texts for marker in self.disconnected_markers
        )
        connected = has_connected_marker and not has_disconnected_marker
        country = self._detect_country(status_texts) or self._detect_country(all_texts)

        return VpnStatus(
            connected=connected,
            country=country,
            source="surfshark_ui",
        )

    def _detect_country(self, texts: list[str]) -> str | None:
        for text in texts or []:
            normalized_text = self._normalize_text(text)
            for country, aliases in self.country_aliases.items():
                if any(alias == normalized_text or alias in normalized_text for alias in aliases):
                    return country
        return None

    def _extract_nodes(self, xml: str) -> list[SurfsharkUiNode]:
        if not xml:
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            log.warning("Failed parsing Surfshark UI XML: %s", exc)
            return []

        nodes: list[SurfsharkUiNode] = []
        for elem in root.iter("node"):
            text = elem.attrib.get("text") or elem.attrib.get("content-desc") or ""
            bounds = elem.attrib.get("bounds") or ""
            parsed = self._parse_bounds(bounds)
            if not parsed:
                continue
            x1, y1, x2, y2 = parsed
            nodes.append(SurfsharkUiNode(text=text, x1=x1, y1=y1, x2=x2, y2=y2))
        return nodes

    def _parse_bounds(self, bounds: str) -> tuple[int, int, int, int] | None:
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
        if not match:
            return None
        return tuple(int(group) for group in match.groups())
