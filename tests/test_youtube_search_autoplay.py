"""
"Search YouTube for X and play the first thing" -- the results deep link stops
at the results page, so the query is resolved to a video id and the box gets a
watch?v= link, then the session is read back to confirm it.

The dumps below are shaped from the real box on 2026-09-14. Two facts drive
the verification tests:

- A session an app walked away from is reported VERBATIM, same stamp and
  same title, for as long as nothing new is published. So "state=3 for
  YouTube" cannot confirm a launch: it was already 3 before it.
- A watch link for a video that is ALREADY on restarts it with a new stamp,
  so before/after is the comparison in every case.
"""

import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from core.orchestrator import _dispatch_tv
from services.media_service import (
    MediaService,
    MediaSession,
    YoutubePlayback,
    _is_new_playback,
    _parse_media_sessions,
)
from services.now_playing import NowPlaying
from services.youtube_search import (
    VideoResult,
    parse_videos,
    speakable_title,
    top_video,
)
from tests.config_fixture import config_for_tests


def _dump(state=3, position=11698, updated=423020856, description="Port Antonio, J. Cole, null"):
    """A Sessions Stack with YouTube and an idle Netflix, as the box prints it."""
    return (
        "  Sessions Stack - have 2 sessions:\n"
        "    YouTube com.google.android.youtube.tv/YouTube (userId=0)\n"
        "      package=com.google.android.youtube.tv\n"
        "      active=true\n"
        f"      state=PlaybackState {{state={state}, position={position}, buffered position=0, "
        f"speed=1.0, updated={updated}, actions=379, custom actions=[], active item id=-1, error=null}}\n"
        f"      metadata: size=5, description={description}\n"
        "    Netflix com.netflix.ninja/Netflix (userId=0)\n"
        "      package=com.netflix.ninja\n"
        "      state=PlaybackState {state=0, position=0, buffered position=0, speed=0.0, "
        "updated=1000, actions=0, custom actions=[], active item id=-1, error=null}\n"
        "      metadata: null\n"
    )


STALE = _dump()
# The two lines seen after a watch link: a loading stub with a new stamp and
# no description, then the title a second or two later.
LOADING = _dump(position=0, updated=423899634, description="null, null, null")
PLAYING = _dump(
    position=1055, updated=423902962,
    description="The Best of J. Cole | DJ set | Greatest Hits - sounds by Winter, WINTER, null",
)
NO_SESSIONS = "  Sessions Stack - have 0 sessions:\n"


def _results_page(*renderers) -> str:
    """A results page body carrying exactly these renderer nodes, in order."""
    contents = [{"itemSectionRenderer": {"contents": list(renderers)}}]
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
        "sectionListRenderer": {"contents": contents}}}}}
    return "<html><script>var ytInitialData = " + json.dumps(data) + ";</script></html>"


def _video(video_id, title, channel="WINTER"):
    return {"videoRenderer": {
        "videoId": video_id,
        "title": {"runs": [{"text": title}]},
        "ownerText": {"runs": [{"text": channel}]},
    }}


def _playlist(playlist_id, title):
    return {"playlistRenderer": {"playlistId": playlist_id, "title": {"simpleText": title}}}


class ResultsPageParsingTests(unittest.TestCase):
    def test_the_first_video_result_is_first(self):
        page = _results_page(_video("aaa", "First"), _video("bbb", "Second"))
        videos = parse_videos(page)
        self.assertEqual([v.video_id for v in videos], ["aaa", "bbb"])
        self.assertEqual(videos[0].title, "First")
        self.assertEqual(videos[0].url, "https://www.youtube.com/watch?v=aaa")

    def test_a_playlist_at_the_top_is_skipped_not_opened(self):
        # Seen live: OK on a playlist top result opens the playlist PAGE and
        # plays nothing. "The first thing" is the first video.
        page = _results_page(_playlist("PL1", "Best of Anderson Paak"), _video("vvv", "The City"))
        videos = parse_videos(page)
        self.assertEqual([v.video_id for v in videos], ["vvv"])

    def test_no_initial_data_raises_for_the_curation_tool(self):
        with self.assertRaises(ValueError):
            parse_videos("<html>consent wall</html>")


