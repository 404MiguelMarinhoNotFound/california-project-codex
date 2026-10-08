"""
PhoneService: the read-back, number rules, one call at a time, and the report.

Every test injects a fake dialer, a fake agent and a no-op mic route, so
nothing here can dial, switch the laptop's microphone, open an audio device or
reach Gemini. NoRealPhoneTests pins that.
"""

import contextlib
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

    def run(self, prompt, max_call_s, no_answer_s, still_connected=None, stop=None, dial=None, mute=None):
        self.prompts.append(prompt)
        self.mute = mute
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
        self.mic_routes = []

    def _svc(self, resolve=None, include_transcripts=True, **phone):
        cfg = config_for_tests(
            # paths: never the real logs/calls.jsonl
            phone={"enabled": True, "log_path": self.log_path, **phone},
            logging={"include_transcripts": include_transcripts},
        )

        def route(what):
            @contextlib.contextmanager
            def cm():
                self.mic_routes.append("enter" if what == "mic" else "speaker enter")
                try:
                    yield
                finally:
                    self.mic_routes.append("exit" if what == "mic" else "speaker exit")
            return cm

        return PhoneService(
            cfg, resolve_contact=resolve, dialer=self.dialer,
            agent_factory=lambda: self.agent, mic_route_factory=route("mic"),
            speaker_route_factory=route("speaker"),
        )

    def _place(self, svc, brief=None, number="+351912345678"):
        brief = brief or _brief()
        first = svc.call(brief, number=number)
        second = svc.call(brief, number=number, confirm=True)
        if svc._thread is not None:
            svc._thread.join(timeout=5)
        return first, second


class ReadBackTests(_Base):
    def test_first_call_reads_back_and_dials_nothing(self):
        svc = self._svc()
        result = svc.call(_brief(), number="912345678")
        self.assertFalse(result)
        self.assertIn("Ready to call Taberna da Praia at 912 345 678", result.message)
        self.assertEqual(self.dialer.dialed, [])
        self.assertEqual(self.agent.prompts, [])

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

    def test_mic_is_routed_for_the_call_and_restored(self):
        svc = self._svc()
        self._place(svc)
        self.assertEqual(self.mic_routes, ["enter", "speaker enter", "speaker exit", "exit"])

    def test_a_failed_dial_reports_without_hanging_up(self):
        self.dialer = FakeDialer(dial_status="NOT_PREFILLED")
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertEqual(report.status, "dial_failed")
        self.assertEqual(self.dialer.hung_up, 0)

    def test_phone_link_not_connected_gets_its_own_reason(self):
        """Seen live 2026-10-07: the Calls pane said it could not reach the phone."""
        self.dialer = FakeDialer(dial_status="PHONE_NOT_CONNECTED")
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertEqual(report.status, "dial_failed")
        self.assertIn("Phone Link isn't connected to the phone", report.result.error)
        self.assertEqual(self.mic_routes, ["enter", "speaker enter", "speaker exit", "exit"])

    def test_an_unconfirmed_dial_still_runs_the_call(self):
        """2026-10-07: the call rang and was answered while no call window was visible."""
        self.dialer = FakeDialer(dial_status="DIALED_UNCONFIRMED")
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertEqual(report.result.ended_by, "end_call")
        self.assertEqual(report.status, "booked")

    def test_an_unconfirmed_call_is_still_hung_up(self):
        # in_call() reads None for a call whose End button was never seen; the
        # hang-up must be tried anyway, inside the mic route.
        self.dialer = FakeDialer(dial_status="DIALED_UNCONFIRMED")
        svc = self._svc()
        self._place(svc)
        self.assertEqual(self.dialer.hung_up, 1)
        self.assertEqual(svc.pop_report().result.error, "")

    def test_an_unconfirmed_call_that_cannot_be_hung_up_says_so(self):
        self.dialer = FakeDialer(dial_status="DIALED_UNCONFIRMED")
        self.dialer.hang_up = lambda: "NO_CALL"
        svc = self._svc()
        self._place(svc)
        self.assertIn("may still be connected", svc.pop_report().result.error)

    def test_a_call_read_as_hung_up_is_still_closed_and_not_flagged(self):
        # If the "they hung up" reading was ever wrong, the line must not stay
        # open on the room mic; when it was right, hang_up's NO_CALL is fine.
        self.agent = FakeAgent(CallResult(lines=[CallLine("them", "Estou?")], ended_by="hung_up"))
        self.dialer = FakeDialer(dial_status="DIALED_UNCONFIRMED")
        self.dialer.hang_up = mock.Mock(return_value="NO_CALL")
        svc = self._svc()
        self._place(svc)
        self.dialer.hang_up.assert_called_once()
        self.assertEqual(svc.pop_report().result.error, "")

    def test_the_room_speaking_hook_reaches_the_agent(self):
        hook = lambda: False
        svc = self._svc()
        svc._room_speaking = hook
        self._place(svc)
        self.assertIs(self.agent.mute, hook)

    def test_a_dialer_that_fails_to_build_still_reports_and_frees_the_line(self):
        svc = self._svc()
        with mock.patch.object(svc, "_get_dialer", side_effect=ImportError("no phone_link")):
            self._place(svc)
        report = svc.pop_report()
        self.assertEqual((report.result.ended_by, report.status), ("error", "failed"))
        self.assertIsNone(svc._active)

    def test_a_failure_before_anyone_spoke_is_failed_not_no_answer(self):
        self.agent = FakeAgent(raise_exc=RuntimeError("401"))
        svc = self._svc()
        self._place(svc)
        self.assertEqual(svc.pop_report().status, "failed")

    def test_agent_crash_still_reports_and_restores_the_mic(self):
        self.agent = FakeAgent(raise_exc=RuntimeError("socket closed"))
        svc = self._svc()
        self._place(svc)
        report = svc.pop_report()
        self.assertIsNotNone(report)
        self.assertEqual(report.result.ended_by, "error")
        self.assertIn("socket closed", report.result.error)
        self.assertEqual(self.mic_routes, ["enter", "speaker enter", "speaker exit", "exit"])
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

    def test_off_windows_disables(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), \
                mock.patch("services.phone_service.sys.platform", "linux"):
            svc = PhoneService(config_for_tests(phone={"enabled": True}))
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
        self.assertEqual(kwargs, {"vertexai": True, "project": "my-proj", "location": "us-central1"})

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


