"""
PhoneService: the read-back, number rules, one call at a time, and the report.

Every test injects a fake phone transport and a fake agent, so nothing here
can reach the phone over adb, open an audio socket or reach Gemini.
NoRealPhoneTests pins that.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from services.gemini_live_call import CallLine, CallResult
from services.phone_prompts import CallBrief
from services.phone_service import PhoneService
from services.whatsapp_service import ContactMatch
from tests.config_fixture import config_for_tests


class FakeDialer:
    def __init__(self, dial_status="DIALED"):
        self.dial_status = dial_status
        self.dialed = []
        self.hung_up = 0
        self.connected = False

    def dial(self, number):
        self.dialed.append(number)
        self.connected = self.dial_status == "DIALED"
        return self.dial_status

    def in_call(self):
        return self.connected

    def hang_up(self):
        self.hung_up += 1
        self.connected = False
        return "ENDED"

    def audio(self, record_path=""):
        self.audio_made = FakeAudio(record_path, failed=getattr(self, "audio_failed", False))
        return self.audio_made


class FakeAudio:
    """What AdbPhone.audio() hands PhoneService: the call's two faces + the answer signal."""

    def __init__(self, record_path, failed=False):
        self.record_path = record_path
        self.failed = failed
        self.armed = False

    def arm(self):
        self.armed = True

    def capture(self):
        return "capture-face"

    def speaker(self):
        return "speaker-face"

    def answered(self):
        return True


class FakeAgent:
    def __init__(self, result=None, raise_exc=None):
        self.result = result or CallResult(
            lines=[CallLine("them", "Estou?"), CallLine("california", "Olá, aqui é a California.")],
            outcome={"status": "booked", "details": "Friday 20:00, 4", "summary": "Booked for Friday at 8."},
            ended_by="end_call",
            duration_s=42.0,
        )
        self.raise_exc = raise_exc
        self.prompts = []

    def run(self, prompt, max_call_s, no_answer_s, still_connected=None, stop=None, dial=None, mute=None,
            answered=None):
        self.prompts.append(prompt)
        self.answered = answered
        self.still_connected = still_connected
        if self.raise_exc:
            raise self.raise_exc
        if dial is not None and not dial():
            return CallResult(ended_by="dial_failed")
        return self.result


def _brief(**kw):
    base = dict(to="Taberna da Praia", kind="book_table", goal="book Friday 20:00 for 4")
    base.update(kw)
    return CallBrief(**base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "calls.jsonl")
        self.dialer = FakeDialer()
        self.agent = FakeAgent()

    def _svc(self, resolve=None, include_transcripts=True, **phone):
        cfg = config_for_tests(
            # paths: never the real logs/calls.jsonl
            phone={"enabled": True, "log_path": self.log_path, **phone},
            logging={"include_transcripts": include_transcripts},
        )
        return PhoneService(cfg, resolve_contact=resolve, dialer=self.dialer, agent_factory=lambda: self.agent)

    def _place(self, svc, brief=None, number="+351912345678"):
        brief = brief or _brief()
        first = svc.call(brief, number=number)
        second = svc.call(brief, number=number, confirm=True)
        if svc._thread is not None:
            svc._thread.join(timeout=5)
        return first, second


class BusyTests(_Base):
    def test_busy_reads_the_active_call(self):
        svc = self._svc()
        self.assertIs(svc.busy(), False)
        svc._active = {"label": "Taberna da Praia", "number": "+351912345678", "since": 0.0}
        self.assertIs(svc.busy(), True)