class TopVideoTests(unittest.TestCase):
    def test_returns_the_first_video(self):
        page = _results_page(_video("ntst90rCiGM", "The Best of J. Cole | DJ set"))
        with patch("services.youtube_search.fetch_results_page", return_value=page):
            video = top_video("hottest hits of J. Cole")
        self.assertEqual(video, VideoResult("ntst90rCiGM", "The Best of J. Cole | DJ set", "WINTER"))

    def test_network_failure_is_none_not_a_traceback(self):
        with patch("services.youtube_search.fetch_results_page", side_effect=URLError("no route")):
            self.assertIsNone(top_video("anything"))

    def test_a_page_without_data_is_none(self):
        with patch("services.youtube_search.fetch_results_page", return_value="<html></html>"):
            self.assertIsNone(top_video("anything"))

    def test_no_video_results_is_none(self):
        with patch("services.youtube_search.fetch_results_page", return_value=_results_page(_playlist("PL1", "x"))):
            self.assertIsNone(top_video("anything"))

    def test_an_empty_query_never_fetches(self):
        with patch("services.youtube_search.fetch_results_page") as fetch:
            self.assertIsNone(top_video("   "))
        fetch.assert_not_called()

    def test_the_timeout_reaches_the_fetch(self):
        with patch("services.youtube_search.fetch_results_page", return_value=_results_page()) as fetch:
            top_video("x", timeout_s=2.5)
        self.assertEqual(fetch.call_args.kwargs["timeout_s"], 2.5)


class SpeakableTitleTests(unittest.TestCase):
    def test_seo_tail_after_the_pipe_is_dropped(self):
        self.assertEqual(
            speakable_title("The Best of J. Cole | DJ set | Greatest Hits - sounds by Winter"),
            "The Best of J. Cole",
        )

    def test_bracketed_tags_are_dropped(self):
        self.assertEqual(speakable_title("Kendrick Lamar - HUMBLE. (Official Video)"), "Kendrick Lamar - HUMBLE.")
        self.assertEqual(speakable_title("Best J. Cole MIX [2026]"), "Best J. Cole MIX")

    def test_a_long_title_is_cut_at_a_word(self):
        spoken = speakable_title("Drake 2025 MIX Best Collection - Gods Plan, Which One, NOKIA, She Will, LANNYA, Family Matters")
        self.assertLessEqual(len(spoken), 70)
        self.assertFalse(spoken.endswith(","))
        self.assertTrue(spoken.startswith("Drake 2025 MIX Best Collection"))

    def test_a_plain_title_is_untouched(self):
        self.assertEqual(speakable_title("Money Trees"), "Money Trees")

    def test_empty_stays_empty(self):
        self.assertEqual(speakable_title(""), "")


class SessionStampParsingTests(unittest.TestCase):
    def test_the_updated_stamp_is_read_off_the_state_line(self):
        youtube = _parse_media_sessions(STALE)[0]
        self.assertEqual(youtube.package, "com.google.android.youtube.tv")
        self.assertEqual(youtube.updated, 423020856)
        self.assertEqual(youtube.title, "Port Antonio, J. Cole")

    def test_a_verbatim_repeat_is_not_a_new_playback(self):
        before = _parse_media_sessions(STALE)[0]
        after = _parse_media_sessions(STALE)[0]
        self.assertFalse(_is_new_playback(before, after))

    def test_a_new_stamp_is_a_new_playback_even_without_a_title(self):
        before = _parse_media_sessions(STALE)[0]
        after = _parse_media_sessions(LOADING)[0]
        self.assertIsNone(after.title)
        self.assertTrue(_is_new_playback(before, after))

    def test_the_same_video_started_again_is_still_new(self):
        # Measured: a watch link for the video already on restarts it from
        # zero with a fresh stamp. Same title, new stamp, new playback.
        before = MediaSession("yt", 3, "The Best of J. Cole", 1055, 423902962)
        after = MediaSession("yt", 3, "The Best of J. Cole", 0, 423941068)
        self.assertTrue(_is_new_playback(before, after))

    def test_no_session_before_and_playing_after_is_new(self):
        self.assertTrue(_is_new_playback(None, _parse_media_sessions(PLAYING)[0]))

    def test_a_paused_session_is_never_new(self):
        self.assertFalse(_is_new_playback(None, _parse_media_sessions(_dump(state=2, updated=999))[0]))


