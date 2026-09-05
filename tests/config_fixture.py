"""The real config.yaml, as the base for test fixtures.

Every fixture in this suite used to re-type its own config dict, and those
copies drifted: test_media_service.py pointed at 192.168.1.26 long after the
box moved to .35, so the tests described a deployment that no longer existed.
A hand-written fixture can only ever assert that the code agrees with the
fixture -- never that it agrees with what actually ships.

So tests build on `config_for_tests()`, which starts from the committed
config.yaml and deep-merges only the keys a test must pin down. Anything not
overridden comes from the real file, which means a renamed key, a changed
package name or a moved IP surfaces as a test failure instead of silently
leaving the suite testing a fiction.

What is legitimate to override, and why:

  * paths      -- watch_state.json / vpn_state.json must go to a tmpdir, never
                  the developer's real cache
  * delays     -- autoplay and Surfshark waits are seconds each in production
                  and buy nothing against a mocked ADB
  * cec_wake   -- disabled so a test never tries to Wake-on-LAN the television

Values that describe the deployment -- IP, port, adb_path, package names,
launch components, route tables -- must NOT be overridden. Those are exactly
the ones worth catching drift in.
"""

import copy
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

_CACHE: dict | None = None


def real_config() -> dict:
    """A deep copy of the committed config.yaml.

    Parsed once, copied per call, so a test mutating its fixture cannot leak
    into the next one.
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(_CACHE)


def deep_merge(base: dict, overrides: dict) -> dict:
    """Recursive dict merge. Non-dict values at any depth replace wholesale."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def config_for_tests(**overrides) -> dict:
    """The real config with `overrides` deep-merged over it.

        config_for_tests(media={"adb_timeout_ms": 100})

    keeps every other media key -- mibox_ip, apps, launch components -- exactly
    as it ships.
    """
    return deep_merge(real_config(), overrides)
