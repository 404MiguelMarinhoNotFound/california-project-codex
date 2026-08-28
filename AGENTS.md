# AGENTS.md - California Project Instructions

## Project Overview

**California** (C.A.L.I.F.O.R.N.I.A. - Cognitively Adaptive Language Intelligence For Operational Research, Navigation, and Intuitive Assistance) is a DIY voice assistant running on a Raspberry Pi or laptop in Carcavelos, Lisbon, Portugal. It is built around a streaming STT -> LLM -> TTS pipeline and now also controls a Mi Box / Android TV over ADB for Stremio, YouTube, and Surfshark routing, plus Govee smart lights over Bluetooth LE. The primary user is **Master Miguel**. Target operational cost: **under EUR5/month**.

-----

## System Architecture

### Hardware

- **Device:** Raspberry Pi 4 or 5 for production, laptop for development
- **Microphone:** ReSpeaker 2-Mic HAT
- **TV target:** Mi Box / Android TV with ADB enabled

### Core Voice Pipeline

```text
Microphone -> Wake word -> VAD -> Groq Whisper (STT) -> Claude or compatible LLM -> Sentence chunker -> TTS -> Speaker
```

### TV Control Pipeline

_Status: Surfshark VPN preflight is currently disabled via `media.vpn_routing_enabled: false` in `config.yaml`. With the flag off, tool calls dispatch straight to `MediaService` / `StremioService` without a VPN step. The diagram below describes the architecture when routing is on, kept for a potential future re-enable._

```text
Voice request -> LLM tool call (control_tv) -> Orchestrator VPN preflight -> MediaService / StremioService / SurfsharkService -> ADB deep link or keyevent -> Mi Box
```

### Light Control Pipeline

```text
Voice request -> LLM tool call (control_lights) -> GoveeService -> BleTransport -> Bluetooth LE -> light strip
```

No VPN preflight and no ADB on this path, and by default no network at all.

### Tech Stack

| Component | Technology |
|-----------|------------|
| Wake word | openWakeWord (Porcupine retired, see note below) |
| STT | Groq Whisper API |
| LLM | Anthropic Claude, Groq, Fireworks, or OpenAI-compatible |
| TTS | Kokoro, Edge TTS, Piper, ElevenLabs |
| TV control | ADB over network to Mi Box / Android TV |
| Light control | Govee over Bluetooth LE (`bleak`); Govee cloud v2 API optional |
| Stremio state | Stremio private API + local `watch_state.json` cache |
| Title resolution | TMDB |
| Audio I/O | `sounddevice`, `soundfile` |
| Language | Python (pinned in `.python-version`, `requires-python = ">=3.11"`) |
| Packages / env | **uv only** — `pyproject.toml` + `uv.lock`, never pip |
| Key libraries | `numpy`, `queue`, `requests`, `yaml` |

### Credentials and Secrets

Required for the default setup:

- `GROQ_API_KEY` - Whisper STT
- `ANTHROPIC_API_KEY` - Claude LLM
- ~~`PICOVOICE_ACCESS_KEY`~~ - **dead.** Picovoice disabled all Free Tier AccessKeys
  on 2026-06-30. The Porcupine backend is no longer usable in this project

Required for Stremio features:

- `STREMIO_EMAIL`
- `STREMIO_PASSWORD`

Required for TMDB fallback title resolution:

- `TMDB_API_KEY` or `TMDB_READ_ACCESS_TOKEN`

Optional, only for the Govee **cloud** transport:

- `GOVEE_API_KEY` - issued from the Govee Home app under profile -> settings -> Apply for API Key,
  not from the developer portal. **Not needed for the default BLE transport**, which uses no
  credentials at all. Without it the cloud transport disables itself and the rest of the
  assistant keeps working

Keep secrets in `.env` or another local-only secret mechanism. Do not commit real credentials.

> **Porcupine is retired in this project.** Picovoice sunset its Free Tier on
> **2026-06-30** and disabled all Free Tier AccessKeys, so `PICOVOICE_ACCESS_KEY`
> no longer activates and the custom `California_*.ppn` models cannot load.
> The wake word now runs on **openWakeWord** (Apache-2.0, no key, no activation
> server), configured as `wake_word.model: "hey_jarvis_v0.1"` in `config.yaml`.
> To restore "California" as the trigger word, train a custom `.onnx` and point
> `wake_word.model` at it — see [`training/README.md`](training/README.md). Training
> runs on **livekit-wakeword** rather than openWakeWord's own trainer: openWakeWord's
> notebook path is blocked by `piper-phonemize` shipping no wheels past cp312, and
> livekit's `conv_attention` head measures 100x fewer false positives, which is the
> whole problem for a wake word sitting next to a TV that plays Hotel California.
>
> **This needs no new backend.** livekit's exported ONNX has the same contract as an
> openWakeWord custom model — input `embeddings (batch, 16, 96)`, output
> `score (batch, 1)` — and `_init_oww` reads the input name off the model rather than
> hardcoding it, so the existing `oww` path loads it as-is. Verified against a real
> exported model. Take the `optimal_threshold` from the trained
> `<model>_metrics.json`; do not keep the 0.6 tuned for `hey_jarvis_v0.1`.
>
> Do not reintroduce Porcupine without a paid key.

-----

## Project Structure

