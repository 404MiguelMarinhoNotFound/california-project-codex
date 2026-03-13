# AGENTS.md - California Project Instructions

## Project Overview

**California** (C.A.L.I.F.O.R.N.I.A. - Cognitively Adaptive Language Intelligence For Operational Research, Navigation, and Intuitive Assistance) is a DIY voice assistant running on a Raspberry Pi or laptop in Carcavelos, Lisbon, Portugal. It is built around a streaming STT -> LLM -> TTS pipeline and now also controls a Mi Box / Android TV over ADB for Stremio, YouTube, and Surfshark routing. The primary user is **Master Miguel**. Target operational cost: **under EUR5/month**.

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

```text
Voice request -> LLM tool call (control_tv) -> Orchestrator VPN preflight -> MediaService / StremioService / SurfsharkService -> ADB deep link or keyevent -> Mi Box
```

### Tech Stack

| Component | Technology |
|-----------|------------|
| Wake word | openWakeWord / Porcupine |
| STT | Groq Whisper API |
| LLM | Anthropic Claude, Groq, Fireworks, or OpenAI-compatible |
| TTS | Kokoro, Edge TTS, Piper, ElevenLabs |
| TV control | ADB over network to Mi Box / Android TV |
| Stremio state | Stremio private API + local `watch_state.json` cache |
| Title resolution | TMDB |
| Audio I/O | `sounddevice`, `soundfile` |
| Language | Python |
| Key libraries | `numpy`, `queue`, `requests`, `yaml` |

### Credentials and Secrets

Required for the default setup:

- `GROQ_API_KEY` - Whisper STT
- `ANTHROPIC_API_KEY` - Claude LLM
- `PICOVOICE_ACCESS_KEY` - only if using a `.ppn` Porcupine wake-word model

Required for Stremio features:

- `STREMIO_EMAIL`
- `STREMIO_PASSWORD`

Required for TMDB fallback title resolution:

- `TMDB_API_KEY` or `TMDB_READ_ACCESS_TOKEN`

