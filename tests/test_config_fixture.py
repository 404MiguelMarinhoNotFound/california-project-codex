"""The fixture layer itself, plus a guard against re-typed config values.

Fixtures used to hardcode their own copies of the deployment: test_media_service
pointed at 192.168.1.26 long after the box moved to .35, and the Surfshark tests
pinned a 3-key quick_connect route while the shipped table has 4. Both kept
passing, because a hand-written fixture only ever proves the code agrees with
the fixture.

The sweep below is deliberately narrow: it looks for the one shape that has
actually drifted silently -- an IP literal fed to the code as fixture input.
A test that genuinely needs one marks it with a reason.
"""

import re
import unittest
from pathlib import Path

from tests.config_fixture import CONFIG_PATH, config_for_tests, deep_merge, real_config

TESTS_DIR = Path(__file__).resolve().parent

# Only IP literals. Package names appear all over the suite in *assertions*
# ("the launch command names the youtube package"), and an assertion against a
# stale literal fails the moment config.yaml changes -- it is self-correcting.
# A fixture *input* is the dangerous direction: it feeds the code a value that
# no longer ships and the test keeps passing. That is what this catches.
_IP_LITERAL = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

# Files allowed to name them, with the reason.
_ALLOWED_FILES = {
    "config_fixture.py",       # defines the loader
    "test_config_fixture.py",  # this file, which must quote them to check them
}

# The opt-out, which must carry a reason:
#     ... "192.168.1.77" ...  # config-literal: an address the TV has moved to
# Scenario data (an address discovery is supposed to *find*, an activity name
# the parser must recognise) is legitimately not config. A deployment value is
# not, and the marker forces that to be a decision someone wrote down.
_OPT_OUT = re.compile(r"#\s*config-literal:\s*\S")


def _offending_lines(pattern: re.Pattern) -> list[str]:
    hits = []
    for path in sorted(TESTS_DIR.glob("test_*.py")) + [TESTS_DIR / "config_fixture.py"]:
        if path.name in _ALLOWED_FILES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not pattern.search(line):
                continue
            if _OPT_OUT.search(line):
                continue
            hits.append(f"{path.name}:{number}: {line.strip()}")
    return hits


class ConfigFixtureTests(unittest.TestCase):
    def test_real_config_is_the_committed_file(self):
        self.assertTrue(CONFIG_PATH.exists(), "config.yaml is missing")
        config = real_config()
        for key in ("media", "stremio", "govee", "llm", "vad", "audio"):
            self.assertIn(key, config, f"config.yaml has no {key} block")

    def test_each_call_returns_an_independent_copy(self):
        first = real_config()
        first["media"]["mibox_ip"] = "0.0.0.0"  # mutation must not leak
        self.assertNotEqual(real_config()["media"]["mibox_ip"], "0.0.0.0")

    def test_overrides_replace_without_dropping_siblings(self):
        config = config_for_tests(media={"adb_timeout_ms": 1})
        self.assertEqual(config["media"]["adb_timeout_ms"], 1)
        self.assertEqual(config["media"]["mibox_ip"], real_config()["media"]["mibox_ip"])
        self.assertIn("stremio", config["media"]["apps"])

    def test_deep_merge_recurses_but_replaces_leaves(self):
        merged = deep_merge(
            {"a": {"b": {"c": 1, "d": 2}}, "keep": True},
            {"a": {"b": {"c": 9}}},
        )
        self.assertEqual(merged["a"]["b"], {"c": 9, "d": 2})
        self.assertTrue(merged["keep"])

        # A list is a leaf: it replaces, it does not concatenate. The provider
        # order and DPAD sequences are lists, and merging them would be silent
        # nonsense.
        self.assertEqual(deep_merge({"x": [1, 2]}, {"x": [3]})["x"], [3])

    def test_no_fixture_hardcodes_an_ip_address(self):
        hits = _offending_lines(_IP_LITERAL)
        self.assertEqual(
            hits,
            [],
            "IP literals in test fixtures go stale silently. Take them from "
            "config_for_tests() instead:\n  " + "\n  ".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
