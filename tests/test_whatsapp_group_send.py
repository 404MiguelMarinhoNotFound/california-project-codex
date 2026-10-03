"""
Sending to a WhatsApp group: the forgiving matcher, the driver's search-and-
open path, the always-read-back service rule, group-only dispatch, and the
one-unresolved-send-per-turn guard.

The case all of this exists for, seen live 2026-10-02: Master Miguel asked for
the group "🦩‍🔥☭flamingus unanounymous☭🦩‍🔥", Whisper and the model turned it
into two sends to "Flamingist" and "Anonymous group", and both searched the
contact book and missed. Nothing here launches a browser.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator import Orchestrator, _dispatch_whatsapp  # noqa: E402
from services import whatsapp_groups as wg  # noqa: E402
from services import whatsapp_service as wa  # noqa: E402
from services import whatsapp_web as web  # noqa: E402
from services.whatsapp_service import WhatsAppCommandResult, WhatsAppService  # noqa: E402
from tests.config_fixture import config_for_tests  # noqa: E402
from tests.test_whatsapp_groups import _group, _service  # noqa: E402


def wa_config(**whatsapp):
    return config_for_tests(whatsapp={"enabled": True, **whatsapp})

# Shaped like the real list (2026-10-02): emoji, symbols, a deliberate
# misspelling, near-duplicate titles, and the word "group" inside titles.
FLAMINGUS = "🦩‍🔥☭flamingus unanounymous☭🦩‍🔥"
REAL_SHAPED = [
    FLAMINGUS,
    "Fms☘️",
    "Âncora da Marina 🍾🍹🥂",
    "Pottery group",
    "BA hiking Group",
    "Anos Rita 🥳",
    "Anos Rita Vinhas 🍷",
    "Porto trip",
    "Porto road trip",
    "Coffee O’clock",
    "BASQUETE!!",
    "Chão de Sintra 2k25",
]


class GroupMatcherTests(unittest.TestCase):
    def test_emoji_symbols_accents_and_filler_are_dropped(self):
        self.assertEqual(wg.words(FLAMINGUS), ["flamingus", "unanounymous"])
        self.assertEqual(wg.words("the Pottery group"), ["pottery"])
        self.assertEqual(wg.words("Chão de Sintra"), ["chao", "sintra"])

    def test_a_title_that_is_only_filler_keeps_its_words(self):
        self.assertEqual(wg.words("Group"), ["group"])

    def test_the_sound_key_hears_through_the_misspelling(self):
        self.assertEqual(wg.sound_key("anonymous"), wg.sound_key("unanounymous"))

    def test_the_live_mishearings_find_the_group(self):
        """Whisper's actual output on 2026-10-02, and what he might say instead."""
        for spoken in ("Flamingist", "Anonymous group", "Flamingist anonymous", "flamingo anonymous group"):
            self.assertEqual(wg.match_group(spoken, REAL_SHAPED).title, FLAMINGUS, spoken)

    def test_an_exact_name_wins_over_a_longer_title_that_contains_it(self):
        self.assertEqual(wg.match_group("anos rita", REAL_SHAPED).title, "Anos Rita 🥳")

    def test_near_ties_ask_which_rather_than_pick(self):
        found = wg.match_group("anos rita wine", REAL_SHAPED)
        self.assertFalse(found)
        self.assertEqual(set(found.candidates), {"Anos Rita Vinhas 🍷", "Anos Rita 🥳"})
        self.assertEqual(
            set(wg.match_group("porto", REAL_SHAPED).candidates), {"Porto trip", "Porto road trip"}
        )

    def test_noise_finds_nothing(self):
        for spoken in ("xyz nonsense", "banana", "hello"):
            found = wg.match_group(spoken, REAL_SHAPED)
            self.assertFalse(found, spoken)
            self.assertEqual(found.candidates, [], spoken)

    def test_two_letters_are_never_fuzzed(self):
        self.assertEqual(wg._word_similarity("dv", "da"), 0.0)

    def test_a_shared_title_is_flagged(self):
        self.assertTrue(wg.match_group("pottery", ["Pottery group", "Pottery group"]).shared)

    def test_speakable_title_drops_what_cannot_be_said(self):
        self.assertEqual(wg.speakable_title(FLAMINGUS), "flamingus unanounymous")
        self.assertEqual(wg.speakable_title("Anos Rita 🥳"), "Anos Rita")
        self.assertEqual(wg.speakable_title("🥳"), "🥳")