class ReadBackTests(_Base):
    def test_first_call_reads_back_and_dials_nothing(self):
        svc = self._svc()
        result = svc.call(_brief(), number="912345678")
        self.assertFalse(result)
        self.assertIn("Ready to call Taberna da Praia at 912 345 678", result.message)
        self.assertEqual(self.dialer.dialed, [])
        self.assertEqual(self.agent.prompts, [])
        self.assertIs(result.awaiting, True)   # the live UI shows it waiting for him

    def test_confirm_on_a_first_attempt_still_reads_back(self):
        svc = self._svc()
        result = svc.call(_brief(), number="912345678", confirm=True)
        self.assertFalse(result)
        self.assertIn("Ready to call", result.message)
        self.assertEqual(self.dialer.dialed, [])

    def test_confirmed_call_goes_out_and_reports(self):
        svc = self._svc()
        first, second = self._place(svc)
        self.assertFalse(first)
        self.assertTrue(second)
        self.assertIs(second.awaiting, False)
        self.assertIn("Calling Taberna da Praia now", second.message)
        self.assertEqual(self.dialer.dialed, ["+351912345678"])
        report = svc.pop_report()
        self.assertIsNotNone(report)
        self.assertEqual(report.status, "booked")
        self.assertIsNone(svc.pop_report())

    def test_a_changed_brief_is_read_back_again(self):
        svc = self._svc()
        svc.call(_brief(), number="912345678")
        again = svc.call(_brief(goal="book Saturday instead"), number="912345678", confirm=True)
        self.assertFalse(again)
        self.assertIn("Ready to call", again.message)
        self.assertEqual(self.dialer.dialed, [])

    def test_the_confirmation_is_one_shot(self):
        svc = self._svc()
        self._place(svc)
        repeat = svc.call(_brief(), number="912345678", confirm=True)
        self.assertFalse(repeat)
        self.assertEqual(len(self.dialer.dialed), 1)

    def test_an_expired_confirmation_asks_again(self):
        svc = self._svc()
        svc.call(_brief(), number="912345678")
        with mock.patch("services.phone_service.time.monotonic", return_value=10**9):
            late = svc.call(_brief(), number="912345678", confirm=True)
        self.assertFalse(late)
        self.assertEqual(self.dialer.dialed, [])


class NumberTests(_Base):
    def test_contact_name_resolves_through_the_book(self):
        resolve = mock.Mock(return_value=ContactMatch(key="marta 🫰🏽", phone="+351961000000", certain=True))
        svc = self._svc(resolve=resolve)
        result = svc.call(CallBrief(to="marta", kind="deliver_message", goal="say I'm late"))
        resolve.assert_called_once_with("marta")
        self.assertIn("Ready to call marta 🫰🏽 at 961 000 000", result.message)

    def test_two_close_contacts_ask_which(self):
        resolve = mock.Mock(return_value=ContactMatch(candidates=["Ana Costa", "Ana Lopes"]))
        result = self._svc(resolve=resolve).call(CallBrief(to="ana"))
        self.assertEqual(result.message, "Which ana: Ana Costa or Ana Lopes?")

    def test_an_unknown_name_says_search_a_business_but_ask_for_a_person(self):
        resolve = mock.Mock(return_value=ContactMatch())
        result = self._svc(resolve=resolve).call(CallBrief(to="Taberna X"))
        self.assertFalse(result)
        self.assertIn("Taberna X is not in his contacts", result.message)
        self.assertIn("web search", result.message)
        self.assertIn("never search for a private person's number", result.message)

    def test_a_searched_number_is_used_when_the_name_is_no_contact(self):
        resolve = mock.Mock(return_value=ContactMatch())
        result = self._svc(resolve=resolve).call(_brief(), number="+351 912 345 678")
        self.assertIn("Ready to call Taberna da Praia at 912 345 678 (the number I found)", result.message)

    def test_a_certain_contact_beats_a_number_the_model_passed(self):
        # A number Claude passes for a person may be made up; his phone's book is not.
        resolve = mock.Mock(return_value=ContactMatch(key="marta 🫰🏽", phone="+351961000000", certain=True))
        result = self._svc(resolve=resolve).call(
            CallBrief(to="marta", kind="personal", goal="dinner Friday"), number="+351 912 345 678"
        )
        self.assertIn("at 961 000 000 (from your contacts)", result.message)

    def test_a_passed_number_beats_a_loose_or_tied_contact_match(self):
        # A restaurant name brushing a contact's surname must not redirect the call.
        for match in (
            ContactMatch(key="Rui Praia", phone="+351961000000", certain=False),
            ContactMatch(candidates=["Rui Praia", "Ana Praia"]),
        ):
            with self.subTest(match=match):
                result = self._svc(resolve=mock.Mock(return_value=match)).call(
                    _brief(), number="+351 912 345 678"
                )
                self.assertIn("at 912 345 678 (the number I found)", result.message)

    def test_a_loose_contact_match_is_read_back_by_its_saved_name(self):
        resolve = mock.Mock(return_value=ContactMatch(key="Mariana Carvalho", phone="+351961000000"))
        result = self._svc(resolve=resolve).call(CallBrief(to="mariah", kind="personal"))
        self.assertIn("Ready to call Mariana Carvalho at 961 000 000 (from your contacts)", result.message)

    def test_digits_in_to_skip_the_book(self):
        resolve = mock.Mock()
        result = self._svc(resolve=resolve).call(CallBrief(to="912 345 678", goal="ask the hours"))
        resolve.assert_not_called()
        self.assertIn("(the number you said)", result.message)

    def test_short_and_emergency_numbers_are_refused(self):
        svc = self._svc()
        for number in ("112", "115", "12055"):
            with self.subTest(number=number):
                self.assertEqual(svc.call(_brief(), number=number).message, "I won't call that number myself.")

    def test_premium_rate_and_foreign_numbers_are_refused(self):
        svc = self._svc()
        for number in ("760100200", "+34612345678"):
            with self.subTest(number=number):
                result = svc.call(_brief(), number=number)
                self.assertFalse(result)
                self.assertEqual(result.message, "I won't call that number myself.")