```text
california/
├── AGENTS.md                    # This file, agent-facing project guidance
├── CLAUDE.md                    # Parallel project guidance file kept in sync when relevant
├── README.md                    # Project overview and quick start
├── main.py                      # Entry point and manual test modes
├── config.yaml                  # Main configuration
├── surfshark_routes.json        # Named Surfshark route table for TV VPN automation
├── pyproject.toml               # Dependency + project metadata (uv, source of truth)
├── uv.lock                      # Fully resolved cross-platform lockfile, commit this
├── .python-version              # Interpreter pin used by uv
├── setup.sh                     # uv bootstrap for Linux / macOS / Pi
├── setup.ps1                    # uv bootstrap for Windows
├── generate_bootup_sounds.py    # Regenerates the startup one-liners in sounds/bootup/
├── generate_activation_phrases.py # Regenerates the post-wake acknowledgements
├── core/
│   ├── orchestrator.py          # Main state machine and tool dispatch
│   ├── audio_pipeline.py        # Microphone capture and playback
│   ├── wake_word.py             # Wake-word detection
│   └── vad.py                   # Voice activity detection
├── services/
│   ├── activation_phrases.py    # Wake-acknowledgement tiers + speaker-bleed echo gating
│   ├── llm.py                   # Multi-provider LLM streaming + tool calling
│   ├── govee_service.py         # Govee cloud v2 light control
│   ├── name_matcher.py          # Shared fuzzy hint -> key matching (playlists, light rooms)
│   ├── media_service.py         # Generic Mi Box / Android TV ADB controls
│   ├── sentence_chunker.py      # Splits streamed LLM output into sentences
│   ├── stremio_service.py       # Stremio auth, sync, TMDB lookup, deep-link playback
│   ├── surfshark_service.py     # Route-based Surfshark VPN automation for YouTube and Stremio
│   ├── stt.py                   # Speech-to-text
│   ├── tts.py                   # Text-to-speech
│   ├── tts_text_sanitizer.py    # Text cleanup for TTS timing
│   └── youtube_playlist_resolver.py # Matches voice playlist names and picks one saved ID at random
├── hardware/
│   └── led_controller.py        # LED state feedback
├── tools/
│   ├── debug_surfshark_sequence.py # Runs named Surfshark routes with optional screenshot capture
│   ├── debug_surfshark_status.py   # Inspects current Surfshark status and route execution
│   ├── run_stremio_e2e.py          # Live end-to-end Stremio routing and playback test
│   ├── run_youtube_playlist_e2e.py # Live end-to-end YouTube playlist routing test
│   ├── probe_govee_devices.py      # Lists Govee devices with sku, device id, and capabilities
│   ├── probe_stremio_sync.py       # Refreshes and inspects Stremio watch-state cache
│   ├── debug_stremio_collections.py # Inspects raw Stremio collection payloads when sync is wrong
│   ├── search_youtube_playlists.py # Finds public YouTube playlist candidates by search query
│   ├── search_youtube_videos.py    # Finds YouTube video candidates and derives radio playlist IDs
│   └── validate_youtube_playlists.py # Fetches real YouTube page titles to confirm playlist IDs match the intended vibe
├── tests/
│   ├── test_activation_phrases.py # Wake tiers, echo stripping, and recording trim
│   ├── test_govee_service.py    # Govee resolution, control payloads, and error mapping
│   ├── test_media_service.py    # YouTube / ADB unit tests
│   ├── test_orchestrator_lights.py # control_lights dispatch behavior
│   ├── test_orchestrator_vpn_routing.py # VPN preflight routing behavior
│   ├── test_stremio_service.py  # Stremio / TMDB / playback unit tests
│   ├── test_surfshark_service.py # Surfshark route execution and cache behavior
│   └── test_youtube_playlist_resolver.py # Matching and random-selection coverage for saved playlists
├── sounds/                      # Wake-word and activation audio assets
├── models/                      # Wake-word and other local models
├── deprecated/                  # Retired files kept for reference, see deprecated/README.md
├── vpn_state.json               # Generated locally, Surfshark diagnostic cache
└── watch_state.json             # Generated locally, cached Stremio progress
```

Important runtime note:

- `watch_state.json` and `vpn_state.json` are generated cache files and should stay local;
  both are gitignored, so they will not show up as pending changes
- `sounds/bootup/`, `sounds/california_activations/`, `sounds/chime.wav`, and
  `sounds/error.wav` are generated audio and are **not** committed. This repo carries
  audio sources, not audio output. Run `generate_bootup_sounds.py` and
  `generate_activation_phrases.py` on a fresh clone, both of which need
  `uv sync --extra default` for Kokoro. Missing files are handled gracefully:
  the orchestrator skips the greeting and `sounds.generate_if_missing` recreates the chime
- `sounds/california_activations/` holds `cold/` and `warm/` subdirectories plus a
  `manifest.json` of line text. A flat directory of WAVs is the pre-tier layout and still
  loads, into both pools; re-run `generate_activation_phrases.py` to get the split
- `deprecated/` holds files retired from the live tree. Nothing there is imported or
  executed. Do not add references to it; see `deprecated/README.md` for what was moved and why
- `core/orchestrator.py` is the main coordinator, not a top-level `orchestrator.py`
- Most integrations live under `services/`
- `surfshark_routes.json` is the preferred place to retune Surfshark route timing or DPAD steps without editing code
- `pyproject.toml` and `uv.lock` are the only dependency sources of truth. There is no `requirements.txt` any more
- `.venv/` is created and owned by uv. Never create it by hand and never commit it

-----

## Dependency Management (uv only)

