"""
WhatsAppService: self-disable rules, VCF parsing, how certain a name match has
to be before it sends, and the confirmation token.

Nothing here may launch a browser or press a key. That is a harder guarantee
than the network guards elsewhere in this suite and it matters more: a leaked
`subprocess.Popen` opens a real WhatsApp Web tab, and a leaked `pyautogui.press`
puts an Enter into whatever window the person running the tests has focused.
NoKeyboardGuardTests pins it, and every test in the file goes through a fake
transport rather than the real one.

The contact book here is synthetic and written to a tmpdir. The real
`whatsapp.contacts_path` is a gitignored export of several hundred actual
people, so a test must never read it -- neither for its data nor for its names.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.config_fixture import config_for_tests

from services import whatsapp_service
from services.whatsapp_service import (
    _CERTAIN_SCORE as _CERTAIN_SCORE_FOR_TESTS,
    Contact,
    ContactMatch,
    WhatsAppCommandResult,
    WhatsAppService,
    load_vcf,
    looks_like_phone,
    normalize_phone,
    score_contacts,
)

# A card per case this suite cares about. Deliberately includes two people who
# share a first name, because that collision is the whole reason the confirm
# step exists.
VCF = """BEGIN:VCARD
VERSION:2.1
N:Zuka;Marta;;;
FN:Marta Zuka
TEL;CELL;PREF:+351933852719
END:VCARD
BEGIN:VCARD
VERSION:2.1
N:Silva;Ana;;;
FN:Ana Silva
TEL;CELL:912000111
END:VCARD
BEGIN:VCARD
VERSION:2.1
N:Costa;Ana;;;
FN:Ana Costa
TEL;CELL:912000222
END:VCARD
BEGIN:VCARD
VERSION:2.1
N:Reis;Joaquim;;;
FN:Joaquim Reis
TEL;HOME:213000111
TEL;CELL;PREF:914000333
END:VCARD
BEGIN:VCARD
VERSION:2.1
N:Saldo;WTF;;;
FN:WTF Saldo
TEL;CELL;PREF:111
END:VCARD
BEGIN:VCARD
VERSION:2.1
FN:Long Name Person
TEL;CELL;PREF:00351915
 000444
END:VCARD
"""


def _book(text: str = VCF) -> Path:
    path = Path(tempfile.mkdtemp()) / "contacts.vcf"
    path.write_text(text, encoding="utf-8")
    return path


def _service(book: Path | None = None, **overrides) -> WhatsAppService:
    """
    A service that believes it is on Windows, with a synthetic contact book.

    `contacts_path` is a path override, which config_fixture explicitly allows:
    the real one is a gitignored file of real phone numbers and the developer's
    CWD must never decide what a test asserts.
    """
    cfg = {"enabled": True, "contacts_path": str(book or _book())}
    cfg.update(overrides)
    config = config_for_tests(whatsapp=cfg)
    config["whatsapp"]["aliases"] = overrides.get("aliases", {})  # replace, don't merge
    with patch.object(WhatsAppService, "_probe_platform", return_value=True):
        return WhatsAppService(config)


class _FakeTransport:
    """Stands in for Firefox plus the keyboard. Records, never acts."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, phone, message):
        self.sent.append((phone, message))
        return WhatsAppCommandResult(True)


def _drive(service: WhatsAppService) -> _FakeTransport:
    """Replace the whole browser leg, so no test can reach a window or a key."""
    transport = _FakeTransport()
    service._send_now = transport
    service.wait_s = 0.0
    service._wait_timeout_s = 5.0
    return transport


class NoKeyboardGuardTests(unittest.TestCase):
    def test_this_file_never_opens_a_browser_or_presses_a_key(self):
        service = _service()
        transport = _drive(service)
        match = service.resolve_contact("+351933852719")
        with patch("services.whatsapp_service.subprocess.Popen", side_effect=AssertionError("browser")):
            with patch.dict(sys.modules, {"pyautogui": None}):
                self.assertTrue(service.send(match, "hello"))
        self.assertEqual(transport.sent, [("+351933852719", "hello")])


