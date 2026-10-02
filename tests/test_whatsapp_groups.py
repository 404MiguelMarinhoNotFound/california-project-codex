"""
Tests for listing WhatsApp groups: the driver's scrolled read of the chat list
under the "Groups" chip, the service's cache around it, and the chip selectors
WhatsApp renamed on 2026-10-02.

Nothing here launches a browser. The driver runs against `_ScrollPage`, a fake
virtualised list: it renders only a window of rows around its scroll position,
the way WhatsApp Web keeps ~20 rows in the DOM out of hundreds. That window is
the whole reason the driver scrolls, so a test that rendered every row at once
could not tell a scrolled read from a single one.
"""

import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import whatsapp_service as wa  # noqa: E402
from services import whatsapp_web as web  # noqa: E402
from services.whatsapp_service import WhatsAppCommandResult, WhatsAppService  # noqa: E402
from tests.config_fixture import config_for_tests  # noqa: E402
from tests.test_whatsapp_service import QP_VCF, _book  # noqa: E402

_CHIP_BY_SELECTOR = {", ".join(web.SELECTORS[k]): k for k in ("filter_all", "filter_unread", "filter_groups")}


def _group(i, name=None, muted=False, indexed=True):
    return {
        "name": name if name is not None else f"group {i:03d}",
        "preview": "",
        "unread": False,
        "count": None,
        "muted": muted,
        "index": i if indexed else None,
        "top": i if indexed else None,  # the fake's rows are one unit tall
    }


class _Chip:
    def __init__(self, page, key):
        self.page, self.key = page, key

    first = property(lambda self: self)

    def is_visible(self):
        return self.key in self.page.chips

    def evaluate(self, script):
        self.page.clicks.append(self.key)
        self.page.active = self.key
        self.page.top = 0

    def get_attribute(self, name):
        return "true" if self.page.active == self.key else "false"


