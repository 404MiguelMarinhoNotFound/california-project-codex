"""
H8: prepare the Stremio request while the room wakes.

Library sync, the watch-state lookup, the TMDB fallback and the deep link touch
only the network and local files -- never adb -- and used to start only after
a deep-standby wake of ~35-40s. `prepare()` now runs on a thread from
`_dispatch_tv` before `_ensure_playable`, and `launch()` waits for both.

What a person must still get: "I couldn't find X" for a title that does not
exist (after the wake, as before), the wake's own line when the wake fails, and
no launch in either case.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.orchestrator import _dispatch_tv
from services.cec_wake import WakeResult
from services.stremio_service import StremioPlan, StremioPlayResult, StremioService
from tests.config_fixture import config_for_tests


class _StremioCase(unittest.TestCase):
    def setUp(self):
        # Same guards as tests/test_stremio_service.py: no adb, no real credentials.
        patcher = patch("services.stremio_service.subprocess.run")
        self.addCleanup(patcher.stop)
        patcher.start().return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("STREMIO_EMAIL", "STREMIO_PASSWORD", "TMDB_API_KEY", "TMDB_READ_ACCESS_TOKEN"):
            os.environ.pop(var, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.watch_state = Path(tmp.name) / "watch_state.json"

    def _svc(self, *, prepare_during_wake=True) -> StremioService:
        cfg = config_for_tests(
            stremio={
                "watch_state_path": str(self.watch_state),
                "email": None,
                "password": None,
                # The flag under test.
                "prepare_during_wake": prepare_during_wake,
            },
            tmdb={"api_key": "dummy", "read_access_token": None},
        )
        return StremioService(cfg, media_service=Mock())

    def _write_state(self, payload: dict):
        self.watch_state.write_text(json.dumps(payload), encoding="utf-8")


class PrepareLaunchSplitTests(_StremioCase):
    def test_play_still_equals_prepare_then_launch(self):
        fresh = "2999-01-01T00:00:00+00:00"
        self._write_state({"fallout": {
            "title": "Fallout", "imdb_id": "tt12637874", "type": "series", "season": 2,
            "episode": 9, "last_successful_source": "Comet", "history_updated_at": fresh}})
        svc = self._svc()
        svc._play_deep_link = Mock(return_value=StremioPlayResult(success=True, target_mode="episode"))

        svc.play("Fallout")
        svc._play_deep_link.assert_called_with(
            imdb_id="tt12637874", media_type="series", season=2, episode=9,
            title_key="fallout", title_label="Fallout", allow_unknown_source=False,
            remembered_source="Comet")

        with patch.object(svc, "resolve_imdb_id", return_value=("tt0000001", "series")):
            result = svc.play("Lanterns")
        self.assertEqual(svc._play_deep_link.call_args.kwargs["season"], 1)
        self.assertEqual(svc._play_deep_link.call_args.kwargs["episode"], 1)
        self.assertIs(result.started_from_first_episode, True)

        with patch.object(svc, "resolve_imdb_id", return_value=("tt0000002", "movie")):
            result = svc.play("Dune", media_type="movie", allow_unknown_source=True)
        kwargs = svc._play_deep_link.call_args.kwargs
        self.assertEqual((kwargs["media_type"], kwargs["season"], kwargs["episode"]), ("movie", None, None))
        self.assertIs(kwargs["allow_unknown_source"], True)
        self.assertIs(result.started_from_first_episode, False)

    def test_prepare_touches_no_adb(self):
        svc = self._svc()
        with patch.object(svc, "resolve_imdb_id", return_value=("tt0000001", "series")), \
                patch.object(svc, "_run_shell") as shell:
            plan = svc.prepare("Lanterns")
        shell.assert_not_called()
        self.assertIsInstance(plan, StremioPlan)


class DispatchDuringWakeTests(_StremioCase):
    def _media(self, *, wakes=True, turn_on=None):
        media = Mock()
        media.prewake.return_value = Mock()  # today's order through _ensure_playable
        media.is_awake.return_value = None   # deep standby: the wake path
        media.turn_on.side_effect = turn_on or (lambda: wakes)
        media.ensure_connected.return_value = wakes
        media.last_wake_result = WakeResult(wakes, "woke", tv_confirmed=True if wakes else None)
        media.unreachable_reason = "not_on_lan"
        return media

    def _plan(self):
        return StremioPlan(imdb_id="tt12637874", media_type="series", season=2, episode=9,
                           title_key="fallout", title_label="Fallout", remembered_source=None,
                           started_from_first_episode=False)

    def test_prepare_runs_during_the_wake(self):
        """Barrier: each side waits for the other, so this only passes if they overlap."""
        barrier = threading.Barrier(2, timeout=2)
        svc = self._svc()
        svc.prepare = Mock(side_effect=lambda *a, **kw: barrier.wait() is not None and self._plan())
        svc.launch = Mock(return_value=StremioPlayResult(success=True, target_mode="episode"))
        media = self._media(turn_on=lambda: barrier.wait() is not None)

        reply = _dispatch_tv({"action": "stremio_play", "title": "Fallout"}, media, svc, None, {})

        self.assertIn("Fallout", reply)
        svc.launch.assert_called_once()
        self.assertIs(svc.launch.call_args.args[0].imdb_id, "tt12637874")

    def test_prepare_error_is_spoken_after_the_wake_and_nothing_launches(self):
        svc = self._svc()
        order = []
        svc.prepare = Mock(side_effect=ValueError("Could not resolve IMDb ID for 'Xyzzy'"))
        svc.launch = Mock()
        media = self._media(turn_on=lambda: order.append("wake") or True)

        reply = _dispatch_tv({"action": "stremio_play", "title": "Xyzzy"}, media, svc, None, {})

        self.assertEqual(reply, "I couldn't find Xyzzy in Stremio or TMDB.")
        self.assertEqual(order, ["wake"])
        svc.launch.assert_not_called()

    def test_failed_wake_returns_its_line_and_never_launches(self):
        svc = self._svc()
        slow_done = threading.Event()

        def slow_prepare(*a, **kw):
            time.sleep(0.2)
            slow_done.set()
            return self._plan()

        svc.prepare = Mock(side_effect=slow_prepare)
        svc.launch = Mock()
        media = self._media(wakes=False)

        reply = _dispatch_tv({"action": "stremio_play", "title": "Fallout"}, media, svc, None, {})

        self.assertNotIn("couldn't find", reply)
        self.assertTrue(reply)
        slow_done.wait(2)
        svc.launch.assert_not_called()

    def test_continue_prepares_as_a_series(self):
        svc = self._svc()
        svc.prepare = Mock(return_value=self._plan())
        svc.launch = Mock(return_value=StremioPlayResult(success=True, target_mode="episode"))
        _dispatch_tv({"action": "stremio_continue", "title": "Fallout"}, self._media(), svc, None, {})
        self.assertEqual(svc.prepare.call_args.kwargs.get("media_type"), "series")
        svc.launch.assert_called_once()

    def test_flag_off_uses_play_as_before(self):
        svc = self._svc(prepare_during_wake=False)
        svc.prepare = Mock()
        svc.play = Mock(return_value=StremioPlayResult(success=True, target_mode="episode"))
        _dispatch_tv({"action": "stremio_play", "title": "Fallout"}, self._media(), svc, None, {})
        svc.play.assert_called_once()
        svc.prepare.assert_not_called()


class WatchStateLockTests(_StremioCase):
    def test_watch_state_write_is_atomic_and_locked(self):
        """
        The background sync and a prepare thread can now overlap, and both
        read-modify-write watch_state.json. Widen the race on purpose: without
        the lock one writer's entry is lost.
        """
        self._write_state({})
        svc = self._svc()
        real_load = svc._load_watch_state

        def slow_load():
            state = real_load()
            time.sleep(0.05)
            return state

        svc._load_watch_state = slow_load
        threads = [
            threading.Thread(target=svc._remember_successful_source,
                             args=(key, key.title(), imdb, "series", "Comet"))
            for key, imdb in (("fallout", "tt1"), ("lanterns", "tt2"))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        saved = json.loads(self.watch_state.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"fallout", "lanterns"})
        self.assertFalse(list(self.watch_state.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
