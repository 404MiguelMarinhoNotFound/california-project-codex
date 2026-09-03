"""
Structural sweep of the REAL config.yaml's playlist data.

Every other test in this suite builds an inline config dict, because it is
testing code. This one tests *data*: an alias's correctness depends entirely on
which other categories share the file with it, so an inline fixture would assert
nothing about what actually ships. It is the only thing that catches an alias
that shadows another category, or one left pointing at a renamed key.

Pure YAML parsing plus pure matcher calls — no network, no services.
"""

import re
import unittest
from pathlib import Path

import yaml

from services.youtube_playlist_resolver import playlist_ids, resolve_playlist_choice

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

# PL community playlists (modern base64-ish and the legacy 16-hex form), RD
# radio mixes seeded from an 11-character video id, and UU channel uploads.
ID_PATTERN = re.compile(r"^(?:PL[0-9A-F]{16}|PL[A-Za-z0-9_-]{16,}|RD[A-Za-z0-9_-]{11}|UU[A-Za-z0-9_-]{16,})$")

_FIRST = lambda ids: ids[0]  # noqa: E731 — deterministic chooser for tests


def _config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


class PlaylistDataTests(unittest.TestCase):
    def setUp(self):
        config = _config()
        self.playlists = config.get("youtube_playlists") or {}
        self.aliases = config.get("youtube_playlist_aliases") or {}

    def test_there_are_playlists_at_all(self):
        self.assertTrue(self.playlists, "config.yaml has no youtube_playlists block")

    def test_every_category_yields_at_least_one_usable_id(self):
        """
        services/llm.py advertises a category in the system prompt only when
        playlist_ids() accepts it, so an empty category here would be a category
        California can never name — or worse, one she names and cannot play.
        """
        for category, value in self.playlists.items():
            self.assertTrue(playlist_ids(value), f"{category!r} has no usable playlist ID")

    def test_every_id_looks_like_a_youtube_playlist_id(self):
        for category, value in self.playlists.items():
            for playlist_id in playlist_ids(value):
                self.assertRegex(playlist_id, ID_PATTERN, f"{category!r} -> {playlist_id!r}")

    def test_no_category_repeats_an_id_within_itself(self):
        # Sharing an ID ACROSS categories is deliberate and must stay legal:
        # RDNwIvYGn3ca4 is in samba, pagode and pagode praia, and
        # PLC90FB71F6ECE17F3 is in both 70s 80s 90s hits and legendary hits.
        # A repeat inside ONE category is a copy-paste slip that skews the
        # random pick toward the duplicated playlist.
        for category, value in self.playlists.items():
            ids = playlist_ids(value)
            self.assertEqual(len(ids), len(set(ids)), f"{category!r} lists the same ID twice")


class PlaylistAliasDataTests(unittest.TestCase):
    def setUp(self):
        config = _config()
        self.playlists = config.get("youtube_playlists") or {}
        self.aliases = config.get("youtube_playlist_aliases") or {}

    def test_every_alias_key_names_a_real_category(self):
        for key in self.aliases:
            self.assertIn(key, self.playlists, f"alias block targets unknown category {key!r}")

    def test_every_alias_resolves_to_its_own_category(self):
        """The shadowing guard. An alias that lands on a different category is
        worse than no alias: she confidently plays the wrong thing."""
        for key, spoken_forms in self.aliases.items():
            for spoken in spoken_forms:
                matched, _ = resolve_playlist_choice(
                    spoken, self.playlists, self.aliases, chooser=_FIRST
                )
                self.assertEqual(key, matched, f"alias {spoken!r} resolved to {matched!r}, not {key!r}")

    def test_every_category_key_still_resolves_to_itself(self):
        """Catches an alias that steals a canonical category name."""
        for key in self.playlists:
            matched, _ = resolve_playlist_choice(key, self.playlists, self.aliases, chooser=_FIRST)
            self.assertEqual(key, matched, f"category {key!r} now resolves to {matched!r}")


class SpokenPhrasingTests(unittest.TestCase):
    """
    Regression table of how real speech should land. The first four are the
    misses that motivated the alias work; the rest already worked and must
    keep working.
    """

    EXPECTED = {
        "road trip": "roadtrip",
        "put on some road trip music": "roadtrip",
        "r and b": "rnb",
        "R&B": "rnb",
        "old school stuff": "legendary hits",
        "some old school stuff": "legendary hits",
        "some throwbacks": "legendary hits",
        "oldies": "70s 80s 90s hits",
        "bedroom music": "sex songs",
        "some samba": "samba",
        "beach samba": "samba",
        "80s": "70s 80s 90s hits",
        "praia": "pagode praia",
        "sex": "sex songs",
        "workout music": "workout",
        "jazz": "jazz",
        # Control: an adjective alias on dark romance would steal this one,
        # which is why none were added. See the comment in config.yaml.
        "moody jazz": "jazz",
    }

    def test_spoken_forms_land_on_the_right_category(self):
        config = _config()
        playlists = config.get("youtube_playlists") or {}
        aliases = config.get("youtube_playlist_aliases") or {}

        for spoken, expected in self.EXPECTED.items():
            with self.subTest(spoken=spoken):
                matched, playlist_id = resolve_playlist_choice(
                    spoken, playlists, aliases, chooser=_FIRST
                )
                self.assertEqual(expected, matched)
                self.assertTrue(playlist_id)


if __name__ == "__main__":
    unittest.main()
