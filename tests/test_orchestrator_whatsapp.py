"""
control_whatsapp dispatch: the spoken strings, and the read-back guard that
stops a loosely matched name from being messaged. The service is a bare Mock,
same as test_orchestrator_lights and test_orchestrator_vacuum.

The assertions that matter most here are the negative ones. Every other
dispatcher in this project drives a device that can be put back the way it was;
this one messages a person. So a fuzzy match, an ambiguous name and an empty
body each have to prove they called `send` zero times, not merely that they
said something sensible.
"""

import unittest
from unittest.mock import Mock

from core.orchestrator import _dispatch_whatsapp
from services.whatsapp_service import ContactMatch, WhatsAppCommandResult

CERTAIN = ContactMatch(key="Marta Zuka", phone="+351933852719", certain=True)
FUZZY = ContactMatch(key="Joana Reis", phone="+351912000111", certain=False)
AMBIGUOUS = ContactMatch(candidates=["Ana Silva", "Ana Costa"])
NOBODY = ContactMatch()


def _svc(match=CERTAIN, result=None, found=None):
    service = Mock()
    service.enabled = True
    service.resolve_contact.return_value = match
    ok = WhatsAppCommandResult(True) if result is None else result
    service.send.return_value = ok
    service.schedule.return_value = ok
    service.find_contacts.return_value = (
        [("Marta Zuka", "+351933852719")] if found is None else found
    )
    return service


class SetupGateTests(unittest.TestCase):
    def test_missing_service(self):
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_send", "to": "Marta", "message": "hi"}, None),
            "WhatsApp isn't set up right now",
        )

    def test_disabled_service_sends_nothing(self):
        service = _svc()
        service.enabled = False
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "hi"}, service
            ),
            "WhatsApp isn't set up right now",
        )
        service.send.assert_not_called()

    def test_unknown_action(self):
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_yodel", "to": "Marta"}, _svc()),
            "unknown action",
        )

    def test_no_recipient_asks(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_send", "message": "hi"}, service),
            "Who should I message?",
        )
        service.send.assert_not_called()


class SendTests(unittest.TestCase):
    def test_certain_match_sends(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "job finished"}, service
            ),
            "Sent to Marta Zuka.",
        )
        service.send.assert_called_once()

    def test_missing_message_asks_rather_than_sending(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_send", "to": "Marta"}, service),
            "What do you want me to say?",
        )
        service.send.assert_not_called()

    def test_blank_message_asks(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "   "}, service
            ),
            "What do you want me to say?",
        )
        service.send.assert_not_called()

    def test_unknown_name_is_reported_not_guessed(self):
        service = _svc(match=NOBODY)
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Bartholomew", "message": "hi"}, service
            ),
            "I don't have anyone called Bartholomew in the contact book.",
        )
        service.send.assert_not_called()

    def test_ambiguous_name_asks_which_and_sends_nothing(self):
        service = _svc(match=AMBIGUOUS)
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Ana", "message": "hi"}, service
            ),
            "I've got more than one Ana: Ana Silva or Ana Costa. Which one?",
        )
        service.send.assert_not_called()

    def test_failed_send_with_no_message_falls_back(self):
        service = _svc(result=WhatsAppCommandResult(False))
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "hi"}, service
            ),
            "I couldn't send that WhatsApp message just now.",
        )

    def test_a_read_back_from_the_service_is_spoken_verbatim(self):
        """
        The service owns the read-back wording, because it owns the pending
        record the confirmation is checked against. The dispatcher must relay
        it rather than inventing its own line.
        """
        read_back = WhatsAppCommandResult(
            False, "I've got Joana Reis at +351912000111. Say send it and I will."
        )
        service = _svc(match=FUZZY, result=read_back)
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Joana", "message": "hi"}, service
            ),
            "I've got Joana Reis at +351912000111. Say send it and I will.",
        )


