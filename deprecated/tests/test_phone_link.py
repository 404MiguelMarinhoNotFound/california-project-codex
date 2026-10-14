"""
Tests of the retired Phone Link transport (services/phone_link.py), moved out of
tests/test_phone_service.py on 2026-10-13 when calls moved to ADB. Kept for
reference with the code in deprecated/; never discovered or run (the suite runs
`-s tests`), and the imports point at the old live paths.
"""

import contextlib
import unittest
from unittest import mock

from services.phone_service import PhoneService
from tests.config_fixture import config_for_tests


class PhoneLinkPlaySoundTests(unittest.TestCase):
    def test_play_sound_returns_the_scripts_status_and_targets_the_toggle(self):
        from services import phone_link

        with mock.patch.object(phone_link, "_run_ps", return_value="PLAYING") as run:
            self.assertEqual(phone_link.PhoneLinkDialer().play_sound(), "PLAYING")
        script = run.call_args.args[0]
        self.assertIn("RingMyPhoneIndicatorToggleButton", script)
        # The key is only sent through the verified-foreground helper.
        self.assertIn("PressEnterOn", script)


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


# From tests/test_gemini_live_call.py (glc = services.gemini_live_call then).
class LoopbackDeviceTests(unittest.TestCase):
    """She must listen to the speaker the call plays on, not whatever is default."""

    def _sc(self):
        realtek, headphones = NS(name="Speakers (Realtek High Definition Audio)"), NS(name="Headphones (Black Diamond)")
        return NS(all_speakers=lambda: [headphones, realtek], default_speaker=lambda: headphones)

    def test_the_named_speaker_is_used_over_the_default(self):
        cap = glc.LoopbackCapture(device="Speakers (Realtek")
        self.assertEqual(cap._speaker(self._sc()).name, "Speakers (Realtek High Definition Audio)")

    def test_no_name_or_a_missing_one_falls_back_to_the_default(self):
        self.assertEqual(glc.LoopbackCapture()._speaker(self._sc()).name, "Headphones (Black Diamond)")
        self.assertEqual(glc.LoopbackCapture(device="Nope")._speaker(self._sc()).name, "Headphones (Black Diamond)")

    def test_the_agent_hands_its_listen_device_to_the_capture(self):
        agent = GeminiLiveAgent(client_kwargs={}, model="m", listen_device="Speakers (Realtek")
        self.assertEqual(agent.capture_factory().device, "Speakers (Realtek")


