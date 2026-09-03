"""
Coverage for the playlist validator's classification logic.

Everything here except the LIVE class is offline: the classifiers are pure, so
they are driven with fixture strings. Fixtures are inline constants rather than
files because a real YouTube page is ~830 KB and the classifiers only look at a
handful of markers — the same reasoning behind the local `_Response` fake in
tests/test_stremio_service.py.
"""

import os
import unittest

from tools.validate_youtube_playlists import (
    STATUS_FETCH_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    check_playlist,
    classify_oembed,
    classify_page,
    extract_title,
    iter_playlist_entries,
    oembed_url,
    playlist_url,
    title_matches_category,
)

LIVE = unittest.skipUnless(
    os.environ.get("CALIFORNIA_LIVE_TESTS") == "1",
    "network test; set CALIFORNIA_LIVE_TESTS=1 to run",
)

HTML_NORMAL = (
    '<html><head><meta property="og:title" content="Casino Jazz Music">'
    "<title>Casino Jazz Music - YouTube</title></head><body></body></html>"
)

# Modelled on the real body of RDgQRtAnPL6HM: HTTP 200, no og:title, an error
# marker, and a "Visit source" chrome string sitting where a title regex looks.
HTML_UNAVAILABLE = (
    '<html><head><title>YouTube</title></head><body>'
    '{"playabilityStatus":{"status":"ERROR","reason":"UNPLAYABLE"}}'
    '{"title":"Visit source"}'
    "</body></html>"
)

HTML_NO_TITLE = "<html><head></head><body>nothing useful here</body></html>"

OEMBED_OK = '{"title":"Casino Jazz Music","author_name":"Some Channel","type":"video"}'
OEMBED_NOT_FOUND = "Not Found"


class PlaylistUrlTests(unittest.TestCase):
    def test_pl_ids_use_the_playlist_page(self):
        self.assertEqual(
            "https://www.youtube.com/playlist?list=PL-ABC",
            playlist_url("PL-ABC"),
        )

    def test_rd_ids_use_the_seed_videos_watch_page(self):
        url = playlist_url("RDgQRtAnPL6HM")
        self.assertIn("v=gQRtAnPL6HM", url)          # the RD prefix is stripped
        self.assertIn("list=RDgQRtAnPL6HM", url)
        self.assertIn("start_radio=1", url)

    def test_an_rd_id_never_produces_a_playlist_url(self):
        """
        Non-obvious and load-bearing: /playlist?list=RD... returns 404 even for a
        healthy mix, so oembed would call every RD entry dead.
        """
        self.assertNotIn("/playlist?list=RD", playlist_url("RDgQRtAnPL6HM"))

    def test_oembed_url_wraps_the_target(self):
        self.assertTrue(oembed_url("https://x/y").startswith("https://www.youtube.com/oembed?"))
        self.assertIn("format=json", oembed_url("https://x/y"))


class ClassifyOembedTests(unittest.TestCase):
    def test_two_hundred_with_a_title_is_ok(self):
        self.assertEqual((STATUS_OK, "Casino Jazz Music"), classify_oembed(200, OEMBED_OK))

    def test_deleted_and_private_codes_are_unavailable(self):
        # A bogus video id answers 400 rather than 404, so the whole set matters.
        for code in (400, 401, 403, 404):
            self.assertEqual((STATUS_UNAVAILABLE, None), classify_oembed(code, OEMBED_NOT_FOUND), code)

    def test_server_errors_and_junk_are_inconclusive_not_dead(self):
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_oembed(500, ""))
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_oembed(0, "timed out"))
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_oembed(200, "<html>not json</html>"))
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_oembed(200, '{"title":""}'))


class ClassifyPageTests(unittest.TestCase):
    def test_a_dead_page_with_a_decoy_title_is_unavailable(self):
        """
        The regression this whole change exists for. This body returns HTTP 200
        and yields the scrapeable string "Visit source"; the old validator
        reported it as ok, and six dead playlists rode through on it.
        """
        self.assertEqual((STATUS_UNAVAILABLE, None), classify_page(200, HTML_UNAVAILABLE))

    def test_a_healthy_page_is_ok_with_its_title(self):
        self.assertEqual((STATUS_OK, "Casino Jazz Music"), classify_page(200, HTML_NORMAL))

    def test_a_page_with_no_title_is_inconclusive(self):
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_page(200, HTML_NO_TITLE))

    def test_error_codes_pass_through(self):
        self.assertEqual((STATUS_UNAVAILABLE, None), classify_page(404, ""))
        self.assertEqual((STATUS_FETCH_ERROR, None), classify_page(503, ""))


class ExtractTitleTests(unittest.TestCase):
    def test_chrome_strings_are_rejected(self):
        for chrome in ("Visit source", "undefined", "YouTube"):
            self.assertIsNone(extract_title(f'<body>{{"title":"{chrome}"}}</body>'), chrome)

    def test_the_youtube_suffix_is_stripped(self):
        self.assertEqual("Casino Jazz Music", extract_title("<title>Casino Jazz Music - YouTube</title>"))

    def test_entities_are_unescaped(self):
        self.assertEqual("Rock & Roll", extract_title('<meta property="og:title" content="Rock &amp; Roll">'))


class TitleMatchTests(unittest.TestCase):
    def test_a_matching_title_passes(self):
        self.assertTrue(title_matches_category("Roda de Samba ao vivo", "samba"))

    def test_an_rd_seed_title_does_not_match_its_category(self):
        # Pins WHY this check is advisory and off by default: this is a healthy
        # entry from the live config, and a strict check would flag it.
        self.assertFalse(title_matches_category("Grupo Revelacao - Deixa Acontecer", "samba"))

    def test_generic_only_categories_never_fail(self):
        self.assertTrue(title_matches_category("Anything At All", "hits"))


class IterEntriesTests(unittest.TestCase):
    def test_both_config_shapes_are_read(self):
        entries = list(iter_playlist_entries({"samba": ["A", " B "], "jazz": "C"}))
        self.assertEqual([("samba", "A"), ("samba", "B"), ("jazz", "C")], entries)

    def test_junk_values_are_skipped(self):
        self.assertEqual([], list(iter_playlist_entries({"a": None, "b": 7, "c": [""], "d": {}})))


@LIVE
class LiveYouTubeChecks(unittest.TestCase):
    """Proves the layered check works against the real YouTube, not just fixtures."""

    def test_a_known_good_playlist_resolves(self):
        result = check_playlist("jazz", "PL0nquJc_KvtI8PZsCGVCKBG1ABIpc7LRC")
        self.assertEqual(STATUS_OK, result["status"])
        self.assertTrue(result["title"])

    def test_a_known_dead_mix_is_reported_dead(self):
        # Confirmed 404 on 2026-09-03. Stable in the useful direction: a deleted
        # playlist does not come back.
        result = check_playlist("rnb", "RDgQRtAnPL6HM")
        self.assertEqual(STATUS_UNAVAILABLE, result["status"])


if __name__ == "__main__":
    unittest.main()