# ---------------------------------------------------------- driver: sending

_KEY_BY_SELECTOR = {", ".join(v): k for k, v in web.SELECTORS.items()}


class _Loc:
    def __init__(self, page, key):
        self.page, self.key = page, key

    first = property(lambda self: self)
    last = property(lambda self: self)

    def is_visible(self):
        if self.key in ("filter_all", "filter_groups", "search"):
            return True
        if self.key == "compose":
            return self.page.open_chat is not None
        return False

    def evaluate(self, script):
        if self.key.startswith("filter_"):
            self.page.active = self.key

    def get_attribute(self, name):
        if name == "aria-selected":
            return "true" if self.page.active == self.key else "false"
        return None

    def inner_text(self):
        if self.key == "chat_title":
            # The real header renders emoji as images: they are not in its text.
            shown = self.page.header_override or self.page.open_chat or ""
            return "".join(ch for ch in shown if ord(ch) < 0x2000)
        return self.page.compose_text if self.key == "compose" else ""

    def nth(self, index):
        self.index = index
        return self

    def click(self, timeout=None):
        title = self.page._visible_rows()[self.index]
        self.page.clicked.append(title)
        self.page.open_chat = title

    def fill(self, text):
        self.page.searches.append(text)

    def press(self, key):
        self.page.presses.append((self.key, key))
        self.page.counts["outgoing"] += 1
        self.page.counts["tick_sent"] += 1

    def filter(self, has_text=None):
        return self

    def count(self):
        return self.page.counts.get(self.key, 0)

    def locator(self, selector):
        return _Loc(self.page, _KEY_BY_SELECTOR[selector])


class _Handle:
    """A JSHandle that is an element (or null), and clicks the row it points at."""

    def __init__(self, page, index):
        self.page, self.index = page, index

    def as_element(self):
        return None if self.index is None else self

    def click(self, timeout=None):
        clicked = self.page._visible_rows()[self.index]
        self.page.clicked.append(clicked)
        self.page.open_chat = self.page.wrong_chat or clicked
        self.page.wrong_chat = None  # only the first click goes astray


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def insert_text(self, text):
        self.page.typed.append(text)
        self.page.compose_text = text


class _GroupPage:
    """WhatsApp Web with a Groups chip, a search box, and chats that open on click."""

    url = web.WHATSAPP_URL

    def __init__(self, titles, header_override=None, wrong_chat=None):
        self.titles = list(titles)
        self.wrong_chat = wrong_chat
        self.active = "filter_all"
        self.searches: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.presses: list = []
        self.open_chat = None
        self.header_override = header_override
        self.compose_text = ""
        self.counts = {"outgoing": 0, "tick_sent": 0}
        self.keyboard = _Keyboard(self)

    def locator(self, selector):
        return _Loc(self, _KEY_BY_SELECTOR[selector])

    def _visible_rows(self):
        query = (self.searches[-1] if self.searches else "").casefold()
        return [t for t in self.titles if query in t.casefold()]

    def evaluate(self, script, args=None):
        if script is web._READ_ROWS_JS:
            return [_group(i, t) for i, t in enumerate(self._visible_rows())]
        raise AssertionError("unexpected script")

    def evaluate_handle(self, script, args=None):
        assert script is web._FIND_ROW_JS
        hits = [i for i, t in enumerate(self._visible_rows()) if t == args[1]]
        first = len(args) > 3 and args[3]
        return _Handle(self, hits[0] if len(hits) == 1 or (first and hits) else None)
        raise AssertionError("unexpected script")


def _send_group(page, title, message="hey, California here"):
    driver = web.WhatsAppWebDriver(Path("unused"), channel=None, headless=True, timeout_s=2.0)
    driver._page = page
    driver.warm = lambda: "linked"
    with patch.object(web.time, "sleep"), patch.object(web, "_SEARCH_WAIT_S", 0.2), \
            patch.object(web, "_OPEN_WAIT_S", 0.2):
        return driver.send_group(title, message)