class SelfDisableTests(unittest.TestCase):
    # The platform checks below belong to the keyboard backend, so each of
    # those tests selects it explicitly: the shipped config runs playwright.
    def test_enabled_with_flag_and_platform(self):
        self.assertTrue(_service().enabled)

    def test_flag_off(self):
        self.assertFalse(_service(enabled=False).enabled)

    def test_missing_config_block_is_tolerated(self):
        with patch.object(WhatsAppService, "_probe_platform", return_value=True):
            self.assertFalse(WhatsAppService({}).enabled)

    def test_non_windows_disables(self):
        with patch.object(whatsapp_service.sys, "platform", "linux"):
            config = config_for_tests(
                whatsapp={"enabled": True, "backend": "keyboard", "contacts_path": str(_book())}
            )
            self.assertFalse(WhatsAppService(config).enabled)

    def test_missing_pyautogui_disables(self):
        with patch.object(whatsapp_service.sys, "platform", "win32"):
            with patch.dict(sys.modules, {"pyautogui": None}):
                config = config_for_tests(
                    whatsapp={"enabled": True, "backend": "keyboard", "contacts_path": str(_book())}
                )
                self.assertFalse(WhatsAppService(config).enabled)

    def test_missing_firefox_disables(self):
        with patch.object(whatsapp_service.sys, "platform", "win32"):
            with patch.dict(sys.modules, {"pyautogui": object()}):
                config = config_for_tests(
                    whatsapp={
                        "enabled": True,
                        "backend": "keyboard",
                        "contacts_path": str(_book()),
                        "firefox_path": "/nowhere/firefox.exe",
                    }
                )
                self.assertFalse(WhatsAppService(config).enabled)

    def test_missing_contact_book_does_not_disable_or_raise(self):
        """
        A missing book costs name lookup, not the tool: he can still say a
        number. It warns rather than disabling, which is why this asserts
        enabled is True and contacts is empty.
        """
        service = _service(contacts_path="/nowhere/contacts.vcf")
        self.assertTrue(service.enabled)
        self.assertEqual(service.contacts, [])

    def test_an_unparseable_book_does_not_raise(self):
        service = _service(_book("this is not a vcard at all"))
        self.assertTrue(service.enabled)
        self.assertEqual(service.contacts, [])

    def test_a_disabled_service_sends_nothing(self):
        service = _service(enabled=False)
        transport = _drive(service)
        result = service.send(ContactMatch(key="x", phone="+351911", certain=True), "hi")
        self.assertFalse(result)
        self.assertEqual(transport.sent, [])


class PhoneNormalizationTests(unittest.TestCase):
    def test_keeps_an_international_number(self):
        self.assertEqual(normalize_phone("+351 933 852 719"), "+351933852719")

    def test_double_zero_becomes_plus(self):
        self.assertEqual(normalize_phone("00351933852719"), "+351933852719")

    def test_bare_portuguese_mobile_gets_the_country_code(self):
        self.assertEqual(normalize_phone("933852719"), "+351933852719")

    def test_bare_portuguese_landline_gets_the_country_code(self):
        self.assertEqual(normalize_phone("213000111"), "+351213000111")

    def test_short_service_codes_are_rejected(self):
        """"111" and "12055" are in the real book and are not WhatsApp reachable."""
        self.assertIsNone(normalize_phone("111"))
        self.assertIsNone(normalize_phone("12055"))

    def test_a_tel_line_is_stripped_of_its_prefix(self):
        self.assertEqual(normalize_phone("TEL;CELL;PREF:+351933852719"), "+351933852719")

    def test_empty_is_none(self):
        self.assertIsNone(normalize_phone(""))

    def test_another_default_country_is_honoured(self):
        self.assertEqual(normalize_phone("933852719", default_cc="44"), "+44933852719")


class VcfParsingTests(unittest.TestCase):
    def setUp(self):
        self.contacts = load_vcf(_book())
        self.by_name = {c.name: c for c in self.contacts}

    def test_reads_every_usable_card(self):
        self.assertIn("Marta Zuka", self.by_name)
        self.assertIn("Ana Silva", self.by_name)
        self.assertIn("Joaquim Reis", self.by_name)

    def test_a_card_with_no_reachable_number_is_dropped(self):
        """WTF Saldo's only number is the service code 111."""
        self.assertNotIn("WTF Saldo", self.by_name)

    def test_preferred_mobile_outranks_home(self):
        """Joaquim's HOME line comes first in the card and must not win."""
        self.assertEqual(self.by_name["Joaquim Reis"].phone, "+351914000333")

    def test_both_of_his_numbers_are_kept(self):
        self.assertEqual(len(self.by_name["Joaquim Reis"].phones), 2)

    def test_soft_line_breaks_are_unfolded(self):
        """A number split across a continuation line is one number, not two."""
        self.assertEqual(self.by_name["Long Name Person"].phone, "+351915000444")


