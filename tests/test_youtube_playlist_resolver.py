import unittest

from services.youtube_playlist_resolver import resolve_playlist_choice


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


if __name__ == "__main__":
    unittest.main()