class ReportFieldsTests(_Base):
    def test_the_report_knows_how_the_call_was_placed(self):
        resolve = mock.Mock(return_value=ContactMatch(key="Sérgio Nokia", phone="+351969685405", certain=True))
        svc = self._svc(resolve=resolve)
        self._place(svc, brief=CallBrief(to="Sérgio", kind="personal", goal="chat"), number="")
        report = svc.pop_report()
        self.assertEqual(report.number_source, "from your contacts")
        self.assertEqual((report.dial_status, report.hang_up_status), ("DIALED", "ENDED"))
        self.assertRegex(report.call_id, r"^\d{8}-\d{6}$")
        self.assertTrue(report.ended_at >= report.started_at)
        self.assertEqual(report.backend, svc.backend)

    def test_the_log_line_carries_the_metadata(self):
        svc = self._svc()
        self._place(svc)
        with open(self.log_path, encoding="utf-8") as handle:
            record = json.loads(handle.readlines()[-1])
        self.assertEqual(record["meta"]["dial_status"], "DIALED")
        self.assertEqual(record["meta"]["ended_how"], "California said goodbye and hung up")


class OutcomeAfterTheCallTests(_Base):
    """The live agent no longer reports an outcome; it is read from the transcript."""

    def _talked(self):
        return FakeAgent(CallResult(lines=[CallLine("them", "Estou?"), CallLine("california", "Olá!")],
                                    ended_by="end_call"))

    def test_the_transcript_is_read_when_the_agent_reported_nothing(self):
        self.agent = self._talked()
        svc = self._svc()
        svc._summarize = mock.Mock(return_value={"status": "booked", "details": "Fri", "summary": "Booked."})
        self._place(svc)
        report = svc.pop_report()
        self.assertEqual(report.status, "booked")
        brief, transcript = svc._summarize.call_args.args
        self.assertIn("Them: Estou?", transcript)

    def test_a_failing_reader_leaves_the_outcome_unknown(self):
        self.agent = self._talked()
        svc = self._svc()
        svc._summarize = mock.Mock(side_effect=RuntimeError("down"))
        self._place(svc)
        self.assertEqual(svc.pop_report().status, "unknown")

    def test_tests_with_an_injected_agent_never_reach_claude(self):
        self.agent = self._talked()
        svc = self._svc()
        with mock.patch("services.call_outcome.summarize_call", side_effect=AssertionError("network")):
            self._place(svc)
        self.assertEqual(svc.pop_report().status, "unknown")