# Android's vCard 2.1 export, verbatim in shape: a name with an emoji or an
# accent is stored quoted-printable, and a long one wraps with a trailing '='
# and NO leading space on the next line.
QP_VCF = """BEGIN:VCARD
VERSION:2.1
N:Lopes;Rita;;;
FN:Rita Lopes
TEL;CELL;PREF:912000111
END:VCARD
BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:;=72=69=74=61=20=F0=9F=AB=B0=F0=9F=8F=BD;;;
FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=72=69=74=61=20=F0=9F=AB=B0=F0=9F=8F=BD
TEL;CELL:912000222
END:VCARD
BEGIN:VCARD
VERSION:2.1
FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=4A=6F=C3=A3=6F=20=43=6F=6E=63=65=69=C3=A7=C3=A3=
=6F
TEL;CELL:912000333
PHOTO;ENCODING=BASE64;JPEG:QUJD=
TEL;HOME:213000444
END:VCARD
BEGIN:VCARD
VERSION:2.1
FN:Clara Mae Rita
TEL;CELL:912000555
END:VCARD
"""


class QuotedPrintableTests(unittest.TestCase):
    """
    Live 2026-09-23: "message <first name>" went straight to one contact,
    because the other one with that first name was saved with an emoji,
    stored quoted-printable, and read as a run of =XX codes that could never
    match. 41 cards in the real book.
    """

    def setUp(self):
        self.by_name = {c.name: c for c in load_vcf(_book(QP_VCF))}

    def test_an_emoji_name_is_decoded(self):
        self.assertIn("rita \U0001FAF0\U0001F3FD", self.by_name)

    def test_an_accented_name_split_by_a_soft_break_is_decoded(self):
        self.assertIn("João Conceição", self.by_name)

    def test_a_base64_line_ending_in_equals_does_not_swallow_the_next_property(self):
        """PHOTO padding ends in '=' too; joining it would eat the HOME number."""
        self.assertEqual(len(self.by_name["João Conceição"].phones), 2)

    def test_no_name_is_left_as_raw_codes(self):
        self.assertFalse([n for n in self.by_name if "=" in n])

    def test_two_ritas_are_ambiguous_again(self):
        service = _service(_book(QP_VCF))
        match = service.resolve_contact("Rita")
        self.assertFalse(match)
        self.assertEqual(set(match.candidates), {"Rita Lopes", "rita \U0001FAF0\U0001F3FD"})

    def test_people_only_tagged_with_the_name_are_not_offered(self):
        """'Clara Mae Rita' is Rita's mum, 10 points behind; not a contender."""
        match = _service(_book(QP_VCF)).resolve_contact("Rita")
        self.assertNotIn("Clara Mae Rita", match.candidates)


