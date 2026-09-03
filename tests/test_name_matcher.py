"""
Direct coverage for the shared fuzzy matcher.

`match_name` was only ever exercised indirectly, through the light/colour and
playlist resolvers. Its tier ORDER is load-bearing in a way neither of those
tests states, so it is pinned here.
"""

import unittest

from services.name_matcher import TOKEN_MATCH_THRESHOLD, match_name, normalize_text


class NormalizeTextTests(unittest.TestCase):
    def test_ampersand_becomes_the_word_and(self):
        # Without this, "R&B" collapses to "rb" — a two-character string that
        # the substring tier finds inside "herbie", "urban" and "superb".
        self.assertEqual("r and b", normalize_text("R&B"))
        self.assertEqual("rock and roll", normalize_text("Rock & Roll"))

    def test_case_punctuation_and_spacing_are_flattened(self):
        self.assertEqual("70s 80s 90s hits", normalize_text("  70s, 80s, 90s   HITS! "))

    def test_empty_and_none_are_safe(self):
        self.assertEqual("", normalize_text(""))
        self.assertEqual("", normalize_text(None))


class MatchTierOrderTests(unittest.TestCase):
    def test_exact_wins_over_despaced(self):
        # "warm white" despaces to the same string as "warmwhite", so the only
        # thing keeping an exact hit from being stolen is tier 1 running first.
        candidates = {"warmwhite": ["warmwhite"], "warm white": ["warm white"]}
        self.assertEqual("warmwhite", match_name("warmwhite", candidates))
        self.assertEqual("warm white", match_name("warm white", candidates))

    def test_despaced_wins_over_substring(self):
        """
        The regression this tier exists for. With only exact + substring,
        "warmwhite" matches "white" (a substring of it) and the strip turns the
        wrong colour. Note there is no hand-built despaced alias here — the
        matcher has to get this right on the bare names.
        """
        candidates = {"white": ["white"], "warm white": ["warm white"]}
        self.assertEqual("warm white", match_name("warmwhite", candidates))

    def test_substring_wins_over_token_overlap(self):
        candidates = {"legendary hits": ["legendary hits"], "70s 80s 90s hits": ["70s 80s 90s hits"]}
        self.assertEqual("legendary hits", match_name("legendary", candidates))

    def test_token_overlap_is_the_last_resort(self):
        self.assertEqual("beach samba", match_name("samba beach", {"beach samba": ["beach samba"]}))


class DespacedTierTests(unittest.TestCase):
    def test_spoken_spacing_reaches_a_squashed_key(self):
        # The live case: the config key is "roadtrip", Whisper writes "road trip".
        self.assertEqual("roadtrip", match_name("road trip", {"roadtrip": ["roadtrip"]}))

    def test_squashed_speech_reaches_a_spaced_key(self):
        self.assertEqual("sex songs", match_name("sexsongs", {"sex songs": ["sex songs"]}))

    def test_ampersand_and_despacing_compose(self):
        # "R n B" -> "r n b" -> "rnb" once despaced.
        self.assertEqual("rnb", match_name("R n B", {"rnb": ["rnb"]}))


class TokenOverlapThresholdTests(unittest.TestCase):
    def test_scores_below_the_threshold_are_rejected(self):
        # One shared token out of the name's four is 0.25, under the 0.5 gate.
        # ("hits" alone would match earlier, as a substring of the key.)
        self.assertIsNone(match_name("hits songs", {"70s 80s 90s hits": ["70s 80s 90s hits"]}))

    def test_threshold_is_unchanged(self):
        self.assertEqual(0.5, TOKEN_MATCH_THRESHOLD)


class NoMatchTests(unittest.TestCase):
    def test_unrelated_hint_returns_none(self):
        self.assertIsNone(match_name("polka", {"samba": ["samba"], "jazz": ["jazz"]}))

    def test_empty_hint_or_candidates_returns_none(self):
        self.assertIsNone(match_name("", {"samba": ["samba"]}))
        self.assertIsNone(match_name("samba", {}))

    def test_aliases_resolve_to_their_key(self):
        self.assertEqual("attic", match_name("the loft", {"attic": ["attic", "the loft"]}))


if __name__ == "__main__":
    unittest.main()
