# Live UI halo: bring California to life (plan)

## Decision (2026-10-13)

The chosen look is a **halo**, in the **Dusk** palette. Draft: https://claude.ai/artifact/N3yAShAhQPWHWksWtjTk1s.

It is a ring of embers with a soft core of light that breathes with her voice. Her moods are acted out by the ring itself: there is no face.

Rejected, in order:

- **A sun.** It is a synthwave cliché, a place rather than a personality, and has no way to show expression.
- **Eyes.** Very expressive, but they read as a generic robot (Cozmo, EVE).
- **A sun with eyes.** It reads as the grimacing emoji.

Research and earlier drafts:
- https://claude.ai/artifact/X9YVoUXW9xFTfQkCLcpg4F
- https://claude.ai/artifact/Y3ctkviHKV4WeeCX5cwuuQ
- https://claude.ai/artifact/4a24vPtQU9SiN8Qf5oRidj

This builds on PR #51 (branch `ccr-cc285211-5jqocu`), which provides the event bus, the SSE server and the `ui/` page.

## Goal

The top of the live page becomes the halo. It reacts as things happen to:

- her **state**;
- her **voice**, syllable by syllable;
- **your voice** while she listens;
- her **mood**;
- her **words**, revealed as she says them.

It must stay smooth on a phone over Wi-Fi and must never add latency to a turn.

## What the halo does

| input | the halo |
|---|---|
| idle | Loosens into a dim, slowly drifting cloud. The core breathes faintly |
| wake word | The cloud snaps into a ring with a flash |
| listening | The ring tightens and leans toward the viewer. Its edge and the core pulse with the mic level |
| thinking | Spins up, with a bright comet head running round the ring |
| speaking | Each syllable pushes the ring out and flares the core. Loud syllables send a ripple outward |
| happy | Sparkles, warms toward coral, grows slightly, spins a little faster |
| smug | Cocks sideways and slows to a lazy spin |
| unimpressed | Flattens toward edge-on, dims, sinks (a sigh) |
| sheepish | Shrinks, cools toward violet, dims, droops |
| eye-roll | The whole ring rolls over once (about 1.3 s), then settles as unimpressed |

## Dusk palette

| use | value |
|---|---|
| core | `#ffe1f0` |
| embers, cool → warm | violet `#b388ff`, rose `#ff6fa8`, coral `#ff9a6b` |
| page accents | rose `#ff6fa8`, violet `#b388ff` |
| background | `#0a0912` |

The live page's existing orange accent (`#f28c38` in `ui/app.css`) moves to rose. That keeps the whole page one palette, and each token gets a light-theme counterpart.

## The constraint that shapes the data

Her voice plays through `sounddevice` on the laptop speaker, never in the browser, so the page cannot measure it with Web Audio. Loudness therefore comes from Python.

Python sends **one event per clip** carrying the whole loudness curve. The page plays that curve on its own clock, which keeps the animation smooth over a patchy link. Streaming 25 levels a second would stutter on any late packet.

## Event contract (new kinds)

| kind | when | data | replayed? |
|---|---|---|---|
| `clip_start` | a clip starts playing: a reply sentence, the acknowledgement line, bootup | `{clip, kind: "reply"\|"ack"\|"boot", hz: 25, env: [0..1 ×N], ms, text}` | no, live only |
| `clip_end` | the clip finished or was cut | `{clip, stopped: bool}` | no, live only |
| `mic` | idle loop, recorder, reply listener | `{level: 0..1, phase}` at ≤15 Hz, keeping the peak | no, live only |
| `mood` | a rule fires (see below) | `{mood, reason}` | yes, so a page opened mid-turn shows it |

- `text` is dropped when `logging.include_transcripts` is false, the same rule as `sentence`.
- A barge-in ends in `clip_end {stopped: true}`, and the page settles within one frame.
- The mood resets to `none` at `turn_start` and after `ui.mood_hold_ms` (default 6000) in idle.

## Where moods come from

**Phase 1 uses rules only.** They are free, testable and cannot misfire into speech:

| rule | mood |
|---|---|
| a tool raised, or the turn outcome is `error` | sheepish |
| a dispatcher reported failure (see below) | sheepish |
| a device action reported success | happy |
| any other completed reply | none |

Smug, unimpressed and the eye-roll have no reliable rule, so phase 1 does not show them.

