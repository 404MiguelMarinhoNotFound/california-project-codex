"""
LLM Service — Multi-provider LLM with streaming support.

Supported providers:
- "claude"     → Anthropic Claude  (ANTHROPIC_API_KEY)
- "groq"       → Groq Llama        (GROQ_API_KEY)
- "fireworks"  → Fireworks.ai      (FIREWORKS_API_KEY)  ← OpenAI-compatible
- "openai"     → OpenAI            (OPENAI_API_KEY)     ← OpenAI-compatible

To add a NEW OpenAI-compatible provider (e.g. Together, Mistral, Ollama):
  1. Add a block under `llm:` in config.yaml with `model`, `max_tokens`, and optionally `base_url`
  2. Add its API key env var name to .env
  3. Copy the "openai" elif branch below, rename it, and point base_url at the new endpoint

Handles:
- Streaming token generation
- Conversation history management
- System prompt injection with current time
"""

import os
import time
import json
import logging
from datetime import datetime
from typing import Generator

from services.sentence_chunker import TOOL_BOUNDARY
from services.youtube_playlist_resolver import playlist_ids

logger = logging.getLogger(__name__)

# --- Tool definitions shared across providers ---

CONTROL_TV_TOOL = {
    "name": "control_tv",
        "description": (
            "Controls Master Miguel's Mi BOX S Android TV via ADB. "
            "Use when asked to play, pause, stop, skip, fast-forward, rewind, "
            "change volume, set volume to a percentage, open an app, launch "
            "Stremio titles, continue Stremio series, check Stremio episode progress, "
            "sync Stremio library, open YouTube playlists, search YouTube, "
            "toggle power, or control the TV in any way. For plain Stremio series "
            "play requests without an explicit season and episode, sync the library "
            "first and resume the latest tracked episode when available. If there is "
            "no tracked progress, open the series page instead of guessing an episode. "
            "If no preferred Stremio source is found, ask before trying the first "
            "unknown source."
        ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "play_pause", "stop", "next", "prev",
                    "fast_forward", "rewind",
                    "volume_up", "volume_down", "volume_set", "mute",
                    "launch_app", "go_home", "go_back",
                    "turn_on", "turn_off", "switch_hdmi",
                    "get_status",
                    "stremio_play", "stremio_continue", "stremio_get_progress", "stremio_sync_library",
                    "youtube_playlist", "youtube_search",
                ],
                "description": "Action to perform on the TV."
            },
            "app_name": {
                "type": "string",
                "enum": ["stremio", "youtube", "surfshark", "spotify"],
                "description": "Required only for launch_app."
            },
            "hdmi_port": {
                "type": "integer",
                "description": "Required only for switch_hdmi. The TV's HDMI input number."
            },
            "volume_steps": {
                "type": "integer",
                "description": "Steps for volume_up/volume_down. Default 10.",
                "default": 10
            },
            "volume_percent": {
                "type": "integer",
                "description": "Target volume as 0-100 percentage. Required only for volume_set.",
                "minimum": 0,
                "maximum": 100
            },
            "title": {
                "type": "string",
                "description": "Media title for Stremio actions."
            },
            "media_type": {
                "type": "string",
                "enum": ["series", "movie", "tv"],
                "description": "Optional media type hint for Stremio title resolution."
            },
            "season": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional season number for explicit Stremio episode playback."
            },
            "episode": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional episode number for explicit Stremio episode playback."
            },
            "playlist_name": {
                "type": "string",
                "description": "Configured playlist key for youtube_playlist. Use one of the saved category names listed in the system prompt, not a guess."
            },
            "playlist_id": {
                "type": "string",
                "description": "Direct YouTube playlist id, if already known."
            },
            "query": {
                "type": "string",
                "description": "Search query for youtube_search."
            },
            "allow_unknown_source": {
                "type": "boolean",
                "description": "For stremio_play only. Set true only after the user confirms trying the first available non-preferred source.",
                "default": False
            }
        },
        "required": ["action"]
    }
}