class PlayVideoTests(unittest.TestCase):
    def setUp(self):
        # Real config.yaml; discovery and CEC off so construction never
        # touches the LAN. Every ADB call is mocked below.
        self.config = config_for_tests(
            media={"cec_wake": {"enabled": False}, "discovery": {"enabled": False}},
        )
        self.svc = MediaService(self.config)
        self.svc.youtube_search_verify_s = 0

    def _run(self, dumps, watch_ok=True):
        """Feed successive dumpsys outputs; a None entry is an unreadable dump."""
        outputs = iter(dumps)
        self.reads = 0

        def adb(command, *args, **kwargs):
            self.assertIn("dumpsys media_session", command)
            self.reads += 1
            nxt = next(outputs, dumps[-1])
            return (False, "") if nxt is None else (True, nxt)

        with patch.object(self.svc, "ensure_connected", return_value=True):
            with patch.object(self.svc, "youtube_watch", return_value=watch_ok) as watch:
                with patch.object(self.svc, "_adb", side_effect=adb):
                    with patch("services.media_service.time.sleep"):
                        result = self.svc.youtube_play_video("ntst90rCiGM")
        return result, watch

    def test_the_watch_link_carries_the_id_and_goes_through_the_youtube_launch_path(self):
        with patch.object(self.svc, "_open_youtube_url", return_value=True) as open_url:
            self.assertTrue(self.svc.youtube_watch("ntst90rCiGM"))
        open_url.assert_called_once_with("https://www.youtube.com/watch?v=ntst90rCiGM")

    def test_an_empty_id_opens_nothing(self):
        with patch.object(self.svc, "_open_youtube_url") as open_url:
            self.assertFalse(self.svc.youtube_watch(""))
        open_url.assert_not_called()

    def test_a_confirmed_launch_returns_the_session_title(self):
        playback, watch = self._run([STALE, PLAYING])
        self.assertEqual(
            playback,
            YoutubePlayback(True, True, "The Best of J. Cole | DJ set | Greatest Hits - sounds by Winter, WINTER"),
        )
        watch.assert_called_once_with("ntst90rCiGM")

    def test_the_stale_session_alone_does_not_count_as_started(self):
        # State was 3 before the link and still is, same stamp, same title:
        # the launch was not seen to do anything.
        playback, _ = self._run([STALE, STALE])
        self.assertEqual(playback, YoutubePlayback(True, False, None))

    def test_an_unreadable_session_is_none_not_false(self):
        playback, _ = self._run([None, None])
        self.assertEqual(playback, YoutubePlayback(True, None, None))

    def test_no_youtube_session_before_is_still_confirmable(self):
        # First YouTube playback since boot: there is no block to compare
        # against, and a readable dump with no block is NOT "cannot tell".
        playback, _ = self._run([NO_SESSIONS, PLAYING])
        self.assertTrue(playback.started)

    def test_the_baseline_is_read_before_the_link_not_after(self):
        order = []
        with patch.object(self.svc, "ensure_connected", return_value=True):
            with patch.object(self.svc, "youtube_watch", side_effect=lambda _: order.append("watch") or True):
                with patch.object(self.svc, "_adb", side_effect=lambda *a, **k: order.append("read") or (True, PLAYING)):
                    with patch("services.media_service.time.sleep"):
                        self.svc.youtube_play_video("x")
        self.assertEqual(order[:2], ["read", "watch"])

    def test_the_loading_stub_is_believed_but_the_title_is_waited_for(self):
        self.svc.youtube_search_verify_s = 6
        with patch("services.media_service.time.monotonic", side_effect=[0, 0.1, 0.6, 1.1, 1.6]):
            playback, _ = self._run([STALE, LOADING, PLAYING])
        self.assertTrue(playback.started)
        self.assertIn("The Best of J. Cole", playback.title)

    def test_a_video_with_no_description_stops_waiting_after_the_grace(self):
        # Seen live: playing, position advancing, description "null, null,
        # null" for good. The window is 6s; the title gets 1.5s past the stub.
        self.svc.youtube_search_verify_s = 6
        clock = iter([0, 0.2, 0.7, 1.2, 1.7, 2.2, 2.7, 3.2, 3.7, 4.2, 4.7, 5.2, 5.7, 6.2])
        with patch("services.media_service.time.monotonic", side_effect=clock):
            playback, _ = self._run([STALE] + [LOADING] * 10)
        self.assertEqual(playback, YoutubePlayback(True, True, None))
        # One read before the link, one that confirmed it, then at most
        # three more inside the 1.5s grace -- not the twelve a 6s window allows.
        self.assertLessEqual(self.reads, 5)

    def test_a_failed_launch_is_not_opened_and_reads_nothing_after(self):
        playback, _ = self._run([STALE], watch_ok=False)
        self.assertEqual(playback, YoutubePlayback(False, False, None))
        self.assertEqual(self.reads, 1)

    def test_disconnected_launches_nothing(self):
        with patch.object(self.svc, "ensure_connected", return_value=False):
            with patch.object(self.svc, "youtube_watch") as watch:
                with patch.object(self.svc, "_adb") as adb:
                    playback = self.svc.youtube_play_video("x")
        self.assertEqual(playback, YoutubePlayback(False, False, None))
        watch.assert_not_called()
        adb.assert_not_called()

    def test_no_key_is_ever_pressed(self):
        # The DPAD approach this replaced pressed OK into whatever the top
        # result was, and a second OK in the player is play/pause.
        with patch.object(self.svc, "keyevent") as key:
            self._run([STALE, PLAYING])
        key.assert_not_called()

    def test_the_shipped_config_turns_autoplay_on(self):
        svc = MediaService(self.config)
        self.assertTrue(svc.youtube_search_autoplay)
        self.assertGreater(svc.youtube_search_resolve_timeout_s, 0)