class MatchCertaintyTests(unittest.TestCase):
    """
    Which matches are safe to send without asking. This is the one behaviour in
    the file whose failure mode is a message to a stranger.
    """

    def setUp(self):
        self.service = _service()

    def test_a_spoken_number_is_certain(self):
        match = self.service.resolve_contact("+351933852719")
        self.assertTrue(match.certain)
        self.assertEqual(match.phone, "+351933852719")

    def test_a_full_name_is_certain(self):
        match = self.service.resolve_contact("Marta Zuka")
        self.assertTrue(match.certain)
        self.assertEqual(match.key, "Marta Zuka")

    def test_a_first_name_is_certain_when_it_is_unique(self):
        match = self.service.resolve_contact("Marta")
        self.assertTrue(match.certain)
        self.assertEqual(match.key, "Marta Zuka")

    def test_a_shared_first_name_is_ambiguous_not_a_guess(self):
        match = self.service.resolve_contact("Ana")
        self.assertFalse(match.certain)
        self.assertEqual(match.phone, "")
        self.assertEqual(set(match.candidates), {"Ana Silva", "Ana Costa"})

    def test_a_mistranscription_is_not_certain(self):
        """'Martah' is what Whisper does to a name; it resolves, but it asks."""
        match = self.service.resolve_contact("Martah")
        self.assertEqual(match.key, "Marta Zuka")
        self.assertFalse(match.certain)

    def test_an_unknown_name_resolves_to_nothing(self):
        self.assertFalse(self.service.resolve_contact("Bartholomew"))

    def test_an_empty_hint_resolves_to_nothing(self):
        self.assertFalse(self.service.resolve_contact("   "))

    def test_a_substring_of_a_name_is_not_certain(self):
        """
        "oaquim" is inside "Joaquim Reis", which the shared name_matcher's
        substring tier would treat as a confident hit. Here it has to ask.
        """
        match = self.service.resolve_contact("oaquim")
        self.assertEqual(match.key, "Joaquim Reis")
        self.assertFalse(match.certain)

    def test_score_bands(self):
        """
        The bands _classify reads. 80 is the line: at or above it the send
        goes without asking, below it the recipient is read back.
        """
        contacts = self.service.contacts
        self.assertEqual(score_contacts("marta zuka", contacts)[0][0], 100.0)
        # A prefix of the full name, which "marta" is.
        self.assertEqual(score_contacts("marta", contacts)[0][0], 90.0)
        # An exact token that is not the first one: the surname alone.
        self.assertEqual(score_contacts("zuka", contacts)[0][0], 80.0)
        # A bare substring, which is where certainty stops.
        self.assertEqual(score_contacts("oaquim", contacts)[0][0], 60.0)

    def test_looks_like_phone(self):
        self.assertTrue(looks_like_phone("+351933852719"))
        self.assertTrue(looks_like_phone("933852719"))
        self.assertFalse(looks_like_phone("Marta"))
        self.assertFalse(looks_like_phone(""))


class AliasTests(unittest.TestCase):
    def test_an_alias_reaches_its_contact_and_is_certain(self):
        service = _service(aliases={"mum": "Marta Zuka"})
        match = service.resolve_contact("mum")
        self.assertTrue(match.certain)
        self.assertEqual(match.phone, "+351933852719")

    def test_an_alias_beats_the_book(self):
        """'ana' is ambiguous in the book; an alias settles it."""
        service = _service(aliases={"ana": "Ana Costa"})
        match = service.resolve_contact("ana")
        self.assertTrue(match.certain)
        self.assertEqual(match.key, "Ana Costa")

    def test_an_alias_pointing_at_a_number_works(self):
        service = _service(aliases={"the landlord": "+351999888777"})
        match = service.resolve_contact("the landlord")
        self.assertTrue(match.certain)
        self.assertEqual(match.phone, "+351999888777")

    def test_an_alias_pointing_at_nobody_falls_through_to_the_book(self):
        service = _service(aliases={"marta": "Someone Who Left"})
        match = service.resolve_contact("marta")
        self.assertEqual(match.key, "Marta Zuka")

    def test_a_blank_alias_is_dropped(self):
        service = _service(aliases={"mum": "", "dad": "Marta Zuka"})
        self.assertEqual(list(service.aliases), ["dad"])


class AliasFileTests(unittest.TestCase):
    """The real nicknames live in a gitignored file; config.yaml holds examples."""

    def _file(self, text: str) -> str:
        path = Path(tempfile.mkdtemp()) / "whatsapp_aliases.yaml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_file_is_merged_over_the_config_block(self):
        cfg = {"aliases": {"boss": "Ana Silva", "quim": "Ana Costa"},
               "aliases_path": self._file('quim: "Joaquim Reis"\n')}
        merged = whatsapp_service.load_alias_config(cfg)
        self.assertEqual(merged, {"boss": "Ana Silva", "quim": "Joaquim Reis"})

    def test_the_service_resolves_through_the_file(self):
        service = _service(aliases_path=self._file('quim: "Joaquim Reis"\n'))
        self.assertEqual(service.resolve_contact("quim").key, "Joaquim Reis")

    def test_a_missing_file_is_just_the_config_block(self):
        cfg = {"aliases": {"boss": "Ana Silva"}, "aliases_path": "/nowhere/aliases.yaml"}
        self.assertEqual(whatsapp_service.load_alias_config(cfg), {"boss": "Ana Silva"})

    def test_a_broken_file_is_ignored_not_raised(self):
        for text in ("quim: [unclosed\n", "- just\n- a list\n"):
            with self.subTest(text=text):
                cfg = {"aliases": {"boss": "Ana Silva"}, "aliases_path": self._file(text)}
                self.assertEqual(whatsapp_service.load_alias_config(cfg), {"boss": "Ana Silva"})