**This project uses `uv` exclusively. Do not use `pip`, `pip3`, `venv`, `virtualenv`,
`pipx`, `poetry`, `conda`, or `easy_install` anywhere — not in code, not in scripts,
not in docs, and not in commands you run or suggest.**

`pyproject.toml` (declared deps) and `uv.lock` (resolved deps) are the single source of
truth. `requirements.txt` has been deleted and must not be reintroduced.

### Command mapping

| Instead of this | Do this |
|-----------------|---------|
| `pip install <pkg>` | `uv add <pkg>` |
| `pip install -r requirements.txt` | `uv sync` |
| `pip uninstall <pkg>` | `uv remove <pkg>` |
| `pip freeze` / `pip list` | `uv tree` or `uv pip list` |
| `python -m venv venv` + `activate` | `uv sync` (uv owns `.venv`) |
| `source venv/bin/activate && python x.py` | `uv run python x.py` |
| `python main.py` | `uv run python main.py` |
| `python -m unittest ...` | `uv run python -m unittest ...` |
| installing a CLI tool globally | `uvx <tool>` or `uv tool install <tool>` |
| `pyenv install 3.11` | `uv python install 3.11` / `uv python pin 3.11` |

### Rules

- **Never activate a virtualenv.** `uv run` syncs the environment and then executes, so
  there is no activation step. Any command that needs project imports goes through `uv run`.
- **Never hand-edit `uv.lock`.** Change `pyproject.toml` via `uv add` / `uv remove`, or run
  `uv lock` to re-resolve.
- **Commit `pyproject.toml` and `uv.lock` together.** A dependency change without a lock
  update is an incomplete change.
- **Run `uv sync` after pulling** so the environment matches the lockfile.
- **Optional/heavy providers go in `[project.optional-dependencies]`,** not in the core
  `dependencies` list. Current extras: `kokoro`, `piper`, `elevenlabs`, `openai`,
  `porcupine`, `silero`, `pi`, `govee`, and the aggregate `default`.
- **`uv sync --extra <name>` syncs ONLY that extra and uninstalls everything else.**
  It is not additive. Running `uv sync --extra govee` on this project removes kokoro,
  torch and spacy, which silently breaks TTS on the next launch because `config.yaml`
  selects `tts.provider: kokoro`. To install several, either pass every one
  (`uv sync --extra kokoro --extra govee`) or use the aggregate: **`uv sync --extra default`**.
  Prefer `--extra default` — it is defined as exactly what the committed `config.yaml`
  selects, so it is the one command that always leaves a bootable environment.
- **Platform-specific deps carry an environment marker** (for example
  `piper-tts>=1.2.0; sys_platform == 'linux'`) so the universal lock still resolves on Windows.
- **Beware packages that download assets into `site-packages` at runtime.** They are
  invisible to `uv.lock` (nothing in the wheel `RECORD` covers them) and any `uv sync`
  that reinstalls the package silently deletes them. Two live cases in this project:
  - `en-core-web-sm` (pulled by kokoro/misaki via `spacy download`) is **pinned as a
    direct URL dependency** in the `kokoro` extra so uv owns it.
  - openWakeWord's wake-word models cannot be pinned (they are not packages), so
    `core/wake_word.py` re-downloads them whenever they are missing. Never assume they
    survive a sync.
- **Error messages that tell a user to install something must name a `uv` command,**
  e.g. `"kokoro package not found. Install it with: uv sync --extra kokoro"`.
- If `uv` is missing on a machine, install it with the official Astral installer
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or the PowerShell equivalent on
  Windows) or just run `./setup.sh` / `./setup.ps1`, which bootstrap uv themselves.

### Note on the config default TTS provider

`config.yaml` defaults to `tts.provider: kokoro`, and `kokoro` is an **extra**, not a core
dependency. A plain `uv sync` therefore does not install it. Use:

```bash
uv sync --extra kokoro
```

or set `tts.provider: edge`, which is covered by the core dependency set.

The same applies to lights: `config.yaml` defaults to `govee.transport: ble`, which needs
`bleak` from the `govee` extra. Without it `GoveeService` logs a warning and disables itself,
and `control_lights` is not offered to the LLM at all. `uv sync --extra default` installs
both `kokoro` and `govee`, which is what the committed config actually selects.

-----

## Architecture Principles

### Streaming Pipeline

- LLM responses must stream token-by-token into `services/sentence_chunker.py`
- Each sentence should go to TTS as soon as it is complete
- Never wait for the full LLM response before speaking
- Target latency remains roughly 1.5 to 3 seconds from end of speech to first audio

### Wake Acknowledgement and Echo Gating

The activation line no longer holds the microphone. `AudioPipeline.play_activation_sound`
is non-blocking and returns an `ActivationPlayback` (name, text, duration), so
`_record_speech` starts capturing the instant the wake word fires and Master Miguel can
talk straight over the line.

- **Two tiers.** `services/activation_phrases.resolve_tier` picks `cold` on the first wake
  of a run and `warm` on every wake after. Cold lines are the long personality ones, warm
  lines are one or two words. The long ones only land on first contact; the spread between
  `"Sup."` and `"This better be good. Kidding. Go ahead."` is a large part of what makes
  California feel slow, and unpredictable latency reads worse than consistent latency
- **The onset carries the information, the tail carries the charm.** You know the wake
  fired within ~150ms of her voice starting. Everything after that is personality, so
  shortening the warm tier costs no confirmation value