Keep secrets in `.env` or another local-only secret mechanism. Do not commit real credentials.

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
├── requirements.txt             # Python dependencies
├── core/
│   ├── orchestrator.py          # Main state machine and tool dispatch
│   ├── audio_pipeline.py        # Microphone capture and playback
│   ├── wake_word.py             # Wake-word detection
│   └── vad.py                   # Voice activity detection
├── services/
│   ├── llm.py                   # Multi-provider LLM streaming + tool calling
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
│   ├── probe_stremio_sync.py       # Refreshes and inspects Stremio watch-state cache
│   ├── debug_stremio_collections.py # Inspects raw Stremio collection payloads when sync is wrong
│   ├── search_youtube_playlists.py # Finds public YouTube playlist candidates by search query
│   ├── search_youtube_videos.py    # Finds YouTube video candidates and derives radio playlist IDs
│   └── validate_youtube_playlists.py # Fetches real YouTube page titles to confirm playlist IDs match the intended vibe
├── tests/
│   ├── test_media_service.py    # YouTube / ADB unit tests
│   ├── test_orchestrator_vpn_routing.py # VPN preflight routing behavior
│   ├── test_stremio_service.py  # Stremio / TMDB / playback unit tests
│   ├── test_surfshark_service.py # Surfshark route execution and cache behavior
│   └── test_youtube_playlist_resolver.py # Matching and random-selection coverage for saved playlists
├── sounds/                      # Wake-word and activation audio assets
├── models/                      # Wake-word and other local models
├── vpn_state.json               # Generated locally, Surfshark diagnostic cache
└── watch_state.json             # Generated locally, cached Stremio progress
```

Important runtime note:

- `watch_state.json` and `vpn_state.json` are generated cache files and should stay local
- `core/orchestrator.py` is the main coordinator, not a top-level `orchestrator.py`
- Most integrations live under `services/`
- `surfshark_routes.json` is the preferred place to retune Surfshark route timing or DPAD steps without editing code

-----

## Architecture Principles

### Streaming Pipeline

- LLM responses must stream token-by-token into `services/sentence_chunker.py`
- Each sentence should go to TTS as soon as it is complete
- Never wait for the full LLM response before speaking
- Target latency remains roughly 1.5 to 3 seconds from end of speech to first audio

### Producer-Consumer Audio Pattern

`core/orchestrator.py` uses a two-stage speech output flow:

1. `_tts_worker` synthesizes sentences into audio
2. `_audio_player_worker` plays synthesized audio from a queue

Use `queue.Queue(maxsize=2)` for synthesized audio buffering so synthesis and playback overlap without growing memory usage.

### Tool-Driven Device Control

- TV control is exposed to the LLM through the `control_tv` tool
- `services.llm.LLMService` defines the tool schema
- `core.orchestrator._handle_tool_call()` dispatches tool calls to `MediaService`, `StremioService`, and `SurfsharkService`
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

`services/surfshark_service.py` handles:

- Route-based Surfshark VPN preflight before cross-app YouTube and Stremio actions
- `restart_autoconnect` for YouTube, which restarts Surfshark and lets the app auto-connect to Albania
- `quick_connect` for Stremio, which uses a calibrated DPAD sequence to reach Portugal on this Mi Box build
- Optional status refresh from Surfshark UI XML when the XML is available
- Diagnostic `vpn_state.json` writes for route attempts and authoritative cache writes for real UI status refreshes
- Debug capture flows used by `tools/debug_surfshark_sequence.py`

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

At the time of this update:

- `restart_autoconnect` uses package launch, no DPAD sequence, and assumes Surfshark auto-connects to Albania after restart
- `quick_connect` currently uses `DPAD_CENTER`, `DPAD_DOWN`, `DPAD_DOWN`, `DPAD_CENTER`
- The initial `DPAD_CENTER` is intentional on this TV because Surfshark starts on the auto-connect screen and the first action is to exit the current auto-connect state before moving down to Portugal

-----

## LLM Tool Integration

### Active Tooling

The Claude path supports:

- Anthropic web search via `web_search_20250305`
- Custom `control_tv` tool for Mi Box control

The tool schema in `services/llm.py` currently supports these TV-related actions:

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

- `python main.py --test-mic`
- `python main.py --test-tts`
- `python main.py --test-stt`
- `python main.py --test-llm`
- `python main.py --test-pipeline`

Use full `python main.py` for real wake-word and TV-tool testing.

### Unit Tests

Run unit tests with:

```bash
python -m unittest discover -s tests -v
```

Current automated coverage exists for:

- Stremio title resolution and watch-state behavior
- Stremio playback retry logic
- Surfshark route execution, route cache semantics, and debug route capture
- Orchestrator VPN preflight routing and warning behavior
- YouTube playlist and search launch behavior
- YouTube playlist name matching and random multi-ID selection

Useful live-debug commands:

```bash
python tools\debug_surfshark_sequence.py restart_autoconnect --capture --debug
python tools\debug_surfshark_sequence.py quick_connect --capture --debug
python tools\run_youtube_playlist_e2e.py --prep-app stremio --debug
python tools\run_stremio_e2e.py --prep-app youtube --debug
python tools\probe_stremio_sync.py
```

Targeted validation used for the latest Stremio resume work:

```bash
python -m py_compile services\stremio_service.py core\orchestrator.py services\llm.py services\media_service.py tests\test_stremio_service.py tests\test_media_service.py tests\test_orchestrator_vpn_routing.py
python -m unittest tests.test_media_service tests.test_stremio_service tests.test_orchestrator_vpn_routing -v
```

-----

## Development Guidelines

### When Adding New Features

- Keep components modular and service-oriented
- Put config in `config.yaml`, not inline constants
- Add new integrations as dedicated service modules when possible
- Prefer extending the `control_tv` tool instead of adding scattered command pathways
- If a feature touches playback or state sync, add unit tests under `tests/`

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
