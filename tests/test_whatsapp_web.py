"""
Tests for the Playwright backend: services/whatsapp_web.py and the parts of
WhatsAppService that exist for it.

Nothing here launches a browser. The driver is exercised against `_FakePage`,
which answers the same small slice of Playwright's Page/Locator API the driver
uses, keyed by SELECTORS entry rather than by real markup -- so these tests pin
the driver's *decisions* (when to press Enter, what counts as sent), and the
live selectors are checked separately against the real page.

`BrowserGuardTests` is the Playwright equivalent of the keyboard guard in
test_whatsapp_service.py: a unit test that launched a real browser would open
WhatsApp Web on whatever profile the developer has linked.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator import _dispatch_whatsapp  # noqa: E402
from services import whatsapp_web as web  # noqa: E402
from services.whatsapp_service import (  # noqa: E402
    _MSG_INVALID_NUMBER,
    _MSG_NEEDS_LINK,
    _MSG_UNCONFIRMED,
    _MSG_UNREACHABLE,
    WhatsAppService,
)
from tests.config_fixture import config_for_tests  # noqa: E402
from tests.test_whatsapp_service import _book  # noqa: E402

_KEY_BY_SELECTOR = {", ".join(v): k for k, v in web.SELECTORS.items()}


class _FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    first = property(lambda self: self)
    last = property(lambda self: self)

    def is_visible(self):
        return self.key in self.page.visible

    def inner_text(self):
        return self.page.compose_text

    def press(self, key):
        self.page.presses.append((self.key, key))
        self.page.on_enter()

    def filter(self, has_text=None):
        self.page.filtered_on.append(has_text)
        return self

    def count(self):
        return self.page.counts.get(self.key, 0)

    def locator(self, selector):
        return _FakeLocator(self.page, _KEY_BY_SELECTOR[selector])


class _FakePage:
    """
    A page in one of WhatsApp Web's states. `after_enter` describes what the
    real page would do when Enter lands: add a bubble, and maybe a tick.
    """

    url = web.WHATSAPP_URL

    def __init__(self, visible=(), compose_text="", bubble=True, tick=True):
        self.visible = set(visible)
        self.compose_text = compose_text
        self.counts = {"outgoing": 0, "tick_sent": 0}
        self._bubble, self._tick = bubble, tick
        self.gotos: list[str] = []
        self.presses: list[tuple[str, str]] = []
        self.filtered_on: list[str] = []

    def goto(self, url, **_):
        self.gotos.append(url)

    def locator(self, selector):
        return _FakeLocator(self, _KEY_BY_SELECTOR[selector])

    def on_enter(self):
        if self._bubble:
            self.counts["outgoing"] += 1
        if self._tick:
            self.counts["tick_sent"] += 1


def _driver(page: _FakePage, timeout_s: float = 0.5) -> web.WhatsAppWebDriver:
    driver = web.WhatsAppWebDriver(Path("unused"), channel=None, headless=True, timeout_s=timeout_s)
    driver.open_page = lambda: page
    return driver


class DriverSendTests(unittest.TestCase):
    def test_sent_needs_a_new_bubble_and_a_tick(self):
        page = _FakePage(visible={"compose"}, compose_text="On my way")
        outcome = _driver(page).send("+351910000001", "On my way")
        self.assertEqual(outcome.status, web.SENT)

    def test_exactly_one_enter_on_the_compose_box(self):
        """A second Enter lands on the record button; the keyboard backend learned that."""
        page = _FakePage(visible={"compose"}, compose_text="hi there")
        _driver(page).send("+351910000001", "hi there")
        self.assertEqual(page.presses, [("compose", "Enter")])

    def test_url_drops_the_plus_and_quotes_the_text(self):
        page = _FakePage(visible={"compose"}, compose_text="a & b")
        _driver(page).send("+351910000001", "a & b")
        self.assertEqual(
            page.gotos, [f"{web.WHATSAPP_URL}send?phone=351910000001&text=a%20%26%20b"]
        )

    def test_a_bubble_without_a_tick_is_unconfirmed_not_sent(self):
        page = _FakePage(visible={"compose"}, compose_text="hello", tick=False)
        self.assertEqual(_driver(page).send("+351910000001", "hello").status, web.UNCONFIRMED)

    def test_no_bubble_is_a_timeout(self):
        page = _FakePage(visible={"compose"}, compose_text="hello", bubble=False, tick=False)
        self.assertEqual(_driver(page).send("+351910000001", "hello").status, web.TIMEOUT)

    def test_qr_code_means_not_linked_and_nothing_is_pressed(self):
        page = _FakePage(visible={"qr"})
        self.assertEqual(_driver(page).send("+351910000001", "hello").status, web.NOT_LINKED)
        self.assertEqual(page.presses, [])

    def test_invalid_number_dialog(self):
        page = _FakePage(visible={"invalid_dialog"})
        self.assertEqual(_driver(page).send("+351900000000", "hello").status, web.INVALID_NUMBER)
        self.assertEqual(page.presses, [])

    def test_never_presses_enter_into_an_empty_box(self):
        """The text lands a beat after the box; Enter before it sends nothing."""
        page = _FakePage(visible={"compose"}, compose_text="")
        self.assertEqual(_driver(page).send("+351910000001", "hello").status, web.TIMEOUT)
        self.assertEqual(page.presses, [])

    def test_the_bubble_is_matched_on_the_first_line_only(self):
        page = _FakePage(visible={"compose"}, compose_text="line one")
        _driver(page).send("+351910000001", "line one\nline two")
        self.assertEqual(page.filtered_on, ["line one"])

    def test_the_bubble_is_matched_without_its_emoji(self):
        """
        WhatsApp renders emoji as <img>, so they are absent from the row's text.
        Live 2026-09-23: matching the literal body timed out on a message that
        had been delivered and read.
        """
        page = _FakePage(visible={"compose"}, compose_text="Test from California")
        outcome = _driver(page).send("+351910000001", "Test from California \U0001F334 (Playwright)")
        self.assertEqual(outcome.status, web.SENT)
        self.assertEqual(page.filtered_on, ["Test from California"])

    def test_snippet_takes_the_longest_emoji_free_run(self):
        self.assertEqual(web._snippet("\U0001F389 hi \U0001F334 happy birthday mate"), "happy birthday mate")
        self.assertEqual(web._snippet("on my way ❤️"), "on my way")
        self.assertEqual(web._snippet("\U0001F334\U0001F334"), "")

    def test_an_emoji_only_message_still_sends_and_confirms(self):
        page = _FakePage(visible={"compose"}, compose_text="\U0001F334")
        outcome = _driver(page).send("+351910000001", "\U0001F334")
        self.assertEqual(outcome.status, web.SENT)
        self.assertEqual(page.presses, [("compose", "Enter")])
        self.assertEqual(page.filtered_on, [])

    def test_message_rows_are_scoped_to_the_open_conversation(self):
        """The chat list items are role=row too and preview the last message."""
        self.assertTrue(all(s.startswith("#main ") for s in web.SELECTORS["outgoing"]))

    def test_invalid_number_is_seen_with_the_chat_list_still_on_screen(self):
        page = _FakePage(visible={"linked", "invalid_dialog"})
        self.assertEqual(_driver(page).send("+351210000000", "hello").status, web.INVALID_NUMBER)

    def test_nothing_rendered_is_a_timeout(self):
        page = _FakePage()
        self.assertEqual(_driver(page).send("+351910000001", "hello").status, web.TIMEOUT)


def _pw_service(**overrides) -> WhatsAppService:
    """A Playwright-backend service whose driver is a MagicMock. Launches nothing."""
    cfg = {"enabled": True, "backend": "playwright", "contacts_path": str(_book())}
    cfg.update(overrides)
    with patch.object(WhatsAppService, "_probe_platform", return_value=True):
        service = WhatsAppService(config_for_tests(whatsapp=cfg))
    service._driver = MagicMock()
    service._driver.is_open = False
    service._wait_timeout_s = 5.0
    return service


class ServiceOutcomeTests(unittest.TestCase):
    def _send(self, status):
        service = _pw_service()
        service._driver.send.return_value = web.SendOutcome(status)
        match = service.resolve_contact("+351910000001")
        return service.send(match, "hello")

    def test_sent_is_success(self):
        self.assertTrue(self._send(web.SENT))

    def test_each_failure_names_its_own_fix(self):
        for status, line in (
            (web.NOT_LINKED, _MSG_NEEDS_LINK),
            (web.INVALID_NUMBER, _MSG_INVALID_NUMBER),
            (web.UNCONFIRMED, _MSG_UNCONFIRMED),
            (web.TIMEOUT, _MSG_UNREACHABLE),
        ):
            with self.subTest(status=status):
                result = self._send(status)
                self.assertFalse(result)
                self.assertEqual(result.message, line)

    def test_a_crashed_browser_is_closed_so_the_next_send_relaunches(self):
        service = _pw_service()
        service._driver.send.side_effect = RuntimeError("target closed")
        result = service.send(service.resolve_contact("+351910000001"), "hello")
        self.assertFalse(result)
        service._driver.close.assert_called_once()

    def test_unconfirmed_is_never_reported_as_sent(self):
        """The dispatcher says "Sent to X" on any truthy result."""
        self.assertFalse(self._send(web.UNCONFIRMED))


class WorkerThreadTests(unittest.TestCase):
    def test_every_send_runs_on_the_same_thread(self):
        """Playwright's sync API is not thread-safe: one thread owns the browser."""
        service = _pw_service()
        seen = []

        def _send(phone, message):
            seen.append(threading.get_ident())
            return web.SendOutcome(web.SENT)

        service._driver.send.side_effect = _send
        match = service.resolve_contact("+351910000001")
        for body in ("one", "two", "three"):
            self.assertTrue(service.send(match, body))
        self.assertEqual(len(set(seen)), 1)
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_close_shuts_the_browser_on_the_worker_thread(self):
        service = _pw_service()
        service._driver.send.return_value = web.SendOutcome(web.SENT)
        closed_on = []
        service._driver.close.side_effect = lambda: closed_on.append(threading.get_ident())
        service.send(service.resolve_contact("+351910000001"), "hello")
        worker = service._worker
        service.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(closed_on, [worker.ident])

    def test_an_idle_browser_is_closed(self):
        service = _pw_service()
        service.idle_close_s = 0.05
        service._driver.send.return_value = web.SendOutcome(web.SENT)
        service._driver.is_open = True
        service.send(service.resolve_contact("+351910000001"), "hello")
        deadline = time.monotonic() + 2
        while not service._driver.close.called and time.monotonic() < deadline:
            time.sleep(0.02)
        service._driver.close.assert_called()
        service.close()


