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
Voice request -> LLM tool call (control_lights) -> GoveeService -> BleTransport  -> Bluetooth LE      -> Govee strip (write only)
                                                                -> TapoTransport -> LAN, TPAP (fw 1.4.2+) or python-kasa -> Tapo bulb (reads back)
```

No VPN preflight and no ADB on this path, and by default no network at all.

### Tech Stack

| Component | Technology |
|-----------|------------|
| Wake word | openWakeWord runtime + custom model trained with livekit-wakeword (Porcupine retired, see note below) |
| STT | Groq Whisper API |
| LLM | Anthropic Claude, Groq, Fireworks, or OpenAI-compatible |
| TTS | Kokoro, Edge TTS, Piper, ElevenLabs, Google Cloud TTS |
| TV control | ADB over network to Mi Box / Android TV |
| Light control | Govee over Bluetooth LE (`bleak`) or TP-Link Tapo over the LAN (`services/tapo_tpap.py` for firmware 1.4.2+, `python-kasa` for older); Govee cloud v2 API optional |
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
- `GOOGLE_TTS_API_KEY` - Google Cloud Text-to-Speech, the current `tts.provider`.
  Without it `TTSService` logs a warning and falls back to Edge, so she still
  speaks, just not as Aoede, and the pre-rendered greeting/acknowledgement clips
  will no longer match the live voice
- ~~`PICOVOICE_ACCESS_KEY`~~ - **dead.** Picovoice disabled all Free Tier AccessKeys
  on 2026-06-30. The Porcupine backend is no longer usable in this project

Required for Stremio features:

- `STREMIO_EMAIL`
- `STREMIO_PASSWORD`

Required for TMDB fallback title resolution:

- `TMDB_API_KEY` or `TMDB_READ_ACCESS_TOKEN`

Optional, only for the Tapo transport (`govee.transport: "tapo"`):

- `TAPO_USERNAME` / `TAPO_PASSWORD` - the TP-Link account the bulbs were onboarded
  with. Control traffic never leaves the LAN, but the KLAP handshake still
  authenticates against that account, so these are not optional for local control.
  Without them `TapoTransport` disables itself and `control_lights` is not offered

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
> **"California" is live again as of 2026-09-01**: `models/california_v2.onnx`,
> trained on Modal with livekit-wakeword. `hey_jarvis_v0.1` above is the historical
> placeholder, not the current setting.
>
> **This needs no new backend.** livekit's exported ONNX has the same contract as an
> openWakeWord custom model — input `embeddings (batch, 16, 96)`, output
> `score (batch, 1)` — and `_init_oww` reads the input name off the model rather than
> hardcoding it, so the existing `oww` path loads it as-is. Verified against a real
> exported model.
>
> **Do not trust `optimal_threshold` from the trained metrics.** It is computed against
> synthetic Piper validation audio and does not survive contact with a microphone. Both
> runs so far were badly optimistic — see Key Learnings. Set the threshold from held-out
> recordings of Master Miguel using `tools/score_wakeword.py --dir`, never from the eval.
>
> **openWakeWord must be fed 1280-sample frames, and `consecutive_frames` did
> nothing until it was.** `audio.chunk_duration_ms: 40` means the detector is fed
> 640-sample chunks, but openWakeWord only computes a new embedding every 1280
> samples (`openwakeword/utils.py::AudioFeatures._streaming_features` gates on
> `accumulated_samples >= 1280 and accumulated_samples % 1280 == 0`). On a shorter
> chunk `Model.predict` takes its `n_prepared_samples < 1280` branch and returns
> the **previous prediction verbatim**. The live score stream was therefore
> `0, S1, S1, S2, S2, ...`, so one real detection always produced two identical
> frames over threshold and `consecutive_frames: 2` was silently equivalent to 1 —
> the false-positive defence was off. `_process_oww` now buffers to the native
> frame, mirroring what `_process_porcupine` always did. Do not "simplify" that
> buffer away, and do not raise `audio.chunk_duration_ms` to 80 instead — the
> recording path wants 40ms chunks.
>
> **She was waking on SILENCE, and `wake_word.dither_rms` is the fix.** Reported
> 2026-09-03 as "false positives, I was purely silent" — and the silence was the
> cause, not the context. Master Miguel's Realtek mic array gates near-silence
> down to RMS ~5 while leaving its spectral structure intact, and openWakeWord's
> log-mel front end turns that into features no training clip ever contained:
> every one of them has real background mixed in at an audible level. The model
> does not fail quietly there, it fails *confidently*. Measured over 150s of the
> real room, the **highest-scoring frames were the quietest ones** (RMS 3-9 across
> their whole context) while the loudest events in the same recording (RMS ~500)
> scored low. Worst ambient score 0.8005 against a 0.81 threshold — 0.0095 of
> headroom is where the false wakes lived.
>
> `WakeWordDetector._apply_dither` mixes white noise at RMS 10 into each native
> frame before scoring. That takes the worst ambient score to **0.032** (measured
> live afterwards: 0.0194) and frames over 0.5 from 14 to 0. Flat digital silence
> and flat white noise both score ~0.002, so the trigger is gated near-silence
> specifically, not low energy. Wake path only — Whisper still gets clean audio
> and the capture ring still records raw, so negatives stay honest.
>
> **Raising the threshold does not fix this, and it was the obvious wrong move.**
> The observed false wakes were 0.896 and 0.927, and real wake words peak in
> exactly that band (0.89/0.94/0.95/0.97/0.97/0.97). No threshold separates them:
> 0.90 costs 5-11 points of recall and leaves the worst ambient score untouched at
> 0.801. The dither removes the artefact instead, which is why it works at *every*
> threshold rather than shuffling the same overlap around — and it is what makes
> the threshold a free parameter again. See the measured table in `config.yaml`.
>
> **`tools/score_wakeword.py` applies the same dither**, in `score_wav`, `run_wav`
> and `run_live`. Without that it reports ~0.80 on a silent room and sends you
> straight back to raising the threshold. `fires_framed` gets it for free by going
> through `process_audio`.
>
> **Wake-word measurements are not reproducible unless you seed numpy.**
> `openwakeword.utils.AudioFeatures.reset()` re-seeds its feature buffer from the
> **global** numpy RNG, so a single pass over a directory varies run to run — two
> honest measurements of the same thing will disagree by a couple of files.
> `tools/score_wakeword.py` now seeds it, and the dither RNG, per file from the
> file name, so its numbers repeat exactly. Ad-hoc scripts must do the same.
>
> **A short clip scored straight after a reset measures the reset, not the
> model.** Until 2026-09-23 `score_wakeword.py` fed each ~0.7s held-out take
> bare: the classifier's ~1.3s window was mostly reset filler, and the loop
> dropped the last partial 1280-sample frame, which is where the word ends. That
> is where "16% EN / 20% PT recall" came from. The same 40 takes scored inside a
> bed of room-level noise (2s before, 1s after — `Bed` in the tool, `--bed` for a
> real room-tone WAV, `--no-pad` to reproduce the old numbers) fire **75% EN /
> 65% PT** at 0.81, framed. Every recall figure recorded before that date,
> including the barge-in ones below and in commit messages, came off the broken
> harness. `--negatives` also used to count `*_ok.wav` captures as false fires;
> their pre-roll holds a real wake word, and they are now skipped.
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
├── generate_her_monologues.py   # Long replies in her voice, the background for wake-word takes over her
├── core/
│   ├── orchestrator.py          # Main state machine and tool dispatch
│   ├── audio_pipeline.py        # Microphone capture and playback
│   ├── wake_word.py             # Wake-word detection
│   └── vad.py                   # Voice activity detection
├── services/
│   ├── activation_phrases.py    # Wake-acknowledgement tiers + speaker-bleed echo gating
│   ├── cec_wake.py              # Wake the box via the TV over HDMI-CEC; ADB cannot turn it on
│   ├── device_finder.py         # Shared find-by-MAC / verify-by-identity / cache-the-IP ladder
│   ├── llm.py                   # Multi-provider LLM streaming + tool calling
│   ├── govee_service.py         # Govee cloud v2 light control
│   ├── tapo_transport.py        # TP-Link Tapo over the LAN; the one transport that reads state back
│   ├── tapo_tpap.py             # TPAP (SPAKE2+/AES-CCM), the local protocol Tapo fw 1.4.2+ speaks and python-kasa cannot
│   ├── name_matcher.py          # Shared fuzzy hint -> key matching: exact, despaced, substring, token overlap
│   ├── media_service.py         # Generic Mi Box / Android TV ADB controls
│   ├── sentence_chunker.py      # Splits streamed LLM output into sentences
│   ├── stremio_service.py       # Stremio auth, sync, TMDB lookup, deep-link playback
│   ├── surfshark_service.py     # Route-based Surfshark VPN automation for YouTube and Stremio
│   ├── stt.py                   # Speech-to-text
│   ├── tts.py                   # Text-to-speech
│   ├── tts_text_sanitizer.py    # Text cleanup for TTS timing
│   ├── youtube_playlist_resolver.py # Matches voice playlist names and picks one saved ID at random
│   └── youtube_search.py        # Resolves a spoken query to the first video id over the public results page
├── hardware/
│   └── led_controller.py        # LED state feedback
├── training/                    # Wake-word training, runs on Modal, see training/README.md
│   ├── modal_train.py           # Modal app: setup / smoke / train entrypoints
│   ├── california.yaml          # Run 1 config, kept for comparison
│   ├── california_v2.yaml       # Current config: large head, wider TTS spread
│   └── california_smoke.yaml    # Tiny end-to-end pipeline check
├── tools/
│   ├── debug_surfshark_sequence.py # Runs named Surfshark routes with optional screenshot capture
│   ├── debug_surfshark_status.py   # Inspects current Surfshark status and route execution
│   ├── check_stremio_adb.py        # Read-only layered health check of the whole Stremio/ADB path
│   ├── run_stremio_e2e.py          # Live end-to-end Stremio routing and playback test
│   ├── run_youtube_playlist_e2e.py # Live end-to-end YouTube playlist routing test
│   ├── probe_govee_devices.py      # Lists Govee devices with sku, device id, and capabilities
│   ├── probe_tapo_devices.py       # Finds Tapo bulbs and prints their host; python-kasa's own CLI cannot run here
│   ├── pair_samsung_tv.py          # Pair/re-pair with the TV for CEC wake; needs on-screen approval
│   ├── score_wakeword.py           # Wake-word scores: live, recall (--dir), false positives (--negatives), threshold sweep
│   ├── wakeword_dataset.py         # Wake-word training data: record sessions, checks, review, audit, export
│   ├── probe_stremio_sync.py       # Refreshes and inspects Stremio watch-state cache
│   ├── debug_stremio_collections.py # Inspects raw Stremio collection payloads when sync is wrong
│   ├── search_youtube_playlists.py # Finds public YouTube playlist candidates by search query
│   ├── search_youtube_videos.py    # Finds YouTube video candidates and derives radio playlist IDs
│   ├── validate_youtube_playlists.py # Checks every saved playlist ID still exists (oembed); nonzero exit on failure
│   └── youtube_http.py             # Shared spoofed-UA fetch for the YouTube tools; the UA lives in services/youtube_search.py
├── tests/
│   ├── config_fixture.py        # THE test fixture base: the real config.yaml, deep-merged
│   ├── test_config_fixture.py   # Fixture behavior + guard against re-typed config values
│   ├── test_activation_capture.py # Activation clip naming and pruning
│   ├── test_activation_phrases.py # Wake tiers, echo stripping, recording trim, dropped turns
│   ├── test_govee_service.py    # Govee resolution, control payloads, and error mapping
│   ├── test_tapo_transport.py   # RGB->HSV, credential self-disable, live reads, no-network guard
│   ├── test_tapo_tpap.py        # SPAKE2+ handshake against a fake bulb, the encrypted channel, protocol routing
│   ├── test_device_discovery.py # DeviceFinder ladder, cache, ARP parsing; no-network guard
│   ├── test_media_power.py      # turn_on/turn_off, BT wake fallback, no blind KEYCODE_POWER
│   ├── test_media_service.py    # YouTube / ADB unit tests
│   ├── test_mic_drain.py        # Stale mic-buffer draining after playback
│   ├── test_speaker_session.py  # Per-turn OutputStream: block writes, stop-within-a-block, reopen after abort, tail on close
│   ├── test_reply_barge_in.py   # Wake word over a reply: interrupt, queue drain, own-name guard, idle-loop chaining
│   ├── test_orchestrator_lights.py # control_lights dispatch behavior
│   ├── test_orchestrator_vpn_routing.py # VPN preflight routing behavior
│   ├── test_stremio_service.py  # Stremio / TMDB / playback unit tests
│   ├── test_stt_hallucination.py # Whisper non-speech filler and segment-probability gating
│   ├── test_tts_chunking.py     # Terminal punctuation kept, fewer/longer chunks, two-sided audio trim
│   ├── test_surfshark_service.py # Surfshark route execution and cache behavior
│   ├── test_vad_silence.py      # Grace window, saw-speech flag, Silero framing
│   ├── test_wake_word_framing.py # openWakeWord native-frame buffering, consecutive frames, dither floor
│   ├── test_score_wakeword.py   # Scorer bed padding, determinism, ok-captures excluded from negatives
│   ├── test_wakeword_dataset.py # Dataset checks, transcript match, holdout leak guard, export
│   ├── test_name_matcher.py     # Matcher tier order, despacing, "&" normalization
│   ├── test_playlist_config.py  # Structural sweep of the real config.yaml playlist data
│   ├── test_youtube_playlist_resolver.py # Matching, aliases, and random-selection coverage
│   ├── test_youtube_search_autoplay.py # Query -> video id -> watch link, session-stamp verification, spoken lines
│   └── test_youtube_validator.py # Playlist existence classification (oembed + dead-page markers)
├── sounds/                      # Wake-word and activation audio assets
├── models/                      # Wake-word and other local models
├── deprecated/                  # Retired files kept for reference, see deprecated/README.md
├── device_state.json            # Generated locally, discovered device addresses
├── vpn_state.json               # Generated locally, Surfshark diagnostic cache
└── watch_state.json             # Generated locally, cached Stremio progress
```