class AliasPreferTests(unittest.TestCase):
    """
    A card that ranks a foreign number first, messaged on the +351 one.
    `prefer` picks by prefix so the number itself stays out of config.yaml.
    Joaquim Reis in the fixture has a +351 HOME landline behind his CELL.
    """

    def test_prefer_picks_the_matching_number(self):
        service = _service(aliases={"quim": {"contact": "Joaquim Reis", "prefer": "+351213"}})
        self.assertEqual(service.resolve_contact("quim").phone, "+351213000111")

    def test_without_prefer_the_card_order_wins(self):
        service = _service(aliases={"quim": "Joaquim Reis"})
        self.assertEqual(service.resolve_contact("quim").phone, "+351914000333")

    def test_a_prefix_no_number_has_falls_back_instead_of_failing(self):
        service = _service(aliases={"quim": {"contact": "Joaquim Reis", "prefer": "+44"}})
        match = service.resolve_contact("quim")
        self.assertTrue(match.certain)
        self.assertEqual(match.phone, "+351914000333")

    def test_a_mapping_without_a_contact_is_skipped(self):
        service = _service(aliases={"ghost": {"prefer": "+351"}, "quim": "Joaquim Reis"})
        self.assertEqual(list(service.aliases), ["quim"])


class AliasOrderingTests(unittest.TestCase):
    """
    The real-world shape: plain "rita" is aliased to one Rita, and the book
    also holds "Rita Lopes" and "Clara Mae Rita" (someone tagged with the
    name). Before 2026-09-23 the alias went through match_name's substring
    tier FIRST, so both of those full names matched the `rita` alias and
    would have messaged the aliased Rita.
    """

    GF = "rita \U0001FAF0\U0001F3FD"

    def setUp(self):
        self.service = _service(
            _book(QP_VCF), aliases={"rita": self.GF, "my girlfriend": self.GF}
        )

    def test_the_exact_alias_wins_over_a_tie_in_the_book(self):
        match = self.service.resolve_contact("Rita")
        self.assertTrue(match.certain)
        self.assertEqual(match.key, self.GF)

    def test_a_full_contact_name_beats_a_loose_alias_match(self):
        self.assertEqual(self.service.resolve_contact("Rita Lopes").key, "Rita Lopes")

    def test_someone_merely_tagged_with_the_name_is_not_rerouted(self):
        match = self.service.resolve_contact("Clara Mae Rita")
        self.assertEqual(match.key, "Clara Mae Rita")

    def test_a_loose_phrase_still_reaches_the_alias(self):
        """Nothing in the book matches "the girlfriend", so the alias may."""
        self.assertEqual(self.service.resolve_contact("my girlfriend").key, self.GF)


class PrefixBoundaryTests(unittest.TestCase):
    def test_a_one_letter_contact_is_not_a_prefix_of_every_z_word(self):
        """Live: "Zuka" resolved, certain, to a contact named just "Z"."""
        contacts = load_vcf(_book(VCF)) + [Contact(name="Z", phones=["+351910000999"])]
        top_score, top = score_contacts("zuka", contacts)[0]
        self.assertEqual(top.name, "Marta Zuka")
        self.assertLess(top_score, 90.0)

    def test_a_partial_first_name_is_not_certain(self):
        contacts = load_vcf(_book(VCF))
        scored = [s for s, c in score_contacts("mar", contacts) if c.name == "Marta Zuka"]
        self.assertLess(scored[0], _CERTAIN_SCORE_FOR_TESTS)