- **Kokoro pads every clip with silence — trim it.** Measured on the real output: about
  0.40s on the front and 0.50s on the back, regardless of line length. On `"Sup."` that
  was more padding than speech (1.30s total for 0.42s of audio), and the leading half is
  the damaging one because it delays the onset. `generate_activation_phrases.trim_silence`
  cuts it to a 20ms lead-in and a 60ms tail with 5ms fades, which took the warm tier from
  a 1.48s mean to 0.61s and the cold tier from 2.16s to 1.25s. Any future generated audio
  from Kokoro is worth measuring the same way
- **`EchoGate` handles speaker bleed.** Recording overlaps playback by design, so the first
  chunks are California's own voice through the speaker. The gate holds the VAD clock until
  either the line ends or the mic goes loud enough that it can only be Master Miguel talking
  over her (which also calls `stop_playback()`). Those chunks are then dropped before STT.
  `vad.start_recording()` is re-called at that moment so `min_recording` and the silence
  timer measure his speech, not her line
- **`barge_in_energy_threshold` sits well above `vad.energy_threshold`** because it has to
  clear the bleed. A bad value costs responsiveness, never correctness: too high just means
  the line plays out, which is still better than before because the mic was already recording
- **`strip_activation_echo` is a safety net, not the primary defence.** The audio trim does
  the real work. The text strip catches residue that reaches Whisper as a prefix, and
  requires a match of at least two words — plenty of real commands open with the same single
  word as a short line (`"Go."` vs `"go home"`, a real `control_tv` action)
- `sounds.activation_blocking: true` restores the old behaviour, where the mic waits

### Producer-Consumer Audio Pattern

`core/orchestrator.py` uses a two-stage speech output flow:

1. `_tts_worker` synthesizes sentences into audio
2. `_audio_player_worker` plays synthesized audio from a queue

Use `queue.Queue(maxsize=2)` for synthesized audio buffering so synthesis and playback overlap without growing memory usage.

### Tool-Driven Device Control

- TV control is exposed to the LLM through the `control_tv` tool, lights through `control_lights`
- `services/llm.py` defines both tool schemas and lists the locally dispatched ones in `LOCAL_TOOL_NAMES`
- `core.orchestrator._handle_tool_call()` routes by tool name to `_dispatch_tv` or `_dispatch_lights`
- Both dispatchers are **module-level functions taking services as parameters**, not methods. That is
  what lets the tests exercise them without constructing an `Orchestrator`. Keep new ones that way
- Handlers return a short natural-language string, never JSON, because the string goes straight back
  to the LLM to be spoken
- The assistant should confirm what happened in one short spoken sentence

-----

## TV and Media Features

### MediaService

`services/media_service.py` handles:

- ADB connection management with cooldown when the TV is offline
- Shared ADB execution helpers with a configurable default timeout from `media.adb_timeout_ms`
- Basic playback controls like play, pause, stop, next, previous, rewind, and fast-forward
- Volume controls including approximate percentage set
- App launching for Stremio, YouTube, Surfshark, and Spotify
- Explicit activity launch when configured, with fallback to plain package launch
- Navigation commands like home and back
- Power and wake commands
- Current-app and media-session inspection
- Current focus inspection, UI dump capture, and screenshot capture for TV debugging
- Screenshot-byte helpers and UI dump flows reused by Stremio scans so raw ADB calls do not hang indefinitely
- YouTube playlist and search deep links
- YouTube warm launch plus one OK press to clear the profile picker on cold starts

### SurfsharkService

_Status: Idle. `media.vpn_routing_enabled` is `false` in `config.yaml`, so the orchestrator never calls `ensure_route()`. The code below is kept in place for easy rollback but does not run in the current setup._

`services/surfshark_service.py` handles:

- Route-based Surfshark VPN preflight before cross-app YouTube and Stremio actions
- `restart_autoconnect` for YouTube, which restarts Surfshark and lets the app auto-connect to Albania
- `quick_connect` for Stremio, which uses a calibrated DPAD sequence to reach Portugal on this Mi Box build
- Optional status refresh from Surfshark UI XML when the XML is available
- Diagnostic `vpn_state.json` writes for route attempts and authoritative cache writes for real UI status refreshes
- Debug capture flows used by `tools/debug_surfshark_sequence.py`

### GoveeService

`services/govee_service.py` controls Govee smart lights through one of two transports,
selected by `govee.transport` in `config.yaml`:

| Transport | Reach | Credentials | Dependency |
|-----------|-------|-------------|------------|
| `ble` (default) | Any Govee BLE device in Bluetooth range | none | `uv sync --extra govee` |
| `cloud` | Only Wi-Fi models on Govee's published whitelist, owned by the key's account | `GOVEE_API_KEY` | core `requests` |

**Why BLE is the default here.** Master Miguel's attic strip is an **H617E**, which is
BLE-only. It is absent from Govee's supported-model list, so `GET /user/devices` returns
`code: 200, "success", data: []` with a perfectly valid API key, and it never joins Wi-Fi
at all, so the LAN API cannot see it either. Its setup flow pairs over Bluetooth and never
asks for an SSID, which is the tell. The cloud transport is kept for any future whitelisted
device and is fully tested, just unused.

Common structure:

- **Power, brightness, and colour.** Both transports implement the same three methods
  (`set_power`, `set_brightness`, `set_color`), so the dispatch layer never branches on
  transport. Cloud maps them to `on_off`/`powerSwitch`, `range`/`brightness`, and
  `color_setting`/`colorRgb` (packed `R*65536 + G*256 + B`); BLE maps them to raw packets.
  `_control()` stays capability-generic, so scenes (`dynamic_scene`/`lightScene`) remain a
  small addition rather than a rewrite
