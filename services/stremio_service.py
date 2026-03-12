import io
import json
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

AUTOPLAY_FALLBACK_LINE = "Stremio's open but it didn't start on its own. Just hit OK on the remote."
UNKNOWN_SOURCE_CONFIRMATION_TEMPLATE = (
    "I couldn't find Comet or MediaFusion for {title}. Want me to try the first available source?"
)
DEFAULT_PROVIDER_PREFERENCES = ["comet", "mediafusion"]
DEFAULT_PROVIDER_ALIASES = {
    "comet": ["comet"],
    "mediafusion": ["mediafusion", "media fusion"],
}
GENERIC_UI_LABELS = {
    "continue",
    "play",
    "resume",
    "trailer",
    "details",
    "episodes",
    "season",
    "seasons",
    "watch",
    "watchnow",
    "back",
    "search",
    "home",
    "settings",
    "cancel",
    "ok",
}
UI_DUMP_REMOTE_PATH = "/sdcard/window_dump.xml"
IMDB_ID_RE = re.compile(r"(tt\d+)")


@dataclass
class SourceCandidate:
    label: str
    center_x: int
    center_y: int
    provider_key: str


@dataclass
class StremioPlayResult:
    success: bool
    message: str | None = None
    played_source: str | None = None
    requires_confirmation: bool = False

    def __bool__(self) -> bool:
        return self.success


