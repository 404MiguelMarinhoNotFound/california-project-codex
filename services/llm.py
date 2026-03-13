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
                    "power_toggle", "sleep", "wake",
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
                "description": "Configured playlist key label for youtube_playlist (for example: samba, lofi, workout, chill, jazz)."
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

# OpenAI-compatible format of the same tool
CONTROL_TV_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": CONTROL_TV_TOOL["name"],
        "description": CONTROL_TV_TOOL["description"],
        "parameters": CONTROL_TV_TOOL["input_schema"],
    }
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
        """Inject current date/time into system prompt."""
        now = datetime.now()
        time_info = f"\nCurrent date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}."
        return self.system_prompt + time_info

    def stream_response(self, user_text: str) -> Generator[str, None, None]:
        """
        Send user message and stream back response tokens.
        Yields individual text chunks as they arrive.
        Also accumulates the full response and adds it to history.
        """
        # Add user message to history
        self.history.append({"role": "user", "content": user_text})
        self._trim_history()

        start = time.time()
        full_response = ""

        try:
            if self.provider == "claude":
                yield from self._stream_claude(full_response_ref := {"text": ""})
                full_response = full_response_ref["text"]
            elif self.provider == "groq":
                yield from self._stream_openai_compatible(full_response_ref := {"text": ""})
                full_response = full_response_ref["text"]
            elif self.provider in ("fireworks", "openai"):
                yield from self._stream_openai_compatible(full_response_ref := {"text": ""})
                full_response = full_response_ref["text"]

        except Exception as e:
            logger.error(f"LLM error: {e}")
            error_msg = "Sorry, I had trouble thinking about that. Could you try again?"
            full_response = error_msg
            yield error_msg

        # Add assistant response to history
        self.history.append({"role": "assistant", "content": full_response})

        elapsed = time.time() - start
        logger.info(f"LLM completed in {elapsed:.2f}s ({len(full_response)} chars)")
    def _stream_claude(self, response_ref: dict) -> Generator[str, None, None]:
        """Stream from Claude API with web search and control_tv tool support."""
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

        messages = list(self.history)

        while True:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._build_system_prompt(),
                messages=messages,
                tools=tools if tools else anthropic.NOT_GIVEN,
            )

            # Process content blocks
            for block in response.content:
                if block.type == "text":
                    response_ref["text"] += block.text
                    yield block.text
                elif block.type == "tool_use" and block.name == "control_tv":
                    logger.info(f"Claude tool call: {block.name}({block.input})")
                    result_text = "tool not available"
                    if self.tool_handler:
                        result_text = self.tool_handler(block.name, block.input)
                    # Append assistant message with tool use + tool result for next turn
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }]
                    })

            # If stop_reason is "tool_use", loop back so Claude can respond with text
            if response.stop_reason != "tool_use":
                break

    def _stream_openai_compatible(self, response_ref: dict) -> Generator[str, None, None]:
        """
        Stream from any OpenAI-compatible API with optional tool calling.
        Used by: groq, fireworks, openai — and any future provider you add.
        """
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            *self.history,
        ]

        tools_arg = [CONTROL_TV_TOOL_OPENAI] if self.media_enabled else None

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

            for chunk in stream:
                delta = chunk.choices[0].delta

                if delta.content:
                    response_ref["text"] += delta.content
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
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": assistant_tool_calls,
                })

                # Execute each tool and append results
                for tc_msg in assistant_tool_calls:
                    tool_name = tc_msg["function"]["name"]
                    try:
                        tool_input = json.loads(tc_msg["function"]["arguments"])
                    except json.JSONDecodeError:
                        tool_input = {}
                    logger.info(f"Tool call: {tool_name}({tool_input})")

                    result_text = "tool not available"
                    if self.tool_handler:
                        result_text = self.tool_handler(tool_name, tool_input)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_msg["id"],
                        "content": result_text,
                    })

            # If no tool call happened, we're done
            if not had_tool_call:
                break

    def _trim_history(self):
        """Keep only the last N exchanges."""
        max_messages = self.max_history * 2  # Each exchange = 2 messages
        while len(self.history) > max_messages:
            self.history.pop(0)

    def clear_history(self):
        """Clear conversation history."""
        self.history.clear()
        logger.info("Conversation history cleared")
