"""
DeviceFinder: the shared "find it by MAC, prove it, cache it" ladder.

These tests must never touch the network. `_NoNetworkMixin` stubs BOTH boundaries --
subprocess (arp, ping) and socket (the TCP scan). The socket one is genuinely new:
every previous guard in this suite patched subprocess only, and a subprocess-only
patch would let a SYN scan loose on whatever LAN the suite happens to run on.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.device_finder import (
    DeviceFinder,
    TraceStep,
    arp_table_candidates,
    normalize_mac,
    port_open,
    subnet_base,
    tcp_port_candidates,
)

# Scenario addresses. These are things discovery is supposed to FIND, not
# deployment values, which is exactly the case the marker exists for.
MOVED_TO = "192.168.1.41"      # config-literal: the address the box drifted to
DECOY = "192.168.1.51"         # config-literal: a decoy answering on the same port
BOX_MAC = "16:da:99:37:d0:89"  # config-literal: scenario MAC, not read from config
TV_MAC = "54:bd:79:24:45:32"   # config-literal: scenario MAC, not read from config


class _NoNetworkMixin:
    def setUp(self):
        super().setUp()
        run = patch("services.device_finder.subprocess.run")
        self.subprocess_run = run.start()
        self.subprocess_run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="")
        self.addCleanup(run.stop)

        conn = patch("services.device_finder.socket.create_connection")
        self.create_connection = conn.start()
        self.create_connection.side_effect = OSError("no network in unit tests")
        self.addCleanup(conn.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name) / "device_state.json"


HINT = "192.168.1.40"  # config-literal: the starting hint discovery is meant to outgrow


def _finder(cache, *, verify, sources=(), hint=HINT, key="mibox", mac=BOX_MAC):
    return DeviceFinder(
        key=key, label="Mi Box", mac=mac, hint=hint,
        cache_path=cache, verify=verify, candidate_sources=list(sources),
    )


class LadderTests(_NoNetworkMixin, unittest.TestCase):
    def test_a_verified_cache_entry_skips_every_candidate_source(self):
        self.cache.write_text(json.dumps({
            "version": 1,
            "devices": {"mibox": {"ip": MOVED_TO, "mac": BOX_MAC}},
        }), "utf-8")
        calls = []
        finder = _finder(self.cache, verify=lambda ip: ip == MOVED_TO,
                         sources=[lambda base: calls.append(base) or []])
        self.assertEqual(finder.resolve(), MOVED_TO)
        self.assertEqual(calls, [], "a good cache entry must not trigger a scan")

    def test_a_hint_hit_is_persisted(self):
        """The old code only wrote on a discovery hit, so the cache stayed empty."""
        finder = _finder(self.cache, verify=lambda ip: ip == HINT)
        self.assertEqual(finder.resolve(), HINT)
        stored = json.loads(self.cache.read_text("utf-8"))
        self.assertEqual(stored["devices"]["mibox"]["ip"], HINT)

    def test_wrong_identity_at_the_cached_address_falls_through(self):
        """Another device taking the address must not be mistaken for the box."""
        self.cache.write_text(json.dumps({
            "devices": {"mibox": {"ip": DECOY, "mac": BOX_MAC}},
        }), "utf-8")
        finder = _finder(self.cache, verify=lambda ip: ip == MOVED_TO,
                         sources=[lambda base: [MOVED_TO]])
        self.assertEqual(finder.resolve(), MOVED_TO)

    def test_candidate_sources_are_tried_in_order_and_stop_at_the_first_hit(self):
        seen = []
        finder = _finder(
            self.cache, verify=lambda ip: ip == MOVED_TO,
            sources=[
                lambda base: seen.append("first") or [DECOY],
                lambda base: seen.append("second") or [MOVED_TO],
                lambda base: seen.append("third") or [MOVED_TO],
            ],
        )
        self.assertEqual(finder.resolve(), MOVED_TO)
        self.assertEqual(seen, ["first", "second"], "must not run sources past a hit")

    def test_total_failure_returns_empty_not_an_exception(self):
        finder = _finder(self.cache, verify=lambda ip: False,
                         sources=[lambda base: [DECOY]])
        self.assertEqual(finder.resolve(), "")
        self.assertEqual(finder.last_verdict, "not-found")

    def test_a_raising_candidate_source_does_not_abort_the_ladder(self):
        def boom(base):
            raise OSError("arp exploded")

        finder = _finder(self.cache, verify=lambda ip: ip == MOVED_TO,
                         sources=[boom, lambda base: [MOVED_TO]])
        self.assertEqual(finder.resolve(), MOVED_TO)

    def test_memory_short_circuits_until_forced(self):
        probes = []
        finder = _finder(self.cache, verify=lambda ip: probes.append(ip) or True)
        self.assertEqual(finder.resolve(), HINT)
        finder.resolve()
        self.assertEqual(len(probes), 1, "a second resolve must reuse memory")
        finder.resolve(force=True)
        self.assertEqual(len(probes), 2, "force must re-probe")

    def test_trace_records_each_rung_in_order(self):
        finder = _finder(self.cache, verify=lambda ip: ip == MOVED_TO,
                         sources=[lambda base: [MOVED_TO]])
        finder.resolve()
        self.assertTrue(all(isinstance(s, TraceStep) for s in finder.last_trace))
        self.assertEqual([s.rung for s in finder.last_trace][0], "hint")
        self.assertEqual(finder.last_trace[-1].verdict, "verified")


class CacheTests(_NoNetworkMixin, unittest.TestCase):
    def test_remembering_one_device_does_not_clobber_the_other(self):
        """
        The bug the old _remember_ip would have introduced: it wrote {"tv_ip": ip}
        over the whole file, so a shared cache would lose a sibling on every write.
        """
        tv = _finder(self.cache, verify=lambda ip: True, key="tv", mac=TV_MAC)
        box = _finder(self.cache, verify=lambda ip: True, key="mibox", mac=BOX_MAC)
        tv.remember("192.168.1.39")  # config-literal: the TV's scenario address
        box.remember(MOVED_TO)
        stored = json.loads(self.cache.read_text("utf-8"))["devices"]
        self.assertEqual(stored["tv"]["ip"], "192.168.1.39")  # config-literal: the TV's scenario address
        self.assertEqual(stored["mibox"]["ip"], MOVED_TO)

    def test_a_cache_entry_for_a_different_mac_is_discarded(self):
        """A new SSID changes the MAC, and the stored address is stale for the
        same reason -- so do not even probe it."""
        self.cache.write_text(json.dumps({
            "devices": {"mibox": {"ip": DECOY, "mac": "aa:bb:cc:dd:ee:ff"}},
        }), "utf-8")
        finder = _finder(self.cache, verify=lambda ip: True)
        self.assertEqual(finder.cached_ip(), "")

    def test_unreadable_cache_degrades_to_the_hint(self):
        self.cache.write_text("{ not json", "utf-8")
        finder = _finder(self.cache, verify=lambda ip: True)
        self.assertEqual(finder.cached_or_hint(), HINT)

    def test_an_unwritable_cache_does_not_raise(self):
        finder = _finder(Path("/nonexistent-dir/nope/device_state.json"),
                         verify=lambda ip: True)
        finder.remember(MOVED_TO)  # must not raise


class HelperTests(_NoNetworkMixin, unittest.TestCase):
    def test_arp_parsing_handles_windows_and_linux_formats(self):
        """Development is Windows ('-' separated), production is a Pi (':')."""
        windows = f"  {MOVED_TO}           16-da-99-37-d0-89     dynamic"
        linux = f"? ({MOVED_TO}) at 16:da:99:37:d0:89 [ether] on wlan0"
        for table in (windows, linux):
            self.subprocess_run.return_value = SimpleNamespace(
                returncode=0, stdout=table, stderr="")
            found = list(arp_table_candidates(BOX_MAC)("192.168.1"))  # config-literal: scenario subnet
            self.assertEqual(found, [MOVED_TO], f"failed to parse: {table}")

    def test_arp_lookup_ignores_a_different_mac(self):
        self.subprocess_run.return_value = SimpleNamespace(
            returncode=0, stdout=f"  {DECOY}   aa-bb-cc-dd-ee-ff   dynamic", stderr="")
        self.assertEqual(list(arp_table_candidates(BOX_MAC)("192.168.1")), [])  # config-literal: scenario subnet

    def test_subnet_base_prefers_the_first_usable_hint(self):
        self.assertEqual(subnet_base("", "not-an-ip", MOVED_TO), "192.168.1")  # config-literal: scenario subnet
        self.assertEqual(subnet_base("", ""), "192.168.1")  # config-literal: the documented fallback

    def test_normalize_mac_matches_both_separators(self):
        self.assertEqual(normalize_mac("16:DA:99:37:D0:89"), normalize_mac("16-da-99-37-d0-89"))

    def test_port_open_is_false_when_the_socket_refuses(self):
        self.assertFalse(port_open(DECOY, 5555, 0.3))

    def test_the_port_scan_never_touches_the_real_network(self):
        # The guard: if someone drops the socket patch, this fails here rather than
        # by SYN-scanning the operator's subnet during a routine test run.
        list(tcp_port_candidates(5555, timeout_s=0.01, workers=4)("192.168.1"))  # config-literal: scenario subnet
        self.create_connection.assert_called()


if __name__ == "__main__":
    unittest.main()