class SearchDispatchTests(unittest.TestCase):
    VIDEO = VideoResult("ntst90rCiGM", "The Best of J. Cole | DJ set | Greatest Hits - sounds by Winter", "WINTER")

    def _media(self, playback=YoutubePlayback(True, True, "The Best of J. Cole | DJ set, WINTER"), autoplay=True):
        media = Mock()
        media.ensure_connected.return_value = True
        media.is_app_foreground.return_value = True
        media.youtube_search.return_value = True
        media.youtube_search_autoplay = autoplay
        media.youtube_search_resolve_timeout_s = 5.0
        media.youtube_play_video.return_value = playback
        return media

    def _dispatch(self, media, store=None, video=VIDEO):
        with patch("core.orchestrator.top_video", return_value=video) as resolve:
            reply = _dispatch_tv(
                {"action": "youtube_search", "query": "hottest hits of J. Cole"},
                media, None, None, {}, now_playing=store,
            )
        self.resolve = resolve
        return reply

    def test_the_top_video_is_played_and_spoken_short(self):
        store = NowPlaying()
        media = self._media()
        reply = self._dispatch(media, store)
        self.assertEqual(reply, "Playing The Best of J. Cole on YouTube.")
        media.youtube_play_video.assert_called_once_with("ntst90rCiGM")
        media.youtube_search.assert_not_called()
        launch = store.current("youtube")
        self.assertEqual(launch.label, "The Best of J. Cole")
        self.assertEqual(launch.kind, "playing")

    def test_the_resolve_timeout_comes_from_config(self):
        self._dispatch(self._media())
        self.assertEqual(self.resolve.call_args.kwargs["timeout_s"], 5.0)

    def test_a_failed_resolve_falls_back_to_the_results_page(self):
        store = NowPlaying()
        media = self._media()
        reply = self._dispatch(media, store, video=None)
        self.assertIn("couldn't pick a result", reply)
        self.assertIn("Pick one with the remote", reply)
        media.youtube_search.assert_called_once_with("hottest hits of J. Cole")
        media.youtube_play_video.assert_not_called()
        self.assertEqual(store.current("youtube").kind, "opened")

    def test_nothing_started_gets_the_remote_line_and_stays_opened(self):
        store = NowPlaying()
        reply = self._dispatch(self._media(YoutubePlayback(True, False, None)), store)
        self.assertIn("didn't start on its own", reply)
        self.assertIn("hit OK on the remote", reply)
        self.assertEqual(store.current("youtube").kind, "opened")

    def test_unconfirmed_is_never_reported_as_playing(self):
        store = NowPlaying()
        reply = self._dispatch(self._media(YoutubePlayback(True, None, None)), store)
        self.assertIn("couldn't confirm", reply)
        self.assertNotIn("Playing", reply)
        self.assertEqual(store.current("youtube").kind, "opened")

    def test_a_launch_that_failed_says_so_and_remembers_nothing(self):
        store = NowPlaying()
        reply = self._dispatch(self._media(YoutubePlayback(False, False, None)), store)
        self.assertEqual(reply, "I couldn't open that on YouTube right now.")
        self.assertIsNone(store.current("youtube"))

    def test_autoplay_off_keeps_the_old_behaviour(self):
        media = self._media(autoplay=False)
        store = NowPlaying()
        reply = self._dispatch(media, store)
        self.assertEqual(reply, "Searching YouTube for hottest hits of J. Cole.")
        self.resolve.assert_not_called()
        media.youtube_play_video.assert_not_called()
        self.assertEqual(store.current("youtube").kind, "opened")

    def test_a_results_page_that_failed_to_open_is_the_old_line(self):
        media = self._media()
        media.youtube_search.return_value = False
        reply = self._dispatch(media, video=None)
        self.assertEqual(reply, "I couldn't open YouTube search right now.")


if __name__ == "__main__":
    unittest.main()