class InterimLineTests(unittest.TestCase):
    """
    The send takes ~15s and drives the keyboard and the screen for all of it,
    so it is announced first -- the same reason _ensure_playable speaks before
    a 25s CEC wake. A read-back is instant, so announcing one would be a lie
    with a fifteen-second shape.
    """

    def test_a_send_is_announced(self):
        spoken = []
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Marta", "message": "hi"},
            _svc(),
            say_now=spoken.append,
        )
        self.assertEqual(
            spoken, ["Opening WhatsApp, hands off the keyboard for a few seconds."]
        )

    def test_a_read_back_is_not_announced(self):
        spoken = []
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Joana", "message": "hi"},
            _svc(match=FUZZY, result=WhatsAppCommandResult(False, "read back")),
            say_now=spoken.append,
        )
        self.assertEqual(spoken, [])

    def test_a_confirmed_fuzzy_send_is_announced(self):
        spoken = []
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Joana", "message": "hi", "confirm": True},
            _svc(match=FUZZY),
            say_now=spoken.append,
        )
        self.assertEqual(
            spoken, ["Opening WhatsApp, hands off the keyboard for a few seconds."]
        )

    def test_no_say_now_is_a_normal_send(self):
        """The test modes in main.py pass no say_now; that must not break a send."""
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "hi"}, service
            ),
            "Sent to Marta Zuka.",
        )


class ConfirmPassthroughTests(unittest.TestCase):
    """
    The dispatcher passes `confirm` through and does not act on it. Whether the
    flag is honoured is the service's decision, because only the service holds
    the pending read-back record -- see test_whatsapp_service.ConfirmationTests.
    """

    def test_confirm_is_forwarded(self):
        service = _svc(match=FUZZY)
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Joana", "message": "hi", "confirm": True},
            service,
        )
        _, kwargs = service.send.call_args
        self.assertTrue(kwargs["confirm"])

    def test_confirm_defaults_to_false(self):
        service = _svc()
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Marta", "message": "hi"}, service
        )
        _, kwargs = service.send.call_args
        self.assertFalse(kwargs["confirm"])


class ScheduleTests(unittest.TestCase):
    def test_a_time_schedules_instead_of_sending(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp(
                {
                    "action": "whatsapp_send",
                    "to": "Marta",
                    "message": "hi",
                    "at": "17:30",
                },
                service,
            ),
            "Set. I'll send that to Marta Zuka at 17:30.",
        )
        service.schedule.assert_called_once()
        service.send.assert_not_called()

    def test_a_refused_schedule_surfaces_its_own_message(self):
        service = _svc(result=WhatsAppCommandResult(False, "That's too soon to schedule."))
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_send", "to": "Marta", "message": "hi", "at": "17:30"},
                service,
            ),
            "That's too soon to schedule.",
        )

    def test_a_schedule_is_not_announced_as_a_send(self):
        spoken = []
        _dispatch_whatsapp(
            {"action": "whatsapp_send", "to": "Marta", "message": "hi", "at": "17:30"},
            _svc(),
            say_now=spoken.append,
        )
        self.assertEqual(spoken, [])


class FindContactTests(unittest.TestCase):
    def test_one_hit_gives_the_number(self):
        service = _svc()
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_find_contact", "to": "Marta"}, service),
            "Marta Zuka, at +351933852719.",
        )
        service.send.assert_not_called()

    def test_several_hits_are_listed(self):
        service = _svc(found=[("Ana Silva", "+351911"), ("Ana Costa", "+351922")])
        self.assertEqual(
            _dispatch_whatsapp({"action": "whatsapp_find_contact", "to": "Ana"}, service),
            "I've got a few: Ana Silva, Ana Costa.",
        )

    def test_no_hit_says_so(self):
        service = _svc(found=[])
        self.assertEqual(
            _dispatch_whatsapp(
                {"action": "whatsapp_find_contact", "to": "Bartholomew"}, service
            ),
            "I don't have anyone called Bartholomew in the contact book.",
        )

    def test_a_lookup_never_sends(self):
        service = _svc()
        _dispatch_whatsapp(
            {"action": "whatsapp_find_contact", "to": "Marta", "message": "hi"}, service
        )
        service.send.assert_not_called()
        service.schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
