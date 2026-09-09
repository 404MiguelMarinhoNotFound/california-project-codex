"""
The one thing California knows that the box does not: what she put on.

`dumpsys media_session` gives a playback state and no title, so "Stremio is
playing" is readable and "Stremio is playing Fallout" is not. She launched it,
so she can remember it -- but memory that is never checked against the room is
just a confident guess, which is the failure mode this whole feature exists to
remove.
"""

import unittest

from services.now_playing import NowPlaying


class NowPlayingTests(unittest.TestCase):
    def setUp(self):
        self.store = NowPlaying()

    def test_nothing_is_remembered_to_start_with(self):
        self.assertIsNone(self.store.current("stremio"))
        self.assertIsNone(self.store.age_s())

    def test_a_launch_is_returned_while_its_app_is_still_up(self):
        self.store.remember("stremio", "Fallout")
        launch = self.store.current("stremio")
        self.assertIsNotNone(launch)
        self.assertEqual(launch.label, "Fallout")
        self.assertEqual(launch.kind, "playing")

    def test_the_memory_is_dropped_once_he_switches_apps(self):
        """
        The corroboration rule. Without it she would still be naming a Stremio
        episode while he is three menus deep in YouTube.
        """
        self.store.remember("stremio", "Fallout")
        self.assertIsNone(self.store.current("youtube"))

    def test_a_raw_package_name_still_matches(self):
        # get_current_app returns the package when its reverse lookup misses.
        self.store.remember("youtube", "your samba playlist")
        self.assertIsNotNone(self.store.current("com.google.android.youtube.tv"))

    def test_an_empty_foreground_app_matches_nothing(self):
        # "I could not read the foreground app" is not "the app is still up".
        self.store.remember("stremio", "Fallout")
        self.assertIsNone(self.store.current(""))

    def test_a_new_launch_replaces_the_old_one(self):
        # One slot on purpose: only one thing is on screen at a time.
        self.store.remember("stremio", "Fallout")
        self.store.remember("youtube", "your samba playlist")
        self.assertIsNone(self.store.current("stremio"))
        self.assertEqual(self.store.current("youtube").label, "your samba playlist")

    def test_forget_clears_it(self):
        self.store.remember("stremio", "Fallout")
        self.store.forget()
        self.assertIsNone(self.store.current("stremio"))
        self.assertIsNone(self.store.age_s())

    def test_a_search_is_remembered_as_opened_not_playing(self):
        # She put a results page on screen. She did not play anything.
        self.store.remember("youtube", "bossa nova", "opened")
        self.assertEqual(self.store.current("youtube").kind, "opened")

    def test_a_launch_with_no_label_is_not_remembered(self):
        # An opaque playlist id has no speakable name; better to forget than to
        # read an id out loud.
        self.store.remember("youtube", "")
        self.assertIsNone(self.store.current("youtube"))

    def test_age_starts_at_roughly_zero_and_is_not_none(self):
        self.store.remember("stremio", "Fallout")
        age = self.store.age_s()
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)


if __name__ == "__main__":
    unittest.main()