**What "reported" means:** `_handle_tool_call` only sees the returned string. Each `_dispatch_*` therefore records the success of its underlying result on the turn, using the existing typed results:

- `GoveeCommandResult`, `DeebotCommandResult`, `WhatsAppCommandResult` and `PhoneCommandResult` are already truthy on success.
- `StremioPlayResult` and the media `WakeResult` carry explicit fields.

Each dispatcher calls `self._turn.set(tool_ok=...)`, and the orchestrator maps that onto a mood at the end of the tool call. `turns.jsonl` gains a `tool_ok` field as a side effect.

**Phase 2 (only if phase 1 feels flat): she tags her own mood.** The system prompt allows an optional leading tag such as `[smug]`.

- It is parsed out of the token stream in `_timed_tokens`, before `sentence_chunker`, so it can never reach TTS. It is then published as `mood`.
- Measure before adopting:
  - prompt cost, a few tokens per turn;
  - how often she forgets the tag;
  - how often the tag leaks into speech. The parser must also strip a tag that arrives mid-sentence.

## Python changes

1. **`core/audio_levels.py` (new, pure, no I/O)**
   - `envelope(audio, sr, hz=25) -> list[float]`: RMS per window, mapped from dBFS (-60..-10 dB) onto 0..1, rounded to 2 decimals. About 100 numbers for a 4 s sentence.
   - `level(chunk_int16) -> float`: the same mapping for one mic chunk.
   - `MicLevelPublisher`: throttled and keeps the peak, like `WakeScorePublisher`. Factor a shared base class out of `core/event_bus.py`.
2. **`core/audio_pipeline.py`: clip hooks, with no knowledge of the bus**
   - Add `on_clip_start(data, sr, text, kind) -> clip_id` and `on_clip_end(clip_id, stopped)`. They default to `None`, and every call is wrapped so a raising hook can never stop playback.
   - `play_audio(..., text="", kind="reply")` fires start before the first block in both the session path and the `sd.play` fallback. It fires end on return, with `stopped = self._abort.is_set()`.
   - `play_activation_sound` fires start for the acknowledgement line (`kind="ack"`).
3. **`core/orchestrator.py`**
   - `_start_live_ui` installs the hooks, which compute `envelope()` and publish. This adds well under 1 ms per sentence on the player thread before the first write.
   - `_audio_player_worker` passes `text=` to `play_audio`.
   - `mic` levels go through `MicLevelPublisher` from `_idle_loop`, `_record_speech` (reusing `_chunk_rms`) and `_reply_listener`. A null meter by default keeps `__new__`-built orchestrators working.
   - Mood rules publish at the end of `_handle_tool_call` (from `tool_ok`) and in the `_handle_activation` `finally` (outcome `error`). The dispatchers gain the `tool_ok` set described above.
4. **`core/live_ui.py`**
   - Add `vendor/three.min.js` (r149, MIT, licence header kept) to the static whitelist. The page's CSP is `script-src 'self'` and the Pi may be offline, so it has to be vendored.
   - Serve it with `Cache-Control: max-age=86400`. Everything else stays `no-store`.
   - `hello` gains `voice_offset_ms` and `palette`.
5. **`config.yaml`, in the `ui:` block:**

   | key | default | what it does |
   |---|---|---|
   | `halo` | `true` | `false` falls back to the plain state pill |
   | `voice_offset_ms` | `120` | delays the animation to match the speaker's latency; tune once by eye |
   | `mic_publish_hz` | `15` | how often the mic level is published |
   | `mood_hold_ms` | `6000` | how long a mood holds in idle before it clears |

## Page changes (`ui/`)

1. **`ui/halo.js` (new)**, ported from the draft with the palette and shell toggles removed.
   - **API:** `createHalo(canvas, {small, palette})` → `{setState, voiceLevel, micLevel, setMood, resize, dispose}`. Inputs only: it never touches the network.
   - **Contents:** the ember ring with its idle cloud, the core and aura sprites, the comet, ripples, mood targets, the roll, drag-to-turn and the wake flash.
2. **`ui/app.js`**
   - On `clip_start`, schedule the envelope at `now + voice_offset_ms`. Each frame reads `env[floor(elapsed × hz)]` through the draft's attack/release smoothing.
   - On `clip_end {stopped}`, drop it at once. When clips overlap, the latest wins.
   - Reveal the caption's words in step with the clip (word index ∝ elapsed / `ms`).
   - Wire the remaining events: `mic` → `micLevel`, `state` → `setState`, `mood` → `setMood`.