class DriverSendGroupTests(unittest.TestCase):
    def test_searches_the_speakable_part_opens_the_exact_title_and_sends_once(self):
        page = _GroupPage(REAL_SHAPED)
        self.assertEqual(_send_group(page, FLAMINGUS).status, web.SENT)
        self.assertIn("flamingus unanounymous", page.searches)
        self.assertEqual(page.clicked, [FLAMINGUS])
        self.assertEqual(page.presses, [("compose", "Enter")])

    def test_a_missing_title_clicks_and_types_nothing(self):
        page = _GroupPage(["Pottery group"])
        self.assertEqual(_send_group(page, "Renamed group").status, web.GROUP_NOT_FOUND)
        self.assertEqual((page.clicked, page.typed, page.presses), ([], [], []))

    def test_two_groups_with_the_title_are_refused_before_any_click(self):
        page = _GroupPage(["book club", "book club"])
        self.assertEqual(_send_group(page, "book club").status, web.GROUP_AMBIGUOUS)
        self.assertEqual((page.clicked, page.typed), ([], []))

    def test_the_wrong_chat_opening_types_nothing(self):
        page = _GroupPage(["Pottery group"], header_override="Somebody else")
        self.assertEqual(_send_group(page, "Pottery group").status, web.WRONG_CHAT)
        self.assertEqual((page.typed, page.presses), ([], []))

    def test_a_wrong_chat_once_is_searched_again_and_then_sent(self):
        """Nothing is typed into the wrong chat, so one retry is safe."""
        page = _GroupPage(["Pottery group", "Somebody"], wrong_chat="Somebody")
        self.assertEqual(_send_group(page, "Pottery group").status, web.SENT)
        self.assertEqual(page.clicked, ["Pottery group", "Pottery group"])
        self.assertEqual(len(page.typed), 1)

    def test_no_fixed_settle_sleep_on_the_chips(self):
        page = _GroupPage(["Pottery group"])
        driver = web.WhatsAppWebDriver(Path("unused"), channel=None, headless=True, timeout_s=0.5)
        driver._page = page
        driver.warm = lambda: "linked"
        with patch.object(web.time, "sleep") as sleep:
            driver.send_group("Pottery group", "hi")
        self.assertNotIn(0.5, [c.args[0] for c in sleep.call_args_list])

    def test_an_older_copy_of_the_text_loading_in_is_not_the_new_bubble(self):
        """Live 2026-10-02: history rendered after the count, 0 -> 2, read as sent."""
        page = _GroupPage(["Pottery group"])
        reads = iter([0, 1])  # the first read beats the history; the old copy lands next

        def count(loc):
            if loc.key == "outgoing":
                return next(reads, 1)  # ...and Enter never adds a bubble of its own
            return page.counts.get(loc.key, 0)

        with patch.object(_Loc, "count", count):
            outcome = _send_group(page, "Pottery group")
        self.assertNotEqual(outcome.status, web.SENT)

    def test_a_newline_never_reaches_the_compose_box(self):
        page = _GroupPage(["Pottery group"])
        _send_group(page, "Pottery group", "line one\nline two")
        self.assertEqual(page.typed, ["line one line two"])

    def test_the_search_is_cleared_and_the_list_put_back_on_all(self):
        page = _GroupPage(["Pottery group"])
        _send_group(page, "Pottery group")
        self.assertEqual(page.searches[-1], "")
        self.assertEqual(page.active, "filter_all")


# ---------------------------------------------------- service: group sends

class ServiceSendGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _svc(self, groups=REAL_SHAPED):
        path = Path(self.tmp.name) / "groups.json"
        path.write_text(
            json.dumps({"groups": [{"name": g, "muted": False} for g in groups]}), encoding="utf-8"
        )
        service = _service(path)
        service._driver.send_group.return_value = web.SendOutcome(web.SENT)
        self.addCleanup(service.close)
        return service

    def test_even_an_exact_group_is_read_back_first(self):
        service = self._svc()
        result = service.send_group(service.resolve_group("pottery"), "hi")
        self.assertFalse(result)
        self.assertIn("Pottery group", result.message)
        service._driver.send_group.assert_not_called()

    def test_confirm_on_a_first_attempt_still_reads_back(self):
        service = self._svc()
        self.assertFalse(service.send_group(service.resolve_group("pottery"), "hi", confirm=True))
        service._driver.send_group.assert_not_called()

    def test_a_confirmed_read_back_sends_to_the_exact_title(self):
        service = self._svc()
        found = service.resolve_group("Flamingist anonymous")
        service.send_group(found, "hey")
        self.assertTrue(service.send_group(found, "hey", confirm=True))
        service._driver.send_group.assert_called_once_with(FLAMINGUS, "hey")

    def test_the_confirmation_covers_only_that_message(self):
        service = self._svc()
        found = service.resolve_group("pottery")
        service.send_group(found, "hey")
        self.assertFalse(service.send_group(found, "something else", confirm=True))
        service._driver.send_group.assert_not_called()

    def test_a_shared_title_is_refused(self):
        service = self._svc(groups=["book club", "book club"])
        result = service.send_group(service.resolve_group("book club"), "hey")
        self.assertIn("can't tell them apart", result.message)
        service._driver.send_group.assert_not_called()

    def test_the_read_back_speaks_no_emoji(self):
        service = self._svc()
        result = service.send_group(service.resolve_group("flamingist"), "hey")
        self.assertIn("flamingus unanounymous", result.message)
        self.assertNotIn("☭", result.message)

    def test_each_failure_has_its_own_line(self):
        cases = {
            web.GROUP_NOT_FOUND: "renamed",
            web.GROUP_AMBIGUOUS: "can't tell them apart",
            web.WRONG_CHAT: "didn't send anything",
            web.UNCONFIRMED: "hasn't gone out yet",
        }
        for status, phrase in cases.items():
            service = self._svc()
            service._driver.send_group.return_value = web.SendOutcome(status)
            found = service.resolve_group("pottery")
            service.send_group(found, "hey")
            self.assertIn(phrase, service.send_group(found, "hey", confirm=True).message, status)

    def test_a_list_read_moments_ago_is_not_re_read_on_a_miss(self):
        service = self._svc()
        self.assertTrue(service.can_refresh_groups())  # the fixture cache has no timestamp
        service._groups_refreshed_at = wa.time.time()
        self.assertFalse(service.can_refresh_groups())

    def test_boot_re_reads_the_list_even_a_fresh_one(self):
        """Bootup is one of the two refreshes, like the Stremio library sync."""
        service = self._svc()
        service._driver.warm.return_value = "linked"
        service._groups_refreshed_at = wa.time.time()
        service._driver.list_groups.return_value = web.GroupListResult(
            web.READ_OK, [web.GroupChat("New one")]
        )
        service.start()
        service.close()
        service._worker.join(timeout=5)
        self.assertEqual(service.group_names(), ["New one"])

    def test_an_unlinked_boot_does_not_try_to_list(self):
        service = self._svc()
        service._driver.warm.return_value = "needs_link"
        service.start()
        service.close()
        service._worker.join(timeout=5)
        service._driver.list_groups.assert_not_called()

    def test_a_send_during_a_background_refresh_waits_instead_of_being_refused(self):
        import threading

        service = self._svc()
        release = threading.Event()

        def slow_list():
            release.wait(5)
            return web.GroupListResult(web.READ_OK, [web.GroupChat("Pottery group")])

        service._driver.list_groups.side_effect = slow_list
        future = service.refresh_groups_in_background()
        found = service.resolve_group("pottery")
        service.send_group(found, "hey")
        threading.Timer(0.2, release.set).start()
        result = service.send_group(found, "hey", confirm=True)
        self.assertTrue(result, result.message)
        self.assertEqual(future.result(timeout=5).status, web.READ_OK)

    def test_no_background_refresh_without_a_browser(self):
        with patch.object(WhatsAppService, "_probe_platform", return_value=True):
            keyboard = WhatsAppService(
                wa_config(backend="keyboard", groups_path=str(Path(self.tmp.name) / "g.json"))
            )
        self.assertIsNone(keyboard.refresh_groups_in_background())
        closed = self._svc()
        closed.close()
        self.assertIsNone(closed.refresh_groups_in_background())

    def test_the_interval_comes_from_config_and_defaults_to_daily(self):
        self.assertEqual(self._svc().groups_refresh_interval_minutes, 1440)