CONTROL_LIGHTS_TOOL = {
    "name": "control_lights",
    "description": (
        "Controls Master Miguel's smart lights: power, brightness, and colour. "
        "Use when asked to turn lights on or off, dim or brighten them, or change "
        "their colour, in any room. If no room is named, the default light is used. "
        "Brightness and colour only show on a light that is already on, so call "
        "light_on first if it might be off. light_status says what the light is "
        "doing, and says so itself when that is memory rather than a live reading. "
        "Do not use this for the TV."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["light_on", "light_off", "light_brightness", "light_color", "light_status"],
                "description": "What to do to the light."
            },
            "light": {
                "type": "string",
                "description": "Room or light name, for example attic. Omit to use the default light."
            },
            "brightness_percent": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Target brightness 1-100. Required only for light_brightness."
            },
            "color": {
                "type": "string",
                "description": (
                    "Colour for light_color. Use a plain name for common colours: red, green, "
                    "blue, white, warm white, cool white, orange, yellow, amber, gold, lime, "
                    "teal, cyan, turquoise, purple, violet, magenta, pink, coral, crimson. "
                    "For anything else pass a hex value like #FF7F00."
                )
            }
        },
        "required": ["action"]
    }
}

# OpenAI-compatible format of the same tools
CONTROL_TV_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": CONTROL_TV_TOOL["name"],
        "description": CONTROL_TV_TOOL["description"],
        "parameters": CONTROL_TV_TOOL["input_schema"],
    }
}

CONTROL_LIGHTS_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": CONTROL_LIGHTS_TOOL["name"],
        "description": CONTROL_LIGHTS_TOOL["description"],
        "parameters": CONTROL_LIGHTS_TOOL["input_schema"],
    }
}

CONTROL_VACUUM_TOOL = {
    "name": "control_vacuum",
    "description": (
        "Controls Master Miguel's Deebot robot vacuum. Use vacuum_clean_all for the "
        "whole house, vacuum_clean_rooms with one or more room names for specific "
        "rooms (only rooms listed in the prompt), vacuum_stop to stop, vacuum_dock "
        "to send it back to charge, and vacuum_status for battery and whether it is "
        "cleaning, docked, or in error. Not for the TV or the lights."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "vacuum_clean_all",
                    "vacuum_clean_rooms",
                    "vacuum_stop",
                    "vacuum_dock",
                    "vacuum_status",
                ],
                "description": "What to do with the vacuum."
            },
            "rooms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Room names for vacuum_clean_rooms, for example [\"kitchen\", \"bedroom\"]."
            }
        },
        "required": ["action"]
    }
}

CONTROL_VACUUM_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": CONTROL_VACUUM_TOOL["name"],
        "description": CONTROL_VACUUM_TOOL["description"],
        "parameters": CONTROL_VACUUM_TOOL["input_schema"],
    }
}

# Tools this project dispatches locally via tool_handler. Claude's built-in
# web_search also arrives as a tool_use block but is executed server-side, so
# the dispatch loop must check membership here rather than block.type alone.
LOCAL_TOOL_NAMES = {
    CONTROL_TV_TOOL["name"],
    CONTROL_LIGHTS_TOOL["name"],
    CONTROL_VACUUM_TOOL["name"],
}