class StartTests(unittest.TestCase):
    def test_start_warms_on_the_worker(self):
        service = _pw_service()
        service._driver.warm.return_value = "linked"
        service.start()
        deadline = time.monotonic() + 2
        while not service._driver.warm.called and time.monotonic() < deadline:
            time.sleep(0.02)
        service._driver.warm.assert_called_once()
        service.close()

    def test_start_does_nothing_with_keep_warm_off(self):
        service = _pw_service(keep_warm=False)
        service.start()
        self.assertIsNone(service._worker)

    def test_start_does_nothing_on_the_keyboard_backend(self):
        with patch.object(WhatsAppService, "_probe_platform", return_value=True):
            service = WhatsAppService(
                config_for_tests(
                    whatsapp={"enabled": True, "backend": "keyboard", "contacts_path": str(_book())}
                )
            )
        service.start()
        self.assertIsNone(service._worker)

    def test_an_unknown_backend_falls_back_to_keyboard(self):
        with patch.object(WhatsAppService, "_probe_platform", return_value=True):
            service = WhatsAppService(
                config_for_tests(
                    whatsapp={"enabled": True, "backend": "carrier pigeon", "contacts_path": str(_book())}
                )
            )
        self.assertEqual(service.backend, "keyboard")


class BrowserGuardTests(unittest.TestCase):
    def test_constructing_the_shipped_service_launches_nothing(self):
        """
        The shipped config selects playwright with keep_warm on. Construction
        alone must never reach sync_playwright -- only start() or a send may.
        """
        with patch(
            "playwright.sync_api.sync_playwright", side_effect=AssertionError("browser launched")
        ):
            with patch.object(WhatsAppService, "_probe_platform", return_value=True):
                service = WhatsAppService(
                    config_for_tests(whatsapp={"enabled": True, "contacts_path": str(_book())})
                )
            service.resolve_contact("+351910000001")
        self.assertEqual(service.backend, "playwright")
        self.assertIsNone(service._worker)


class DispatchLineTests(unittest.TestCase):
    def test_playwright_backend_does_not_claim_the_keyboard(self):
        svc = MagicMock()
        svc.enabled = True
        svc.backend = "playwright"
        svc.resolve_contact.return_value = MagicMock(certain=True, candidates=[], key="Me")
        svc.send.return_value = MagicMock(__bool__=lambda self: True)
        spoken = []
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "+351910000001", "message": "hi"},
            svc,
            say_now=spoken.append,
        )
        self.assertEqual(spoken, ["Sending it on WhatsApp."])


if __name__ == "__main__":
    unittest.main()
