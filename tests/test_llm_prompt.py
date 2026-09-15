import unittest
import unittest.mock as mock

from services.llm import (
    CONTROL_LIGHTS_TOOL,
    CONTROL_LIGHTS_TOOL_OPENAI,
    CONTROL_TV_TOOL,
    CONTROL_TV_TOOL_OPENAI,
    CONTROL_VACUUM_TOOL,
    CONTROL_VACUUM_TOOL_OPENAI,
    LOCAL_TOOL_NAMES,
    LLMService,
)
from tests.config_fixture import config_for_tests


def _build(**kwargs) -> LLMService:
    with mock.patch.dict("os.environ", {"GROQ_API_KEY": "x"}), mock.patch("groq.Groq"):
        return LLMService(_config(**kwargs))


DEFAULT_PLAYLISTS = {"samba": ["RD1", "RD2"], "jazz": "PL1"}


def _config(playlists=DEFAULT_PLAYLISTS, media_enabled=True, **govee) -> dict:
    """The real config.yaml with the two inventories deliberately faked.

    This file tests the *injection mechanism*, not the data: the assertions are
    "whatever is in the config reaches the prompt, and an empty category does
    not". So the playlists and lights are small synthetic sets, and
    system_prompt is a sentinel that makes the appended block easy to see.
    The real data is covered by tests/test_playlist_config.py, which sweeps
    config.yaml itself.

    Everything else -- provider, history size, the groq block -- comes from the
    file, so a renamed llm key fails here rather than at boot.
    """
    lights = {"attic": {"mac": "AA"}, "bedroom": {"mac": "BB"}}
    config = config_for_tests(
        llm={"provider": "groq", "system_prompt": "BASE PROMPT"},
        media={"enabled": media_enabled},
        govee={"enabled": True, "default_light": "attic", **govee},
    )
    config["govee"]["lights"] = govee.get("lights", lights)
    config["youtube_playlists"] = playlists
    return config


class LightInventoryPromptTests(unittest.TestCase):
    """
    The room list used to be hardcoded in config.yaml, which went stale the
    moment a light was added. It is now injected from the live inventory.
    """

    def _service(self, **govee):
        return _build(**govee)

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


class PlaylistInventoryPromptTests(unittest.TestCase):
    """
    Master Miguel asked California what YouTube playlists she could play and she
    did not know: the saved categories lived only in config.yaml and reached the
    model as nothing but a free-text tool parameter. They are injected now, the
    same way the light rooms are.
    """

    def test_saved_categories_appear_in_the_system_prompt(self):
        prompt = _build()._build_system_prompt()
        self.assertIn("Saved YouTube playlists you can put on the TV: samba, jazz.", prompt)

    def test_answering_the_inventory_question_needs_no_tool_call(self):
        self.assertIn("do not call a tool for it", _build()._build_system_prompt())

    def test_a_category_with_no_usable_ids_is_not_advertised(self):
        # The resolver would refuse to match it, so the prompt must not offer it.
        svc = _build(playlists={"samba": ["RD1"], "empty": [], "blank": "  ", "bad": 7})
        self.assertEqual(["samba"], svc.playlist_names)
        self.assertIn("put on the TV: samba.", svc._build_system_prompt())

    def test_nothing_injected_when_the_tv_is_disabled(self):
        # No control_tv tool means no way to act on the list.
        prompt = _build(media_enabled=False)._build_system_prompt()
        self.assertNotIn("Saved YouTube playlists", prompt)

    def test_nothing_injected_when_no_playlists_configured(self):
        self.assertNotIn("Saved YouTube playlists", _build(playlists={})._build_system_prompt())

    def test_account_access_is_never_implied(self):
        prompt = _build()._build_system_prompt()
        self.assertIn("pick one at random", prompt)
        self.assertIn("youtube_search", prompt)




class ToolSchemaTests(unittest.TestCase):
    """
    Every custom schema is sent on EVERY turn, so what is in them is a running
    cost, not a one-off.
    """

    def test_light_status_is_offered(self):
        actions = CONTROL_LIGHTS_TOOL["input_schema"]["properties"]["action"]["enum"]
        self.assertIn("light_status", actions)

    def test_the_description_says_it_is_not_a_live_reading(self):
        # The model must not offer a reading the hardware cannot give.
        self.assertIn("not a live reading", CONTROL_LIGHTS_TOOL["description"])

    def test_the_openai_mirror_shares_the_same_schema_object(self):
        # It is built FROM the same dict, so schema edits flow automatically.
        # If that ever became a copy, an action could be added to one provider
        # and silently missing on the other.
        self.assertIs(
            CONTROL_LIGHTS_TOOL_OPENAI["function"]["parameters"],
            CONTROL_LIGHTS_TOOL["input_schema"],
        )
        self.assertIs(
            CONTROL_TV_TOOL_OPENAI["function"]["parameters"],
            CONTROL_TV_TOOL["input_schema"],
        )

    def test_control_tv_gained_no_new_action(self):
        # get_status already existed. Reading state back cost one enum value in
        # total, on the lights side.
        actions = CONTROL_TV_TOOL["input_schema"]["properties"]["action"]["enum"]
        self.assertIn("get_status", actions)
        self.assertEqual(len(actions), 23)

    def test_control_vacuum_is_the_core_five_and_nothing_more(self):
        # Every enum value is paid for on every turn. Pause/resume/locate were
        # deliberately left out until someone actually asks for them by voice.
        actions = CONTROL_VACUUM_TOOL["input_schema"]["properties"]["action"]["enum"]
        self.assertEqual(
            actions,
            ["vacuum_clean_all", "vacuum_clean_rooms", "vacuum_stop", "vacuum_dock", "vacuum_status"],
        )
        self.assertIs(
            CONTROL_VACUUM_TOOL_OPENAI["function"]["parameters"],
            CONTROL_VACUUM_TOOL["input_schema"],
        )
        self.assertIn("control_vacuum", LOCAL_TOOL_NAMES)


class VacuumInventoryPromptTests(unittest.TestCase):
    """Same rule as the lights: room names are injected, never hardcoded."""

    def _service(self, enabled=True, rooms=None):
        rooms = {"kitchen": {"id": 7}, "bedroom": {"id": 5}} if rooms is None else rooms
        config = _config()
        config["deebot"] = {"enabled": enabled, "rooms": rooms}
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "x"}), mock.patch("groq.Groq"):
            return LLMService(config)

    def test_configured_rooms_appear_in_the_system_prompt(self):
        prompt = self._service()._build_system_prompt()
        self.assertIn("Vacuum rooms you can clean by name: kitchen, bedroom.", prompt)

    def test_orchestrator_can_override_with_what_actually_loaded(self):
        service = self._service()
        service.vacuum_room_names = ["kitchen"]
        self.assertIn("by name: kitchen.", service._build_system_prompt())

    def test_nothing_injected_when_disabled(self):
        self.assertNotIn("Vacuum rooms", self._service(enabled=False)._build_system_prompt())

    def test_nothing_injected_when_no_rooms(self):
        self.assertNotIn("Vacuum rooms", self._service(rooms={})._build_system_prompt())

    def test_the_tool_is_only_offered_when_enabled(self):
        self.assertTrue(self._service().vacuum_enabled)
        self.assertFalse(self._service(enabled=False).vacuum_enabled)


if __name__ == "__main__":
    unittest.main()