- **Brightness is 1-100 on this device, not 0-255.** `clamp_percent()` clamps rather than
  rejecting, so a model that asks for 400 gets full brightness instead of an error
- Named rooms in `config.yaml` under `govee.lights`, each with optional `aliases`.
  `govee.default_light` covers plain "turn my lights on" requests
- **The room list is injected into the system prompt automatically**, so "what lights do you
  have" is answered without a tool call and can never go stale. `LLMService._light_inventory()`
  builds it, and `Orchestrator.__init__` overwrites `llm.light_names` with what `GoveeService`
  actually loaded so a light skipped for a bad mac is not advertised. **Do not hardcode room
  names in `config.yaml`'s `system_prompt`** — that is what this replaced. Available colours
  are self-documented by the `color` parameter description in the tool schema
- Required fields per light depend on the transport: `mac` for `ble`, `sku` + `device` for
  `cloud`. Lights missing them are skipped with a warning rather than crashing startup
- Returns `GoveeCommandResult` rather than raising, matching `StremioPlayResult` and
  `EnsureVpnResult`. **It defines `__bool__`, so a failed result is falsy.** Never
  truthiness-test one to check whether it *exists* — always `is None` / `is not None`.
  Every shape of this bites: `result or default` returns the default on failure, and
  `error if error else call()` runs `call()` on failure. Both happened during this feature,
  once in a test helper and once in `GoveeService.set_power` itself, where it called the
  transport with `light=None`. Truthiness is only ever a *success* check
  (`if result:` after an actual command), never an existence check.
  `test_unknown_light_returns_the_error_not_a_transport_call` guards it
- Self-disables when the config flag is off, the transport is unknown, or the transport's
  dependency/credential is missing. Never raises at construction

**BLE protocol** (reverse-engineered, verified against the real H617E):

- Characteristic `00010203-0405-0607-0809-0a0b0c0d2b11`, **Write Without Response**
- 20-byte packets: 19 command bytes then a XOR checksum of those 19. `ble_packet()` builds them
- Power on `33 01 01 ...`, power off `33 01 00 ...`
- Brightness `33 04 <1-100>`
- Colour `33 05 15 01 R G B 00*5 FF 7F ...`. **The `FF 7F` at offsets 12-13 selects
  "all segments" and is mandatory** — without it the strip silently ignores the colour

**Colour naming does not go through plain `match_name`.** `resolve_color()` adds a despaced
alias for every colour ("warmwhite" for "warm white") so the exact-match tier fires first.
Without it the substring tier matches "white" inside "warmwhite" and returns the wrong colour.
Hex input is checked before name matching. Do not "simplify" either away.

**Three BLE findings worth not rediscovering:**

