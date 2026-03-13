import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from services.stremio_service import StremioPlayResult, StremioService


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class StremioServiceTests(unittest.TestCase):
    def _config(self, watch_state_path: Path) -> dict:
        return {
            "stremio": {
                "watch_state_path": str(watch_state_path),
                "autoplay_delay_ms": 1,
                "history_refresh_max_age_minutes": 5,
                "provider_preferences": ["comet", "mediafusion", "torrent"],
                "provider_aliases": {
                    "comet": ["comet"],
                    "mediafusion": ["mediafusion", "media fusion"],
                    "torrent": ["torrent", "torrentio"],
                },
                "provider_scan_pages": 3,
                "provider_scan_delay_ms": 1,
                "provider_fallback_policy": "ask",
                "provider_ocr_enabled": False,
            },
            "tmdb": {
                "api_key": "dummy",
                "read_access_token": None,
            },
            "media": {
                "adb_path": "adb",
                "adb_timeout_ms": 5000,
                "mibox_ip": "192.168.1.26",
                "adb_port": 5555,
            },
        }

    def _write_state(self, watch_state: Path, payload: dict):
        watch_state.write_text(json.dumps(payload), encoding="utf-8")

    def _iso_minutes_ago(self, minutes: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

    def _iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def test_sync_library_preserves_last_successful_source_and_last_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            self._write_state(
                watch_state,
                {
                    "shrinking": {
                        "title": "Shrinking",
                        "imdb_id": "tt13315786",
                        "type": "series",
                        "last_successful_source": "Comet",
                        "history_updated_at": self._iso_minutes_ago(30),
                    }
                },
            )

            config = self._config(watch_state)
            config["stremio"]["email"] = "user@example.com"
            config["stremio"]["password"] = "secret"

            library_payload = {
                "result": [
                    {
                        "_id": "tt13315786",
                        "name": "Shrinking",
                        "type": "series",
                        "state": {
                            "video_id": "tt13315786:2:4",
                            "timeOffset": 900,
                            "duration": 1000,
                        },
                    }
                ]
            }

            with patch("services.stremio_service.requests.post") as post:
                post.side_effect = [
                    _Response({"result": {"authKey": "auth-key"}}),
                    _Response(library_payload),
                ]
                StremioService(config)

            cached = json.loads(watch_state.read_text(encoding="utf-8"))
            self.assertEqual(cached["shrinking"]["last_video_id"], "tt13315786:2:4")
            self.assertEqual(cached["shrinking"]["season"], 2)
            self.assertEqual(cached["shrinking"]["episode"], 5)
            self.assertEqual(cached["shrinking"]["last_successful_source"], "Comet")
            self.assertIn("history_updated_at", cached["shrinking"])

    def test_sync_library_can_extract_imdb_id_from_video_id_when_id_fields_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"

            config = self._config(watch_state)
            config["stremio"]["email"] = "user@example.com"
            config["stremio"]["password"] = "secret"

            library_payload = {
                "result": [
                    {
                        "name": "Shrinking",
                        "type": "series",
                        "state": {
                            "video_id": "tt13315786:2:4",
                            "timeOffset": 0,
                            "duration": 1000,
                            "season": 2,
                            "episode": 4,
                        },
                    }
                ]
            }

            with patch("services.stremio_service.requests.post") as post:
                post.side_effect = [
                    _Response({"result": {"authKey": "auth-key"}}),
                    _Response(library_payload),
                ]
                StremioService(config)

            cached = json.loads(watch_state.read_text(encoding="utf-8"))
            self.assertEqual(cached["shrinking"]["imdb_id"], "tt13315786")
            self.assertEqual(cached["shrinking"]["season"], 2)
            self.assertEqual(cached["shrinking"]["episode"], 4)

    def test_plain_series_play_forces_sync_and_uses_latest_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            self._write_state(
                watch_state,
                {
                    "shrinking": {
                        "title": "Shrinking",
                        "imdb_id": "tt13315786",
                        "type": "series",
                        "season": 2,
                        "episode": 4,
                        "last_successful_source": "Comet",
                        "history_updated_at": self._iso_now(),
                    }
                },
            )

            svc = StremioService(self._config(watch_state))
            svc.email = "user@example.com"
            svc.password = "secret"

            def sync_side_effect():
                self._write_state(
                    watch_state,
                    {
                        "shrinking": {
                            "title": "Shrinking",
                            "imdb_id": "tt13315786",
                            "type": "series",
                            "season": 2,
                            "episode": 5,
                            "last_successful_source": "Comet",
                            "history_updated_at": self._iso_now(),
                        }
                    },
                )
                return True

            with patch.object(svc, "sync_library", side_effect=sync_side_effect) as sync_library:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(
                            success=True,
                            played_source="Comet",
                            target_mode="episode",
                        ),
                    ) as deep_link:
                        result = svc.play("Shrinking", media_type="series")

            self.assertTrue(result.success)
            sync_library.assert_called_once()
            self.assertEqual(deep_link.call_args.kwargs["season"], 2)
            self.assertEqual(deep_link.call_args.kwargs["episode"], 5)
            self.assertEqual(deep_link.call_args.kwargs["remembered_source"], "Comet")

    def test_plain_series_play_uses_cached_episode_when_forced_sync_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            self._write_state(
                watch_state,
                {
                    "shrinking": {
                        "title": "Shrinking",
                        "imdb_id": "tt13315786",
                        "type": "series",
                        "season": 2,
                        "episode": 4,
                        "last_successful_source": "Comet",
                        "history_updated_at": self._iso_minutes_ago(30),
                    }
                },
            )

            svc = StremioService(self._config(watch_state))
            svc.email = "user@example.com"
            svc.password = "secret"

            with patch.object(svc, "sync_library", side_effect=RuntimeError("boom")) as sync_library:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(
                            success=True,
                            played_source="Comet",
                            target_mode="episode",
                        ),
                    ) as deep_link:
                        result = svc.play("Shrinking", media_type="series")

            self.assertTrue(result.success)
            sync_library.assert_called_once()
            self.assertEqual(deep_link.call_args.kwargs["season"], 2)
            self.assertEqual(deep_link.call_args.kwargs["episode"], 4)

    def test_plain_series_play_falls_back_to_series_detail_when_no_progress_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))
            svc.email = "user@example.com"
            svc.password = "secret"

            with patch.object(svc, "sync_library", side_effect=RuntimeError("boom")):
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(
                            success=True,
                            played_source="Comet",
                            target_mode="series_detail",
                        ),
                    ) as deep_link:
                        result = svc.play("Shrinking", media_type="series")

            self.assertTrue(result.success)
            self.assertEqual(result.target_mode, "series_detail")
            self.assertIsNone(deep_link.call_args.kwargs["season"])
            self.assertIsNone(deep_link.call_args.kwargs["episode"])

    def test_explicit_episode_bypasses_resume_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            self._write_state(
                watch_state,
                {
                    "shrinking": {
                        "title": "Shrinking",
                        "imdb_id": "tt13315786",
                        "type": "series",
                        "season": 2,
                        "episode": 4,
                        "history_updated_at": self._iso_minutes_ago(20),
                    }
                },
            )

            svc = StremioService(self._config(watch_state))
            svc.email = "user@example.com"
            svc.password = "secret"

            with patch.object(svc, "sync_library") as sync_library:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(
                            success=True,
                            played_source="Comet",
                            target_mode="episode",
                        ),
                    ) as deep_link:
                        svc.play("Shrinking", media_type="series", season=1, episode=1)

            sync_library.assert_not_called()
            self.assertEqual(deep_link.call_args.kwargs["season"], 1)
            self.assertEqual(deep_link.call_args.kwargs["episode"], 1)

    def test_source_preference_order_tries_remembered_source_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))

            order = svc._source_preference_order("Torrentio")

            self.assertEqual(order, ["torrentio", "comet", "mediafusion", "torrent"])

    def test_extract_candidates_from_ui_xml_matches_known_providers_including_torrent(self):
        svc = StremioService(self._config(Path("watch_state.json")))
        xml_text = """
        <hierarchy>
          <node text="Fallout" bounds="[100,80][500,180]" />
          <node text="Comet" bounds="[120,520][420,620]" />
          <node text="MediaFusion" bounds="[120,700][420,800]" />
          <node text="Torrentio" bounds="[120,880][420,980]" />
        </hierarchy>
        """

        candidates = svc._extract_candidates_from_ui_xml(xml_text)

        self.assertEqual(
            [candidate.provider_key for candidate in candidates],
            ["comet", "mediafusion", "torrent"],
        )

    def test_play_returns_confirmation_only_after_all_preferred_providers_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))

            with patch.object(svc, "_attempt_provider", return_value=None) as attempt_provider:
                with patch.object(svc, "_attempt_unknown_source") as unknown_attempt:
                    result = svc._play_deep_link(
                        imdb_id="tt0903747",
                        media_type="series",
                        title_key="fallout",
                        title_label="Fallout",
                        allow_unknown_source=False,
                    )

            self.assertFalse(result.success)
            self.assertTrue(result.requires_confirmation)
            self.assertEqual(result.target_mode, "series_detail")
            self.assertEqual(attempt_provider.call_count, 3)
            self.assertIn("Want me to try the first available source?", result.message)
            unknown_attempt.assert_not_called()

    def test_confirmed_unknown_source_can_play(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))

            with patch.object(svc, "_attempt_provider", return_value=None):
                with patch.object(
                    svc,
                    "_attempt_unknown_source",
                    return_value=StremioPlayResult(success=True, played_source="Torrentio"),
                ) as unknown_attempt:
                    result = svc._play_deep_link(
                        imdb_id="tt0903747",
                        media_type="series",
                        title_key="fallout",
                        title_label="Fallout",
                        allow_unknown_source=True,
                    )

            self.assertTrue(result.success)
            self.assertEqual(result.played_source, "Torrentio")
            self.assertEqual(result.target_mode, "series_detail")
            unknown_attempt.assert_called_once()

    def test_successful_play_updates_last_successful_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            self._write_state(
                watch_state,
                {
                    "shrinking": {
                        "title": "Shrinking",
                        "imdb_id": "tt13315786",
                        "type": "series",
                        "season": 2,
                        "episode": 4,
                        "history_updated_at": self._iso_now(),
                    }
                },
            )

            svc = StremioService(self._config(watch_state))
            with patch.object(
                svc,
                "_attempt_provider",
                return_value=StremioPlayResult(
                    success=True,
                    played_source="Comet",
                    target_mode="episode",
                ),
            ):
                result = svc._play_deep_link(
                    imdb_id="tt13315786",
                    media_type="series",
                    title_key="shrinking",
                    title_label="Shrinking",
                    season=2,
                    episode=4,
                    remembered_source=None,
                )

            self.assertTrue(result.success)
            cached = json.loads(watch_state.read_text(encoding="utf-8"))
            self.assertEqual(cached["shrinking"]["last_successful_source"], "Comet")

    def test_shared_media_service_helpers_are_used_for_ui_actions(self):
        media_service = Mock()
        media_service.dump_ui_hierarchy.return_value = "<hierarchy />"
        media_service.capture_screenshot_bytes.return_value = b"png"
        svc = StremioService(self._config(Path("watch_state.json")), media_service=media_service)

        self.assertEqual(svc._dump_ui_hierarchy(), "<hierarchy />")
        svc._scroll_source_list()
        svc._tap(200, 300)
        svc._keyevent(23)
        self.assertEqual(svc._capture_screenshot(), b"png")

        media_service.dump_ui_hierarchy.assert_called_once_with()
        media_service.swipe.assert_called_once_with(960, 900, 960, 260, 250)
        media_service.tap.assert_called_once_with(200, 300)
        media_service.keyevent.assert_called_once_with(23)
        media_service.capture_screenshot_bytes.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