class StremioService:
    def __init__(self, config: dict, media_service=None):
        self.config = config
        self.media_service = media_service

        stremio_cfg = config.get("stremio", {})
        self.email = os.getenv("STREMIO_EMAIL") or stremio_cfg.get("email")
        self.password = os.getenv("STREMIO_PASSWORD") or stremio_cfg.get("password")
        self.autoplay_delay_ms = int(stremio_cfg.get("autoplay_delay_ms", 2500))
        self.history_refresh_max_age = timedelta(
            minutes=int(stremio_cfg.get("history_refresh_max_age_minutes", 5))
        )
        self.provider_scan_pages = max(1, int(stremio_cfg.get("provider_scan_pages", 3)))
        self.provider_scan_delay_ms = int(stremio_cfg.get("provider_scan_delay_ms", 800))
        self.provider_fallback_policy = str(
            stremio_cfg.get("provider_fallback_policy", "ask")
        ).strip().lower()
        self.provider_preferences = self._build_provider_preferences(
            stremio_cfg.get("provider_preferences", DEFAULT_PROVIDER_PREFERENCES)
        )
        self.provider_aliases = self._build_provider_aliases(
            stremio_cfg.get("provider_aliases", {})
        )
        self.ocr_enabled = bool(stremio_cfg.get("provider_ocr_enabled", True))

        watch_state_name = stremio_cfg.get("watch_state_path", "watch_state.json")
        self.watch_state_path = Path(watch_state_name)

        media_cfg = config.get("media", {})
        self.adb_path = media_cfg.get("adb_path", "adb")
        self.adb_target = f"{media_cfg.get('mibox_ip', '')}:{media_cfg.get('adb_port', 5555)}"

        self._auth_key: str | None = None

        if self._has_auth_credentials():
            try:
                self._auth_key = self._authenticate()
                self.sync_library()
            except Exception as exc:
                log.warning("Stremio startup sync failed: %s", exc)
        else:
            log.info("Stremio credentials not set, skipping startup library sync")

    def _build_provider_preferences(self, configured: list | tuple | None) -> list[str]:
        seen = set()
        ordered = []
        for item in configured or DEFAULT_PROVIDER_PREFERENCES:
            normalized = self._normalize_provider_key(str(item))
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered or list(DEFAULT_PROVIDER_PREFERENCES)

    def _build_provider_aliases(self, configured_aliases: dict) -> dict[str, list[str]]:
        aliases: dict[str, list[str]] = {}
        all_keys = {
            self._normalize_provider_key(key)
            for key in list(DEFAULT_PROVIDER_ALIASES.keys()) + list(configured_aliases.keys())
        }

        for key in all_keys:
            values = list(DEFAULT_PROVIDER_ALIASES.get(key, []))
            values.extend(configured_aliases.get(key, []))
            values.append(key)

            normalized_values = []
            seen = set()
            for value in values:
                normalized = self._normalize_provider_key(str(value))
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    normalized_values.append(normalized)
            aliases[key] = normalized_values

        return aliases

    def _has_auth_credentials(self) -> bool:
        return bool(self.email and self.password)

    def can_sync(self) -> bool:
        return self._has_auth_credentials()

    def _authenticate(self) -> str:
        if not self._has_auth_credentials():
            raise ValueError("Stremio credentials are missing")

        resp = requests.post(
            "https://api.strem.io/api/login",
            json={
                "email": self.email,
                "password": self.password,
                "type": "Login",
            },
            timeout=10,
        )
        resp.raise_for_status()

        data = resp.json()
        auth_key = data.get("result", {}).get("authKey")
        if not auth_key:
            raise ValueError("Stremio auth succeeded but authKey was missing")
        return auth_key

    def sync_library(self) -> bool:
        """
        Fetch full Stremio library and write watch_state.json.
        Returns True when sync succeeds.
        """
        if not self._has_auth_credentials():
            log.warning("Cannot sync Stremio library: credentials are missing")
            return False

        if not self._auth_key:
            self._auth_key = self._authenticate()

        previous_state = self._load_watch_state()

        resp = requests.post(
            "https://api.strem.io/api/datastoreGet",
            json={
                "authKey": self._auth_key,
                "collection": "libraryItem",
                "ids": [],
                "all": True,
            },
            timeout=15,
        )
        resp.raise_for_status()

        items = resp.json().get("result", [])
        state = {}
        synced_previous_keys = set()
        history_updated_at = self._now_iso()

        for item in items:
            media_type = item.get("type")
            if media_type not in ("series", "movie"):
                continue

            imdb_id = self._extract_imdb_id(item)
            if not imdb_id:
                continue

            name = (item.get("name") or "").strip()
            if not name:
                continue

            previous_key, previous_entry = self._find_existing_entry(previous_state, name, imdb_id)
            if previous_key:
                synced_previous_keys.add(previous_key)

            video_id = item.get("state", {}).get("video_id", "")
            season, episode = self._extract_season_episode(item)

            time_offset = item.get("state", {}).get("timeOffset", 0) or 0
            duration = item.get("state", {}).get("duration", 0) or 0
            finished = duration > 0 and (time_offset / duration) > 0.85

            if media_type == "series" and episode is not None and finished:
                episode += 1

            key = name.lower()
            state[key] = {
                "title": name,
                "imdb_id": imdb_id,
                "type": media_type,
                "last_video_id": video_id or previous_entry.get("last_video_id"),
                "season": season,
                "episode": episode,
                "finished_last": finished,
                "last_successful_source": previous_entry.get("last_successful_source"),
                "history_updated_at": history_updated_at,
            }

        for previous_key, previous_entry in previous_state.items():
            if previous_key in synced_previous_keys or previous_key in state:
                continue
            state[previous_key] = previous_entry

        self._write_watch_state(state)
        log.info("Stremio library sync complete: %d items", len(state))
        return True

    def _extract_imdb_id(self, item: dict) -> str | None:
        candidates = [
            item.get("id"),
            item.get("_id"),
            item.get("state", {}).get("video_id"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                continue
            match = IMDB_ID_RE.search(candidate)
            if match:
                return match.group(1)
        return None

    def _extract_season_episode(self, item: dict) -> tuple[int | None, int | None]:
        state = item.get("state", {}) or {}
        video_id = state.get("video_id", "")
        parts = video_id.split(":") if isinstance(video_id, str) else []
        season = int(parts[1]) if len(parts) >= 3 and parts[1].isdigit() else None
        episode = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None

        if season is None:
            raw_season = state.get("season")
            if isinstance(raw_season, int) and raw_season > 0:
                season = raw_season

        if episode is None:
            raw_episode = state.get("episode")
            if isinstance(raw_episode, int) and raw_episode > 0:
                episode = raw_episode

        return season, episode

    def _load_watch_state(self) -> dict:
        if not self.watch_state_path.exists():
            return {}
        try:
            return json.loads(self.watch_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("watch_state.json is invalid JSON, treating it as empty")
            return {}

    def _write_watch_state(self, state: dict):
        self.watch_state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _find_existing_entry(self, state: dict, title: str, imdb_id: str) -> tuple[str | None, dict]:
        title_key = (title or "").strip().lower()
        if title_key in state:
            return title_key, state[title_key]

        for key, entry in state.items():
            if entry.get("imdb_id") == imdb_id:
                return key, entry

        return None, {}

    def _find_watch_entry(
        self, title: str, state: dict | None = None
    ) -> tuple[str | None, dict | None]:
        title_key = (title or "").strip().lower()
        if not title_key:
            return None, None

        state = state if state is not None else self._load_watch_state()

        if title_key in state:
            return title_key, state[title_key]

        for key, value in state.items():
            if title_key in key or key in title_key:
                return key, value

        return None, None

    def _find_watch_entry_by_imdb(
        self, imdb_id: str, state: dict | None = None
    ) -> tuple[str | None, dict | None]:
        if not imdb_id:
            return None, None

        state = state if state is not None else self._load_watch_state()
        for key, value in state.items():
            if value.get("imdb_id") == imdb_id:
                return key, value
        return None, None

    def get_progress(self, title: str, refresh_if_stale: bool = False) -> dict | None:
        if refresh_if_stale:
            self._ensure_fresh_history(title)
        _, entry = self._find_watch_entry(title)
        return entry

    def _ensure_fresh_history(self, title: str | None = None) -> bool:
        if not self.can_sync():
            return False
        if not self._is_history_stale(title):
            return False
        try:
            return self.sync_library()
        except Exception as exc:
            log.warning("Stremio history refresh failed: %s", exc)
            return False

    def _is_history_stale(self, title: str | None = None) -> bool:
        if not self.watch_state_path.exists():
            return True

        state = self._load_watch_state()
        if not state:
            return True

        if title:
            _, entry = self._find_watch_entry(title, state)
            if entry:
                return self._entry_is_stale(entry)

        latest_timestamp = None
        for entry in state.values():
            parsed = self._parse_timestamp(entry.get("history_updated_at"))
            if parsed and (latest_timestamp is None or parsed > latest_timestamp):
                latest_timestamp = parsed

        if latest_timestamp is None:
            latest_timestamp = datetime.fromtimestamp(
                self.watch_state_path.stat().st_mtime, tz=timezone.utc
            )

        return (datetime.now(timezone.utc) - latest_timestamp) > self.history_refresh_max_age

    def _entry_is_stale(self, entry: dict) -> bool:
        timestamp = self._parse_timestamp(entry.get("history_updated_at"))
        if timestamp is None:
            return True
        return (datetime.now(timezone.utc) - timestamp) > self.history_refresh_max_age

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _tmdb_headers(self) -> dict:
        token = os.getenv("TMDB_READ_ACCESS_TOKEN") or self.config.get("tmdb", {}).get(
            "read_access_token"
        )
        if token:
            return {
                "accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
        return {"accept": "application/json"}

    def _tmdb_params(self, extra: dict | None = None) -> dict:
        params = extra.copy() if extra else {}
        api_key = os.getenv("TMDB_API_KEY") or self.config.get("tmdb", {}).get("api_key")
        if api_key:
            params["api_key"] = api_key
        return params

    def _tmdb_get(self, path: str, params: dict | None = None) -> dict:
        token = os.getenv("TMDB_READ_ACCESS_TOKEN") or self.config.get("tmdb", {}).get(
            "read_access_token"
        )
        api_key = os.getenv("TMDB_API_KEY") or self.config.get("tmdb", {}).get("api_key")
        if not token and not api_key:
            raise ValueError("TMDB credentials are missing")

        resp = requests.get(
            f"https://api.themoviedb.org/3{path}",
            headers=self._tmdb_headers(),
            params=self._tmdb_params(params),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def resolve_imdb_id(self, title: str, media_type: str | None = None) -> tuple[str, str]:
        """
        Returns (imdb_id, resolved_media_type)
        where resolved_media_type is 'series' or 'movie'.
        """
        _, entry = self._find_watch_entry(title)
        if entry:
            return entry["imdb_id"], entry["type"]

        if media_type == "movie":
            search_types = ["movie"]
        elif media_type in ("series", "tv"):
            search_types = ["tv"]
        else:
            search_types = ["tv", "movie"]

        for tmdb_type in search_types:
            search_data = self._tmdb_get(
                f"/search/{tmdb_type}",
                {"query": title, "page": 1},
            )
            results = search_data.get("results", [])
            if not results:
                continue

            tmdb_id = results[0]["id"]
            external_ids = self._tmdb_get(f"/{tmdb_type}/{tmdb_id}/external_ids")
            imdb_id = external_ids.get("imdb_id")
            if imdb_id:
                resolved_type = "series" if tmdb_type == "tv" else "movie"
                return imdb_id, resolved_type

        raise ValueError(f"Could not resolve IMDb ID for '{title}'")

    def play(
        self,
        title: str,
        media_type: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        allow_unknown_source: bool = False,
    ) -> StremioPlayResult:
        refresh_before_lookup = not (season and episode)
        if refresh_before_lookup:
            self._ensure_fresh_history(title)

        imdb_id, resolved_type = self.resolve_imdb_id(title, media_type)
        state = self._load_watch_state()
        title_key, entry = self._find_watch_entry(title, state)
        if not entry:
            title_key, entry = self._find_watch_entry_by_imdb(imdb_id, state)

        resolved_title = entry.get("title", title) if entry else title
        remembered_source = entry.get("last_successful_source") if entry else None

        if resolved_type == "series" and not (season and episode) and entry:
            season = entry.get("season")
            episode = entry.get("episode")

        return self._play_deep_link(
            imdb_id=imdb_id,
            media_type=resolved_type,
            season=season,
            episode=episode,
            title_key=title_key or resolved_title.lower(),
            title_label=resolved_title,
            allow_unknown_source=allow_unknown_source,
            remembered_source=remembered_source,
        )

    def _play_deep_link(
        self,
        imdb_id: str,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        title_key: str | None = None,
        title_label: str | None = None,
        allow_unknown_source: bool = False,
        remembered_source: str | None = None,
    ) -> StremioPlayResult:
        if media_type == "movie":
            uri = f"stremio:///detail/movie/{imdb_id}/{imdb_id}"
        elif season and episode:
            uri = f"stremio:///detail/series/{imdb_id}/{imdb_id}:{season}:{episode}"
        else:
            uri = f"stremio:///detail/series/{imdb_id}/{imdb_id}"

        source_order = self._source_preference_order(remembered_source)
        found_preferred_source = False

        for source_key in source_order:
            selection = self._attempt_provider(uri, source_key)
            if not selection:
                continue

            found_preferred_source = True
            if selection.success:
                self._remember_successful_source(
                    title_key=title_key,
                    title_label=title_label,
                    imdb_id=imdb_id,
                    media_type=media_type,
                    source_label=selection.played_source,
                )
                return selection

        if self.provider_fallback_policy == "ask" and not allow_unknown_source:
            title_for_message = title_label or title_key or "that title"
            return StremioPlayResult(
                success=False,
                message=UNKNOWN_SOURCE_CONFIRMATION_TEMPLATE.format(title=title_for_message),
                requires_confirmation=True,
            )

        fallback_attempt = self._attempt_unknown_source(uri)
        if fallback_attempt.success:
            self._remember_successful_source(
                title_key=title_key,
                title_label=title_label,
                imdb_id=imdb_id,
                media_type=media_type,
                source_label=fallback_attempt.played_source,
            )
            return fallback_attempt

        if found_preferred_source:
            return StremioPlayResult(success=False, message=AUTOPLAY_FALLBACK_LINE)
        if fallback_attempt.message:
            return fallback_attempt
        return StremioPlayResult(success=False, message=AUTOPLAY_FALLBACK_LINE)

    def _attempt_provider(self, uri: str, provider_key: str) -> StremioPlayResult | None:
        self._launch_uri(uri)
        time.sleep(self.autoplay_delay_ms / 1000)

        candidate = self._find_provider_candidate(provider_key)
        if not candidate:
            return None

        self._tap(candidate.center_x, candidate.center_y)
        if self._wait_for_playback():
            return StremioPlayResult(success=True, played_source=candidate.label)

        log.info("Preferred source '%s' did not start playback", provider_key)
        return StremioPlayResult(success=False, played_source=candidate.label)

    def _attempt_unknown_source(self, uri: str) -> StremioPlayResult:
        self._launch_uri(uri)
        time.sleep(self.autoplay_delay_ms / 1000)

        candidate = self._find_first_unknown_candidate()
        if candidate:
            self._tap(candidate.center_x, candidate.center_y)
            if self._wait_for_playback():
                return StremioPlayResult(success=True, played_source=candidate.label)

        self._keyevent(23)
        if self._wait_for_playback():
            return StremioPlayResult(success=True, played_source="default")

        return StremioPlayResult(success=False, message=AUTOPLAY_FALLBACK_LINE)

    def _launch_uri(self, uri: str):
        self._run_shell(f'am start -a android.intent.action.VIEW -d "{uri}"')

    def _wait_for_playback(self, timeout_seconds: float = 2.5) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._is_playing():
                return True
            time.sleep(0.5)
        return self._is_playing()

    def _source_preference_order(self, remembered_source: str | None) -> list[str]:
        ordered = []
        seen = set()

        if remembered_source:
            normalized = self._normalize_provider_key(remembered_source)
            if normalized:
                seen.add(normalized)
                ordered.append(normalized)

        for provider in self.provider_preferences:
            if provider not in seen:
                seen.add(provider)
                ordered.append(provider)

        return ordered

    def _normalize_provider_key(self, value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())

    def _provider_key_for_label(self, label: str) -> str:
        normalized_label = self._normalize_provider_key(label)
        for provider_key, aliases in self.provider_aliases.items():
            for alias in aliases:
                if alias == normalized_label or alias in normalized_label or normalized_label in alias:
                    return provider_key
        return normalized_label

    def _provider_matches(self, provider_key: str, candidate: SourceCandidate) -> bool:
        normalized_provider = self._normalize_provider_key(provider_key)
        normalized_candidate = self._normalize_provider_key(candidate.provider_key)
        if normalized_provider == normalized_candidate:
            return True

        aliases = self.provider_aliases.get(normalized_provider, [normalized_provider])
        candidate_label = self._normalize_provider_key(candidate.label)
        return any(
            alias == candidate_label or alias in candidate_label or candidate_label in alias
            for alias in aliases
            if alias
        )

    def _find_provider_candidate(self, provider_key: str) -> SourceCandidate | None:
        for page_index in range(self.provider_scan_pages):
            candidates = self._get_visible_source_candidates()
            for candidate in candidates:
                if self._provider_matches(provider_key, candidate):
                    return candidate
            if page_index < self.provider_scan_pages - 1:
                self._scroll_source_list()
                time.sleep(self.provider_scan_delay_ms / 1000)
        return None

    def _find_first_unknown_candidate(self) -> SourceCandidate | None:
        excluded = {
            self._normalize_provider_key(provider)
            for provider in self._source_preference_order(None)
        }

        for page_index in range(self.provider_scan_pages):
            candidates = self._get_visible_source_candidates()
            for candidate in candidates:
                if self._normalize_provider_key(candidate.provider_key) not in excluded:
                    return candidate
            if page_index < self.provider_scan_pages - 1:
                self._scroll_source_list()
                time.sleep(self.provider_scan_delay_ms / 1000)
        return None

    def _get_visible_source_candidates(self) -> list[SourceCandidate]:
        xml_text = self._dump_ui_hierarchy()
        candidates = self._extract_candidates_from_ui_xml(xml_text)
        if candidates:
            return candidates
        return self._extract_candidates_from_ocr()

    def _dump_ui_hierarchy(self) -> str:
        self._run_shell(f"uiautomator dump --compressed {UI_DUMP_REMOTE_PATH}")
        _, output = self._run_shell(f"cat {UI_DUMP_REMOTE_PATH}")
        return output or ""

    def _extract_candidates_from_ui_xml(self, xml_text: str) -> list[SourceCandidate]:
        if not xml_text.strip():
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            log.debug("Unable to parse uiautomator XML dump")
            return []

        raw_nodes = []
        max_y = 0

        for node in root.iter("node"):
            label = (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()
            bounds = self._parse_bounds(node.attrib.get("bounds", ""))
            if not label or not bounds:
                continue

            x1, y1, x2, y2 = bounds
            max_y = max(max_y, y2)
            raw_nodes.append((label, bounds))

        if not raw_nodes:
            return []

        min_source_y = int(max_y * 0.25)
        candidates = []
        seen = set()

        for label, bounds in raw_nodes:
            x1, y1, x2, y2 = bounds
            center_x = (x1 + x2) // 2
            center_y = (y1 + y2) // 2

            if center_y < min_source_y:
                continue
            if not self._looks_like_source_label(label):
                continue

            provider_key = self._provider_key_for_label(label)
            dedupe_key = (provider_key, center_x, center_y)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates.append(
                SourceCandidate(
                    label=label,
                    center_x=center_x,
                    center_y=center_y,
                    provider_key=provider_key,
                )
            )

        candidates.sort(key=lambda item: (item.center_y, item.center_x))
        return candidates

    def _looks_like_source_label(self, label: str) -> bool:
        cleaned = " ".join(label.split()).strip()
        normalized = self._normalize_provider_key(cleaned)
        if not normalized:
            return False

        if normalized in GENERIC_UI_LABELS:
            return False

        if any(
            alias == normalized or alias in normalized or normalized in alias
            for aliases in self.provider_aliases.values()
            for alias in aliases
        ):
            return True

        if len(cleaned) > 32:
            return False
        if cleaned.count(" ") > 3:
            return False
        if not any(ch.isalpha() for ch in cleaned):
            return False
        return True

    def _parse_bounds(self, bounds_text: str) -> tuple[int, int, int, int] | None:
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_text or "")
        if not match:
            return None
        return tuple(int(part) for part in match.groups())

    def _scroll_source_list(self):
        self._run_shell("input swipe 960 900 960 260 250")

    def _tap(self, x: int, y: int):
        self._run_shell(f"input tap {x} {y}")

    def _extract_candidates_from_ocr(self) -> list[SourceCandidate]:
        if not self.ocr_enabled:
            return []

        try:
            from PIL import Image
            import pytesseract
        except ImportError:
            log.debug("OCR fallback unavailable because Pillow or pytesseract is not installed")
            return []

        screenshot = self._capture_screenshot()
        if not screenshot:
            return []

        try:
            image = Image.open(io.BytesIO(screenshot))
            ocr_data = pytesseract.image_to_data(
                image, output_type=pytesseract.Output.DICT
            )
        except Exception as exc:
            log.debug("OCR fallback failed: %s", exc)
            return []

        lines = {}
        for index, word in enumerate(ocr_data.get("text", [])):
            cleaned = word.strip()
            if not cleaned:
                continue

            line_key = (
                ocr_data["page_num"][index],
                ocr_data["block_num"][index],
                ocr_data["par_num"][index],
                ocr_data["line_num"][index],
            )
            entry = lines.setdefault(
                line_key,
                {
                    "words": [],
                    "left": ocr_data["left"][index],
                    "top": ocr_data["top"][index],
                    "right": ocr_data["left"][index] + ocr_data["width"][index],
                    "bottom": ocr_data["top"][index] + ocr_data["height"][index],
                },
            )
            entry["words"].append(cleaned)
            entry["left"] = min(entry["left"], ocr_data["left"][index])
            entry["top"] = min(entry["top"], ocr_data["top"][index])
            entry["right"] = max(
                entry["right"], ocr_data["left"][index] + ocr_data["width"][index]
            )
            entry["bottom"] = max(
                entry["bottom"], ocr_data["top"][index] + ocr_data["height"][index]
            )

        candidates = []
        for entry in lines.values():
            label = " ".join(entry["words"]).strip()
            if not self._looks_like_source_label(label):
                continue
            candidates.append(
                SourceCandidate(
                    label=label,
                    center_x=(entry["left"] + entry["right"]) // 2,
                    center_y=(entry["top"] + entry["bottom"]) // 2,
                    provider_key=self._provider_key_for_label(label),
                )
            )

        candidates.sort(key=lambda item: (item.center_y, item.center_x))
        return candidates

    def _capture_screenshot(self) -> bytes:
        result = self._run_adb_command("exec-out", "screencap", "-p", capture_text=False)
        if not result[0]:
            return b""
        return result[1]

    def _remember_successful_source(
        self,
        title_key: str | None,
        title_label: str | None,
        imdb_id: str,
        media_type: str,
        source_label: str | None,
    ):
        if not source_label:
            return

        state = self._load_watch_state()
        existing_key = title_key if title_key in state else None
        if not existing_key:
            existing_key, existing_entry = self._find_watch_entry_by_imdb(imdb_id, state)
        else:
            existing_entry = state.get(existing_key)

        if not existing_key:
            existing_key = (title_key or title_label or imdb_id).strip().lower()
            existing_entry = {}

        state[existing_key] = {
            **existing_entry,
            "title": title_label or existing_entry.get("title") or existing_key,
            "imdb_id": imdb_id,
            "type": media_type,
            "last_video_id": existing_entry.get("last_video_id"),
            "season": existing_entry.get("season"),
            "episode": existing_entry.get("episode"),
            "finished_last": existing_entry.get("finished_last", False),
            "last_successful_source": source_label,
            "history_updated_at": existing_entry.get("history_updated_at", self._now_iso()),
        }
        self._write_watch_state(state)

    def _is_playing(self) -> bool:
        _, output = self._run_shell("dumpsys media_session")
        return "state=3" in (output or "").lower()

    def _run_adb_command(self, *args, capture_text: bool = True) -> tuple[bool, str | bytes]:
        cmd = [self.adb_path]
        if self.adb_target and ":" in self.adb_target:
            cmd.extend(["-s", self.adb_target])
        cmd.extend(args)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=capture_text,
            encoding="utf-8" if capture_text else None,
            errors="replace" if capture_text else None,
            check=False,
        )
        if capture_text:
            output = (result.stdout or result.stderr or "").strip()
        else:
            output = result.stdout if result.stdout else result.stderr
        return result.returncode == 0, output

    def _run_shell(self, shell_command: str) -> tuple[bool, str]:
        if self.media_service is not None:
            return self.media_service._adb(f"shell {shell_command}")

        ok, output = self._run_adb_command("shell", shell_command, capture_text=True)
        return ok, output  # type: ignore[return-value]

    def _keyevent(self, code: int):
        self._run_shell(f"input keyevent {code}")