Important runtime note:

- `watch_state.json`, `vpn_state.json` and `device_state.json` are generated cache
  files and should stay local; all are gitignored, so they will not show up as pending
  changes. `device_state.json` holds **both** the box and the TV, keyed by device, and
  is what makes a moved device self-heal -- deleting it costs one rediscovery (~0.5-2s),
  never a failure. `tv_state.json` is the TV's pre-2026-09-11 private cache; it is no
  longer read or written and can be deleted
- `sounds/bootup/`, `sounds/california_activations/`, `sounds/chime.wav`, and
  `sounds/error.wav` are generated audio and are **not** committed. This repo carries
  audio sources, not audio output. Run `generate_bootup_sounds.py` and
  `generate_activation_phrases.py` on a fresh clone. Both synthesize with whatever
  `config.yaml`'s `tts` block selects (the same `TTSService` the assistant speaks
  with), so the clips always match the live voice. Missing files are handled
  gracefully: the orchestrator skips the greeting and `sounds.generate_if_missing`
  recreates the chime
- **Pre-rendered clips live in one folder per voice**, named by
  `TTSService.voice_slug()` as `<provider>_<voice>`:
  `sounds/bootup/google_en-US-Chirp3-HD-Aoede/`,
  `sounds/california_activations/kokoro_af_bella/`, and so on. `config.yaml` picks
  which folder plays via `sounds.bootup_dir` and `sounds.activation_dir`. Switching
  voice is therefore three lines: `tts.provider` (+ its voice) and those two paths.
  The generators never overwrite another voice's set, so Bella's clips survive a
  move to Aoede and back. When you change voice, regenerate both sets **and** repoint
  both paths, or she acknowledges in one voice and answers in another
- Inside each voice folder, `california_activations/` holds `cold/` and `warm/`
  subdirectories plus a `manifest.json` of line text. A flat directory of WAVs is the
  pre-tier layout and still loads, into both pools; re-run
  `generate_activation_phrases.py` to get the split. The flat `.wav` files sitting
  directly in `sounds/california_activations/` are pre-tier Bella leftovers that
  nothing reads
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
  `cec`,
  `porcupine`, `silero`, `pi`, `govee`, `tapo`, and the aggregate `default`.
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

`config.yaml` now selects `tts.provider: google` (Chirp 3 HD, voice
`en-US-Chirp3-HD-Aoede`). That provider is plain REST over the core `requests`
dependency and needs no extra -- only `GOOGLE_TTS_API_KEY` in `.env`. Without the
key it falls back to Edge at init.

If you switch back to `tts.provider: kokoro`, remember `kokoro` is an **extra**, not
a core dependency, so a plain `uv sync` does not install it. Use:

```bash
uv sync --extra kokoro
```

or set `tts.provider: edge`, which is covered by the core dependency set.

The same applies to lights: `config.yaml` defaults to `govee.transport: ble`, which needs
`bleak` from the `govee` extra. Without it `GoveeService` logs a warning and disables itself,
and `control_lights` is not offered to the LLM at all.

The same applies a third time to the VAD, and this one hid for the life of the project:
`config.yaml` sets `vad.engine: "silero"`, which needs **both** `torch` and `torchaudio`.
`torch` arrives anyway as a kokoro dependency, `torchaudio` did not, and `core/vad.py`
catches the resulting `ModuleNotFoundError` and falls back to energy VAD with a warning
nobody reads. The config had been describing a VAD that never ran. `silero` is now part of
`default` for exactly this reason.

`uv sync --extra default` installs `kokoro`, `govee` and `silero` — which is what the
committed config actually selects. If a future config key selects a provider from an extra,
add that extra to `default` in the same change, or the config is lying again.

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
- **`sounds.activation_blocking` is `true`, and that is not the default this section
  describes.** With the mic open during playback, California's own line came back through
  the speaker above `barge_in_energy_threshold: 900`, so the gate read her as Master Miguel
  talking over her: it cut the line short and handed Whisper her own voice, transcribing as
  `"I'm with-"`. Blocking costs the ability to talk over her, which the warm tier makes
  cheap. To go back, raise `barge_in_energy_threshold` above the speaker's bleed level
  first — 900 is under it. `play_activation_sound` returns `duration = 0.0` when blocking,
  so the EchoGate window collapses to zero rather than waiting out a finished line

### The Microphone Buffer Is Not A Live Feed

`AudioPipeline.create_mic_stream()` is called once in `run()` and the stream is
never stopped until shutdown. It keeps capturing the whole time, and
`sd.RawInputStream.read()` returns the **oldest buffered frames**, not what the
room is doing now. So every stretch where the orchestrator is not reading —
the bootup line, a blocking activation line, the entire LLM and TTS response —
piles up in that buffer, and the next `read()` replays it.

- **This is why `sounds.activation_blocking: true` did not fix the overlap.**
  It really does wait for the line to finish. The line is then sitting in the
  mic buffer, and `_record_speech` reads it straight back. Observed live: a
  1.25s cold line, `Recorded 1.2s of audio`, transcript `"Yeah."` — her own
  tail, answered as a command. `EchoGate` cannot help here; with blocking
  playback `duration` is `0.0`, so the gate is pre-armed and inert
- **It is also a false-positive source.** After she finishes speaking a reply,
  `_idle_loop`'s next read hands her own voice to the wake-word detector
- `AudioPipeline.drain_mic_stream()` discards what is buffered, and
  `Orchestrator._drain_mic()` wraps it. It is called after the bootup sound, at
  the top of `_record_speech`, and after every activation completes. **Any new
  code path that plays audio and then listens must drain too**
- Draining is a no-op while the idle loop is running, because that loop
  consumes chunks in real time and the buffer stays near empty. Never drain
  inside the idle loop itself — that would throw away the wake word

### Listening: The Grace Window, and Why Silence Is Not An Input

Recording has two phases, and they are deliberately asymmetric. `core/vad.py`
tracks `saw_speech`, and the silence timer does not run until it is set.

- **Pre-speech.** From `start_recording()` until the first real speech, the only
  clock that runs is `vad.speech_timeout` (4.0s). If it expires,
  `should_stop_recording` returns `"no_speech"` and the turn is dropped: no
  Whisper call, no LLM call, no spoken reply, no LED change beyond going back to
  idle. Call California and take two seconds to think — that now works
- **Post-speech.** The old behaviour, except `min_recording` is measured from the
  first speech rather than from the top of the recording
- **`min_recording` used to be wall-clock elapsed, and silence counted toward it.**
  That is the entire bug: on pure room tone the stop fired at
  `max(min_recording 0.6, silence_duration 0.9)` ≈ 0.9s, and `_record_speech`
  returned a buffer of nothing. Whisper reliably hallucinates its silence filler
  out of that (`"Thank you."`, `"you"`, `"."`), the only pre-LLM guard was
  `transcript.strip() == ""`, so Claude answered it and California spoke. Every
  false wake cost a full turn and two API calls
- **`speech_start_frames: 2`** means two consecutive 40ms chunks above the energy
  threshold before the recorder decides he has started. A door or a keystroke is
  one chunk
- **`_record_speech` keeps its docstring's promise now.** It returns `None` on
  `"no_speech"` and on any recording under `vad.min_recording`, and
  `_handle_activation`'s `audio_data is None` branch — which was unreachable for
  the life of the function — aborts silently, resets the wake detector so stale
  audio cannot immediately re-fire, and rolls `_wake_count` back so a false
  positive does not burn the one long cold-open line
- **Only `"no_speech"` drops a turn.** A `"max_duration"` stop is still a command;
  a 30-second monologue gets transcribed

### Producer-Consumer Audio Pattern

`core/orchestrator.py` uses a two-stage speech output flow:

1. `_tts_worker` synthesizes sentences into audio
2. `_audio_player_worker` plays synthesized audio from a queue

Use `queue.Queue(maxsize=2)` for synthesized audio buffering so synthesis and playback overlap without growing memory usage.

### The Speaker Is Opened Once Per Turn

`sd.play` opens and closes a fresh PortAudio stream for every clip. Measured on
the dev laptop (MME host API, sounddevice `latency='high'`): ~180ms of primed
silence plus ~30ms of device open in front of **every** sentence, and in front of
an activation clip that `generate_activation_phrases.py` trims to a 20ms lead-in.
That is the hole between chunks and the missing first syllable of "Sup.".
`sd.play(blocking=True)` itself does *not* return early -- it returns clip length
plus ~100-180ms -- so the tail was never the problem; the start was.

`AudioPipeline.open_speaker()` now opens one `sd.OutputStream` when the wake word
fires, `_idle_loop` holds it through the acknowledgement, recording, thinking,
the whole reply and any turn chained onto it by a barge-in, and
`close_speaker()` writes `sounds.speaker_tail_ms` of silence then closes it. It
is per turn, not per process: the device is released while she is idle.
`play_audio` writes clips into that stream in `SPEAKER_BLOCK_MS` (40ms) blocks
and checks for a stop between blocks, which is what makes an interrupt land in
tens of milliseconds instead of at the end of the sentence. With no session open
(`_play_bootup_sound`, the manual test modes in `main.py`,
`activation_blocking: false`) it falls back to `sd.play` exactly as before.

**A stop stays in force until the next turn.** `stop_playback()` sets the abort
flag and `abort()`s the stream so the ~180ms PortAudio already holds is dropped;
`reset_playback()`, called at the top of `_handle_activation`, is the only thing
that clears it. `play_audio` must not clear it per clip: the player thread may
have already dequeued the next sentence when the stop lands, and clearing on
entry played that sentence in full. `tests/test_speaker_session.py` pins it.

### Interrupting Her Is the Wake Word, Not Loudness

Until 2026-09-16 barge-in over a reply was dead code: `_interrupted` was
assigned `False` in two places and `True` nowhere (BUG_AUDIT H1), and nothing
read the microphone while she spoke -- the main thread sat inside the LLM
stream, the mic buffer filled, and `_idle_loop` drained it afterwards. The 30s
`join(timeout=30)` calls on the TTS threads made it worse: a reply with more
than 30s of speech still queued when the LLM finished was abandoned, the
orchestrator went back to idle *while she was still talking*, fed her own voice
to the wake detector, and the next activation's `sd.play` cut her mid-word.

Now `_stream_response(user_text, mic_stream)` starts `_reply_listener` on a
thread for the length of the reply. It reads the mic and runs the same
`WakeWordDetector.process_audio` the idle loop uses, at
`wake_word.barge_in_threshold` (default: the idle threshold). A hit sets the
`_interrupted` Event, calls `stop_playback()`, and the turn ends: the LLM
generator is closed (`services/llm.py` catches `GeneratorExit` and stores the
partial answer so history never ends on a user turn), both queues are drained,
and `_handle_activation` returns `True`. `_idle_loop` then chains straight into
another `_handle_activation` on the same open speaker session, so "California,
no, the other one" is one motion. The joins have no timeout any more; a long
reply is spoken to the end, and the only way to cut it short is the wake word
or "stop".

- **Why the wake word and not energy.** `EchoGate`'s RMS barge-in already had to
  be switched off for the acknowledgement (`activation_blocking: true`) because
  her own voice through the speaker clears any energy threshold. The detector is
  trained on one word in his voice and scores hers near zero: **measured
  2026-09-16, 45s of her own lines recorded through this mic at the current
  volume (RMS ~480) peak at 0.053, zero fires at any threshold down to 0.2.**
  `barge_in_threshold` is therefore 0.5, well under the idle 0.81, and can go
  lower still without her waking herself