class LLMService:
    def __init__(self, config: dict):
        llm_cfg = config["llm"]
        self.provider = llm_cfg["provider"]
        self.system_prompt = llm_cfg["system_prompt"].strip()
        self.max_history = llm_cfg["conversation_history_size"]

        # Conversation history: list of {"role": "user"/"assistant", "content": "..."}
        self.history: list[dict] = []

        # Tool handler callback — set by orchestrator for control_tv dispatch
        self.tool_handler: callable | None = None

        # Whether media tools are available (set after config is checked)
        self.media_enabled = config.get("media", {}).get("enabled", False)

        # Whether Govee light control is available
        govee_cfg = config.get("govee", {}) or {}
        self.lights_enabled = govee_cfg.get("enabled", False)

        # Light inventory injected into the system prompt each turn, so "what
        # lights do you have" stays correct when config.yaml changes without
        # anyone remembering to edit the prompt text too. The orchestrator
        # overwrites these with what GoveeService actually loaded, which excludes
        # any light skipped for a missing mac/sku.
        self.light_names: list[str] = list((govee_cfg.get("lights") or {}).keys())
        self.default_light: str = str(govee_cfg.get("default_light") or "").strip()

        # Deebot vacuum: same injection rule as the lights. The orchestrator
        # overwrites vacuum_room_names with what DeebotService actually loaded.
        deebot_cfg = config.get("deebot", {}) or {}
        self.vacuum_enabled = bool(deebot_cfg.get("enabled", False))
        self.vacuum_room_names: list[str] = list((deebot_cfg.get("rooms") or {}).keys())
        # The robot's spoken name. It used to be a comment in config.yaml, which
        # the model never saw, so "where is Sir Sucks-a-Lot" got "I don't do
        # location tracking" instead of a vacuum_status call.
        self.vacuum_nickname: str = str(deebot_cfg.get("nickname") or "").strip()

        # Saved YouTube playlist categories, injected for the same reason as the
        # lights: "what playlists do you know" is an inventory question, and the
        # model cannot answer it from a tool schema that only takes a name. Only
        # categories the resolver would actually accept are advertised, so a key
        # with an empty ID list is never offered.
        # HDMI inputs on the television, injected for the same reason as the
        # lights and playlists: which port is which is a fact about his living
        # room, not about the tool, and hardcoding it in system_prompt is what
        # goes stale.
        self.hdmi_ports: dict[int, str] = {
            int(port): str(name)
            for port, name in (
                ((config.get("media") or {}).get("cec_wake") or {}).get("hdmi_ports") or {}
            ).items()
        }

        self.playlist_names: list[str] = [
            name
            for name, value in (config.get("youtube_playlists") or {}).items()
            if playlist_ids(value)
        ]

        if self.provider == "claude":
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set in environment")
            self.client = anthropic.Anthropic(api_key=api_key)
            claude_cfg = llm_cfg["claude"]
            self.model = claude_cfg["model"]
            self.max_tokens = claude_cfg["max_tokens"]
            self.web_search_enabled = claude_cfg.get("web_search", False)
            self.max_searches = claude_cfg.get("max_searches_per_turn", 5)
            logger.info(f"LLM initialized: Claude ({self.model}), web_search={self.web_search_enabled}")
        elif self.provider == "groq":
            from groq import Groq
            api_key = os.environ.get("GROQ_API_KEY")
            if not api_key:
                raise ValueError("GROQ_API_KEY not set in environment")
            self.client = Groq(api_key=api_key)
            self.model = llm_cfg["groq"]["model"]
            self.max_tokens = llm_cfg["groq"]["max_tokens"]
            logger.info(f"LLM initialized: Groq ({self.model})")

        elif self.provider == "fireworks":
            # Fireworks.ai is OpenAI-compatible — uses the openai package with a custom base_url
            from openai import OpenAI
            api_key = os.environ.get("FIREWORKS_API_KEY")
            if not api_key:
                raise ValueError("FIREWORKS_API_KEY not set in environment")
            fw_cfg = llm_cfg["fireworks"]
            self.client = OpenAI(
                api_key=api_key,
                base_url=fw_cfg.get("base_url", "https://api.fireworks.ai/inference/v1"),
            )
            self.model = fw_cfg["model"]
            self.max_tokens = fw_cfg["max_tokens"]
            logger.info(f"LLM initialized: Fireworks ({self.model})")

        elif self.provider == "openai":
            # Standard OpenAI — also works for any other OpenAI-compatible endpoint.
            # Set base_url in config.yaml to point at a different host (Ollama, Together, etc.)
            from openai import OpenAI
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not set in environment")
            oa_cfg = llm_cfg["openai"]
            self.client = OpenAI(
                api_key=api_key,
                base_url=oa_cfg.get("base_url", None),  # None = default OpenAI endpoint
            )
            self.model = oa_cfg["model"]
            self.max_tokens = oa_cfg["max_tokens"]
            logger.info(f"LLM initialized: OpenAI-compatible ({self.model})")

        else:
            raise ValueError(
                f"Unknown LLM provider: '{self.provider}'. "
                f"Choose from: claude, groq, fireworks, openai"
            )

    def _build_system_prompt(self) -> str:
        """Inject current date/time and the live device inventories into the system prompt."""
        now = datetime.now()
        time_info = f"\nCurrent date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}."
        return (self.system_prompt + time_info + self._light_inventory()
                + self._playlist_inventory() + self._hdmi_inventory()
                + self._vacuum_inventory())

    def _vacuum_inventory(self) -> str:
        """
        Describe the vacuum's name and its named rooms so "clean the kitchen"
        resolves and "which rooms can you clean" is answered without a tool
        call. Same rule as the lights: never hardcode these in system_prompt.

        The nickname line also warns that speech recognition mangles the name.
        "Sir Sucks-a-Lot" came through as SirSoxalot, Sir Soxalot and "Sursocks
        a lot" in one session, and without the hint the model treated the
        first of those as an unknown person.
        """
        if not self.vacuum_enabled:
            return ""
        parts = []
        if self.vacuum_nickname:
            parts.append(
                f"\nThe vacuum is called {self.vacuum_nickname}. Speech recognition"
                " often misspells that name, so any similar-sounding name means the"
                " vacuum, and asking where it is, what it is doing, or how its"
                " battery is means vacuum_status."
            )
        if self.vacuum_room_names:
            rooms = ", ".join(self.vacuum_room_names)
            parts.append(f"\nVacuum rooms you can clean by name: {rooms}.")
        return "".join(parts)

    def _light_inventory(self) -> str:
        """
        Describe the configured lights so the assistant can answer "what lights
        do you have" without a tool call, and never from a stale hardcoded list.
        """
        if not self.lights_enabled or not self.light_names:
            return ""
        rooms = ", ".join(self.light_names)
        line = f"\nLights you can control: {rooms}."
        if self.default_light in self.light_names:
            line += f" If a request names no room, use {self.default_light}."
        elif len(self.light_names) == 1:
            line += " If a request names no room, use that one."
        return line

    def _hdmi_inventory(self) -> str:
        """
        Describe the TV's HDMI inputs so switch_hdmi can be asked for by name.

        Same rule as the lights and playlists: never hardcode these in
        system_prompt. An unnamed port is not advertised, because offering an
        input nobody labelled is worse than offering none.
        """
        if not self.media_enabled or not self.hdmi_ports:
            return ""
        inputs = ", ".join(f"HDMI {port} is the {name}"
                           for port, name in sorted(self.hdmi_ports.items()))
        return f"\nTV inputs you can switch between: {inputs}."

    def _playlist_inventory(self) -> str:
        """
        Describe the saved YouTube playlist categories so "what playlists do you
        know" is answered directly instead of guessed at or refused.

        These are curated buckets in config.yaml, not Master Miguel's YouTube
        account — there is no account integration — but they play on his own
        signed-in Mi Box, so from his side they are simply "his" playlists. The
        wording below says what he can ask for without promising a live library.
        """
        if not self.media_enabled or not self.playlist_names:
            return ""
        names = ", ".join(self.playlist_names)
        return (
            f"\nSaved YouTube playlists you can put on the TV: {names}."
            " If he asks what playlists or music you know, or what you can put on,"
            " just read that list back conversationally, do not call a tool for it,"
            " and do not invent categories that are not on it. Each one holds several"
            " playlists and you pick one at random, so the same request stays fresh."
            " A close phrasing still counts, so 'put on some samba' or 'some 80s' should"
            " go straight to youtube_playlist. Matching is on the words themselves and"
            " not on the vibe, so if a request does not clearly contain one of those"
            " names, use youtube_search rather than guessing at a category."
        )

    def stream_response(self, user_text: str) -> Generator[str, None, None]:
        """
        Send user message and stream back response tokens.
        Yields individual text chunks as they arrive, plus TOOL_BOUNDARY
        right before each local tool dispatch (see sentence_chunker).
        Also accumulates the full response and adds it to history.
        """
        # Add user message to history
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()

        start = time.time()
        full_response = ""
        # "text" accumulates everything spoken this turn, across tool rounds;
        # "last_text" is only what the final generation said.
        full_response_ref = {"text": "", "last_text": ""}

        try:
            if self.provider == "claude":
                yield from self._stream_claude(full_response_ref)
            elif self.provider in ("groq", "fireworks", "openai"):
                yield from self._stream_openai_compatible(full_response_ref)
            full_response = full_response_ref["text"]

        except GeneratorExit:
            # The orchestrator closed the stream mid-reply: he said her name
            # over her. Keep what she got to say, or the history ends on a
            # user turn and the next request is malformed. It is what was
            # *generated*, which may run a sentence past what was spoken.
            partial = full_response_ref["last_text"].strip()
            if partial:
                self.history.append({"role": "assistant", "content": partial})
            logger.info(
                "LLM interrupted after %d chars", len(full_response_ref["text"])
            )
            raise

        except Exception as e:
            logger.error(f"LLM error: {e}")
            error_msg = "Sorry, I had trouble thinking about that. Could you try again?"
            full_response = error_msg
            yield error_msg

        # The final assistant message holds only the text of the *last*
        # generation. Any tool round before it was already appended by the
        # provider loop as its own assistant/tool_use and user/tool_result
        # pair, and that pair carries the preamble text, so storing the full
        # spoken string here would duplicate it. An empty final generation is
        # skipped rather than stored: an empty text block is an invalid
        # message, and consecutive user turns are merged by the API.
        final_text = full_response_ref["last_text"] if full_response else ""
        final_text = final_text or full_response
        if final_text:
            self.history.append({"role": "assistant", "content": final_text})

        elapsed = time.time() - start
        logger.info(f"LLM completed in {elapsed:.2f}s ({len(full_response)} chars)")

    def _stream_claude(self, response_ref: dict) -> Generator[str, None, None]:
        """Stream from Claude API with web search, control_tv, and control_lights support."""
        import anthropic

        tools = []
        if self.web_search_enabled:
            tools.append({
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": self.max_searches,
            })
        if self.media_enabled:
            tools.append({
                "type": "custom",
                "name": CONTROL_TV_TOOL["name"],
                "description": CONTROL_TV_TOOL["description"],
                "input_schema": CONTROL_TV_TOOL["input_schema"],
            })
        if self.lights_enabled:
            tools.append({
                "type": "custom",
                "name": CONTROL_LIGHTS_TOOL["name"],
                "description": CONTROL_LIGHTS_TOOL["description"],
                "input_schema": CONTROL_LIGHTS_TOOL["input_schema"],
            })
        if self.vacuum_enabled:
            tools.append({
                "type": "custom",
                "name": CONTROL_VACUUM_TOOL["name"],
                "description": CONTROL_VACUUM_TOOL["description"],
                "input_schema": CONTROL_VACUUM_TOOL["input_schema"],
            })

        messages = list(self.history)

        while True:
            # messages.stream() yields real token deltas. The old messages.create()
            # returned only after the whole generation finished, so the first
            # sentence could not reach TTS until the last token was written —
            # which defeated the sentence-chunker overlap on the default provider.
            response_ref["last_text"] = ""
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._build_system_prompt(),
                messages=messages,
                tools=tools if tools else anthropic.NOT_GIVEN,
            ) as stream:
                for text in stream.text_stream:
                    response_ref["text"] += text
                    response_ref["last_text"] += text
                    yield text
                response = stream.get_final_message()

            # Tool calls are dispatched only after the text has been streamed out,
            # so speech starts while the tool is still to run.
            #
            # Every tool_use block in this response must be answered in ONE user
            # message containing ONE tool_result per block — that is what the API
            # expects. Appending per block (the old shape) resent the whole
            # assistant message once per tool and split the results across
            # several user messages, which breaks the next request as soon as
            # Claude asks for two tools in a turn ("lights on and play my show").
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name in LOCAL_TOOL_NAMES:
                    logger.info(f"Claude tool call: {block.name}({block.input})")
                    # Flush the chunker before blocking: a short "On it." would
                    # otherwise wait out the whole tool call unspoken.
                    yield TOOL_BOUNDARY
                    result_text = "tool not available"
                    if self.tool_handler:
                        result_text = self.tool_handler(block.name, block.input)
                else:
                    # Not one of ours. Still answer it: leaving a tool_use block
                    # unanswered makes the next request invalid, and returning
                    # nothing would spin this loop on an identical payload.
                    logger.warning(f"Claude asked for an unknown tool: {block.name}")
                    result_text = "tool not available"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

            # Nothing to answer means nothing would change on the next pass, so
            # stop even if stop_reason still says tool_use.
            if not tool_results:
                break

            # The round goes into history as well as this request. History
            # used to keep only the spoken text, so every earlier device
            # action looked, from the model's side, like it had been done by
            # announcing it -- and after two such turns it started announcing
            # instead of calling ("Sending him to dock now. Done." with no
            # vacuum_dock in the log). Both messages are appended together so
            # a handler that raises leaves no half-round behind.
            round_messages = [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results},
            ]
            messages.extend(round_messages)
            self.history.extend(round_messages)

    def _stream_openai_compatible(self, response_ref: dict) -> Generator[str, None, None]:
        """
        Stream from any OpenAI-compatible API with optional tool calling.
        Used by: groq, fireworks, openai — and any future provider you add.
        """
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            *self.history,
        ]

        tools_arg = []
        if self.media_enabled:
            tools_arg.append(CONTROL_TV_TOOL_OPENAI)
        if self.lights_enabled:
            tools_arg.append(CONTROL_LIGHTS_TOOL_OPENAI)
        if self.vacuum_enabled:
            tools_arg.append(CONTROL_VACUUM_TOOL_OPENAI)

        while True:
            create_kwargs = dict(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                stream=True,
            )
            if tools_arg:
                create_kwargs["tools"] = tools_arg

            stream = self.client.chat.completions.create(**create_kwargs)

            # Accumulate streamed tool calls and text
            tool_calls_acc = {}  # index -> {id, name, arguments_str}
            had_tool_call = False
            response_ref["last_text"] = ""

            for chunk in stream:
                delta = chunk.choices[0].delta

                if delta.content:
                    response_ref["text"] += delta.content
                    response_ref["last_text"] += delta.content
                    yield delta.content

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name or "",
                                "arguments": ""
                            }
                        if tc.id:
                            tool_calls_acc[idx]["id"] = tc.id
                        if tc.function.name:
                            tool_calls_acc[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments

            # Process any accumulated tool calls
            if tool_calls_acc:
                had_tool_call = True
                # Build the assistant message with tool_calls
                assistant_tool_calls = []
                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    assistant_tool_calls.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        }
                    })
                # Same persistence rule as the Claude loop: the round is kept
                # in history, and the preamble spoken alongside the call stays
                # attached to it rather than being dropped.
                assistant_msg = {
                    "role": "assistant",
                    "content": response_ref["last_text"] or None,
                    "tool_calls": assistant_tool_calls,
                }
                tool_messages = []

                # Execute each tool and collect results
                for tc_msg in assistant_tool_calls:
                    tool_name = tc_msg["function"]["name"]
                    try:
                        tool_input = json.loads(tc_msg["function"]["arguments"])
                    except json.JSONDecodeError:
                        tool_input = {}
                    logger.info(f"Tool call: {tool_name}({tool_input})")

                    # Same flush as the Claude path, for the same reason.
                    yield TOOL_BOUNDARY
                    result_text = "tool not available"
                    if self.tool_handler:
                        result_text = self.tool_handler(tool_name, tool_input)

                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_msg["id"],
                        "content": result_text,
                    })

                round_messages = [assistant_msg, *tool_messages]
                messages.extend(round_messages)
                self.history.extend(round_messages)

            # If no tool call happened, we're done
            if not had_tool_call:
                break

    @staticmethod
    def _starts_exchange(message: dict) -> bool:
        """A plain spoken user message. Tool results are user-role too on the
        Claude path, but their content is a list of blocks, not a string."""
        return message.get("role") == "user" and isinstance(message.get("content"), str)

    def _trim_history(self):
        """
        Keep only the last N exchanges, where an exchange is everything from
        one spoken user message up to the next.

        History now carries the tool rounds, so one exchange can be four or
        more messages, not two. Trimming by message count would cut a round
        in half and leave a tool_result at the front with no tool_use before
        it, which the API rejects outright. Whole exchanges only.
        """
        starts = [i for i, m in enumerate(self.history) if self._starts_exchange(m)]
        if len(starts) <= self.max_history:
            return
        del self.history[:starts[len(starts) - self.max_history]]

    def clear_history(self):
        """Clear conversation history."""
        self.history.clear()
        logger.info("Conversation history cleared")
