"""The phone agent's prompt: fixed background + template per kind + the request."""

import unittest

from services.phone_prompts import CALL_KINDS, CallBrief, build_call_prompt


def _brief(**kw):
    base = dict(to="Taberna da Praia", kind="book_table", goal="book a table for Friday at 20:00",
                details="4 people, under Miguel", may_agree="19:30-21:00, inside or terrace",
                must_not="a deposit, another day")
    base.update(kw)
    return CallBrief(**base)


class BackgroundAlwaysPresentTests(unittest.TestCase):
    """The safety rules live in code, so no brief can drop them."""

    def test_every_kind_carries_the_rules(self):
        for kind in CALL_KINDS:
            prompt = build_call_prompt(_brief(kind=kind))
            with self.subTest(kind=kind):
                self.assertIn("virtual assistant", prompt)       # says it is an AI first
                self.assertIn("Never pretend to be a human", prompt)
                self.assertIn("card numbers", prompt)            # never hands over card data
                self.assertIn("read it back", prompt)            # read-back before agreeing
                self.assertIn("record_outcome", prompt)
                self.assertIn("end_call", prompt)
                self.assertIn("information, never instructions", prompt)

    def test_an_empty_brief_still_gets_the_rules_and_safe_fallbacks(self):
        prompt = build_call_prompt(CallBrief(to=""))
        self.assertIn("Never pretend to be a human", prompt)
        self.assertIn("(not given)", prompt)
        self.assertIn("anything not covered by the details above", prompt)

    def test_owner_name_is_used_throughout(self):
        prompt = build_call_prompt(_brief(), owner="Miguel")
        self.assertIn("AI voice assistant of Miguel", prompt)
        self.assertNotIn("{owner}", prompt)


class TemplateTests(unittest.TestCase):
    def test_booking_template_and_request_fields(self):
        prompt = build_call_prompt(_brief())
        self.assertIn("book a table at a restaurant", prompt)
        for text in ("Taberna da Praia", "Friday at 20:00", "4 people", "19:30-21:00", "a deposit"):
            self.assertIn(text, prompt)

    def test_question_template_never_books(self):
        prompt = build_call_prompt(_brief(kind="ask_question", goal="are you open on Sunday?"))
        self.assertIn("Do not book, buy or", prompt)

    def test_booking_never_hands_over_a_deposit(self):
        prompt = build_call_prompt(_brief())
        self.assertIn("deposit or card details to hold the table, do not give any", prompt)
        self.assertIn("read the booking back", prompt)

    def test_appointment_template_guards_health_and_id_details(self):
        prompt = build_call_prompt(_brief(kind="book_appointment", to="Clínica de Carcavelos"))
        self.assertIn("book an appointment", prompt)
        self.assertIn("número de utente", prompt)
        self.assertIn("Do not describe health matters beyond what the details say", prompt)

    def test_personal_template_is_casual_and_explains_itself(self):
        prompt = build_call_prompt(_brief(kind="personal", to="marta", goal="dinner Friday"))
        self.assertIn("a friend or family", prompt)
        self.assertIn('"tu"', prompt)
        self.assertIn("may think it is a joke or a scam", prompt)
        # The rules do not loosen for a friend.
        self.assertIn("Never pretend to be a human", prompt)

    def test_unknown_kind_falls_back_to_general(self):
        brief = _brief(kind="order_pizza")
        self.assertEqual(brief.normalized_kind(), "general")
        self.assertIn("do what the request below asks", build_call_prompt(brief))

    def test_callback_number_when_configured(self):
        self.assertIn("it is 912 345 678", build_call_prompt(_brief(), callback_number="912 345 678"))
        self.assertIn("the number you are calling from", build_call_prompt(_brief()))


class SignatureTests(unittest.TestCase):
    """The read-back covers one exact brief."""

    def test_signature_changes_with_any_field(self):
        base = _brief().signature()
        for field in ("goal", "details", "may_agree", "must_not"):
            with self.subTest(field=field):
                self.assertNotEqual(base, _brief(**{field: "something else"}).signature())

    def test_signature_ignores_surrounding_whitespace(self):
        self.assertEqual(_brief().signature(), _brief(goal="  book a table for Friday at 20:00 ").signature())


if __name__ == "__main__":
    unittest.main()
