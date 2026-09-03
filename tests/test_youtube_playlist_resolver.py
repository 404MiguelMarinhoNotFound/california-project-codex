import unittest

from services.youtube_playlist_resolver import (
    playlist_aliases,
    playlist_ids,
    resolve_playlist_choice,
)


class YouTubePlaylistResolverTests(unittest.TestCase):
    def test_exact_match_returns_deterministic_choice_from_list(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "samba",
            {
                "samba": ["PL-SAMBA-1", "PL-SAMBA-2"],
                "jazz": "PL-JAZZ-1",
            },
            chooser=lambda ids: ids[-1],
        )

        self.assertEqual(matched_key, "samba")
        self.assertEqual(playlist_id, "PL-SAMBA-2")

    def test_partial_match_works_with_multi_word_keys(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "beach samba",
            {
                "brazilian beach samba": ["PL-PAGODE-1", "PL-PAGODE-2"],
            },
            chooser=lambda ids: ids[0],
        )

        self.assertEqual(matched_key, "brazilian beach samba")
        self.assertEqual(playlist_id, "PL-PAGODE-1")

    def test_token_match_can_resolve_related_playlist_name(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "seventies classics",
            {
                "70s classics": "PL-70S-1",
                "workout": "PL-WORKOUT-1",
            },
            chooser=lambda ids: ids[0],
        )

        self.assertEqual(matched_key, "70s classics")
        self.assertEqual(playlist_id, "PL-70S-1")

    def test_no_match_returns_none_pair(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "metalcore",
            {
                "samba": ["PL-SAMBA-1"],
                "jazz": "PL-JAZZ-1",
            },
            chooser=lambda ids: ids[0],
        )

        self.assertIsNone(matched_key)
        self.assertIsNone(playlist_id)


class PlaylistIdsTests(unittest.TestCase):
    def test_accepts_a_bare_string_or_a_list(self):
        self.assertEqual(["PL-1"], playlist_ids("PL-1"))
        self.assertEqual(["PL-1", "PL-2"], playlist_ids(["PL-1", " PL-2 "]))

    def test_unusable_values_yield_nothing(self):
        # The system-prompt inventory in services/llm.py filters on this, so a
        # category that returns [] here is never advertised to the model either.
        for value in ("", "   ", [], ["", "  "], None, 7, {"ids": ["PL-1"]}):
            self.assertEqual([], playlist_ids(value), value)


class PlaylistAliasTests(unittest.TestCase):
    def test_missing_map_or_key_yields_nothing(self):
        self.assertEqual([], playlist_aliases(None, "samba"))
        self.assertEqual([], playlist_aliases({}, "samba"))
        self.assertEqual([], playlist_aliases({"jazz": ["smooth"]}, "samba"))

    def test_configured_aliases_are_stripped(self):
        self.assertEqual(["road trip"], playlist_aliases({"roadtrip": [" road trip "]}, "roadtrip"))

    def test_non_string_and_empty_entries_are_dropped(self):
        self.assertEqual(["ok"], playlist_aliases({"k": ["ok", "", "  ", 7, None]}, "k"))

    def test_an_alias_resolves_to_its_category(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "r and b",
            {"rnb": ["PL-RNB-1"], "jazz": ["PL-JAZZ-1"]},
            aliases={"rnb": ["r and b"]},
            chooser=lambda ids: ids[0],
        )

        self.assertEqual("rnb", matched_key)
        self.assertEqual("PL-RNB-1", playlist_id)

    def test_alias_for_a_category_with_no_ids_is_inert(self):
        # Dead config, not a crash: ids_by_key gates which candidates exist.
        matched_key, playlist_id = resolve_playlist_choice(
            "road trip",
            {"roadtrip": [], "jazz": ["PL-JAZZ-1"]},
            aliases={"roadtrip": ["road trip"]},
            chooser=lambda ids: ids[0],
        )

        self.assertIsNone(matched_key)
        self.assertIsNone(playlist_id)

    def test_alias_naming_an_unknown_category_is_inert(self):
        matched_key, _ = resolve_playlist_choice(
            "polka",
            {"jazz": ["PL-JAZZ-1"]},
            aliases={"nonexistent": ["polka"]},
            chooser=lambda ids: ids[0],
        )

        self.assertIsNone(matched_key)

    def test_aliases_default_to_none_so_old_call_sites_still_work(self):
        matched_key, playlist_id = resolve_playlist_choice(
            "samba", {"samba": ["PL-SAMBA-1"]}, chooser=lambda ids: ids[0]
        )

        self.assertEqual("samba", matched_key)
        self.assertEqual("PL-SAMBA-1", playlist_id)


if __name__ == "__main__":
    unittest.main()