- **The model cannot hear him over her, and that is a training gap, not a
  runtime one.** Same session, same 40 real holdout takes (EN+PT): 10/40 fire
  clean at 0.81, **1/40** mixed with her voice at 0.81, 5/40 at 0.5, 9/40 at
  0.2. Those came off the bare-clip scorer (see the wake-word note at the top);
  re-measured 2026-09-23 inside a bed, with her Aoede clips mixed under the
  word, it is **28/40 clean and 0/40 over her voice** at bleed RMS 300 or 480,
  at 0.81 and at 0.5. The conclusion holds and is starker. Even a clean mix at
  bleed RMS 300 — below his own voice at ~840 — takes
  the median peak from 0.81 to 0.01. Ducking her volume once he starts was
  simulated before being built: cutting her to 15% or even to zero 150-400ms
  into the word recovers at best 7/40, because the model decides on the onset
  and the onset is already contaminated. Do not build a ducker. The fix is
  `augmentation.background_paths` in `training/california_v2.yaml`: her own
  clips (every `sounds/**/google_en-US-Chirp3-HD-Aoede/*.wav`, plus a batch of
  ordinary TTS sentences) as a background source, so the positives are heard
  over her voice in training. Until that run, expect "California" over her to
  land roughly one time in five, and read the `Reply listener: peak wake score`
  line logged after every reply to see how close it got
- **She cannot wake herself by saying her name.** `_audio_player_worker`
  publishes the text of the chunk it is playing in `_speaking_text`, and the
  listener ignores a hit while that text contains "California"
- **The listener is the only mic reader for the length of the reply**, and it is
  joined before `_idle_loop` reads again, so the stream never has two readers.
  Its errors are logged and end the listener; they can never take the turn down
- **A tool call cannot be interrupted, only silenced.** The LLM stream is
  consumed on the main thread and a `_dispatch_*` runs inside it, so a wake word
  during a 25s CEC wake stops her talking at once but the turn ends when the
  tool returns. Same as before; it is called out so nobody adds a thread for it
- `tests/test_reply_barge_in.py` covers the interrupt, the chaining, the
  own-name guard, the threshold override, and that a reply with no mic (the
  test modes) is a normal reply

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
- Power commands, which are asymmetric — see the section below
- Current-app and media-session inspection
- Current focus inspection, UI dump capture, and screenshot capture for TV debugging
- Screenshot-byte helpers and UI dump flows reused by Stremio scans so raw ADB calls do not hang indefinitely
- YouTube playlist and search deep links
- YouTube warm launch plus one OK press to clear the profile picker on cold starts

### Power: Off Is ADB, On Is The Television

**Turning the box off and turning it on are not symmetric.** Off is one ADB
keyevent. On goes through the TV, because the Mi Box suspends in standby and
`adbd` suspends with it. Measured on the real box 2026-09-03:

```text
adb shell input keyevent KEYCODE_SLEEP   -> ok, mWakefulness=Asleep
adb shell input keyevent KEYCODE_WAKEUP  -> error: closed
adb connect 192.168.1.35:5555            -> WSA 10060, every time
```