class ConfirmationTests(unittest.TestCase):
    """
    The read-back cannot be short-circuited by the model.

    `confirm` is a flag in a tool call, so a model can set it whenever it
    likes. The guard is that the service also has to be holding a matching
    pending record, which only a previous read-back creates.
    """

    def setUp(self):
        self.service = _service()
        self.transport = _drive(self.service)
        self.match = self.service.resolve_contact("Martah")  # fuzzy on purpose
        self.assertFalse(self.match.certain)

    def test_a_certain_match_sends_with_no_confirmation(self):
        match = self.service.resolve_contact("Marta Zuka")
        self.assertTrue(self.service.send(match, "hello"))
        self.assertEqual(self.transport.sent, [("+351933852719", "hello")])

    def test_a_fuzzy_match_reads_back_instead_of_sending(self):
        result = self.service.send(self.match, "hello")
        self.assertFalse(result)
        self.assertIn("Marta Zuka", result.message)
        self.assertEqual(self.transport.sent, [])

    def test_confirm_on_a_first_attempt_still_reads_back(self):
        """The model setting the flag by itself must not be enough."""
        result = self.service.send(self.match, "hello", confirm=True)
        self.assertFalse(result)
        self.assertEqual(self.transport.sent, [])

    def test_confirm_after_a_read_back_sends(self):
        self.service.send(self.match, "hello")
        self.assertTrue(self.service.send(self.match, "hello", confirm=True))
        self.assertEqual(self.transport.sent, [("+351933852719", "hello")])

    def test_a_different_message_is_not_covered_by_the_confirmation(self):
        """
        He agreed to one message, not to the recipient forever. A changed body
        after the read-back has to be read back again.
        """
        self.service.send(self.match, "hello")
        result = self.service.send(self.match, "transfer the money", confirm=True)
        self.assertFalse(result)
        self.assertEqual(self.transport.sent, [])

    def test_a_different_recipient_is_not_covered_by_the_confirmation(self):
        self.service.send(self.match, "hello")
        other = self.service.resolve_contact("oaquim")
        self.assertFalse(self.service.send(other, "hello", confirm=True))
        self.assertEqual(self.transport.sent, [])

    def test_an_expired_confirmation_asks_again(self):
        self.service.confirm_timeout_s = 10.0
        self.service.send(self.match, "hello")
        with patch.object(
            whatsapp_service.time, "monotonic", return_value=time.monotonic() + 9999
        ):
            result = self.service.send(self.match, "hello", confirm=True)
        self.assertFalse(result)
        self.assertEqual(self.transport.sent, [])

    def test_a_confirmation_is_one_shot(self):
        """A second message to the same person is read back again."""
        self.service.send(self.match, "hello")
        self.assertTrue(self.service.send(self.match, "hello", confirm=True))
        self.assertFalse(self.service.send(self.match, "hello", confirm=True))
        self.assertEqual(len(self.transport.sent), 1)