class GroupRefreshLoopTests(unittest.TestCase):
    def test_the_loop_refreshes_each_interval_and_stops_with_the_stremio_loop(self):
        import threading

        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = MagicMock()
        orch._background_stop = threading.Event()
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            return len(waits) > 2  # two passes, then stop

        orch._background_stop.wait = fake_wait
        orch._whatsapp_groups_loop(1440)
        self.assertEqual(waits[0], 1440 * 60)
        self.assertEqual(orch.whatsapp_service.refresh_groups_in_background.call_count, 2)

    def test_a_raising_refresh_does_not_end_the_loop(self):
        import threading

        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = MagicMock()
        orch.whatsapp_service.refresh_groups_in_background.side_effect = RuntimeError("boom")
        orch._background_stop = threading.Event()
        calls = iter([False, False, True])
        orch._background_stop.wait = lambda seconds: next(calls)
        orch._whatsapp_groups_loop(1)
        self.assertEqual(orch.whatsapp_service.refresh_groups_in_background.call_count, 2)


# -------------------------------------------------------- dispatch: groups

TATTOO = wg.GroupMatch(title="Pottery group", score=1.0)
_READ_BACK = WhatsAppCommandResult(False, "That's the group Pottery group. Say send it and I will.")


def _disp_svc(found, refreshed=None, can_refresh=False):
    svc = MagicMock()
    svc.enabled = True
    svc.backend = "playwright"
    svc.resolve_group.side_effect = [found, refreshed if refreshed is not None else found]
    svc.can_refresh_groups.return_value = can_refresh
    svc.send_group.return_value = _READ_BACK
    svc.resolve_contact.side_effect = AssertionError("a group request must never search people")
    svc.send.side_effect = AssertionError("a group request must never message a person")
    return svc


def _ask(svc, **params):
    params.setdefault("action", "whatsapp_send")
    params.setdefault("message", "hey")
    spoken = []
    return _dispatch_whatsapp(params, svc, say_now=spoken.append), spoken