3. **Layout:** the halo becomes the top section, full width (about 46 dvh on a phone, 56 vh on desktop).
   - `CALIFORNIA`, the state and the mood sit in the HUD corner, with the caption below the ring as in the draft.
   - The existing panels follow below.
   - The wake panel keeps its graph and drops its big number.
4. **Ambient mode:** a "Full screen" button and `#ambient` show only the halo and the caption, for a tablet by the TV. Both the Fullscreen API and a Screen Wake Lock are optional, and a rejection is tolerated.
5. **Battery and accessibility:**
   - About 30 fps after 10 s idle with no voice; full rate on any change.
   - Pixel ratio capped at 1.75; half the embers under 700 px.
   - `prefers-reduced-motion` renders a still frame per change.
   - Without WebGL, the plain state pill is shown as today.

## Tests

- **`tests/test_audio_levels.py`:**
  - silence → zeros, full-scale tone → near 1, never above 1;
  - length is `ceil(duration × hz)`;
  - int16 and float32 inputs agree;
  - a 10 s clip runs in under 5 ms.
- **`tests/test_speaker_session.py` (extend):**
  - start fires before the first write and end after the last;
  - a stop mid-clip gives `stopped=True`;
  - a raising hook never stops playback;
  - the `sd.play` fallback fires both hooks.
- **`tests/test_event_bus.py` (extend):**
  - `clip_start`, `clip_end` and `mic` are live only;
  - `mood` is replayed;
  - `MicLevelPublisher` keeps the peak.
- **`tests/test_reply_barge_in.py` (extend):** a barge-in gives `clip_end {stopped: true}`.
- **Mood rules:**
  - a raising tool gives sheepish;
  - each dispatcher's `tool_ok` follows its typed result (one test per dispatcher, using the existing fakes);
  - a successful device action gives happy;
  - a bare `Mock` service never produces a mood (compare with `is True`, as `can_read_state` does);
  - the mood clears at `turn_start`.
- **Orchestrator:**
  - text is passed through to `play_audio`;
  - `include_transcripts: false` strips `clip_start.text`;
  - a `__new__`-built orchestrator publishes nothing and does not raise.
- **`tests/test_live_ui.py` (extend):**
  - the vendored script is served under the token and 404s without it;
  - it has its own cache header.

All of these run under the existing guards: no audio device, no adb, no LAN.

## Verification

1. `uv run python -m unittest discover -s tests`: the failure set must match master's.
2. **`tools/demo_live_ui.py` (new, committed):** runs the real `EventBus` + `LiveUI` and plays three scripted turns: Fallout, a failed tool, a barge-in. Envelopes come from real WAVs in `sounds/` when present.
   - A Playwright pass screenshots each state and mood at phone and desktop width.
   - It checks for no console errors and no horizontal scroll.
   - It checks that `clip_end {stopped}` drops the voice level within one frame.
3. **On the real laptop, with the page open on the phone:**
   - "California, put on Fallout": the edge follows your voice while recording, the comet runs while thinking, the ring pushes out in step with her syllables, and it ends happy.
   - Tune `voice_offset_ms` until the ripples land on her syllables.
   - Turn on a light that is unplugged: it ends sheepish.
   - Barge in: the ring settles when she stops.
4. **Latency:** compare `turns.jsonl` over 5 turns with `ui.enabled` on and off. `first_audio` must stay within its normal spread.

## Order of work

Each step is shippable on its own:

1. `audio_levels.py` + tests.
2. Pipeline clip hooks + tests.
3. Orchestrator wiring, mic levels + tests.
4. `tool_ok` in the dispatchers, mood rules + tests.
5. Vendor Three.js, the `live_ui` whitelist, `hello` fields + tests.
6. `halo.js`, `app.js`, layout, the Dusk tokens, ambient mode.
7. Demo tool + Playwright pass.
8. Update the Live UI section of CLAUDE.md / AGENTS.md:
   - the clip contract;
   - why levels come from Python;
   - mood rules, and what phase 2 would add.

## Out of scope

- Word-level timing. Chirp 3 HD has no timepoints, so words are spread over the clip's length.
- Smug, unimpressed and the eye-roll before phase 2.
- A halo for the phone-call agent's voice.
- Typed turns.
