import unittest

from services.llm import LLMService


def _config(**govee) -> dict:
    base = {"enabled": True, "default_light": "attic",
            "lights": {"attic": {"mac": "AA"}, "bedroom": {"mac": "BB"}}}
    base.update(govee)
    return {
        "llm": {
            "provider": "groq",
            "system_prompt": "BASE PROMPT",
            "conversation_history_size": 6,
            "groq": {"model": "m", "max_tokens": 10},
        },
        "media": {"enabled": False},
        "govee": base,
    }


class LightInventoryPromptTests(unittest.TestCase):
    """
    The room list used to be hardcoded in config.yaml, which went stale the
    moment a light was added. It is now injected from the live inventory.
    """

    def _service(self, **govee):
        import unittest.mock as mock
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "x"}),              mock.patch("groq.Groq"):
            return LLMService(_config(**govee))

    def test_configured_rooms_appear_in_the_system_prompt(self):
        prompt = self._service()._build_system_prompt()
        self.assertIn("Lights you can control: attic, bedroom.", prompt)
        self.assertIn("use attic", prompt)

    def test_orchestrator_can_override_with_what_actually_loaded(self):
        svc = self._service()
        svc.light_names = ["attic"]          # bedroom was skipped for a bad mac
        svc.default_light = "attic"
        self.assertIn("Lights you can control: attic.", svc._build_system_prompt())

    def test_single_light_without_a_default_still_gets_guidance(self):
        svc = self._service(default_light=None, lights={"attic": {"mac": "AA"}})
        self.assertIn("If a request names no room, use that one.", svc._build_system_prompt())

    def test_nothing_injected_when_lights_are_disabled(self):
        prompt = self._service(enabled=False)._build_system_prompt()
        self.assertNotIn("Lights you can control", prompt)

    def test_nothing_injected_when_no_lights_configured(self):
        prompt = self._service(lights={})._build_system_prompt()
        self.assertNotIn("Lights you can control", prompt)

    def test_base_prompt_and_time_are_preserved(self):
        prompt = self._service()._build_system_prompt()
        self.assertTrue(prompt.startswith("BASE PROMPT"))
        self.assertIn("Current date and time:", prompt)


if __name__ == "__main__":
    unittest.main()