- **All BLE work must run on the transport's own thread, in an MTA COM apartment.**
  bleak's WinRT backend fails with `Thread is configured for Windows GUI but callbacks
  are not working` whenever it is driven from a thread in an **STA** apartment, and the
  orchestrator's audio stack (`sounddevice`/PortAudio) puts its thread into STA. This one
  is nasty because it does not reproduce in isolation: a standalone script runs on a clean
  main thread and works, then the identical call fails inside the running assistant.
  `BleTransport._worker` runs `CoInitializeEx(None, COINIT_MULTITHREADED)` on a dedicated
  single-worker `ThreadPoolExecutor` before `asyncio.run`. Never call `asyncio.run` on a
  bleak coroutine directly from orchestrator code.
  `test_ble_work_runs_off_the_calling_thread` guards this

- **`BleakScanner.find_device_by_address` is unreliable on Windows.** It returned "not found"
  three times running while the strip was advertising steadily at -82 dBm. A
  `detection_callback` scan finds it immediately, every time. `_find()` uses the callback
- **Connect-per-command beats a persistent connection.** Measured here: cold connect 2.7-4.8s
  (it varies with how recently the strip advertised), warm reconnect ~0.2s, whole dispatch
  ~0.3s. A persistent session with the documented 2s heartbeat timed out outright. Windows
  keeps the link warm, so the simple approach is both faster and more reliable. The
  `BLEDevice` object is cached and dropped on any failure to force a rescan

**Operational notes:**

- Signal at the current laptop position averages **-82 to -83 dBm** — usable, not comfortable.
  Below about -90 it stops working. `govee.ble.retries` exists because of this
- **BLE allows one connection at a time.** An open Govee phone app will hold the device and
  lock the assistant out, and the strip stops advertising entirely while connected
- Transient `Characteristic ... was not found` and `[WinError -2147024809] The parameter is
  incorrect` show up on a first connect after idle. They are a WinRT GATT-cache artefact, the
  retry loop absorbs them, and they are not worth chasing
- If range becomes the limiting factor, an ESP32 BLE proxy in the attic slots in behind the
  same transport interface without touching the tool or dispatch layers

Setup: `uv sync --extra govee`, then `uv run python tools/probe_govee_devices.py` to get the
MAC, and put it in `config.yaml` under `govee.lights.<room>.mac`.

### StremioService

`services/stremio_service.py` handles:

- Stremio login using `STREMIO_EMAIL` and `STREMIO_PASSWORD`
- Full library sync into local `watch_state.json`
- Progress lookup for "what episode am I on" style questions
- Resume-first playback for plain series requests by syncing the library first and using the latest tracked episode when available
- Continue-watching playback using the synced or cached season and episode
- TMDB fallback when a requested title is not already in `watch_state.json`
- IMDb ID resolution for both series and movies
- Extraction of IMDb IDs from `id`, `_id`, or `state.video_id` so sync still works when Stremio payloads vary
- Stremio deep links over ADB
- Autoplay retry flow using `KEYCODE_DPAD_CENTER` / OK semantics
- Playback verification by checking `dumpsys media_session` for `state=3`
- Remembering the last successful source label for better future source selection
- Provider fallback order of remembered source, then `comet`, then `mediafusion`, then `torrent` / `torrentio` aliases before asking the user
- Series-detail fallback when no tracked episode exists instead of forcing season 1 episode 1

### Stremio Lookup Order

When asked to play or continue a title:

1. For `stremio_continue` and plain series `stremio_play` requests with no explicit season and episode, sync the Stremio library first
2. Use the synced `watch_state.json` entry when the title already exists in the library or history
3. If the title is unknown locally, query TMDB and resolve to IMDb ID
4. For series with no tracked progress, open the series detail page instead of inventing episode numbers
5. Build the Stremio deep link for either an episode target or a detail-page target
6. Try the remembered source first, then `comet`, then `mediafusion`, then `torrent` / `torrentio`
7. Launch on Mi Box with ADB
8. Wait `stremio.autoplay_delay_ms`
9. Press OK once
10. Check `dumpsys media_session`
11. Retry OK one time if playback still is not active

If playback still does not start, use this exact fallback line:

```text
Stremio's open but it didn't start on its own. Just hit OK on the remote.
```

### Watch-State Cache

`watch_state.json` is the local source of truth for:

- Known titles in the user's Stremio history or library
- Cached media type
- Current season and episode
- Whether the last episode was effectively finished
- The latest resume target for plain "play <show>" requests

Conceptual shape:

```json
{
  "shrinking": {
    "title": "Shrinking",
    "imdb_id": "tt13315786",
    "type": "series",
    "season": 2,
    "episode": 4,
    "finished_last": false
  }
}
```

Completion heuristic:

- If `timeOffset / duration > 0.85`, treat the last episode as finished
- For series entries, bump the cached episode forward by one

### Background Sync

`core/orchestrator.py` creates `StremioService` at startup and:

- syncs once during service initialization when credentials are present
- starts a background sync thread using `stremio.library_sync_interval_minutes`
- supports an on-demand sync via the `stremio_sync_library` tool action
- uses the same sync path before resume-sensitive Stremio play requests so library progress stays fresh

Important implementation note:

- The Stremio sync bug that produced `0 items` was fixed by supporting `_id`-based library entries and `state.video_id`
- Windows ADB output decoding was hardened so odd bytes in `dumpsys` or related commands do not crash the sync/playback flow

### YouTube Playback

YouTube support is intentionally simple and predictable:

- Saved playlists are configured statically in `config.yaml` under `youtube_playlists`
- Each playlist category can store one ID or a list of IDs
- When a category has multiple IDs, the system picks one at random at runtime
- The tool can launch a known playlist with a YouTube deep link
- If the playlist name does not match confidently, the assistant should ask before doing a search
- Search opens YouTube TV results using a search URL

Cold-start behavior:

- If YouTube is already foreground, California does not relaunch it before opening the requested playlist or search
- If YouTube is not foreground, California warm-launches YouTube first, waits briefly, presses OK once for the profile picker, then opens the target URL

### Playlist Matching Rules

`services/youtube_playlist_resolver.py` is responsible for turning a spoken request into a saved playlist launch:

- exact category matches win first
- then partial string matches
- then token overlap matches for near-phrases like "beach samba" or "old school hits"
- if a category resolves, one playlist ID is chosen from that category's saved list
- if nothing resolves confidently, the assistant offers a YouTube search instead of guessing

### Playlist Curation Workflow

The project now uses a stricter workflow for YouTube playlist data because random public IDs often point to unrelated content:

1. Find candidates with `tools/search_youtube_playlists.py` or `tools/search_youtube_videos.py`
2. Prefer strong video-based radio seeds for vibe-heavy categories when normal playlists are unreliable
3. Validate every candidate with `tools/validate_youtube_playlists.py`
4. Only keep IDs whose fetched YouTube page title clearly matches the intended category

This matters because the playlist ID itself is not trustworthy. The fetched title is the real check.

### Current Playlist Strategy

The current saved categories in `config.yaml` include:

- Brazilian vibe buckets such as `samba`, `pagode`, and `pagode praia`
- mood buckets such as `rnb`, `sex songs`, and `dark romance`
- nostalgia buckets such as `70s 80s 90s hits` and `legendary hits`

Many of the newer entries are `RD...` radio playlist IDs rather than community playlist IDs. That is intentional. They were easier to verify semantically and are often a better fit for vibe-based voice requests.

Recommended behavior:

- Prefer playlists for known repeated requests like samba, lofi, workout, chill, or jazz
- If a playlist is unknown, ask before falling back to a generic search

### Operational Recommendation

For the Mi Box itself, **Wakelock Revamp** is a good deployment-side addition to reduce suspend and sleep issues that can break ADB reliability over time. That is an environment recommendation, not a code dependency.

### Final VPN Routing Rules

_Status: Inactive. Routing is disabled via `media.vpn_routing_enabled: false`. Stremio and YouTube actions now launch directly with no VPN preflight. The rules below are retained for history and in case the flag is flipped back on._

These are the current final decisions and should be preserved unless the user explicitly wants a policy change:

- If the requested app is already foreground, skip Surfshark entirely
- Cross-app YouTube requests use `restart_autoconnect`
- Cross-app Stremio requests use `quick_connect`
- `restart_autoconnect` is intentionally operational, not visual: restart Surfshark, let it auto-connect in the background, then continue
- `quick_connect` is intentionally DPAD-driven and calibrated for this Mi Box build, not generic country search logic
- Current `quick_connect` route is the Portugal path and lives in `surfshark_routes.json`
- Current `restart_autoconnect` route is the Albania path and also lives in `surfshark_routes.json`
- The route table is easier to retune than hardcoding more logic into `surfshark_service.py`

### Current Surfshark Route Calibration

_Status: Not in use while routing is disabled. Retained for reference if the flag is flipped back on._

At the time of this update:

- `restart_autoconnect` uses package launch, no DPAD sequence, and assumes Surfshark auto-connects to Albania after restart
- `quick_connect` currently uses `DPAD_CENTER`, `DPAD_DOWN`, `DPAD_DOWN`, `DPAD_CENTER`
- The initial `DPAD_CENTER` is intentional on this TV because Surfshark starts on the auto-connect screen and the first action is to exit the current auto-connect state before moving down to Portugal

-----

## LLM Tool Integration

### Active Tooling

The Claude path supports:

- Anthropic web search via `web_search_20250305`
- Custom `control_tv` tool for Mi Box control (23 actions)
- Custom `control_lights` tool for Govee light control (2 actions)

With the committed `config.yaml` that is **3 tools** in every request: `web_search`,
`control_tv`, `control_lights`. Each custom tool's full schema is sent on every turn, so
adding actions and parameters costs input tokens on every single exchange. Keep descriptions
tight — see Cost Discipline.

Both custom tools are gated by config: `control_tv` on `media.enabled`, `control_lights` on
`govee.enabled`. Web search runs server-side at Anthropic and is **not** dispatched locally, which
is why the Claude tool loop checks `block.name in LOCAL_TOOL_NAMES` instead of `block.type` alone.
Widening that check to any `tool_use` block would break web search.

`control_lights` supports these actions:

- `light_on`, `light_off`
- `light_brightness` (needs `brightness_percent`, 1-100, clamped not rejected)
- `light_color` (needs `color`)

with an optional `light` parameter naming the room. Omitting it uses `govee.default_light`.

`color` accepts a spoken name from `COLOR_NAMES` in `services/govee_service.py` or a hex
value like `#FF7F00`; the tool description tells the model to use hex for anything outside
the name table. Brightness and colour only take visible effect on a light that is already
on, which the tool description and system prompt both state so the model turns it on first.