class CallLifecycleTests(_Base):
    def test_one_call_at_a_time(self):
        svc = self._svc()
        svc._active = {"label": "x", "number": "+351900000000", "since": 0}
        self.assertEqual(svc.call(_brief(), number="912345678").message,
                         "I'm already on a call. Let me finish that one first.")

    def test_a_failed_dial_reports_without_hanging_up(self):
        self.dialer = FakeDialer(dial_status="NOT_PLACED")
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertEqual(report.status, "dial_failed")
        self.assertEqual(self.dialer.hung_up, 0)

    def test_each_dial_failure_gets_its_own_reason(self):
        """Each needs a different fix, so none is collapsed into "couldn't call"."""
        reasons = {
            "NO_PHONE": "Wireless debugging",
            "LOCKED_DOWN": "USB debugging (Security settings)",
            "NO_INTERCEPTION": "call audio",
            "HELPER_MISMATCH": "isn't the one I installed",
            "NOT_PLACED": "never started",
            "BUSY": "already on a call",
        }
        for status, words in reasons.items():
            with self.subTest(status=status):
                self.dialer = FakeDialer(dial_status=status)
                svc = self._svc()
                self._place(svc)
                report = svc.pop_report()
                self.assertEqual(report.status, "dial_failed")
                self.assertIn(words, report.result.error)

    def test_a_placed_call_is_hung_up_afterwards(self):
        svc = self._svc()
        self._place(svc)
        self.assertEqual(self.dialer.hung_up, 1)
        self.assertEqual(svc.pop_report().result.error, "")

    def test_a_call_the_phone_cannot_confirm_ended_says_so(self):
        self.dialer.hang_up = lambda: ""
        svc = self._svc()
        self._place(svc)
        self.assertIn("may still be connected", svc.pop_report().result.error)

    def test_a_call_read_as_hung_up_is_still_closed_and_not_flagged(self):
        self.agent = FakeAgent(CallResult(lines=[CallLine("them", "Estou?")], ended_by="hung_up"))
        self.dialer.hang_up = mock.Mock(return_value="NO_CALL")
        svc = self._svc()
        self._place(svc)
        self.dialer.hang_up.assert_called_once()
        self.assertEqual(svc.pop_report().result.error, "")

    def test_the_agent_gets_the_answered_signal_from_the_phone_audio(self):
        svc = self._svc()
        self._place(svc)
        self.assertEqual(self.agent.answered, self.dialer.audio_made.answered)

    def test_the_bridge_is_armed_only_by_a_placed_call(self):
        svc = self._svc()
        self._place(svc)
        self.assertTrue(self.dialer.audio_made.armed)
        self.dialer = FakeDialer(dial_status="BUSY")
        svc = self._svc()
        self._place(svc)
        self.assertFalse(self.dialer.audio_made.armed)

    def test_a_failed_bridge_ends_the_call_and_says_why(self):
        self.dialer.audio_failed = True
        svc = self._svc()
        self._place(svc)
        self.assertFalse(self.agent.still_connected())
        self.assertIn("call audio", svc.pop_report().result.error)

    def test_the_real_agent_is_wired_to_the_phone_audio(self):
        svc = self._svc()
        svc._agent_factory = None
        audio = FakeAudio("")
        with mock.patch("services.gemini_live_call.GeminiLiveAgent") as agent_cls:
            svc._make_agent(audio)
        kwargs = agent_cls.call_args.kwargs
        self.assertEqual(kwargs["capture_factory"](), "capture-face")
        self.assertEqual(kwargs["speaker_factory"](), "speaker-face")
        self.assertNotIn("cable_device", kwargs)
        # The phone's call state is authoritative: no "they spoke recently" grace.
        self.assertEqual(kwargs["live_if_heard_s"], 0)

    def test_a_dialer_that_fails_to_build_still_reports_and_frees_the_line(self):
        svc = self._svc()
        with mock.patch.object(svc, "_get_dialer", side_effect=ImportError("no phone_adb")):
            self._place(svc)
        report = svc.pop_report()
        self.assertEqual((report.result.ended_by, report.status), ("error", "failed"))
        self.assertIsNone(svc._active)

    def test_a_failure_before_anyone_spoke_is_failed_not_no_answer(self):
        self.agent = FakeAgent(raise_exc=RuntimeError("401"))
        svc = self._svc()
        self._place(svc)
        self.assertEqual(svc.pop_report().status, "failed")

    def test_agent_crash_still_reports_and_frees_the_line(self):
        self.agent = FakeAgent(raise_exc=RuntimeError("socket closed"))
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertIsNotNone(report)
        self.assertEqual(report.result.ended_by, "error")
        self.assertIn("socket closed", report.result.error)
        self.assertIsNone(svc._active)

    def test_status_line_before_during_after(self):
        svc = self._svc()
        self.assertIn("No call right now", svc.status_line())
        svc._active = {"label": "Taberna", "number": "+351", "since": 0}
        with mock.patch("services.phone_service.time.monotonic", return_value=75):
            self.assertEqual(svc.status_line(), "On the phone with Taberna, 1m15s in.")
        svc._active = None
        self._place(svc)
        self.assertIn("Booked for Friday at 8.", svc.status_line())

    def test_the_prompt_reaches_the_agent(self):
        svc = self._svc()
        self._place(svc)
        self.assertIn("book Friday 20:00 for 4", self.agent.prompts[0])
        self.assertIn("Never pretend to be a human", self.agent.prompts[0])