class _ScrollPage:
    """A virtualised chat list: `client` rows visible, a few more rendered either side."""

    url = web.WHATSAPP_URL

    def __init__(self, groups, client=8, overscan=2, chips=("filter_all", "filter_unread", "filter_groups")):
        self.views = {"filter_groups": groups, "filter_all": [_group(900, "Bruno")]}
        self.client, self.overscan = client, overscan
        self.chips = set(chips)
        self.active = "filter_all"
        self.top = 0
        self.clicks: list[str] = []
        self.gotos: list[str] = []
        self.scrolls = 0
        # index -> how many more reads show WhatsApp's "Group" placeholder
        # before the real name has loaded. None: only a revisit loads it.
        self.loading: dict = {}
        self.revisited: list = []

    def locator(self, selector):
        return _Chip(self, _CHIP_BY_SELECTOR[selector])

    def _rows(self):
        return self.views.get(self.active, [])

    def _shown(self, row):
        left = self.loading.get(row["index"], 0)
        if left is None or left > 0:
            if left:
                self.loading[row["index"]] = left - 1
            return dict(row, name="Group")
        return row

    def evaluate(self, script, args=None):
        rows = self._rows()
        if script is web._SCROLL_LIST_JS:
            _, fraction = args
            self.scrolls += 1
            bottom = max(0, len(rows) - self.client)
            self.top = 0 if fraction <= 0 else min(bottom, self.top + int(self.client * fraction))
            return {"top": self.top, "height": len(rows), "client": self.client}
        if script is web._SCROLL_TO_JS:
            _, top = args
            self.revisited.append(top)
            self.top = max(0, int(top) - self.client // 2)
            if self.loading.get(top, 0) is None:
                self.loading[top] = 0  # being looked at is what loads it
            return None
        lo = max(0, self.top - self.overscan)
        return [self._shown(r) for r in rows[lo : self.top + self.client + self.overscan]]

    def goto(self, url, **_):
        self.gotos.append(url)


def _driver(page, state="linked"):
    driver = web.WhatsAppWebDriver(Path("unused"), channel=None, headless=True)
    driver._page = page
    driver.warm = lambda: state
    return driver


def _list(page, state="linked"):
    with patch.object(web.time, "sleep"):
        return _driver(page, state).list_groups()


class ChipSelectorTests(unittest.TestCase):
    """On 2026-10-02 #unread-filter / #group-filter became positional label_item_N."""

    def test_the_chips_fall_back_on_their_label_in_both_languages(self):
        unread = ", ".join(web.SELECTORS["filter_unread"])
        groups = ", ".join(web.SELECTORS["filter_groups"])
        for text in ('"Unread"', '"Não lidas"'):
            self.assertIn(text, unread)
        for text in ('"Groups"', '"Grupos"'):
            self.assertIn(text, groups)

    def test_no_selector_keys_on_a_positional_id(self):
        """label_item_3 is Groups only while there are exactly two chips before it."""
        for key in ("filter_all", "filter_unread", "filter_groups"):
            self.assertNotIn("label_item", ", ".join(web.SELECTORS[key]))


class DriverListGroupsTests(unittest.TestCase):
    def test_reads_every_group_not_just_the_first_render(self):
        page = _ScrollPage([_group(i) for i in range(50)])
        result = _list(page)
        self.assertTrue(result)
        self.assertEqual([g.name for g in result.groups], [f"group {i:03d}" for i in range(50)])
        self.assertGreater(page.scrolls, 2)

    def test_two_groups_with_one_title_are_both_kept(self):
        """Seen live: two groups sharing one title. Keyed by list position, not title."""
        groups = [_group(i) for i in range(30)]
        groups[3]["name"] = groups[25]["name"] = "book club"
        result = _list(_ScrollPage(groups))
        self.assertEqual([g.name for g in result.groups].count("book club"), 2)
        self.assertEqual(len(result.groups), 30)

    def test_without_a_list_index_rows_are_keyed_by_title(self):
        result = _list(_ScrollPage([_group(i, indexed=False) for i in range(30)]))
        self.assertEqual(len(result.groups), 30)

    def test_muted_is_carried_and_direction_marks_are_stripped(self):
        result = _list(_ScrollPage([_group(0, "‪Família 🏠‬", muted=True), _group(1)]))
        self.assertEqual(result.groups[0].name, "Família 🏠")
        self.assertTrue(result.groups[0].muted)
        self.assertFalse(result.groups[1].muted)

    def test_it_never_opens_a_chat(self):
        page = _ScrollPage([_group(i) for i in range(40)])
        _list(page)
        self.assertEqual(page.gotos, [])

    def test_the_list_is_put_back_on_all_and_at_the_top(self):
        page = _ScrollPage([_group(i) for i in range(40)])
        _list(page)
        self.assertEqual(page.clicks[-1], "filter_all")
        self.assertEqual(page.top, 0)

    def test_no_groups_chip_is_its_own_outcome(self):
        result = _list(_ScrollPage([_group(0)], chips=("filter_all",)))
        self.assertFalse(result)
        self.assertEqual(result.status, web.NO_GROUP_FILTER)

    def test_an_unlinked_profile_says_so(self):
        result = _list(_ScrollPage([]), state="needs_link")
        self.assertEqual(result.status, web.NOT_LINKED)

    def test_a_placeholder_name_is_revisited_until_the_real_one_loads(self):
        """Live 2026-10-02: at the faster settle row 25 read "Group", then its real name 0.3s later."""
        groups = [_group(i) for i in range(40)]
        page = _ScrollPage(groups)
        page.loading = {25: None}
        result = _list(page)
        names = [g.name for g in result.groups]
        self.assertNotIn("Group", names)
        self.assertIn("group 025", names)
        self.assertEqual(page.revisited, [25])

    def test_a_later_read_with_the_real_name_replaces_the_placeholder(self):
        groups = [_group(i) for i in range(40)]
        page = _ScrollPage(groups)
        page.loading = {9: 1}  # placeholder on the first read only; overscan shows it again
        names = [g.name for g in _list(page).groups]
        self.assertIn("group 009", names)
        self.assertNotIn("Group", names)

    def test_a_group_really_called_group_is_kept_after_its_revisits(self):
        groups = [_group(i) for i in range(10)]
        groups[4] = _group(4, "Group")
        page = _ScrollPage(groups)
        names = [g.name for g in _list(page).groups]
        self.assertEqual(names.count("Group"), 1)
        self.assertEqual(len(page.revisited), web._PLACEHOLDER_TRIES)

    def test_the_scroll_never_steps_past_the_rendered_window(self):
        """Above 1.0 rows outside the rendered window are skipped; 1.5 only got
        all 218 groups live because of the overscan."""
        self.assertLessEqual(web._SCROLL_FRACTION, 1.0)

    def test_a_list_that_never_ends_is_bounded(self):
        """Every read brings a new row and the bottom never comes: stop anyway."""
        page = _ScrollPage([])
        fresh = itertools.count()
        page.evaluate = lambda script, args=None: (
            {"top": 0, "height": 10**9, "client": 8} if script is web._SCROLL_LIST_JS else [_group(next(fresh))]
        )
        with patch.object(web, "_MAX_SCROLL_STEPS", 5):
            result = _list(page)
        self.assertTrue(result)
        self.assertLessEqual(len(result.groups), 7)


class DriverUnreadKnownGroupsTests(unittest.TestCase):
    def test_a_cached_group_beyond_the_first_render_is_still_a_group(self):
        page = _ScrollPage([])
        page.views["filter_unread"] = [dict(_group(0, "the band"), unread=True, count=3)]
        with patch.object(web.time, "sleep"):
            result = _driver(page).unread_chats(known_groups=["the band"])
        self.assertTrue(result.chats[0].is_group)


def _service(groups_path, **overrides) -> WhatsAppService:
    cfg = {"enabled": True, "backend": "playwright", "contacts_path": str(_book(QP_VCF)), "groups_path": str(groups_path)}
    cfg.update(overrides)
    with patch.object(WhatsAppService, "_probe_platform", return_value=True):
        service = WhatsAppService(config_for_tests(whatsapp=cfg))
    service._driver = MagicMock()
    service._driver.is_open = False
    return service


class ServiceListGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "groups.json"

    def _ok(self, *names):
        return web.GroupListResult(web.READ_OK, [web.GroupChat(n, muted=(n == "loud")) for n in names])

    def test_a_listing_is_cached_and_read_back_by_the_next_service(self):
        service = _service(self.path)
        service._driver.list_groups.return_value = self._ok("the band", "loud")
        self.assertTrue(service.list_groups())
        service.close()
        self.assertEqual(service.group_names(), ["the band", "loud"])
        cached = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(cached["groups"][1], {"name": "loud", "muted": True})
        self.assertEqual(_service(self.path).group_names(), ["the band", "loud"])

    def test_a_failed_listing_leaves_the_cache_alone(self):
        service = _service(self.path)
        service._driver.list_groups.return_value = self._ok("the band")
        service.list_groups()
        service._driver.list_groups.return_value = web.GroupListResult(web.TIMEOUT, [])
        self.assertFalse(service.list_groups())
        service.close()
        self.assertEqual(_service(self.path).group_names(), ["the band"])

    def test_each_failure_has_its_own_line(self):
        cases = {
            web.NOT_LINKED: wa._MSG_NEEDS_LINK,
            web.NO_GROUP_FILTER: wa._MSG_NO_GROUP_FILTER,
            web.TIMEOUT: wa._MSG_READ_FAILED,
        }
        for status, line in cases.items():
            service = _service(self.path)
            service._driver.list_groups.return_value = web.GroupListResult(status, [])
            self.assertEqual(service.list_groups().message, line, status)
            service.close()

    def test_a_crash_closes_the_browser(self):
        service = _service(self.path)
        service._driver.list_groups.side_effect = RuntimeError("target closed")
        self.assertEqual(service.list_groups().message, wa._MSG_READ_FAILED)
        service._driver.close.assert_called()
        service.close()

    def test_it_gets_the_long_budget_not_the_send_one(self):
        service = _service(self.path)
        service._run = MagicMock(return_value=WhatsAppCommandResult(True, ""))
        service.list_groups()
        self.assertEqual(service._run.call_args.kwargs["timeout_s"], wa._GROUP_LIST_TIMEOUT_S)
        self.assertGreater(wa._GROUP_LIST_TIMEOUT_S, service._wait_timeout_s)

    def test_the_keyboard_backend_cannot_list(self):
        with patch.object(WhatsAppService, "_probe_platform", return_value=True):
            service = WhatsAppService(
                config_for_tests(whatsapp={"enabled": True, "backend": "keyboard", "groups_path": str(self.path)})
            )
        self.assertEqual(service.list_groups().message, wa._MSG_READ_NEEDS_PLAYWRIGHT)

    def test_an_unreadable_cache_is_an_empty_list(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(_service(self.path).groups, [])

    def test_unread_is_told_the_cached_groups(self):
        self.path.write_text(json.dumps({"groups": [{"name": "the band", "muted": False}]}), encoding="utf-8")
        service = _service(self.path)
        service._driver.unread_chats.return_value = web.UnreadResult(web.READ_OK, [])
        service.unread()
        service.close()
        service._driver.unread_chats.assert_called_with(known_groups=["the band"])

    def test_the_fixture_never_points_at_the_real_cache(self):
        """list_groups() writes the file; a test on the real path would overwrite his list."""
        self.assertIn("nonexistent", config_for_tests()["whatsapp"]["groups_path"])


if __name__ == "__main__":
    unittest.main()
