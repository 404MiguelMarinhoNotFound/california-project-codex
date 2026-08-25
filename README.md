# Project California

California is a DIY voice assistant for Raspberry Pi or laptop development with a low-latency streaming voice pipeline and Mi Box / Android TV control over ADB.

The current system does two main jobs:

- listen, transcribe, think, and speak back with low latency
- launch and control Stremio, YouTube, and other TV actions from voice commands

The target setup is practical rather than flashy: responsive speech, predictable media control, and operating cost under EUR5/month.

## Core Flows

### Voice assistant

```text
Microphone -> Wake word -> VAD -> Groq Whisper -> LLM -> Sentence chunker -> TTS -> Speaker
```

The important part is streaming. LLM output is split sentence by sentence so TTS can begin speaking before the full answer is finished.

### TV and media control

```text
Voice request -> LLM tool call -> MediaService / StremioService -> ADB deep link or keyevent -> Mi Box / Android TV
```

This lets California do things like:

- play or continue a Stremio title
- launch YouTube playlists or search results
- control playback, volume, home, back, wake, and sleep
- inspect current app and media session state

## Main Features

- Streaming STT -> LLM -> TTS pipeline with sentence-level overlap for lower perceived latency
- Wake-word support via openWakeWord or Porcupine
- Multi-provider LLM support through `services/llm.py`
- TTS support for Kokoro, Edge TTS, Piper, and ElevenLabs
- ADB-based control of Mi Box / Android TV
- Stremio integration with local watch-state caching in `watch_state.json`
- TMDB-backed title resolution when a requested title is not already cached
- Static YouTube playlist categories with fuzzy voice matching and optional multi-ID random selection

## Quick Start

> **This project is uv-only.** Do not use `pip`, `venv`, or `virtualenv` here.
> `pyproject.toml` + `uv.lock` are the single source of truth for dependencies.

```bash
# 1. Install uv (one time)
# Linux / macOS / Pi
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Enter the project
cd california

# 3. Create the environment from the lockfile
# uv downloads the pinned CPython (see .python-version) and builds .venv itself
uv sync

# 4. Create local env file
cp .env.example .env        # Windows: copy .env.example .env

# 5. Edit .env with at least:
# GROQ_API_KEY
# ANTHROPIC_API_KEY

# 6. Run individual tests or the full assistant
# uv run re-syncs the environment first, so there is no "activate" step
uv run python main.py --test-mic
uv run python main.py --test-tts
uv run python main.py --test-stt
uv run python main.py --test-llm
uv run python main.py --test-pipeline
uv run python main.py
```

Or just run the setup script, which installs uv if it is missing:

```bash
./setup.sh          # Linux / macOS / Pi
```

```powershell
./setup.ps1         # Windows
```

### Optional Feature Sets

Non-core providers live in `[project.optional-dependencies]` and are installed on demand:

| Extra | Installs | Used for |
|-------|----------|----------|
| `kokoro` | `kokoro` | Kokoro TTS (the `config.yaml` default `tts.provider`) |
| `piper` | `piper-tts` | Local Piper TTS, Linux / Pi only |
| `elevenlabs` | `elevenlabs` | Premium ElevenLabs TTS |
| `openai` | `openai` | OpenAI-compatible LLM endpoints (Fireworks, local, ...) |
| `porcupine` | `pvporcupine` | Porcupine wake word instead of openWakeWord |
| `silero` | `torch`, `torchaudio` | Silero VAD (heavy, ~2GB) |
| `pi` | `pixel-ring` | ReSpeaker 2-Mic HAT LED ring, Linux / Pi only |

```bash
uv sync --extra kokoro                  # one extra
uv sync --extra kokoro --extra silero   # several
uv sync --all-extras                    # everything
```

### Managing Dependencies

```bash
uv add <package>                # add a runtime dependency (updates pyproject.toml + uv.lock)
uv add --optional kokoro <pkg>  # add to an optional feature set
uv remove <package>             # drop a dependency
uv lock                         # re-resolve without changing installs
uv sync                         # make .venv match uv.lock exactly (run after git pull)
uv tree                         # inspect the resolved dependency tree
```

Commit both `pyproject.toml` and `uv.lock`. Never commit `.venv/`.

## Environment Variables

Default voice setup:

- `GROQ_API_KEY` for Whisper STT
- `ANTHROPIC_API_KEY` for Claude
- `PICOVOICE_ACCESS_KEY` if using a Porcupine `.ppn` wake-word model

Stremio support:

- `STREMIO_EMAIL`
- `STREMIO_PASSWORD`

TMDB fallback title lookup:

- `TMDB_API_KEY` or `TMDB_READ_ACCESS_TOKEN`

Keep credentials in `.env` only. Do not commit real secrets.

## Configuration

Most tuning lives in `config.yaml`.

Common things to adjust:

- wake-word model and threshold
- VAD sensitivity
- LLM provider and model
- TTS provider and voice
- Stremio sync interval and autoplay delay
- YouTube saved playlist categories
- ADB target device settings for the Mi Box / Android TV

## Manual Test Modes

`main.py` includes:

- `uv run python main.py --test-mic`
- `uv run python main.py --test-tts`
- `uv run python main.py --test-stt`
- `uv run python main.py --test-llm`
- `uv run python main.py --test-pipeline`

Run the full assistant with:

```bash
uv run python main.py
```

## Running Tests

```bash
uv run python -m unittest discover -s tests -v
```

Current tests cover core media integrations such as Stremio playback flows, TV control behavior, and YouTube playlist matching.

## Project Structure

```text
california/
├── CLAUDE.md
├── README.md
├── main.py
├── config.yaml
├── pyproject.toml
├── uv.lock
├── .python-version
├── core/
│   ├── orchestrator.py
│   ├── audio_pipeline.py
│   ├── wake_word.py
│   └── vad.py
├── services/
│   ├── llm.py
│   ├── media_service.py
│   ├── sentence_chunker.py
│   ├── stremio_service.py
│   ├── stt.py
│   ├── tts.py
│   ├── tts_text_sanitizer.py
│   └── youtube_playlist_resolver.py
├── hardware/
│   └── led_controller.py
├── tools/
│   ├── search_youtube_playlists.py
│   ├── search_youtube_videos.py
│   └── validate_youtube_playlists.py
├── tests/
├── sounds/
├── models/
└── watch_state.json
```

## Stremio and YouTube Notes

Stremio playback follows a reliability-first flow:

1. check `watch_state.json`
2. fall back to TMDB if needed
3. resolve IMDb ID
4. launch Stremio via ADB
5. retry autoplay confirmation if playback does not start

YouTube support is intentionally simple:

- playlist categories are stored in `config.yaml`
- exact and fuzzy category matching is handled in `services/youtube_playlist_resolver.py`
- categories can hold one playlist ID or several IDs
- when multiple IDs exist, one is chosen at random

## Operational Notes

- `watch_state.json` is a generated local cache and should stay disposable
- `core/orchestrator.py` is the main coordinator for speech flow and tool dispatch
- most integrations live under `services/`
- for stable ADB behavior on the Mi Box, Wakelock Revamp is a useful deployment-side helper

## Cost Target

The project is designed to stay below EUR5/month in normal use by leaning on:

- Groq Whisper free-tier STT
- concise prompts
- lightweight, direct integrations instead of heavier cloud tooling