class DispatchGroupTests(unittest.TestCase):
    def test_saying_group_searches_groups_only(self):
        svc = _disp_svc(TATTOO)
        line, _ = _ask(svc, to="the pottery group")
        self.assertIn("Say send it", line)
        svc.send_group.assert_called_once()

    def test_the_group_flag_alone_is_enough(self):
        svc = _disp_svc(TATTOO)
        _ask(svc, to="pottery", group=True)
        svc.send_group.assert_called_once()

    def test_portuguese_grupo_counts(self):
        svc = _disp_svc(TATTOO)
        _ask(svc, to="grupo pottery")
        svc.send_group.assert_called_once()

    def test_a_miss_re_reads_the_list_once_and_says_so(self):
        svc = _disp_svc(wg.GroupMatch(), refreshed=TATTOO, can_refresh=True)
        line, spoken = _ask(svc, to="pottery group")
        svc.list_groups.assert_called_once()
        self.assertEqual(spoken, ["Let me check your WhatsApp groups."])
        self.assertIn("Say send it", line)

    def test_a_miss_against_a_fresh_list_is_just_a_miss(self):
        svc = _disp_svc(wg.GroupMatch(), can_refresh=False)
        line, _ = _ask(svc, to="nonsense group")
        svc.list_groups.assert_not_called()
        self.assertEqual(line, "I can't find a WhatsApp group called nonsense group.")
        svc.send_group.assert_not_called()

    def test_a_bare_mock_never_triggers_a_refresh(self):
        """can_refresh_groups is compared with `is True`: a Mock attribute is truthy."""
        svc = _disp_svc(wg.GroupMatch())
        svc.can_refresh_groups.return_value = MagicMock()
        _ask(svc, to="nonsense group")
        svc.list_groups.assert_not_called()

    def test_near_ties_ask_which_and_send_nothing(self):
        svc = _disp_svc(wg.GroupMatch(candidates=["Anos Rita 🥳", "Anos Rita Vinhas 🍷"]))
        line, _ = _ask(svc, to="anos rita group")
        self.assertEqual(line, "A few groups sound like that: Anos Rita or Anos Rita Vinhas. Which one?")
        svc.send_group.assert_not_called()

    def test_a_scheduled_group_send_is_refused(self):
        svc = _disp_svc(TATTOO)
        line, _ = _ask(svc, to="pottery group", at="18:00")
        self.assertIn("not groups", line)
        svc.send_group.assert_not_called()

    def test_a_confirmed_send_is_announced_and_reported(self):
        svc = _disp_svc(TATTOO)
        svc.send_group.return_value = WhatsAppCommandResult(True)
        line, spoken = _ask(svc, to="pottery group", confirm=True)
        self.assertEqual(spoken, ["Sending it to the group."])
        self.assertEqual(line, "Sent to the group Pottery group.")

    def test_a_person_without_the_word_group_is_still_a_person(self):
        svc = MagicMock()
        svc.enabled = True
        svc.resolve_group.side_effect = AssertionError("no group search for a person")
        _dispatch_whatsapp({"action": "whatsapp_find_contact", "to": "Marta"}, svc)
        svc.resolve_contact.assert_called_once()

    def test_unread_from_a_group_looks_at_groups_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real = _service(Path(tmp.name) / "none.json")
        svc = MagicMock()
        svc.enabled = True
        svc.unread.return_value = web.UnreadResult(web.READ_OK, [
            web.UnreadChat("Pottery", 1, "a person called Pottery", is_group=False),
            web.UnreadChat("Pottery group", 2, "ink tonight?", is_group=True),
        ])
        svc.find_unread_chat.side_effect = real.find_unread_chat
        line = _dispatch_whatsapp({"action": "whatsapp_unread", "to": "pottery group"}, svc)
        self.assertIn("ink tonight?", line)
        self.assertNotIn("a person called", line)


class OneUnresolvedSendPerTurnTests(unittest.TestCase):
    """The live failure: two guesses at one group name fired as two sends."""

    def _orch(self, lines):
        orch = Orchestrator.__new__(Orchestrator)
        orch.whatsapp_service = MagicMock()
        orch._say_now = lambda text: None
        self.dispatched = []

        def fake(params, svc, say_now=None):
            self.dispatched.append(params["to"])
            return lines.pop(0)

        patcher = patch("core.orchestrator._dispatch_whatsapp", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return orch

    def _send(self, orch, to):
        return orch._dispatch_tool(
            "control_whatsapp", {"action": "whatsapp_send", "to": to, "message": "hi"}
        )

    def test_a_second_guess_in_the_same_turn_is_refused(self):
        orch = self._orch(["I can't find a WhatsApp group called Flamingist."])
        orch._activation_seq = 1
        self._send(orch, "Flamingist")
        self.assertIn("Not sent", self._send(orch, "Anonymous group"))
        self.assertEqual(self.dispatched, ["Flamingist"])

    def test_the_next_turn_may_send(self):
        orch = self._orch(["That's the group X. Say send it and I will.", "Sent to the group X."])
        orch._activation_seq = 1
        self._send(orch, "x group")
        orch._activation_seq = 2
        self.assertEqual(self._send(orch, "x group"), "Sent to the group X.")

    def test_two_real_recipients_in_one_turn_both_go(self):
        orch = self._orch(["Sent to Mum.", "Sent to Dad."])
        orch._activation_seq = 1
        self._send(orch, "mum")
        self.assertEqual(self._send(orch, "dad"), "Sent to Dad.")

    def test_lookups_are_never_blocked(self):
        orch = self._orch(["I can't find a WhatsApp group called X.", "You're in the group Y."])
        orch._activation_seq = 1
        self._send(orch, "x group")
        line = orch._dispatch_tool("control_whatsapp", {"action": "whatsapp_find_contact", "to": "y group"})
        self.assertEqual(line, "You're in the group Y.")


if __name__ == "__main__":
    unittest.main()