The `control_tv` schema in `services/llm.py` currently supports these TV-related actions:

- `play_pause`, `stop`, `next`, `prev`
- `fast_forward`, `rewind`
- `volume_up`, `volume_down`, `volume_set`, `mute`
- `launch_app`, `go_home`, `go_back`
- `power_toggle`, `sleep`, `wake`
- `get_status`
- `stremio_play`, `stremio_continue`, `stremio_get_progress`, `stremio_sync_library`
- `youtube_playlist`, `youtube_search`

When tools are active:

- Stream only spoken text to TTS
- Do not read raw `tool_use` or `tool_result` structures aloud
- Keep tool confirmations short and natural
- Treat plain show requests for `stremio_play` as "sync the library, then resume the latest tracked episode"

-----

## TTS Guidance

### Current Defaults

- Current config default is Kokoro
- Current preferred voice is `af_bella`
- Kokoro uses `lang_code="a"` in this project

### Text Sanitization

TTS output is sensitive to punctuation. Avoid text that causes long pauses:

- replace em dashes with commas or connector words
- avoid awkward punctuation clusters
- prefer flowing spoken sentences over rigid written prose

`services/tts_text_sanitizer.py` is the place for normalization logic, and TTS output should stay optimized for the ear, not the page.

-----

## California's Personality

California should stay in character:

- West Coast energy, sharp, warm, and relaxed
- Dry humor is welcome when it helps
- Spoken replies should be short and natural
- Address the user as **Master Miguel** when it feels natural
- Avoid formal, robotic, or corporate language

System-prompt guidance:

- no markdown in spoken replies
- no bullets, headers, or formatting in spoken replies
- keep most answers to one to three short sentences
- if TV control succeeded or failed, say so plainly

-----

## Localization

- **Location:** Carcavelos, Lisbon, Portugal
- **Temperature:** Celsius
- **Default interaction language:** English

-----

## Testing and Validation

### Manual Test Modes

`main.py` exposes these manual test modes:

- `uv run python main.py --test-mic`
- `uv run python main.py --test-tts`
- `uv run python main.py --test-stt`
- `uv run python main.py --test-llm`
- `uv run python main.py --test-pipeline`

Use full `uv run python main.py` for real wake-word and TV-tool testing.

### Unit Tests

Run unit tests with:

```bash
uv run python -m unittest discover -s tests -v
```

Current automated coverage exists for:

- Stremio title resolution and watch-state behavior
- Stremio playback retry logic
- Surfshark route execution, route cache semantics, and debug route capture
- Orchestrator VPN preflight routing and warning behavior
- Govee transport selection, light resolution, BLE packet format, and cloud HTTP error mapping
- `control_lights` dispatch strings and failure fallbacks
- YouTube playlist and search launch behavior
- YouTube playlist name matching and random multi-ID selection
- Activation tier selection, echo stripping, `EchoGate` arming, and the recording trim