class WorkerTests(unittest.TestCase):
    def test_a_second_send_is_refused_while_one_is_running(self):
        service = _service()
        _drive(service)
        started = __import__("threading").Event()
        release = __import__("threading").Event()

        def _slow(phone, message):
            started.set()
            release.wait(5)
            return WhatsAppCommandResult(True)

        service._send_now = _slow
        service._wait_timeout_s = 5.0
        match = service.resolve_contact("Marta Zuka")

        thread = __import__("threading").Thread(
            target=lambda: service.send(match, "one"), daemon=True
        )
        thread.start()
        started.wait(5)
        try:
            result = service.send(match, "two")
            self.assertFalse(result)
            self.assertEqual(result.message, whatsapp_service._MSG_BUSY)
        finally:
            release.set()
            thread.join(5)

    def test_the_worker_thread_is_a_daemon(self):
        """
        A non-daemon worker would hold the process open on Ctrl-C while it sat
        out a WhatsApp Web load -- the bug DeebotService hit with a pool.
        """
        service = _service()
        seen = []

        def _capture(phone, message):
            seen.append(__import__("threading").current_thread().daemon)
            return WhatsAppCommandResult(True)

        service._send_now = _capture
        service._wait_timeout_s = 5.0
        service.send(service.resolve_contact("Marta Zuka"), "hi")
        self.assertEqual(seen, [True])

    def test_a_timeout_reports_unreachable(self):
        service = _service()
        _drive(service)
        service._wait_timeout_s = 0.1

        def _hang(phone, message):
            time.sleep(3)
            return WhatsAppCommandResult(True)

        service._send_now = _hang
        result = service.send(service.resolve_contact("Marta Zuka"), "hi")
        self.assertFalse(result)
        self.assertEqual(result.message, whatsapp_service._MSG_UNREACHABLE)

    def test_a_raising_transport_never_reaches_the_orchestrator(self):
        service = _service()
        _drive(service)
        service._send_now = lambda *_: 1 / 0
        result = service.send(service.resolve_contact("Marta Zuka"), "hi")
        self.assertFalse(result)

    def test_close_stops_accepting_sends(self):
        service = _service()
        transport = _drive(service)
        service.close()
        self.assertFalse(service.send(service.resolve_contact("Marta Zuka"), "hi"))
        self.assertEqual(transport.sent, [])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.service = _service()
        self.transport = _drive(self.service)
        self.match = self.service.resolve_contact("Marta Zuka")

    def test_a_bad_time_is_refused(self):
        self.assertFalse(self.service.schedule(self.match, "hi", "half past nine"))
        self.assertFalse(self.service.schedule(self.match, "hi", "99:99"))

    def test_a_time_too_soon_is_refused(self):
        self.service.wait_s = 86400  # everything is "too soon" against a day
        result = self.service.schedule(self.match, "hi", "17:30")
        self.assertFalse(result)
        self.assertIn("too soon", result.message)

    def test_a_fuzzy_recipient_is_read_back_before_arming(self):
        fuzzy = self.service.resolve_contact("Martah")
        result = self.service.schedule(fuzzy, "hi", "17:30")
        self.assertFalse(result)
        self.assertEqual(self.service._timers, [])

    def test_arming_creates_a_daemon_timer(self):
        with patch.object(whatsapp_service, "_seconds_until", return_value=600):
            self.assertTrue(self.service.schedule(self.match, "hi", "17:30"))
        self.assertEqual(len(self.service._timers), 1)
        self.assertTrue(self.service._timers[0].daemon)
        self.service.close()

    def test_close_cancels_a_scheduled_send(self):
        """
        Left armed, the timer would wake after shutdown, raise a browser window
        and press Enter at a machine nobody is sitting at.
        """
        with patch.object(whatsapp_service, "_seconds_until", return_value=600):
            self.service.schedule(self.match, "hi", "17:30")
        timer = self.service._timers[0]
        self.service.close()
        self.assertEqual(self.service._timers, [])
        self.assertTrue(timer.finished.is_set())

    def test_a_scheduled_send_fires_through_the_transport(self):
        with patch.object(whatsapp_service, "_seconds_until", return_value=0.05):
            self.assertTrue(self.service.schedule(self.match, "later", "17:30"))
        time.sleep(0.5)
        self.assertEqual(self.transport.sent, [("+351933852719", "later")])

    def test_seconds_until_rolls_over_to_tomorrow(self):
        """A time already past today means tomorrow, not a negative delay."""
        for at in ("00:00", "23:59", "9:05"):
            delay = whatsapp_service._seconds_until(at)
            self.assertIsNotNone(delay)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 86400)

    def test_seconds_until_rejects_nonsense(self):
        for at in ("", "later", "25:00", "12:70", "noon"):
            self.assertIsNone(whatsapp_service._seconds_until(at))


class FindContactsTests(unittest.TestCase):
    def setUp(self):
        self.service = _service()

    def test_finds_by_first_name(self):
        found = self.service.find_contacts("Ana")
        self.assertEqual({name for name, _ in found}, {"Ana Silva", "Ana Costa"})

    def test_a_number_comes_back_as_itself(self):
        self.assertEqual(
            self.service.find_contacts("+351933852719"), [("+351933852719", "+351933852719")]
        )

    def test_nothing_for_an_unknown_name(self):
        self.assertEqual(self.service.find_contacts("Bartholomew"), [])


class ReloadTests(unittest.TestCase):
    def test_reload_picks_up_a_new_export(self):
        book = _book("")
        service = _service(book)
        self.assertEqual(service.contacts, [])
        book.write_text(VCF, encoding="utf-8")
        self.assertGreater(service.reload_contacts(), 0)
        self.assertTrue(service.resolve_contact("Marta Zuka"))


class ShippedConfigTests(unittest.TestCase):
    def test_the_shipped_config_has_a_whatsapp_block(self):
        cfg = config_for_tests()["whatsapp"]
        self.assertIn("enabled", cfg)
        self.assertIn("contacts_path", cfg)

    def test_the_shipped_contact_path_is_gitignored(self):
        """
        The book is hundreds of real phone numbers. If the configured path ever
        stops being ignored, it gets committed on the next `git add -A`.
        """
        path = config_for_tests()["whatsapp"]["contacts_path"]
        ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
        self.assertIn(path, ignored.split())


if __name__ == "__main__":
    unittest.main()