**Ping is not a reachability test here, and believing it wastes an hour.** The
box answers ICMP intermittently in standby — windows at t=39s, 48s, 54s after
sleeping, roughly one every 6-15s. That is the Wi-Fi firmware's offload replying
while the CPU stays suspended. Twelve `adb connect` attempts fired the instant
ICMP replied gave twelve WSA 10060s. `wifi_sleep_policy` is already `2` ("never
sleep") and `stay_on_while_plugged_in=3` does not prevent the suspend either —
both measured.

#### Dead routes, with evidence. Do not retry these.

| Route | Why it is dead |
|---|---|
| ADB wake | `adbd` suspends with the box; ICMP replies are firmware offload, not the OS |
| Wake-on-LAN **to the box** | No Ethernet. (The MAC caveat that used to sit here was wrong -- see the correction under "Finding the box" below. WoL is dead because there is no wired NIC, not because of the MAC) |
| BLE / `bleak` | The box does not advertise over BLE at all — a 20s scan beside it while awake sees nothing. The Govee transport is not reusable |
| Bluetooth Classic from Windows | `AF_BTH` Winsock `bind()` fails against the **local** radio with `WSAEADDRNOTAVAIL`; WinRT `PairAsync` refuses an undiscovered address, and a TV box is never discoverable. Five approaches tried |

The Bluetooth page to `9C:12:21:1C:95:AF` (what MiPower does) is a real mechanism
and needs a host this project does not have. It was built, then removed.

#### What works

`services/cec_wake.py`, two steps:

1. **Wake-on-LAN the Samsung TV.** Needs no IP and no token — it is a MAC
   broadcast — which is what makes the whole chain recoverable. Took 2 attempts
   in testing.
2. **Toggle the TV's HDMI input** over its WebSocket API. The TV emits
   `<Routing Change>` + `<Set Stream Path>` on the CEC bus and the box wakes.

**Step 2 is load-bearing.** The box was still unreachable after the TV powered
on and only woke after the input toggle, so WoL alone is not enough and the
pairing token is a hard dependency. That is why a rejected token gets its own
`WakeResult.needs_pairing` and its own spoken line — "approve me on screen" and
"use the remote" are opposite fixes, and giving the wrong one strands him.

- **`dumpsys hdmi_control` history is a capped ring buffer (~246 entries).**
  Counting entries to detect new CEC traffic reports `+0` and looks exactly like
  failure — read the *tail* instead. This nearly caused a working mechanism to be
  thrown away as broken
- **Sleeping the box switches the TV off too** (`mAutoTvOff: true`,
  `hdmi_control_auto_device_off_enabled=1`). Intended — "turn off the TV" means
  both — but it is why the wake must WoL the TV first
- The Mi Box is CEC physical address `0x2000` = **HDMI 2**. `KEY_HDMI` toggles
  HDMI2 <-> HDMI1 on this set, so **odd presses select the box**
- `tv_ip` is a **cached hint, not configuration.** Identity keys off `tv_mac` and
  `tv_duid`, both stable. On a miss `resolve_tv_ip()` goes cached -> hint -> ARP by
  MAC -> TCP scan on :8001, verifying the duid at every step, and caches the answer
  under `tv` in `device_state.json`. **The duid check is the point**: without it a
  stale IP that another device has since taken would answer 200 and be trusted
- **`mibox_ip` is now the same kind of hint**, for the same reason. See "Finding the
  box (and the TV) when the address moves" below
- Re-pair with `uv run python tools/pair_samsung_tv.py`. It WoLs the TV first,
  because approval needs the TV on and a person in front of it

**`KEYCODE_POWER` is never sent, in any state.** It is a *toggle*, so firing it at
an already-awake box turns the TV off — and on this hardware that ends every other
form of control until someone picks up the physical remote. `power_toggle` did
exactly that, three times, during this work. `MediaService` is now state-aware:

| `is_awake()` | meaning | `turn_on()` does |
|---|---|---|
| `True` | awake | nothing — returns success |
| `False` | asleep, radio up | `KEYCODE_WAKEUP` |
| `None` | unreachable | CEC wake through the TV, then wait |

`None` and `False` take different branches. **Do not collapse them into a
boolean** — that is the whole wake path.

**The power actions are deliberately absent from `_dispatch_tv`'s `requires_tv`
set.** That gate returns `"TV is off or unreachable right now"` when
`ensure_connected()` fails, which is precisely the state `turn_on` exists to fix.
`wake` used to be listed there, a second and independent reason it was dead code.

**Reachable is not ready, and this is the difference between a wake that
works and one that quietly does nothing.** `adbd` comes up early in boot, so
`ensure_connected()` succeeds against a box that cannot launch an app yet --
and a Stremio deep link fired into that window is a launch that fails
silently. `_wait_for_box` therefore polls `getprop sys.boot_completed` once
the connection returns, and only reports success when Android says it is up.
Verified present on the real box (Android 11, SDK 30).

That also changes what `settle_ms` means: it is an **upper bound**, not a
fixed wait. A fast boot returns early instead of spending the whole budget.

**The check fails open on purpose.** An unreadable `getprop` returns `None`
and is accepted, because the previous behaviour accepted the connection by
itself -- a box that will not answer the property must never end up worse off
than before the check existed. `None` is "cannot tell", never "still
booting": treating it as the latter would burn the entire settle window
waiting for something that already happened.

`_wait_for_box` clears `_last_fail_time` on every poll, because
`ensure_connected()` stamps it on each miss and then refuses to retry for
`_OFFLINE_COOLDOWN` — right in normal operation, wrong while waiting out a boot.

### Finding The Box (And The TV) When The Address Moves

**No address in `config.yaml` is authoritative.** `media.mibox_ip` and
`media.cec_wake.tv_ip` are *hints* that seed a cache. Identity keys off things that do
not move -- MAC plus `ro.serialno` for the box, MAC plus duid for the TV -- and nothing
at runtime ever rewrites `config.yaml`.

This exists because the box drifted `192.168.1.35` -> `.40` on a plain DHCP renewal
(no reboot, 9.6 days of uptime) on **2026-09-05**, and every `control_tv` call started
returning "TV is off or unreachable right now" until the file was hand-edited.

`services/device_finder.py` is the shared ladder for both devices:

```text
memory -> cache (device_state.json) -> config hint -> candidate sources
                                          ^ every rung verified before it is believed
```

The two devices differ only in **`verify`** and **candidate sources**, both injected --
there is no `if device == "tv"` in the module:

| | verify | candidate sources |
|---|---|---|
| Box | TCP 5555 open -> `adb connect` -> `ro.serialno` matches | warm ARP -> TCP scan on 5555 |
| TV | TCP 8001 open -> HTTP `:8001/api/v2/` -> `duid` matches | warm ARP -> TCP scan on 8001 |

**The TV was not actually on this ladder until 2026-09-11.** This table described it
as shared while `cec_wake.py` still carried a private copy: an HTTP probe with a 4s
timeout and no port gate, cached and hint tried in series, a 254-host ping flood, and
then an HTTP GET to every host on the /24. Against a TV that is off none of those
refuse -- they time out -- so one miss cost ~35s (measured in the 2026-09-11 log:
16:46:17 -> 16:46:48 to find a TV that had moved `.34` -> `.59`). On the shared ladder
the same rediscovery measures **0.47s**: hint rejected in 0.30s by the port gate, ARP
hit verified in 0.06s. `CecWaker._probe` is the TV's `verify`, port-gated exactly like
`_verify_box`, and `media.discovery.port_probe_timeout_ms` / `scan_workers` now govern
both devices.

`power_on_tv`'s poll loop follows the `_wait_for_box` rule: **every miss rediscovers,
then retries**, polling on `cec_wake.poll_interval_ms`. The one deliberate exception is
the check *before* Wake-on-LAN (`_tv_is_up`), which probes known addresses only
(`resolve(discover=False)`): a TV that is off cannot be found by any scan, so a scan
there only delays the packet that turns it on.

#### The 21-second rule. This is the load-bearing constraint.

Measured on the real box, 2026-09-05:

| operation | cost |
|---|---|
| `adb connect` to a host with 5555 **closed** | **21.1s** — adb's own timeout, not tunable |
| `adb connect` + `getprop ro.serialno` (right box) | 0.37s |
| `adb -s <target>` with no open transport | 0.21s |
| parallel TCP connect scan of a whole /24 on 5555 | 1.55s -> exactly 1 candidate |
| `arp -a` grep for a known MAC, warm | 0.16s |

**Discovery must never hand an unverified host to `adb connect`.** A 0.3s socket probe
(`device_finder.port_open`) gates every rung; without it a /24 sweep costs 254 x 21s.
Never raise `media.discovery.port_probe_timeout_ms` toward adb's own timeout "to be
safe" — that reintroduces exactly the cost the gate exists to prevent.

`MediaService.connect()` carries the same gate, which independently fixed
`_wait_for_box`: it is meant to poll ~12 times across `settle_ms: 25000` and was
managing one or two, because every failed poll burned 21s.

#### Corrections to what this file used to say

- **The box's Wi-Fi MAC is stable within an SSID.** The dead-routes table called
  `16:da:99:37:d0:89` "unstable across reconnects". Android randomises per *network*,
  not per *reconnect*: the value measured on 2026-09-05 is byte-identical to the one
  recorded weeks earlier and to the live ARP entry. It changes on a new SSID or a
  factory reset — not on a DHCP renewal, which is the only case discovery exists for.
  **Nothing depends on it anyway**: the MAC is one fast rung, and the port scan finds
  the box by serial with no MAC at all.
- **`self.target` is no longer frozen.** It is a property over a mutable `self.ip`, so
  the ~30 `ensure_connected`-gated call sites follow a rediscovery with no edits.
  `StremioService.adb_target` is likewise a property delegating to the injected media
  service — it used to snapshot config at construction, so a rediscovered box would
  never have reached its standalone ADB path.

#### Cache file

`device_state.json` (gitignored), keyed by device, read-modify-write via `os.replace`:

```json
{"version": 1,
 "devices": {"mibox": {"ip": "...", "mac": "...", "verified_at": "..."}}}
```

Both devices live in this one file (`mibox` and `tv`). **Do not make `_remember` a
full overwrite.** The old `cec_wake._remember_ip` wrote `{"tv_ip": ip}` over the whole
file; pointed at a shared cache, each rediscovery would erase the other device's entry.
`media.cec_wake.tv_state_path` still exists as an override to give the TV its own file
(the tests use it) but is commented out in the shipped config. A cached entry whose stored MAC no longer matches
config is discarded rather than probed — a new SSID invalidates the address for the
same reason it changes the MAC.

#### "Unreachable" is four different problems

They need opposite responses, and they used to produce one identical sentence.
`MediaService.unreachable_reason` feeds `core.orchestrator._unreachable_line`:

| reason | meaning | spoken |
|---|---|---|
| `cooldown` | a connect failed seconds ago | "unreachable a moment ago, give me a few seconds" |
| `not_on_lan` | MAC absent from ARP after a full scan | "the box is off, I can't see it on the network" |
| `no_adb_port` | on the LAN, nothing listening on 5555 | "ADB over Wi-Fi needs turning back on" |
| `identity_mismatch` | something answers 5555, wrong serial | "something else has the box's address" |

Telling `not_on_lan` from `no_adb_port` is free: the TCP scan SYNs every host, which
populates the ARP table for everything that exists, so classification is one 0.16s
re-read.

**`no_adb_port` is the `persist.adb.tcp.port` case and discovery cannot fix it.**
`service.adb.tcp.port` is `5555` on this box but `persist.adb.tcp.port` is empty, so
ADB over TCP may not survive a reboot. No amount of rediscovery re-enables it — it
needs Developer options or `adb tcpip 5555` over USB. Say that instead of blaming the
network.

#### Two cooldowns, and they are not the same cooldown

`_OFFLINE_COOLDOWN` (30s) answers "don't retry a connect that just failed".
`media.discovery.rescan_cooldown_ms` (120s) answers "don't rescan a LAN we just
scanned". They compose rather than override: on the **first** miss after a drift there
is no offline cooldown yet, so the call falls straight through to discovery and
self-heals. Do not merge them.

`_wait_for_box` rediscovers on **every** miss, then retries. It used to suppress
discovery for the whole settle window -- "the box is at a known address, scanning each
poll is waste" -- and rediscover once after the timeout. On **2026-09-11** that cost
47s: the box came back from a CEC wake on a new lease (`.47` -> `.60`) and the loop
polled the dead address until its deadline, then found the box in 2s. The suppression
was justified when the expensive step was `adb connect` (21s); with the port-probe gate
a full rediscovery is ~1.7s against a 2s poll interval, cheaper than one wasted poll.
The loop clears **both** cooldowns (`_last_fail_time` and `_last_discovery_t`) before
each pass, for the same reason it always cleared the first, and keeps calling the
*public* `ensure_connected()` that tests patch by name. On this router a lease change
after sleep is the common case, not the edge case -- a DHCP reservation for the box's
MAC is the real fix, and it costs no code.

-----

### SurfsharkService### SurfsharkService

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
| `tapo` | TP-Link Tapo bulbs on the LAN. **The only transport that can be read back.** Speaks TPAP natively; see below | `TAPO_USERNAME` / `TAPO_PASSWORD` | `uv sync --extra default` |

**The transport is chosen PER LIGHT, from the fields each one carries.** `mac` means
BLE, `host` means Tapo, `sku` + `device` means cloud. So the attic Govee strip and the
living-room Tapo bulb are both live in the same run, which is the shipped state since
2026-09-17.

Until that date `govee.transport` selected one transport for the entire service and
`_build_lights` skipped every light missing that transport's required field. Switching
to `tapo` for the L530E therefore silently dropped the attic -- it loaded zero lights
it could drive, logged one warning nobody reads, and `_light_inventory()` stopped
advertising the room at all, so "turn on the attic" answered *"I don't have a light
called attic saved."* One room per run was never a design decision, just the shape the
first transport happened to have.

What `govee.transport` still does, and why it cannot simply be deleted:

- **It is the tie-break** for a light carrying fields for more than one transport. The
  cloud test fixture's attic has a `mac` AND a `sku`/`device` pair; pure field
  inference would reroute it to BLE and quietly stop using the cloud the operator
  configured. The preferred transport is tried first, the others only after.
- **It names the requirements in the skip warning** for a light carrying fields for
  none. Telling someone setting up a Govee strip that their light is "missing host"
  would send them entirely the wrong way.

**One transport instance is shared by every light that uses it**, and that is
load-bearing rather than frugal: `BleTransport` caches the `BLEDevice` it found and
`TapoTransport` caches a per-host protocol decision and a live TPAP session. A fresh
instance per light would throw both away on every command.

**`enabled` means "at least one light can be driven"**, not "the primary transport came
up". With two transports in play, a missing `bleak` must not disable a Tapo bulb that
is working perfectly well. `can_read_state` is the same shape -- "some light can be
read" -- and the per-light answer lives in `get_state`, which returns `None` for a
light whose own transport has no `get_state`. The caller already treats `None` as
"fall back to shadow memory", so a mixed setup needed nothing else.

**Why BLE is still the tie-break in the shipped config.** Master Miguel's attic strip is an **H617E**, which is
BLE-only. It is absent from Govee's supported-model list, so `GET /user/devices` returns
`code: 200, "success", data: []` with a perfectly valid API key, and it never joins Wi-Fi
at all, so the LAN API cannot see it either. Its setup flow pairs over Bluetooth and never
asks for an SSID, which is the tell. The cloud transport is kept for any future whitelisted
device and is fully tested, just unused.

### Tapo: the bulb speaks TPAP, and python-kasa does not

**Firmware 1.4.2 (Build 260113, early 2026) moved Tapo bulbs to a new local
protocol, and the library this transport was built on cannot talk to it.**
Discovery on the living-room L530E(EU) at `192.168.1.74` answers
`encrypt_type='TPAP', http_port=80, lv=2`, and python-kasa 0.10.2 -- the newest
release -- raises `UnsupportedDeviceError` and stops. Upstream is stuck:
[python-kasa#1590](https://github.com/python-kasa/python-kasa/issues/1590) is open,
its two PRs ([#1592](https://github.com/python-kasa/python-kasa/pull/1592),
[#1706](https://github.com/python-kasa/python-kasa/pull/1706)) are unmerged and
incomplete, and Home Assistant's integration has the same open bug
([home-assistant/core#167990](https://github.com/home-assistant/core/issues/167990)).
The python-kasa authors believed the handshake needed cloud-issued certificates
(NOC); it does not for `tls: 0, dac: 0` bulbs like this one.

**The working reference is a .NET library, and `services/tapo_tpap.py` is a port
of it.** [KasaTapoClient](https://github.com/oznetmaster/KasaTapoClient) (MIT,
Neil Colvin) has TPAP running locally over plain HTTP with the L530E(EU) on its
confirmed list. Its `TpapTransport.cs` is 1,800 lines because it covers cameras,
hubs, TLS and DAC; the port is the ~150 lines a bulb needs. Verified against the
real bulb 2026-09-17, handshake to colour, through the real `GoveeService` and
`_dispatch_lights`.

The protocol, so nobody has to re-derive it from C#:

1. `POST /` `{"method":"login","params":{"sub_method":"discover"}}` -- no auth.
   Returns the MAC and a `tpap` block: `pake: [2]` means "account password".
2. `pake_register` -- we send 32 random bytes; the bulb returns its SPAKE2+ share,
   a PBKDF2 salt, `iterations: 3000`, and `extra_crypt`
   (`password_shadow`/`passwd_id: 2` = the password is SHA-1-hexed first).
   The `username` field is `md5("admin")`, not the account email.
3. `pake_share` -- SPAKE2+ on P-256 with the RFC 9383 M/N points, w0/w1 from
   PBKDF2-SHA256 over the hashed password (80 bytes, split 40/40, each mod n).
   Transcript is `PAKE V1` context hash then eight-byte little-endian
   length-prefixed fields; `ConfirmationKeys` and `SharedKey` come out of HKDF
   over its hash. We check the bulb's `dev_confirm`; it hands back `stok` and
   `start_seq`.
4. `POST /stok=<stok>/ds` with `[seq:4 BE][AES-128-CCM, 16-byte tag,
   nonce = base_nonce[:8] + seq]` around the ordinary smart-protocol JSON --
   `get_device_info`, `set_device_info {device_on|brightness|hue,saturation}` --
   the same bodies python-kasa sends over KLAP. Plain JSON back on that endpoint
   means the session is dead.

**Every constant is load-bearing.** The M/N points, the `PAKE V1` tag, the
little-endian `len8` prefixes, the sign-byte encoding of w0, the
`tp-kdf-salt-aes128-key` / `-iv` HKDF strings: change any of them and the
`dev_confirm` check fails, which is the right failure but says nothing about why.
`tests/test_tapo_tpap.py` runs the *bulb's* side of SPAKE2+ against the port with
the module's own curve helpers, so a broken constant fails offline.

**How the transport decides.** `TapoTransport._with_device` is still the one
network boundary. On the first command to a host it sends the discover POST
(`tapo_tpap.probe`, ~30ms) and remembers "tpap" for the process; anything else
falls through to python-kasa, and "kasa" is remembered only once a kasa command
*succeeds* -- a bulb switched off at the wall fails the probe too, and caching that
as "kasa" would send every later command down the wrong path once it came back.
The three writes, `get_state`, the worker thread and the retry loop are
protocol-blind; `TpapDevice` wears the slice of kasa's `Device`/`Light` surface
the actions use, so the actions and their tests are untouched.

**The TPAP session is cached per host.** The connect-per-command rule above is
about kasa `Device` objects being bound to the event loop that made them; TPAP
here is synchronous `requests` with no loop, so a session is safe to keep. A
failed command drops it and the retry handshakes afresh. Measured on the
laptop: **2.4s for the first command** (probe + three login round trips + five
pure-Python P-256 scalar multiplications), **60-300ms per command after**.

**Discovery: `255.255.255.255` finds nothing on this laptop, `192.168.1.255`
finds the bulb every time.** Windows sends the global broadcast out the first
adapter, which here is a virtual one on `192.168.128.0/20`. python-kasa's
`Discover.discover` also *drops* TPAP bulbs silently as unsupported, so it
reported "no devices" with a bulb answering on the LAN.
`tools/probe_tapo_devices.py` now broadcasts to the global address and every
local /24's directed broadcast, collects unsupported hits through
`on_unsupported`, and describes each host through the transport (so a TPAP bulb
prints as `l530e sala (L530, tpap) at 192.168.1.74 -- on at 100%`). A bulb that
is off at the wall is not on the network at all, and the Tapo app showing it
online means nothing about the LAN -- the app goes through the cloud.

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

`match_name` now has a despaced tier of its own, sitting between exact and substring, which
makes `resolve_color()`'s manual despaced alias strictly redundant. **It stays anyway**: it keeps
`resolve_color` correct without depending on `name_matcher`'s tier list staying in its current
order. Still do not remove either. `tests/test_name_matcher.py` pins the ordering.

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
- **Search plays the first video result, it does not stop at the results page.** See below

Cold-start behavior:

- If YouTube is already foreground, California does not relaunch it before opening the requested playlist or search
- If YouTube is not foreground, California warm-launches YouTube first, waits briefly, presses OK once for the profile picker, then opens the target URL

### "Search for X and play it": the results page never plays

Reported 2026-09-14: "search YouTube for the hottest hits of J. Cole and play
the first thing" searched correctly and never played. The results deep link
(`/results?search_query=`) stops at the results page, and measured on the real
box it **never starts anything on its own** -- 20s on the page, media session
untouched.

Pressing into it blind was tried first and is the wrong primitive, for three
measured reasons:

- **The top result is not always a video.** For "anderson paak hottest hits"
  it was a playlist: one OK opened the playlist *page* (screenshot confirmed),
  a second OK would have been needed on its first item. Channels and Mixes are
  different again. The YouTube TV app is a Cobalt web view, so `uiautomator`
  returns one full-screen node and cannot say which
- **A second OK inside the player is play/pause.** The retry paused the very
  thing the first press had started
- **The previous video keeps playing under the results page** (`dumpsys audio`
  AudioTrack stays `started`), so no live reading separates "OK did nothing"
  from "OK picked the video that was already on"

`services/youtube_search.py` resolves the query instead: it fetches the
public web results page and takes the first `videoRenderer` -- the same
`ytInitialData` scrape `tools/search_youtube_videos.py` has used since day
one, now shared. No API key, no account, no quota; ~1s from the laptop. The
box then gets `watch?v=<id>`, which **plays directly**, publishes the title in
the session ~2s later, and **restarts from zero with a fresh stamp even when
that video is already on**. No key press anywhere on the path.

`MediaService.youtube_play_video()` confirms it the only way the box allows:
`dumpsys media_session` before the link and after, compared by the
`updated=` stamp. This is load-bearing -- **a session an app walked away from
is reported verbatim**, state=3, same stamp, same title, for as long as
nothing new is published, so "is YouTube playing" was already true before the
link and proves nothing. A changed stamp does. Some videos never publish a
description (`null, null, null` with the position advancing), so the title
gets a 1.5s grace after the stub, not the whole verify window; the spoken
title comes from the scrape anyway, cut at the first `|` and stripped of
`[tags]` by `speakable_title`.

Fallbacks, each with its own line: a resolve that fails (no internet, consent
wall, markup change) opens the results page and says "pick one with the
remote"; a link that went but a session that never moved gets Stremio's
"didn't start on its own, hit OK"; an unreadable session is "couldn't
confirm", never "playing". `youtube_search_autoplay: false` restores the bare
results page.

### Playlist Matching Rules

`services/youtube_playlist_resolver.py` is responsible for turning a spoken request into a saved playlist launch:

- exact category matches win first
- then exact matches once spaces are removed, so "road trip" reaches the `roadtrip` key
- then partial string matches
- then token overlap matches for near-phrases like "beach samba" or "old school hits"
- if a category resolves, one playlist ID is chosen from that category's saved list
- if nothing resolves confidently, the assistant offers a YouTube search instead of guessing

**Aliases live in a separate top-level `youtube_playlist_aliases` map**, not nested inside
`youtube_playlists`. That is deliberate: `youtube_playlists` has four readers, and two of them
(`tools/validate_youtube_playlists.py`, `tools/run_youtube_playlist_e2e.py`) hand-roll the
str-or-list parse. A nested shape would make the validator *silently skip* every aliased
category and crash the e2e tool. Keeping the block a plain key -> IDs map also means
`LLMService._playlist_inventory()`'s "advertise exactly what the resolver accepts" invariant
cannot be broken from here. `resolve_playlist_choice(hint, playlists, aliases)` merges them.

**Prefer bucket nouns over bare adjectives when adding an alias.** The substring tier scans in
config order, so an adjective on an earlier category steals `"<adjective> <genre>"` from a later
one: `"moody"` on `dark romance` sends `"moody jazz"` to dark romance instead of `jazz`. Measured
and rejected for the same reason: `"classics"` on `legendary hits` steals `"80s classics"`, and
`"r&b"` normalizes to `"rb"`, which the substring tier finds inside "he**rb**ie" and "u**rb**an" —
`normalize_text` expands `&` to " and " instead, so `"R&B"` resolves with no alias needed.
`tests/test_playlist_config.py` asserts every configured alias still resolves to its own key.

**The category list is injected into the system prompt**, exactly like the light rooms and
for the same reason: "what playlists do you know" is an inventory question, and the model
could not answer it from a tool schema that only takes a free-text name. Master Miguel asked
and she did not know. `LLMService._playlist_inventory()` builds the line from
`config.yaml`'s `youtube_playlists`, gated on `media.enabled`, filtering through the
resolver's own `playlist_ids()` so a category with no usable ID is never advertised —
the prompt must offer exactly what `resolve_playlist_choice` would accept. **Do not
hardcode playlist names in the `system_prompt`.**

**There is no YouTube account integration, and the prompt says so.** No OAuth, no Data API,
no access to his subscriptions, liked songs, or account playlists. The saved categories are
curated IDs that play on the Mi Box's own signed-in YouTube app, which is why his private
playlists work at all — adding one is a `config.yaml` edit, not a code change. The
`system_prompt` also routes "put on X" / "play X" / "what can you play" to the TV rather
than letting her answer as if they were questions about her own abilities.

### Playlist Curation Workflow

The project now uses a stricter workflow for YouTube playlist data because random public IDs often point to unrelated content:

1. Find candidates with `tools/search_youtube_playlists.py` or `tools/search_youtube_videos.py`
2. Prefer strong video-based radio seeds for vibe-heavy categories when normal playlists are unreliable
3. Validate every candidate with `tools/validate_youtube_playlists.py`, which proves existence
   through YouTube's oembed endpoint and exits nonzero when anything is gone
4. Only keep IDs whose fetched title clearly matches the intended category — that comparison is
   the separate `--strict-title` pass, and it is advisory (see below)

This matters because the playlist ID itself is not trustworthy. The fetched title is the real check.

### Current Playlist Strategy

The current saved categories in `config.yaml` include:

- Brazilian vibe buckets such as `samba`, `pagode`, and `pagode praia`
- mood buckets such as `rnb`, `sex songs`, and `dark romance`
- nostalgia buckets such as `70s 80s 90s hits` and `legendary hits`

Many of the newer entries are `RD...` radio playlist IDs rather than community playlist IDs. That is intentional. They were easier to verify semantically and are often a better fit for vibe-based voice requests.

**An RD id is validated through its seed video, and that is all it proves.** `RDxxxx` is an
auto-generated mix seeded from one video, so every HTTP check runs against that seed. `ok` means
the seed still exists — never that the mix still sounds like the category. YouTube regenerates
those mixes and they drift; only a human listening can catch that. This is also why
`--strict-title` is advisory and off by default: oembed returns the *seed's* title, so
`samba/RDc4XeTP11EI8` comes back as "Grupo Revelacao - Deixa Acontecer", which shares no word
with "samba". A strict check by default would flag most healthy entries.

**IDs rot, and the old validator could not see it.** On 2026-09-03 a sweep found **6 of 54
saved IDs already dead** (404): two each in `rnb` and `legendary hits`, one each in
`dark romance` and `70s 80s 90s hits` — so a third of `rnb` and `legendary hits` requests were
launching a dud. All six were replaced the same day and the sweep is clean. Re-run the validator
after any curation pass; it is the only thing standing between a rotted ID and a dead-air
request.

Recommended behavior:

- Prefer playlists for known repeated requests like samba, lofi, workout, chill, or jazz
- If a playlist is unknown, ask before falling back to a generic search

### Operational Recommendation

A wakelock app on the Mi Box does **not** help: the firmware force-suspends ~15s after sleep while listing the wakelocks it ignores (measured 2026-09-21). The deployment-side lever is `media.power.tv_only_standby`, which needs no box setting — see CLAUDE.md "Standby depth". Both devices have static addresses outside the DHCP pool since 2026-09-22 (box `192.168.1.200`, device MAC `9c:12:21:1c:95:ae`; TV `192.168.1.201`) — see CLAUDE.md "Static addresses".

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
- Custom `control_tv` tool for Mi Box and TV control (23 actions)
- Custom `control_lights` tool for Govee light control (5 actions)

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
- `light_status` (a live reading on `tapo`, shadow memory on `ble`/`cloud` -- see Reading State Back)
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
- `turn_on`, `turn_off`, `switch_hdmi`
- `get_status`
- `stremio_play`, `stremio_continue`, `stremio_get_progress`, `stremio_sync_library`
- `youtube_playlist`, `youtube_search`

When tools are active:

- Stream only spoken text to TTS
- Do not read raw `tool_use` or `tool_result` structures aloud
- Keep tool confirmations short and natural
- Treat plain show requests for `stremio_play` as "sync the library, then resume the latest tracked episode"

-----

## Reading State Back

She could control all three devices and read almost nothing back. What she can
read, and what she deliberately does not pretend to, is now the point.

### The television's power is on the CEC bus, never in its REST reply

`get_status` used to report TV power from `cec_waker._tv_is_up()`. That probes
`:8001/api/v2/`, **which this set answers in standby**, so a television that was
off came back as "TV: on" for the life of the feature. `cec_wake.py` already
said not to: "The set exposes no PowerState field at all... What we must NOT do
is treat the answer as 'powered on'." `_tv_is_up` verifies an *address*. It is
not, and can never be made into, a power check.

`MediaService.tv_power_status()` is the real oracle, and it reads **two kinds of
evidence, whichever came last**:

- `<Report Power Status>` is an **answer**. The only thing that asks is the
  box's own wake sequence -- in `tests/fixtures/hdmi_control_dump.txt` every one
  sits a second after a `<Give Device Power Status>`, and nothing polls
  periodically. On its own it therefore cannot see Master Miguel reaching for
  the television's remote, and goes hours stale.
- `<Standby> 0F:36` is **volunteered**. The TV broadcasts it on its way off and
  the box logs it even though it was not addressed to us. Confirmed on the real
  box 2026-09-08: nine power-off cycles in one capture, every one logged
  (`tests/fixtures/hdmi_control_standby_dump.txt`). This is the only thing that
  makes "he turned it off himself" observable, and without it that capture reads
  as `on` from four hours earlier with three `<Standby>` broadcasts after it.

`[S]` is the box talking, never the television -- counting our own `<Standby>`
or `<Report Power Status>` reports the box's state as the TV's.

The staleness gate stays as a backstop, because a television can be switched on
again at an input the box never hears about. `_parse_tv_power` compares the
winning line's timestamp against the newest line in the log -- both off the
box's own clock, so no host timezone is involved -- and returns `None` past
`_TV_POWER_MAX_AGE_S`. In practice it does not fire during normal operation: an
idle CEC bus is silent, so the newest line IS the wake burst that carries the
power report.

### `ensure_connected()` is a live ADB ping, which is why `room_status()` exists

Every public reader starts with `ensure_connected()`, and that fires a real
`adb shell echo ping` **every call**. Composing status out of `is_awake()` +
`hdmi_state()` + `get_current_app()` + `is_playing()` therefore costs eight round
trips to answer one question. `room_status()` is the one method allowed to read
dumpsys without re-entering the gate, and short-circuits on a sleeping box
because nothing is on screen to ask about. Do not "tidy" it into calls to the
four public readers.

`is_active_source()` and `tv_power_status()` used to run their own
`dumpsys hdmi_control` over identical output. Both now delegate to
`hdmi_state()` and keep their names, signatures and `None` semantics, because
`ensure_active_source()` and `_ensure_playable()` call them.

**`None` means "could not tell", and a field she could not read is a clause she
does not say.** The old branch printed "unknown" for exactly the fields it had
failed to read, which is the least useful thing to say out loud.

### The box does publish a title, and there is more than one session

Captured off the live box on 2026-09-08 with Stremio playing
(`tests/fixtures/media_session_dump.txt`), and it corrected two assumptions this
feature was designed around:

- **Stremio publishes the show AND the episode.**
  `metadata: size=4, description=Fallout, The Strip, null` -- a MediaDescription
  printed as "title, subtitle, iconUri", with unset fields as the literal
  "null". So "what is playing" is readable after all, and unlike the launch
  memory it survives him starting something with the remote. **YouTube
  publishes too** -- corrected 2026-09-14: `description=Port Antonio, J. Cole,
  null` is title and channel -- but not for every video (some sit at
  `null, null, null` with the position advancing), which is why the memory
  below still earns its place.
- **Sessions are plural.** Spotify held an active session at `state=0` the whole
  time Stremio was playing. A flat `"state=3" in dump` therefore answers a
  different question than the one asked and would hand Spotify's playback to
  whatever is on screen. `_parse_media_sessions` splits the stack on `package=`
  and `room_status()` scopes to the foreground app's package. An app holding no
  session of its own is not playing -- do NOT reintroduce an `any(...)` fallback
  there, that is the bug.

`StremioService._is_playing` keeps its own flat check on purpose: it has a
standalone-ADB path for when no MediaService exists, and its bool return is
load-bearing in the autoplay retry.

### What else the box will tell you, and what it will not

Measured on the real box 2026-09-09. `room_status()` reads five dumpsys in
one connection, ~0.9s of ADB in total:

| read | costs | gives |
|---|---|---|
| `dumpsys power` | 0.30s | `mWakefulness` |
| `dumpsys hdmi_control` | 0.11s | active source + TV power |
| `dumpsys window displays` | 0.11s | foreground app |
| `dumpsys media_session` | 0.11s | playback state, title, position |
| `dumpsys audio` | 0.13s | STREAM_MUSIC volume and mute |

**Position yes, duration no.** The session prints
`position=240301` in ms, but `metadata: size=4` prints only the description
-- there is no length anywhere in the dump. So elapsed time is available and
**a percentage or a "time left" is not**, and nothing may imply otherwise.
`position=-1` means stopped, not the start of the film, and is discarded.

**The playback word is read, never guessed.** The session state is an int
(`_PLAYBACK_WORDS`): 0, 2 and 3 observed on this box, the rest documented
Android values. Without it the title is dropped rather than pinned to a
guessed verb -- "paused on X" must mean the box said paused.

**Volume is the BOX's, not the television's.** `_parse_volume` scopes to the
`- STREAM_MUSIC:` block, which matters: `STREAM_VOICE_CALL` sits above it
with a Max of 5 and `- VOLUME GROUP AUDIO_STREAM_MUSIC` below with its own
numbers, so a loose scan picks up whichever it meets first. This is the
volume `volume_set` moves, which is what makes it worth reporting. The
Samsung's own volume is not readable over ADB, and on this setup the box
often sits pinned at 15/15 while the room's loudness is ridden from the TV
remote -- so treat a box volume of max as "not the reason it is quiet".

### She also knows what she put on, as a fallback

For everything that publishes no metadata, `services/now_playing.py` remembers
the one thing she launched, in the words she used, under two rules:

- **Corroborated.** `current()` returns nothing unless the live foreground app
  still matches, so a memory of a Stremio episode is dropped the moment he moves
  to YouTube rather than narrated over it.
- **`kind` separates "playing" from "opened".** A YouTube search and a Stremio
  series page were put on screen, not played. With no matching memory she says
  she did not start it, rather than guessing.

The session title wins where both exist; the memory is the fallback.

Recording happens in `_dispatch_tv`, not in the services, because the speakable
label only exists there: `StremioService` holds an IMDb id and a deep link, and
`youtube_playlist()` holds an opaque `PL...` id. An opaque id is deliberately
not remembered at all rather than read out loud later.

### The lights cannot be read, and saying so is the feature

**Corrected 2026-09-17: that is true of the Govee strip, not of every light.**
A TP-Link Tapo bulb answers, so `govee.transport: "tapo"` makes `light_status`
a real reading and `_light_reading_line` states it with none of the hedge below.
Everything in this section still holds for the attic strip over `ble` and for `cloud`.
Both rooms are live at once since 2026-09-17, so the two hedges now coexist in one
run, and the fallback is deliberate: a Tapo read that fails drops through to the same
shadow memory rather than to silence.

**"It cannot tell me" and "I could not reach it" are different sentences, and they
need opposite responses.** `_light_memory_line(..., unreachable=)` splits them. The
Govee strip's characteristic is write-only, so *"it can't tell me anything back"* is a
permanent fact with nothing to go and fix. A Tapo bulb that normally answers and did
not is *"I couldn't reach it just now"*, which means the wall switch is off or it has
dropped off Wi-Fi. Speaking the first about the second is a false claim about the
hardware and sends him nowhere -- the same mistake `needs_pairing` exists to avoid on
the TV. The dispatcher decides by asking the transport that owns *that room* whether
it has a `get_state`, never the service-wide `can_read_state`, which would report the
write-only strip as unreachable on every single status call.

Two rules carried over into the readable path:

- **The hedge lives in the returned string, never in the system prompt**, which is
  why one transport can hedge and another not without the prompt knowing anything.
- **A field the bulb did not answer is not narrated.** `LightReading.power is None`
  means "it did not tell me", and the dispatcher falls back to memory rather than
  speaking a blank -- the same `None` contract `MediaService`'s readers use, and the
  same trap that made a television in standby report as "on" for the life of that
  feature.
- **`GoveeService.can_read_state` is compared with `is True`, not truthiness.**
  `_svc()` in `tests/test_orchestrator_lights.py` is a bare `Mock`, so every
  attribute of it is truthy; a loose check would claim a live reading off a service
  that reads nothing. `test_a_bare_mock_service_never_produces_a_reading_line`
  pins it.


The Govee characteristic `00010203-...-2b11` is **Write Without Response**, with
no notify characteristic beside it, and the attic H617E is absent from Govee's
cloud whitelist, so `GET /user/devices` returns an empty array with a valid key.
There is no reading to be had by any route.

`services/light_shadow.py` therefore stores what she last *sent*, and
`_light_memory_line` says so in the returned string. **The hedge lives in the
string, not the system prompt**, because the string is what gets spoken and a
prompt instruction is forgotten six exchanges later. Two rules:

- It stores the **clamped percent** and the **spoken colour word**, not rgb.
  Nothing in this project maps rgb back to a name and nothing should; the
  dispatcher already holds the word Master Miguel actually said.
- **Brightness and colour do not imply power.** The strip accepts both while it
  is off, so inferring "on" from them would be a guess dressed as a fact.

Both stores are **in memory only**. A restart is the moment either is least
trustworthy, and persisting would mean answering confidently about a room that
had hours to change. They are also deliberately **two modules, not one**: the
now-playing memory is corroborated against a live reading and the light memory
can never be, and a shared base class would carry the contract "sometimes
verifiable", which is the exact ambiguity that produced the TV bug above.

Both are injected as **trailing defaulted parameters** (`now_playing` on
`_dispatch_tv`, `light_shadow` on `_dispatch_lights`) so the existing call sites
that pass five and two positionals keep working, and `None` keeps meaning "no
memory available". Neither is a module-level singleton: a shared store would
leak a label from one test into the next. `light_shadow` is injected rather than
living on `GoveeService` for a second reason -- `_svc()` in
`tests/test_orchestrator_lights.py` is a bare `Mock`, so a `get_state()` method
would auto-stub **truthy** and the tests would pass while reading nothing.

-----

## TTS Guidance

### Current Defaults

- Current config default is **Google Cloud TTS, Chirp 3 HD**, voice
  `en-US-Chirp3-HD-Aoede` (switched 2026-09-16; Kokoro's baked-in pauses were
  the reason). Standout female substitutes are hashtagged under `tts.google` in
  `config.yaml`: `en-GB-Chirp3-HD-Aoede`, `Kore`, `Leda`, `Zephyr`
- The previous default was Kokoro `af_bella`, `lang_code="a"`. Its config block and
  pre-rendered clips (`sounds/*/kokoro_af_bella/`) are kept so switching back is a
  config change, not a regeneration
- Google is called over REST with an API key (`GOOGLE_TTS_API_KEY`), see
  `TTSService._synthesize_google`. Request body is the documented minimum:
  `input.text`, `voice.{languageCode,name}`, `audioConfig.{LINEAR16, sampleRateHertz,
  speakingRate}`. Chirp 3 HD has **no style/emotion prompt** -- its only delivery
  knobs are `speakingRate` (0.25-2.0), `[pause short|pause|pause long]` markup, and
  custom pronunciations. Natural-language style prompting is a Gemini-TTS feature,
  and Gemini-TTS has no free tier
- Cost: Chirp 3 HD includes 1M characters/month free (roughly 15-20 hours of
  speech), then $30/1M. Billing must be enabled on the Google Cloud project even
  inside the free allowance. A home assistant does not get near the limit; the
  47 activation lines + 19 bootup lines together are ~1,200 characters

### Text Sanitization

TTS output is sensitive to punctuation. Avoid text that causes long pauses:

- replace em dashes with commas or connector words
- avoid awkward punctuation clusters
- prefer flowing spoken sentences over rigid written prose

`services/tts_text_sanitizer.py` is the place for normalization logic, and TTS output should stay optimized for the ear, not the page.

**Terminal `.` `!` `?` must reach Kokoro. Do not strip them.** Kokoro is
StyleTTS2-based, and phrase-final punctuation is the cue its duration predictor
learned to drop pitch and close a clause on. The sanitizer used to strip it on
the theory that it only bought silence at the tail, so every chunk rendered on a
rising, unresolved contour — "I am not finished" over text that was. Reported
2026-09-08 as long replies "sounding like it's gonna stop entirely then
continuing", fixed 2026-09-11. The contract now: a chunk ends in `.!?` when it is
a finished sentence and in a single `,` (the continuation cue) when it is a
fragment; the last chunk of a response always resolves to a full stop.

### Chunk Size: Fewer, Longer Calls

`services/sentence_chunker.py` used to split on `;` `:` and dashes from eight
characters up, and one paragraph became **eight** synthesis calls. Each one is
paid for twice: Kokoro bakes **~400ms of silence onto the front** of every clip
(measured 390–420ms, independent of text) and a fragment gives the model no
sentence to shape prosody from. Measured live on a twelve-second reply:
**~4.7s of injected dead air**, which is where the hesitations lived.

- `MIN_CHUNK_CHARS: 100` — finished sentences are held and travel together,
  each keeping its own punctuation, until the chunk is worth a call
- `FIRST_CHUNK_CHARS: 25` / `FIRST_CHUNK_MAX_CHARS: 60` — the first chunk is the
  exception in both directions. It ships as soon as a sentence exists, and an
  opening sentence that runs long is soft-split at a comma instead: Kokoro is
  roughly real time on this laptop, so **time-to-first-audio IS the first
  chunk's length** — a 153-char opener measured 9.4s before anything was heard
- `tts.kokoro.trim` in `config.yaml` now strips **both** ends of every clip on
  the live path, with fades, the same trim `generate_activation_phrases.py` has
  always applied. `tail_ms` is the gap between chunks
- Same reply after: 4 calls, 563ms of padding, first audio at 3.3s, every chunk
  resolved. Pinned by `tests/test_tts_chunking.py`

**None of this changes the real-time factor, and on the Pi that is the
remaining problem.** Kokoro measures RTF 0.8–1.5 on the dev laptop and is
documented slower than real time on a Pi 4. At RTF ≥ 1 no queue depth keeps
playback fed: `maxsize=2` only postpones the first underrun, and a long sentence
drains it. Piper measures ~0.03 RTF on a Pi 5 and is already a supported
provider. Decide the Pi's `tts.provider` from a measurement on the actual box,
not from the laptop.

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

**Test fixtures start from the real `config.yaml`.** `tests/config_fixture.py`
exposes `config_for_tests(**overrides)`, which loads the committed config and
deep-merges only what a test must pin down. Anything not overridden is what
ships, so a renamed key, a moved IP, a repackaged app or a retuned route fails a
test instead of leaving the suite asserting against a deployment that no longer
exists.

Only three categories are legitimate to override, and each should say why:

- **paths** — `watch_state.json`, `vpn_state.json`, `device_state.json` (via
  `discovery.state_path` for the box and `cec_wake.tv_state_path` for the TV) and
  the Samsung token must go to a tmpdir or a nonexistent file, never the real caches
- **waits** — autoplay delays, Surfshark settle times and CEC boot timeouts are
  seconds each and buy nothing against a mocked ADB
- **a flag the test exists to exercise** — `vpn_routing_enabled` is false in the
  shipped config, `vad.engine` is `silero` (which would load torch), and
  `llm.claude.web_search` runs server-side; those get flipped, with a comment
- **`media.discovery.enabled: false`, for the same reason `cec_wake.enabled` is
  false** — a live `DeviceFinder` socket-probes the operator's whole subnet, and
  `MediaService.__init__` reads the real `device_state.json` out of the CWD. This
  is not optional hygiene: `_wait_for_box` rediscovers after its timeout, and with
  the flag left on a routine `test_media_power` run found the actual Mi Box on the
  real LAN. Tests that *exercise* discovery turn it back on and stub the finder

Values that describe the deployment — IP, port, `adb_path`, package names,
launch components, provider order, route tables, light MACs and aliases — must
**not** be overridden. Those are exactly the ones worth catching drift in.
`tests/test_config_fixture.py` fails on a fixture that hardcodes an IP; mark a
deliberate one with `# config-literal: <reason>`.

Test data that is genuinely synthetic stays synthetic and says so: the prompt
tests use fake playlists and lights because they test the *injection mechanism*,
not the data, and `tests/test_playlist_config.py` sweeps the real file.

Current automated coverage exists for:

- That test fixtures derive from the real `config.yaml` rather than re-typing it,
  that overrides deep-merge without dropping siblings, that each load is an
  independent copy, and that no fixture feeds the code a hardcoded IP
- Stremio title resolution and watch-state behavior
- The boot gate: that `sys.boot_completed` is read from the right property,
  that `1`/`0` map to booted/booting, that a failed or empty read is `None`
  rather than `False`, that the wait loop keeps polling while the box is
  still booting and accepts an unreadable flag, that the check short-circuits
  while the box is unreachable, and that the loop never shells out to a real
  adb
- Power: that `KEYCODE_POWER` is never sent in any state, that `turn_on`
  is a no-op when already awake, that unreachable falls back to Bluetooth,
  that `is_awake()` keeps `None` distinct from `False`, that the wait loop
  clears the offline cooldown, and that `turn_on` survives the
  `requires_tv` gate while the TV is unreachable
- CEC wake: self-disabling without `tv_mac`/`tv_duid`, WoL skipped when the TV
  already answers and retried when it does not, the input key sent exactly twice,
  and an auth failure flagged `needs_pairing` while other failures are not
- TV discovery: a cached IP that verifies skips discovery entirely, a wrong duid
  at that IP is rejected rather than trusted, warm ARP then the TCP scan are what
  ships and are tried in that order, total failure returns `""` instead of raising,
  the probe never opens HTTP when the port gate says :8001 is closed, the WoL wait
  rediscovers on its first miss rather than at its timeout, and the pre-WoL check
  never scans. The CEC test guard stubs **both** `subprocess` and `socket` in
  `services.device_finder`, because the TV now SYN-scans like the box does
- Box discovery (`tests/test_device_discovery.py`): the ladder's rung order and
  short-circuiting, that a hint hit is persisted (the old code only wrote on a
  discovery hit), that a cache entry for a different MAC is discarded, that
  remembering one device does not clobber its sibling, that a raising candidate
  source does not abort the ladder, and that `arp -a` parses on **both** Windows
  (`-` separated) and Linux (`:` separated, address in parentheses)
- The 21-second guard: that `connect()` and `_verify_box` never reach adb when the
  port probe says 5555 is closed, and that a serial mismatch disconnects the
  stranger rather than leaving its transport open
- Drift recovery: that `target` follows a rediscovered `ip`, that a drift self-heals
  through `ensure_connected`, that repeated failures scan only once (rescan
  cooldown), and that `_wait_for_box` rediscovers on every miss with the rescan
  cooldown cleared, so a box that came back on a new lease is found on the next poll
- That a revoked token produces "approve me on screen" and NOT "use the remote" —
  they are opposite fixes and the wrong one strands him
- `_hdmi_inventory()` advertising configured ports and omitting the block entirely
  when none are named
- That `get_status` reports a standby television as off rather than on, never
  touches the CEC waker, says nothing it could not read, and returns one line
  with no dumpsys text in it
- That a status question never wakes the room, and that an unreachable box
  still gets the four-way classified line
- `room_status()` pinging the box exactly once, skipping the app and session
  reads on a sleeping box, and reporting `None` rather than `False` throughout
- That a CEC power report far older than the live log is not trusted, that a
  `<Standby>` broadcast after the last report wins and a newer report wins back,
  and that a `[S]` standby the box sent is not read as the television's state
- Volume parsed from a real `dumpsys audio` capture, scoped past
  STREAM_VOICE_CALL and the VOLUME GROUP block; that muted replaces the
  level rather than joining it, and an unreadable volume is not mentioned
- That elapsed position is reported but never a percentage or time left,
  and that the paused/buffering word is read from the session state rather
  than guessed
- Media sessions parsed from a real capture: that Stremio's show and episode
  are read out of the metadata, that `metadata: null` is no title rather than
  the word "null", that playback is scoped to the foreground app so an idle
  Spotify session cannot be mistaken for it, and that an app holding no session
  is reported as not playing
- Now-playing memory: quoted only while the foreground app still matches,
  dropped when he switched apps, never claiming a search was played, and not
  recorded at all when the launch failed
- YouTube search autoplay: the first `videoRenderer` wins and a playlist at
  the top is skipped, a failed resolve is `None` rather than a traceback and
  falls back to the results page, the `updated=` stamp is parsed and a
  verbatim repeat of a playing session is not a new playback while the same
  video restarted is, the baseline is read before the link, an undescribed
  video stops waiting after the grace, no key is ever pressed, and each
  outcome gets its own spoken line
- Light shadow state: recorded only on success, brightness stored clamped,
  colour stored as the spoken word, brightness never implying power, and
  `light_status` never asking the service for state
- Stremio playback retry logic
- Surfshark route execution, route cache semantics, and debug route capture
- Orchestrator VPN preflight routing and warning behavior
- Govee transport selection, light resolution, BLE packet format, and cloud HTTP error mapping
- Per-light transports: that a Govee strip and a Tapo bulb both load under one service,
  that each room is bound to the transport its fields imply, that a command reaches the
  transport owning that room and not the other, that one instance is shared by every
  room using it, that the preferred transport wins for a light carrying fields for two,
  that a light carrying fields for none is still skipped, that an unreadable room reads
  `None` while a readable one answers, and that a dead transport does not disable a
  working one
- That a readable room which did not answer is spoken as unreachable while a write-only
  room keeps the permanent hedge
- That the light fixtures blank every transport credential (`GOVEE_API_KEY`,
  `TAPO_USERNAME`, `TAPO_PASSWORD`), because each transport reads the environment
  before config and ambient credentials otherwise decide whether `enabled` is true
- Tapo transport: RGB->HSV conversion with the value component discarded so a dark
  colour is not a dimmer, colour never touching brightness (`set_hsv(..., None)`),
  brightness clamped rather than rejected, self-disable without credentials, a live
  reading carrying power and brightness, an unreadable bulb reading `None` rather
  than a blank, that a `LightReading` has no `__bool__` to re-create the falsy-result
  trap, that work runs off the calling thread, and a guard that the file never opens
  a socket
- TPAP (`tests/test_tapo_tpap.py`): the SPAKE2+ M/N points are on P-256, the sign-byte
  encoding of w0, the `password_shadow` credential pre-hash, that a full handshake
  completes against a fake bulb running the device side of the protocol and the
  encrypted channel round-trips with the sequence advancing, that a wrong password is
  an authentication error carrying the bulb's lockout budget, that a forged
  `dev_confirm` is rejected, that plain JSON on the `/ds` endpoint drops the session
  as retryable, that an unsupported PAKE mode is named, that a TPAP bulb is driven
  without importing kasa at all, that the session is reused across commands, that a
  failed probe is not cached as "kasa", and that the file never opens a socket
- `light_status` on a readable transport: a live reading stated without the memory
  hedge, brightness not quoted on a light that is off, a failed read falling back to
  memory, and a bare `Mock` service never producing a reading line
- `control_lights` dispatch strings and failure fallbacks
- YouTube playlist and search launch behavior
- YouTube playlist name matching and random multi-ID selection
- System-prompt inventory injection for lights and saved YouTube playlists,
  including that an empty category is not advertised
- `match_name` tier order, including that the despaced tier fires before the
  substring tier (the guard on "warmwhite" not resolving to "white"), and that
  `normalize_text` expands "&" rather than collapsing "R&B" to "rb"
- Playlist aliases: parsing, that an alias for an empty or unknown category is
  inert, and a structural sweep of the real `config.yaml` asserting every alias
  resolves to its own key and no alias steals a canonical category name
- Playlist existence classification: that a dead page returning HTTP 200 with a
  "Visit source" decoy title classifies as `unavailable` rather than `ok`, and
  that an RD id never produces a `/playlist?list=RD` URL (which 404s even when
  the mix is healthy)
- Activation tier selection, echo stripping, `EchoGate` arming, and the recording trim
- The VAD grace window, the `saw_speech` flag, and Silero's 512-sample framing
- Mic-buffer draining after playback, and that the idle loop does not drain
- openWakeWord native-frame buffering and real consecutive-frame behaviour
- The wake-word dither floor: that it reaches the model, clips instead of
  overflowing int16, leaves framing and the carried remainder untouched, and is
  bit-exact identity at `dither_rms: 0`
- The wake-word scorer (`tests/test_score_wakeword.py`): every file is scored
  inside a 2s/1s bed, the last samples of the word reach the model, framed
  scoring gets the same bed, a file scores identically every run, and
  `--negatives` skips `*_ok.wav` captures while recall mode does not
- The wake-word dataset tool (`tests/test_wakeword_dataset.py`): silence,
  clipping, a blip and a word cut off by the window are rejected; a second
  sound or a word under 10 dB is flagged, not rejected; too-long only flags
  with a TV on; a short gap inside the word does not split it; Whisper's
  renderings of "California"/"Califórnia" match and near-misses do not; only
  ok/accepted takes export; a session or identical audio on both sides of the
  split refuses the export; holdout room tone never becomes a training
  background
- Whisper hallucination rejection: the filler blocklist (and that live control
  words like "go" and "stop" are not in it), `no_speech_prob` / `avg_logprob`
  gating on the worst segment, and failing open on an unexpected response shape
- Dropped turns: `_record_speech` returning `None`, and `_handle_activation`
  aborting without touching STT, the LLM, or TTS
- The per-turn speaker session: clips written in blocks and never through
  `sd.play` while a session is open, a stop from another thread landing within
  one block, a stop staying in force until `reset_playback`, an aborted stream
  being closed and replaced rather than restarted, the silent tail on close, a
  rate change reopening, and a failed open falling back to `sd.play`
- Barge-in: the wake word mid-reply stops her, closes the LLM stream, drains
  both queues and returns `True`; a hit while she is saying "California" is
  ignored; the listener scores at `barge_in_threshold`; a listener error does
  not end the turn; `_idle_loop` chains a barged-in turn into another activation
  on one speaker session; and a closed LLM generator keeps the partial answer
  in history

Useful live-debug commands:

```bash
uv run python tools\debug_surfshark_sequence.py restart_autoconnect --capture --debug
uv run python tools\debug_surfshark_sequence.py quick_connect --capture --debug
uv run python tools\run_youtube_playlist_e2e.py --prep-app stremio --debug
uv run python tools\run_stremio_e2e.py --prep-app youtube --debug
uv run python tools\probe_stremio_sync.py

# Stremio health check. Read-only by default: it never starts playback and
# never rewrites watch_state.json unless asked. Exits nonzero per failed check.
uv run python tools\check_stremio_adb.py                    # full sweep
uv run python tools\check_stremio_adb.py --skip-adb         # offline, no TV needed
uv run python tools\check_stremio_adb.py --sync --json      # refresh library, machine-readable
uv run python tools\check_stremio_adb.py --title Fallout --launch   # actually plays
uv run python tools\probe_govee_devices.py
uv run python tools\probe_govee_devices.py --transport cloud
uv run python tools\probe_tapo_devices.py
uv run python tools\probe_tapo_devices.py --host 192.168.1.42

# Playlist rot. Exits nonzero when an ID is gone, so it can gate a curation pass.
uv run python toolsalidate_youtube_playlists.py
uv run python toolsalidate_youtube_playlists.py --json --strict-title
uv run python toolsalidate_youtube_playlists.py --id RDgQRtAnPL6HM

# The only network-touching tests. Skipped unless the env var is set.
set CALIFORNIA_LIVE_TESTS=1 && uv run python -m unittest tests.test_youtube_validator -v

# Wake word. score_wakeword prints raw scores instead of a yes/no, which is the
# only way to tell "she did not hear me" from "she heard me at 0.55 and the
# threshold is 0.81" — those need opposite fixes.
uv run python tools\score_wakeword.py
uv run python tools\score_wakeword.py --save-clips debug\wakeword

# Recall and false positives together. Set wake_word.capture.enabled: true in
# config.yaml, live with her for a day, then score what she recorded of her own
# false wakes. --framed decides with the live detector (threshold +
# consecutive_frames + debounce) rather than peak model score; since the
# 1280-frame fix those genuinely differ.
uv run python tools\score_wakeword.py --negatives debug\activations --sweep
uv run python tools\score_wakeword.py --dir training\recordings\holdout_en --framed
uv run python tools\score_wakeword.py --dir training
ecordings\holdout_pt
uv run python tools\score_wakeword.py --dir training
ecordings\holdout_en --model models\california.onnx --threshold 0.59
uv run python tools\wakeword_dataset.py record --lang pt --distance couch --background tv
```

Targeted validation used for the latest Stremio resume work:

```bash
uv run python -m py_compile services\stremio_service.py core\orchestrator.py services\llm.py services\media_service.py tests\test_stremio_service.py tests\test_media_service.py tests\test_orchestrator_vpn_routing.py

# Power path. Safe against a live box: turn_on is a no-op while already awake.
uv run python -m unittest tests.test_media_power -v
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
- **A prosody cue looks like dead time if you only measure duration.** The
  sanitizer stripped terminal periods because the tail of the clip was silence,
  and it was — but the silence was the model closing the clause, and without the
  cue it never did. Two independent problems, "sounds unfinished" and "pauses
  between chunks", came out of one line and could not be told apart by ear.
  The measurement that separated them was the contour, not the clock
- **The cheapest chunk is not the smallest one.** Eight-character splits looked
  like the aggressive low-latency choice, and every one of them bought 400ms of
  Kokoro's front padding plus a fresh model call. Fixed per-call overhead means
  the optimum is few, whole sentences — with the first chunk the only place
  where small is worth paying for
- **A wake-word eval measured on synthetic audio predicts nothing about a real
  microphone, and it fails optimistically.** Run 1 reported 92.4% recall and caught
  **25%** of real English utterances and **0%** of Portuguese ones. Run 2 reported 93.3%
  at its `optimal_threshold` of 0.81 and caught **40%**. Both were measured against the
  same Piper voices that generated the training data, so they measure self-consistency,
  not detection. Always hold back real recordings and score those instead.
  (The 25% / 40% real-audio figures were themselves measured bare — see the
  next learning — so they understate both runs.)
- **A clip scored in isolation is not a clip heard in a room.** The live mic
  never stops, so the model always has a second of audio before the word and
  audio after it. Scoring a trimmed 0.7s take straight after a reset gave it
  neither, and reported 16-20% recall for a model that catches 65-75% of the
  same takes in a bed of room noise. The harness was believed for three weeks
  and drove a retrain plan; test the measuring tool against a case where you
  know the answer before trusting what it says about the model
- **An accent absent from the training data is absent from the model, and no threshold
  recovers it.** Piper's checkpoint is `en-us-libritts-high` and `synthesis.py` reads its
  espeak voice from that checkpoint's own JSON, so every synthetic positive is American
  English regardless of config. Merging ~50 real Portuguese-accented recordings moved the
  median score from 0.007 to 0.77
- Real recordings are scarce and synthetic clips are cheap, so replication and recording
  count are different levers: replication sets training *weight*, recording count sets
  *diversity*, and only the second adds information
- **A buffered stream is a recording of the past, not a view of the present.**
  Blocking playback looked like the conservative, obviously-correct way to keep
  California from hearing herself, and it was not, because nothing was reading
  the mic while she spoke. Two separate defences — `activation_blocking` and
  `EchoGate` — were both aimed at speaker bleed arriving *live*, and neither
  addressed audio arriving *late*
- **A knob that is never measured is a knob that may not be connected.**
  `consecutive_frames: 2` read as a working false-positive defence for the life
  of the project and was equivalent to 1, because the chunk size the orchestrator
  reads (640) and the frame size openWakeWord scores (1280) were never reconciled.
  Nothing failed loudly; the score stream just quietly repeated itself
- **Silence is not an input, and a VAD that cannot tell "he has not started" from
  "he has stopped" will treat it as one.** One boolean — has this recording ever
  heard speech — is the difference between a false wake costing nothing and a
  false wake costing two API calls and a spoken non-sequitur
- **A model that has only ever seen audio with a noise floor will not behave on
  audio without one, and it fails confidently rather than quietly.** Every
  training clip had background mixed in at an audible level, so gated
  near-silence was never in the distribution — and the model answered 0.80 on it
  instead of 0.00. The quietest frames in the room scored highest and the loudest
  scored low, which is the exact inverse of the intuition that sent the first
  three fixes after *noise*. Adding back the noise floor the training data always
  had costs nothing and removes the artefact at every threshold.
- **When the obvious knob does not separate the two distributions, it is the
  wrong knob.** Raising the threshold was the natural response to false wakes at
  0.896 and 0.927, and it could not work: real wake words peak in that same band.
  Plotting both distributions before turning anything is what showed the overlap,
  and what showed that the threshold was pinned at 0.81 by an artefact rather
  than by the model's actual quality.
- **A measurement harness that does not apply the fix will send you back to the
  bug.** `tools/score_wakeword.py` scored raw frames, so post-dither it would
  still have reported 0.80 on silence. A tool that measures something other than
  what runs is worse than no tool, because it is believed.
- **Recall tooling without false-positive tooling optimises one direction only.**
  `tools/score_wakeword.py` could measure "would she hear me" from the first day
  and had no way at all to measure "would she wake for nothing", so every
  threshold decision was half-blind. `--negatives` and `--sweep` close that, fed
  by clips California records of her own false wakes (`wake_word.capture`)
- **A device that answers is not a device that is reachable, and "off" can mean
  off the network.** Every fix for the Mi Box wake assumed the box was still
  *there* in standby, just not listening — raise a timeout, retry the connect,
  pick a better keycode. The ARP table settled it in one line: `.32`, `.33` and
  `.34` answered and `.35` had no entry at all. Once the interface is down, no
  keycode and no timeout can matter, and the only way out is a radio that stays
  up. Check whether the target exists on the network before tuning how you talk
  to it.
- **A toggle is the wrong primitive for anything with an asymmetric cost.**
  `KEYCODE_POWER` reads as symmetric and is not: off costs one keyevent, on
  costs a physical remote. `power_toggle` therefore had a 50% chance of being
  unrecoverable, which is not a risk profile a voice interface should carry.
  Explicit `turn_on` / `turn_off` also cost one *fewer* action in the tool
  schema than the three they replaced.
- **This one recurs, so the guard has to be at the boundary, not in a habit.**
  Adding a `getprop` inside `_wait_for_box` made two existing power tests fire
  a real `getprop` at whatever box adb was attached to, and they kept passing.
  They patched `ensure_connected`, which used to be everything the loop
  touched. The rule that falls out: anything the wait loop asks the box must
  be a **named, patchable method**, never a bare `_adb` call --
  `test_the_wait_loop_never_shells_out_to_a_real_adb` pins it by patching
  `subprocess.run` to raise. Audit with an interpreter-level patch after
  touching that path; a green run proves nothing here.
- **A unit test holding a real service object will use a real transport, and only
  a machine *missing* the binary tells you.** Four tests in
  `tests/test_stremio_service.py` built a live `StremioService` with
  `adb_path: "adb"` and patched only `_attempt_provider`, so `_play_deep_link`
  reached `_launch_uri` and `_keyevent` and fired real `am start` and
  `input keyevent 23` at whatever device adb was attached to. On Master Miguel's
  laptop they passed; on a box without adb on PATH they raised `FileNotFoundError`,
  which is the only reason it was ever visible. The class `setUp` now stubs
  `subprocess.run` for every test in the file, and
  `test_unit_tests_never_shell_out_to_a_real_adb` fails loudly if that guard is
  removed. **`uv run python -m unittest discover` must never touch the TV.**
- **A device that is merely *addressed* is not a device that is *found*.** Every
  address in this project was config until the box moved on a DHCP renewal and broke
  every tool call. The fix is not a better default -- it is to stop treating an
  address as identity. Identity is the MAC and the serial; the address is a cache.
- **The cheap check must come before the expensive one, and here the ratio is 70x.**
  `adb connect` to a closed port takes 21.1s and the timeout is not tunable, while a
  raw socket probe answers in 0.3s. That single ordering is the difference between a
  1.55s subnet scan and a 90-minute one. Any "verify" that wraps a slow tool needs a
  fast gate in front of it.
- **A test suite that passes is not a test suite that stayed in its lane.** An audit
  hook over `subprocess.Popen` *and* `socket.connect` caught three things a green run
  did not: a new `_wait_for_box` code path socket-probing the real LAN and finding the
  actual box, `_classify_failure` shelling out to a live `arp -a`, and -- separately --
  a `arp -a` parser that worked on Windows and would have silently returned nothing on
  the Pi, because Linux prints the address as `(192.168.1.41)` with parentheses.
  Guards must be pointed at the boundary the code actually crosses: every previous
  guard here patched `subprocess` only, which cannot stop a SYN scan.
- **A hand-written test fixture can only prove the code agrees with the fixture.**
  `tests/test_media_service.py` pointed at an IP two moves stale and kept
  passing; the Surfshark tests pinned a 3-key `quick_connect` route while
  `surfshark_routes.json` ships 4 — including the leading `DPAD_CENTER` that
  CLAUDE.md calls out as load-bearing on this box. Neither could ever fail,
  because nothing in either test referred to what ships. Fixtures now derive
  from `config.yaml` and override only paths, waits, and the flag under test.
- **Only fixture *inputs* drift silently; assertions are self-correcting.**
  A test asserting `"com.google.android.youtube.tv"` appears in a launch command
  fails the moment the package changes — loud, and fine. A *fixture* feeding the
  service a stale IP keeps passing forever. That asymmetry is why the guard in
  `tests/test_config_fixture.py` checks inputs and deliberately ignores the
  dozens of package literals in assertions: a guard needing 18 opt-out markers
  is friction, not a guard.
- **Environment variables outrank config, so a hermetic fixture is not enough.**
  `StremioService` reads `STREMIO_EMAIL` before `config.stremio.email`, so on a
  machine with real credentials exported, a test's stub config was ignored and
  `sync_library()` could reach `api.strem.io` for real. The Stremio tests now
  drop those vars for the duration, the way the Govee tests already blanked
  `GOVEE_API_KEY`. Verify with:
  `STREMIO_EMAIL=x TMDB_API_KEY=y uv run python -m unittest discover -s tests`
- **A single play attempt cannot tell you which layer broke.** "Stremio didn't
  start" has at least six distinct causes — box asleep, adb unreachable, package
  gone, `stremio://` handler unregistered, `dumpsys media_session` unreadable so
  verification is blind, `uiautomator dump` empty so provider scraping falls back
  to blind OK presses — and they need different fixes. `tools/check_stremio_adb.py`
  walks them in order and is read-only, so it can be run while something is playing
- Plain show requests should sync first, then use `watch_state.json` as the resume source of truth for Stremio titles
- TMDB is the fallback resolver for titles outside the local Stremio cache
- `state=3` in `dumpsys media_session` is the practical playback signal
- If no resume progress exists for a series, opening the Stremio detail page is better than guessing an episode
- Shared ADB helpers with explicit timeouts are more reliable than scattered raw shell calls for UI dumps and screenshot capture
- Static YouTube playlist mapping is simpler and more reliable than OAuth-heavy integrations
- **Pressing into a screen you cannot read is a coin flip, and "opened"
  is not "playing".** The YouTube results page looked like one OK away from
  playback and was one OK away from a playlist page, a channel page, or a
  pause, depending on what happened to rank first. Resolving the query to a
  video id off-box and deep-linking `watch?v=` turned a UI guess into a
  deterministic launch that the session can confirm
- **A published state is a record, not a reading.** `dumpsys media_session`
  reports the last thing an app said, verbatim, for as long as it says
  nothing new -- YouTube sat at state=3 on a results page for 20s. "Is it
  playing" was true before the launch and proved nothing; only a change
  (the `updated=` stamp) does. Same shape as the CEC power report
- For YouTube curation, validation against the real fetched page title is more reliable than trusting search snippets or guessed IDs
- Multi-ID playlist categories are a simple way to keep repeated requests fresh without changing the voice interface
- The YouTube region path is best handled by restarting Surfshark and then opening YouTube, not by trying to select Albania manually inside the app
- On this Mi Box, the Stremio VPN path is best handled as a small calibrated DPAD sequence to Portugal
- Same-app requests should preserve the active session and skip Surfshark, even if that means VPN policy is only enforced on cross-app transitions
- Package launch plus named route tables is easier to maintain than scattering Surfshark timing and key sequences through the codebase
- Graceful fallback lines build trust more than pretending automation is perfect
- **"Unsupported device" is a statement about the library, and "no devices found" is
  a statement about the broadcast.** python-kasa's discovery heard the L530E, dropped
  it as unsupported and reported nothing; a TCP sweep showed no port 80 open until the
  bulb had been on for a minute; the global broadcast never reached it from a
  two-adapter laptop. Three separate "it is not there" readings, none of which meant
  the bulb was absent. The protocol itself was a solved problem in a .NET repo the
  Python ecosystem had not noticed -- worth a search before concluding "upstream is
  blocked, so we are"

-----

## Known Bugs / Audit

A whole-codebase bug audit was run on **2026-07-12**. Full report:
[`BUG_AUDIT.md`](BUG_AUDIT.md). Baseline at audit time: all 63 unit tests passing;
`py_compile` clean except the stale `services/tts copy.py` dead file (that file, and
`services/sentence_chunker copy.py`, were moved to `deprecated/` on 2026-08-25; the
live tree now parses clean).

Two bugs reported by Master Miguel were fixed on **2026-09-01**, both in the
listening path and both compounding each other:

- ~~**False wake-ups**~~ — the openWakeWord frame-size mismatch that disabled
  `consecutive_frames` is fixed (see the wake-word note above). Threshold left at
  0.81 deliberately; retune it from `--negatives --sweep` rather than from the
  synthetic eval.

  **This was only half of it, and the framing fix did not stop the false wakes.**
  Reported again on 2026-09-03 and fixed the same day: the real driver was gated
  near-silence scoring ~0.80 out of the model, not transient spikes. See
  `wake_word.dither_rms` in the note above. Do not mark false wake-ups closed
  again without a `--negatives` measurement of the actual room.
- ~~**Silence treated as the command**~~ — `vad.speech_timeout` plus a
  `saw_speech` flag; a wake nobody speaks into is now dropped before STT.

Three more were found and fixed on **2026-09-03**, all in the same path:

- ~~**Wake-word false positives on silence**~~ — `wake_word.dither_rms: 10`.
  Worst score on 150s of the real quiet room: 0.8005 → 0.0194.
- ~~**Silero VAD had never run once**~~ — `vad.engine` was `"silero"` and
  `_load_silero` failed with `ModuleNotFoundError: torchaudio` on every boot,
  falling back to energy VAD silently. `torch` arrives via kokoro; `torchaudio`
  did not. `california[silero]` is now part of the `default` extra, so
  `uv sync --extra default` gives the VAD the config claims. The energy fallback
  mattered: the room produced 5 transient bursts over RMS 200 lasting ≥2 chunks
  in 150s, each enough to set `saw_speech` and defeat the `no_speech` guard.
- ~~**Whisper hallucinations reached the LLM**~~ — `services/stt.py` asked for
  `response_format="text"`, discarding `no_speech_prob` and `avg_logprob`. A false
  wake produced `"Hmm."` from 5.2s of room tone and Claude answered it. Now
  `verbose_json` plus a filler blocklist; a rejected transcript returns `""`,
  which the existing empty-transcript guard already drops.

One more was found and fixed on **2026-09-03**, in the power path:

- ~~**`power_toggle` was a one-way door, and `wake()` was dead code**~~ — the tool
  sent a bare `KEYCODE_POWER`, a toggle, so "turn on the TV" turned it off
  whenever it was already on, and nothing could turn it back on. Found the hard
  way: the box was slept over ADB to test the wake path and could not be
  recovered without the physical remote, three separate times. `wake()` was
  doubly unreachable, since `wake` sat in `_dispatch_tv`'s `requires_tv` set
  behind an `ensure_connected()` gate. Now `turn_on` / `turn_off` with
  state-aware dispatch and a CEC wake through the television.
  **Verified end to end**: both devices fully off, recovered by script alone —
  WoL to the Samsung, HDMI toggle, box awake. See "Power: Off Is ADB, On Is The
  Television".

Confirmed **High-severity** backlog:

- ~~**Barge-in / "stop" is non-functional**~~ — **fixed 2026-09-16.** The wake word is
  listened for through every reply and cuts it short; see "Interrupting Her Is the
  Wake Word, Not Loudness". The same change opened the speaker once per turn, which
  is what was clipping the first syllable of the acknowledgement, and removed the
  30s TTS joins that abandoned long replies mid-word.
- ~~**Claude provider does not stream**~~ — **fixed 2026-08-27.** `_stream_claude` now
  uses `client.messages.stream()` and yields real token deltas, so the default provider
  gets the sentence-chunker/TTS overlap the design assumes. The same change fixed **M2**
  (a response with two `tool_use` blocks used to build a malformed next request).
  Covered by `tests/test_llm_claude_streaming.py`.
- ~~**Default TTS `kokoro` missing from `requirements.txt`**~~ — **obsolete.** There is no
  `requirements.txt` any more; `kokoro` is an extra and `uv sync --extra default` installs
  exactly what the committed `config.yaml` selects.

See `BUG_AUDIT.md` for Medium/Low findings and the verified-rejected false positives.