class CallLogTests(_Base):
    def _record(self):
        with open(self.log_path, encoding="utf-8") as handle:
            return json.loads(handle.read().strip())

    def test_log_line_has_outcome_and_both_sides(self):
        self._place(self._svc())
        record = self._record()
        self.assertEqual(record["status"], "booked")
        self.assertEqual(record["number"], "+351912345678")
        self.assertEqual([t["who"] for t in record["transcript"]], ["them", "california"])

    def test_transcripts_off_keeps_only_the_outcome(self):
        self._place(self._svc(include_transcripts=False))
        record = self._record()
        self.assertNotIn("transcript", record)
        self.assertEqual(record["status"], "booked")


class SetupTests(unittest.TestCase):
    def test_disabled_flag(self):
        svc = PhoneService(config_for_tests(phone={"enabled": False}))
        self.assertFalse(svc.enabled)
        self.assertEqual(svc.call(_brief(), number="912345678").message, "Phone calls aren't set up right now.")

    def test_missing_key_disables_without_raising(self):
        no_creds = {"GEMINI_API_KEY": "", "VERTEX_API_KEY": "", "GOOGLE_APPLICATION_CREDENTIALS": ""}
        # The laptop has a real gcloud login; it must not count here.
        with mock.patch.dict(os.environ, no_creds), \
                mock.patch("services.phone_service._user_adc_path", return_value=""), \
                mock.patch("services.phone_service.sys.platform", "win32"):
            svc = PhoneService(config_for_tests(phone={"enabled": True}))
        self.assertFalse(svc.enabled)

    def test_no_adb_disables(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
                mock.patch("services.phone_service.shutil.which", return_value=None):
            svc = PhoneService(config_for_tests(
                phone={"enabled": True, "backend": "ai_studio", "adb": {"path": "Z:/nowhere/adb.exe"}}))
        self.assertFalse(svc.enabled)


class BackendCredentialTests(unittest.TestCase):
    """Which Gemini endpoint a call uses, decided from config and .env."""

    def _kwargs(self, env, adc_path="", **phone):
        clean = {"GEMINI_API_KEY": "", "VERTEX_API_KEY": "", "GOOGLE_APPLICATION_CREDENTIALS": "",
                 "GOOGLE_CLOUD_PROJECT": ""}
        clean.update(env)
        # A real gcloud login on the machine running the tests must not decide them.
        with mock.patch.dict(os.environ, clean),                 mock.patch("services.phone_service._user_adc_path", return_value=adc_path):
            svc = PhoneService(config_for_tests(phone={"enabled": False, **phone}))
            return svc._client_kwargs(), svc

    def test_vertex_with_the_gcloud_user_login(self):
        """No key file needed: new orgs block them (iam.disableServiceAccountKeyCreation)."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            adc = handle.name
        self.addCleanup(os.remove, adc)
        (kwargs, _), _ = self._kwargs({}, adc_path=adc, backend="vertex", vertex={"project": "my-proj"})
        self.assertEqual(kwargs, {"vertexai": True, "project": "my-proj", "location": "europe-west1"})

    def test_vertex_with_a_service_account_file(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            path = handle.name
        self.addCleanup(os.remove, path)
        (kwargs, why), svc = self._kwargs({"GOOGLE_APPLICATION_CREDENTIALS": path},
                                          backend="vertex", vertex={"project": "my-proj", "location": "europe-west4"})
        self.assertEqual(kwargs, {"vertexai": True, "project": "my-proj", "location": "europe-west4"})
        self.assertEqual(svc.model, "gemini-live-2.5-flash-native-audio")

    def test_vertex_service_account_needs_a_project(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            path = handle.name
        self.addCleanup(os.remove, path)
        (kwargs, why), _ = self._kwargs({"GOOGLE_APPLICATION_CREDENTIALS": path}, backend="vertex",
                                        vertex={"project": ""})
        self.assertIsNone(kwargs)
        self.assertIn("phone.vertex.project", why)

    def test_vertex_missing_credentials_file_is_named(self):
        (kwargs, why), _ = self._kwargs({"GOOGLE_APPLICATION_CREDENTIALS": "C:/nope/sa.json"},
                                        backend="vertex", vertex={"project": "p"})
        self.assertIsNone(kwargs)
        self.assertIn("missing file", why)

    def test_vertex_express_key(self):
        (kwargs, _), _ = self._kwargs({"VERTEX_API_KEY": "vk"}, backend="vertex")
        self.assertEqual(kwargs, {"vertexai": True, "api_key": "vk"})

    def test_vertex_with_nothing_explains_both_routes(self):
        (kwargs, why), _ = self._kwargs({}, backend="vertex")
        self.assertIsNone(kwargs)
        self.assertIn("gcloud auth application-default login", why)
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", why)
        self.assertIn("VERTEX_API_KEY", why)

    def test_ai_studio_uses_the_gemini_key_and_its_own_model(self):
        (kwargs, _), svc = self._kwargs({"GEMINI_API_KEY": "gk"}, backend="ai_studio",
                                        model="gemini-2.5-flash-native-audio-latest")
        self.assertEqual(kwargs, {"api_key": "gk"})
        self.assertEqual(svc.model, "gemini-2.5-flash-native-audio-latest")

    def test_unknown_backend(self):
        (kwargs, why), _ = self._kwargs({"GEMINI_API_KEY": "gk"}, backend="openai")
        self.assertIsNone(kwargs)
        self.assertIn("unknown phone.backend", why)


class FindPhoneTests(_Base):
    """phone_find: ring the phone over adb, never during a call."""

    def _find(self, status, **kw):
        self.dialer.play_sound = mock.Mock(return_value=status)
        return self._svc(**kw).find_phone()

    def test_playing_is_success_and_needs_no_read_back(self):
        result = self._find("PLAYING")
        self.assertTrue(result)
        self.assertIn("ringing", result.message)
        self.dialer.play_sound.assert_called_once_with()
        self.assertEqual(self.dialer.dialed, [])

    def test_ring_again_while_it_rings_is_success(self):
        result = self._find("ALREADY_PLAYING")
        self.assertTrue(result)
        self.assertIn("already", result.message)

    def test_each_failure_has_its_own_line(self):
        lines = set()
        for status in ("NO_PHONE", "LOCKED_DOWN", "RING_FAILED"):
            with self.subTest(status=status):
                result = self._find(status)
                self.assertFalse(result)
                lines.add(result.message)
        self.assertEqual(len(lines), 3)

    def test_find_phone_no_phone_says_wireless_debugging(self):
        self.assertIn("Wireless debugging", self._find("NO_PHONE").message)

    def test_an_unknown_answer_is_a_failure_not_a_success(self):
        result = self._find("")
        self.assertFalse(result)
        self.assertIn("couldn't ring", result.message)

    def test_a_dialer_that_raises_is_a_failure(self):
        self.dialer.play_sound = mock.Mock(side_effect=OSError("boom"))
        self.assertFalse(self._svc().find_phone())

    def test_not_while_on_a_call(self):
        svc = self._svc()
        self.dialer.play_sound = mock.Mock(return_value="PLAYING")
        svc._active = {"label": "X", "number": "+351912345678", "since": 0.0}
        result = svc.find_phone()
        self.assertFalse(result)
        self.assertIn("already on a call", result.message)
        self.dialer.play_sound.assert_not_called()

    def test_disabled_service_presses_nothing(self):
        svc = self._svc()
        svc.enabled = False
        self.dialer.play_sound = mock.Mock(return_value="PLAYING")
        self.assertFalse(svc.find_phone())
        self.dialer.play_sound.assert_not_called()


_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
_CALL_ROWS = (
    "Row: 0 number=+351910000001, date=1791900000000, new=1, name=Rita\n"
    "Row: 1 number=+351910000002, date=1791890000000, new=0, name=NULL\n"
)


def _notification_dump() -> str:
    with open(os.path.join(_FIXTURES, "phone_notification_dump.txt"), encoding="utf-8") as handle:
        return handle.read()


class DigestTests(_Base):
    """phone_missed / phone_digest: read-only, and an unreadable phone is never "nothing new"."""

    def setUp(self):
        super().setUp()
        self.dialer.reachable = mock.Mock(return_value="OK")
        self.dialer.missed_calls_raw = mock.Mock(return_value=_CALL_ROWS)
        self.dialer.notifications_raw = mock.Mock(return_value=_notification_dump())

    def test_missed_calls_reads_the_configured_window(self):
        now = 1791910000.0
        with mock.patch("services.phone_service.time.time", return_value=now):
            result = self._svc().missed_calls()
        self.assertTrue(result)
        self.dialer.missed_calls_raw.assert_called_once_with(int((now - 24 * 3600) * 1000))
        self.assertIn("2 missed calls", result.message)
        self.assertIn("Rita", result.message)

    def test_unreachable_phone_is_not_nothing_new(self):
        self.dialer.reachable.return_value = "NO_PHONE"
        for method in ("missed_calls", "digest"):
            with self.subTest(method=method):
                result = getattr(self._svc(), method)()
                self.assertIs(result.success, False)
                self.assertEqual(result.message, (
                    "I can't reach his phone over Wi-Fi, so I can't see what he missed. "
                    "Wireless debugging may be off on it."
                ))

    def test_a_failed_raw_read_is_not_nothing_new(self):
        didnt_answer = "His phone didn't answer just now, so I can't say what he missed."
        cases = (
            ("missed_calls", "missed_calls_raw", ()),
            ("digest", "missed_calls_raw", ()),
            ("digest", "notifications_raw", ()),
            ("digest", "notifications_raw", ("WhatsApp",)),
        )
        for method, raw, args in cases:
            with self.subTest(method=method, raw=raw, args=args):
                self.setUp()
                getattr(self.dialer, raw).return_value = None
                result = getattr(self._svc(), method)(*args)
                self.assertIs(result.success, False)
                self.assertEqual(result.message, didnt_answer)

    def test_a_read_that_raises_is_the_didnt_answer_line(self):
        self.dialer.notifications_raw.side_effect = OSError("boom")
        with self.assertLogs("services.phone_service", level="ERROR"):
            result = self._svc().digest()
        self.assertIs(result.success, False)
        self.assertIn("didn't answer", result.message)

    def test_nothing_new_is_still_success(self):
        self.dialer.missed_calls_raw.return_value = "No result found.\n"
        self.dialer.notifications_raw.return_value = "  Notification List:\n"
        result = self._svc().digest()
        self.assertIs(result.success, True)
        self.assertIn("Nothing new", result.message)

    def test_ignored_packages_are_dropped(self):
        dump = _notification_dump().replace("pkg=com.google.android.gm", "pkg=com.miui.securitycenter")
        self.dialer.notifications_raw.return_value = dump
        message = self._svc().digest().message
        self.assertNotIn("Booking.com", message)
        self.assertNotIn("Securitycenter", message)
        self.assertIn("WhatsApp", message)

    def test_digest_names_apps_and_senders_but_no_text(self):
        message = self._svc().digest().message
        self.assertIn("WhatsApp", message)
        self.assertIn("Gmail", message)
        self.assertNotIn("Já cheguei", message)

    def test_digest_with_app_gives_texts(self):
        result = self._svc().digest("WhatsApp")
        self.assertTrue(result)
        self.assertIn("Já cheguei", result.message)
        self.dialer.missed_calls_raw.assert_not_called()

    def test_digest_respects_the_configured_caps(self):
        message = self._svc(digest={"max_apps": 1}).digest().message
        self.assertIn("and 1 more app", message)

    def test_disabled_service(self):
        svc = self._svc()
        svc.enabled = False
        self.assertEqual(svc.missed_calls().message, "Phone calls aren't set up right now.")
        self.assertEqual(svc.digest().message, "Phone calls aren't set up right now.")
        self.dialer.reachable.assert_not_called()

    def test_reads_allowed_during_a_call(self):
        svc = self._svc()
        svc._active = {"label": "X", "number": "+351912345678", "since": 0.0}
        result = svc.digest()
        self.assertTrue(result)
        self.dialer.notifications_raw.assert_called_once_with()

    def test_shipped_config_has_the_digest_block(self):
        svc = self._svc()
        self.assertEqual(svc.digest_window_hours, 24)
        self.assertIn("com.miui.securitycenter", svc.digest_ignore_packages)
        self.assertEqual(svc.digest_app_names.get("com.whatsapp"), "WhatsApp")


class NoRealPhoneTests(unittest.TestCase):
    """Constructing the shipped service must never reach the phone."""

    def test_construction_never_runs_adb(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("shelled out")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("shelled out")), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}):
            PhoneService(config_for_tests(phone={"enabled": True, "backend": "ai_studio"}))










if __name__ == "__main__":
    unittest.main()