class NoRealPhoneTests(unittest.TestCase):
    """Constructing the shipped service must never shell out (dial, mic swap)."""

    def test_construction_never_runs_powershell(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("shelled out")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("shelled out")):
            PhoneService(config_for_tests())


class PhoneLinkInCallTests(unittest.TestCase):
    """The End button is the only call signal: seen-then-gone is a hang-up, never-seen is unknown."""

    def _dialer(self, outputs):
        from services import phone_link

        dialer = phone_link.PhoneLinkDialer()
        patcher = mock.patch.object(phone_link, "_run_ps", side_effect=outputs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return dialer

    def test_never_seen_is_unknown_not_ended(self):
        dialer = self._dialer(["DIALED_UNCONFIRMED", "NO", "NO"])
        dialer.dial("+351912345678")
        self.assertIsNone(dialer.in_call())
        self.assertIsNone(dialer.in_call())

    def test_seen_then_gone_twice_is_a_hang_up(self):
        dialer = self._dialer(["DIALED_UNCONFIRMED", "YES", "NO", "NO"])
        dialer.dial("+351912345678")
        self.assertIs(dialer.in_call(), True)
        self.assertIsNone(dialer.in_call())
        self.assertIs(dialer.in_call(), False)

    def test_one_missed_reading_does_not_end_the_call(self):
        # 2026-10-07: calls ended mid-sentence on what may have been a single
        # UIA miss. A miss followed by the button again is still a call.
        dialer = self._dialer(["DIALED", "NO", "YES", "NO", "YES"])
        dialer.dial("+351912345678")
        for expected in (None, True, None, True):
            self.assertIs(dialer.in_call(), expected)

    def test_a_new_dial_forgets_the_last_call(self):
        dialer = self._dialer(["DIALED", "NO", "NO", "DIALED_UNCONFIRMED", "NO"])
        dialer.dial("+351912345678")
        dialer.in_call()
        self.assertIs(dialer.in_call(), False)
        dialer.dial("+351912345678")
        self.assertIsNone(dialer.in_call())


class PhoneLinkScriptTests(unittest.TestCase):
    def test_the_speaker_route_reads_and_restores_the_render_default(self):
        from services import phone_link

        calls = []

        def run_ps(script, timeout_s=0):
            calls.append(script)
            if "Get-PnpDevice" in script:
                return "{0.0.0.00000000}.{realtek}"
            if "GetDefault(0)" in script:
                return "{0.0.0.00000000}.{headphones}"
            return "OK"

        with mock.patch.object(phone_link, "_run_ps", side_effect=run_ps):
            with phone_link.SpeakerRoute("Speakers (Realtek High Definition Audio)"):
                pass
        sets = [c for c in calls if "[CalAudio]::SetDefault(" in c]
        self.assertIn("{realtek}", sets[0])
        self.assertIn("{headphones}", sets[-1])  # put back what was there

    def test_no_speaker_endpoint_means_no_switch(self):
        cfg = config_for_tests(phone={"enabled": True, "call_speaker_endpoint": ""})
        self.assertIsInstance(PhoneService(cfg)._speaker_route(), contextlib.nullcontext)

    def test_the_in_call_poll_compiles_no_csharp(self):
        # It runs every couple of seconds for the whole call.
        from services import phone_link

        self.assertNotIn("Add-Type -TypeDefinition", phone_link._IN_CALL)
        self.assertIn("CalFg", phone_link._DIAL)
        self.assertIn("CalFg", phone_link._HANG_UP)

    def test_mic_is_not_switched_when_the_current_default_is_unreadable(self):
        from services import phone_link

        calls = []

        def run_ps(script, timeout_s=0):
            calls.append(script)
            return "{0.0.1.00000000}.{abc}" if "Get-PnpDevice" in script else ""

        with mock.patch.object(phone_link, "_run_ps", side_effect=run_ps):
            with self.assertRaises(RuntimeError):
                with phone_link.MicRoute("CABLE Output (VB-Audio Virtual Cable)"):
                    pass
        self.assertFalse(any("[CalAudio]::SetDefault(" in c for c in calls))




class RepairCallingLinkTests(unittest.TestCase):
    """tools/fix_phone_link.py: cheapest step first, never touches a working link."""

    def _run(self, states, radio=("Off", "On")):
        from services import phone_link

        states = list(states)
        radio = list(radio)
        calls = {"try_again": 0, "radio": []}

        def state():
            return states.pop(0) if len(states) > 1 else states[0]

        def run_ps(script, timeout_s=0):
            calls["try_again"] += 1
            return "SENT"

        def bt(value):
            calls["radio"].append(value)
            return radio.pop(0)

        now = [0.0]
        with mock.patch.object(phone_link, "calls_state", side_effect=state), \
                mock.patch.object(phone_link, "_run_ps", side_effect=run_ps), \
                mock.patch.object(phone_link, "bluetooth_radio", side_effect=bt):
            ready, steps = phone_link.repair_calling_link(
                wait_s=10, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
        return ready, steps, calls

    def test_a_working_link_is_left_alone(self):
        ready, steps, calls = self._run(["READY"])
        self.assertTrue(ready)
        self.assertEqual((calls["try_again"], calls["radio"]), (0, []))

    def test_try_again_alone_is_enough_sometimes(self):
        ready, steps, calls = self._run(["BROKEN", "READY"])
        self.assertTrue(ready)
        self.assertEqual(calls["radio"], [])

    def test_otherwise_bluetooth_is_toggled_off_then_on(self):
        ready, steps, calls = self._run(["BROKEN"] * 8 + ["READY"])
        self.assertTrue(ready)
        self.assertEqual(calls["radio"], ["Off", "On"])
        self.assertIn("Bluetooth toggle", steps[-1])

    def test_still_down_says_to_toggle_the_phone(self):
        ready, steps, calls = self._run(["BROKEN"])
        self.assertFalse(ready)
        self.assertIn("on the phone", steps[-1])

    def test_a_radio_that_does_not_come_back_on_stops_there(self):
        ready, steps, calls = self._run(["BROKEN"], radio=("Off", "Off"))
        self.assertFalse(ready)
        self.assertEqual(calls["radio"], ["Off", "On"])


if __name__ == "__main__":
    unittest.main()