Useful live-debug commands:

```bash
uv run python tools\debug_surfshark_sequence.py restart_autoconnect --capture --debug
uv run python tools\debug_surfshark_sequence.py quick_connect --capture --debug
uv run python tools\run_youtube_playlist_e2e.py --prep-app stremio --debug
uv run python tools\run_stremio_e2e.py --prep-app youtube --debug
uv run python tools\probe_stremio_sync.py
uv run python tools\probe_govee_devices.py
uv run python tools\probe_govee_devices.py --transport cloud
```

Targeted validation used for the latest Stremio resume work:

```bash
uv run python -m py_compile services\stremio_service.py core\orchestrator.py services\llm.py services\media_service.py tests\test_stremio_service.py tests\test_media_service.py tests\test_orchestrator_vpn_routing.py
uv run python -m unittest tests.test_media_service tests.test_stremio_service tests.test_orchestrator_vpn_routing -v
```

-----

## Development Guidelines

### When Adding New Features

- Keep components modular and service-oriented
- Put config in `config.yaml`, not inline constants
- Add new integrations as dedicated service modules when possible
- Extend `control_tv` for TV actions and `control_lights` for light actions instead of adding
  scattered command pathways. A genuinely new device class gets its own tool: add the schema and
  its OpenAI re-wrap in `services/llm.py`, add the name to `LOCAL_TOOL_NAMES`, add a module-level
  `_dispatch_*` in `core/orchestrator.py`, and branch on the name in `_handle_tool_call`
- If a feature touches playback or state sync, add unit tests under `tests/`
- Add dependencies with `uv add` only. Never `pip install`, and never reintroduce a
  `requirements.txt`. Commit the resulting `pyproject.toml` and `uv.lock` together

### Problem-Solving Approach

1. Start with config and text-shaping fixes
2. Prefer observable flows and explicit fallback behavior
3. Only add architectural complexity when the simpler path fails
4. For Android TV, prefer package launch plus small named DPAD sequences over taps or giant macros
5. Use screenshots and UI dumps for calibration and debugging, not as a mandatory runtime dependency unless needed

### Reliability Rules

- Never pretend playback worked if `media_session` does not confirm it
- Prefer graceful spoken fallback over silent failure
- Keep `watch_state.json` local and disposable
- Keep `vpn_state.json` local and treat it as diagnostic unless it came from a real Surfshark UI refresh
- Tune `stremio.autoplay_delay_ms` on the real Mi Box if needed
- Tune `media.adb_timeout_ms` before adding more Stremio UI automation if the Mi Box starts timing out under load
- Prefer `surfshark_routes.json` edits over code edits when the Surfshark focus path changes

### Cost Discipline

- Keep Groq Whisper on the free tier
- Keep prompts concise to control token cost
- Avoid integrations that push monthly operating cost beyond the project target

-----

## Key Learnings

- Streaming is non-negotiable for a responsive assistant
- Sentence-level TTS overlap is the right pattern for voice latency
- Plain show requests should sync first, then use `watch_state.json` as the resume source of truth for Stremio titles
- TMDB is the fallback resolver for titles outside the local Stremio cache
- `state=3` in `dumpsys media_session` is the practical playback signal
- If no resume progress exists for a series, opening the Stremio detail page is better than guessing an episode
- Shared ADB helpers with explicit timeouts are more reliable than scattered raw shell calls for UI dumps and screenshot capture
- Static YouTube playlist mapping is simpler and more reliable than OAuth-heavy integrations
- For YouTube curation, validation against the real fetched page title is more reliable than trusting search snippets or guessed IDs
- Multi-ID playlist categories are a simple way to keep repeated requests fresh without changing the voice interface
- The YouTube region path is best handled by restarting Surfshark and then opening YouTube, not by trying to select Albania manually inside the app
- On this Mi Box, the Stremio VPN path is best handled as a small calibrated DPAD sequence to Portugal
- Same-app requests should preserve the active session and skip Surfshark, even if that means VPN policy is only enforced on cross-app transitions
- Package launch plus named route tables is easier to maintain than scattering Surfshark timing and key sequences through the codebase
- Graceful fallback lines build trust more than pretending automation is perfect

-----

## Known Bugs / Audit

A whole-codebase bug audit was run on **2026-07-12**. Full report:
[`BUG_AUDIT.md`](BUG_AUDIT.md). Baseline at audit time: all 63 unit tests passing;
`py_compile` clean except the stale `services/tts copy.py` dead file (that file, and
`services/sentence_chunker copy.py`, were moved to `deprecated/` on 2026-08-25; the
live tree now parses clean).

Confirmed **High-severity** backlog (identification only — not yet fixed):

- **Barge-in / "stop" is non-functional** — `_interrupted` is never set `True`, so the
  interrupt guards are dead code and "stop" only skips the current sentence
  (`core/orchestrator.py`).
- ~~**Claude provider does not stream**~~ — **fixed 2026-08-27.** `_stream_claude` now
  uses `client.messages.stream()` and yields real token deltas, so the default provider
  gets the sentence-chunker/TTS overlap the design assumes. The same change fixed **M2**
  (a response with two `tool_use` blocks used to build a malformed next request).
  Covered by `tests/test_llm_claude_streaming.py`.
- **Default TTS `kokoro` missing from `requirements.txt`** — clean install crashes at
  `TTSService.__init__` (`config.yaml:133`).

See `BUG_AUDIT.md` for Medium/Low findings and the verified-rejected false positives.
