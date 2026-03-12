import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
                "provider_preferences": ["comet", "mediafusion"],
                "provider_aliases": {
                    "comet": ["comet"],
                    "mediafusion": ["mediafusion", "media fusion"],
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

    def test_plain_series_play_uses_cached_resume_episode(self):
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
            with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                with patch.object(
                    svc,
                    "_play_deep_link",
                    return_value=StremioPlayResult(success=True, played_source="Comet"),
                ) as deep_link:
                    result = svc.play("Shrinking")

            self.assertTrue(result.success)
            self.assertEqual(deep_link.call_args.kwargs["season"], 2)
            self.assertEqual(deep_link.call_args.kwargs["episode"], 4)
            self.assertEqual(deep_link.call_args.kwargs["remembered_source"], "Comet")

    def test_explicit_episode_bypasses_resume_refresh(self):
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
            with patch.object(svc, "_ensure_fresh_history") as refresh_history:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(success=True, played_source="Comet"),
                    ) as deep_link:
                        svc.play("Shrinking", season=1, episode=1)

            refresh_history.assert_not_called()
            self.assertEqual(deep_link.call_args.kwargs["season"], 1)
            self.assertEqual(deep_link.call_args.kwargs["episode"], 1)

    def test_stale_history_triggers_sync_before_resume_sensitive_play(self):
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

            with patch.object(svc, "sync_library", return_value=True) as sync_library:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(success=True, played_source="Comet"),
                    ):
                        svc.play("Shrinking")

            sync_library.assert_called_once()

    def test_fresh_history_skips_sync_before_resume_sensitive_play(self):
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
            svc.email = "user@example.com"
            svc.password = "secret"

            with patch.object(svc, "sync_library", return_value=True) as sync_library:
                with patch.object(svc, "resolve_imdb_id", return_value=("tt13315786", "series")):
                    with patch.object(
                        svc,
                        "_play_deep_link",
                        return_value=StremioPlayResult(success=True, played_source="Comet"),
                    ):
                        svc.play("Shrinking")

            sync_library.assert_not_called()

    def test_source_preference_order_tries_remembered_source_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))

            order = svc._source_preference_order("Torrentio")

            self.assertEqual(order, ["torrentio", "comet", "mediafusion"])

    def test_extract_candidates_from_ui_xml_matches_known_providers(self):
        svc = StremioService(self._config(Path("watch_state.json")))
        xml_text = """
        <hierarchy>
          <node text="Fallout" bounds="[100,80][500,180]" />
          <node text="Comet" bounds="[120,520][420,620]" />
          <node text="MediaFusion" bounds="[120,700][420,800]" />
        </hierarchy>
        """

        candidates = svc._extract_candidates_from_ui_xml(xml_text)

        self.assertEqual([candidate.provider_key for candidate in candidates], ["comet", "mediafusion"])

    def test_play_returns_confirmation_when_no_preferred_provider_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch_state = Path(tmp) / "watch_state.json"
            svc = StremioService(self._config(watch_state))

            with patch.object(svc, "_attempt_provider", return_value=None):
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
                return_value=StremioPlayResult(success=True, played_source="Comet"),
            ):
                result = svc._play_deep_link(
                    imdb_id="tt13315786",
                    media_type="series",
                    title_key="shrinking",
                    title_label="Shrinking",
                    remembered_source=None,
                )

            self.assertTrue(result.success)
            cached = json.loads(watch_state.read_text(encoding="utf-8"))
            self.assertEqual(cached["shrinking"]["last_successful_source"], "Comet")


if __name__ == "__main__":
    unittest.main()
