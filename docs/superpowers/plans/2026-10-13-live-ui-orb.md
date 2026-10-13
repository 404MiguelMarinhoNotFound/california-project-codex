# Live UI orb: bring California to life (plan)

Draft the user approved: https://claude.ai/artifact/Y3ctkviHKV4WeeCX5cwuuQ. It combines concept A (the sunset core), concept C (an ember halo) and the shell from openclaw-jarvis-ui. Research behind it: https://claude.ai/artifact/X9YVoUXW9xFTfQkCLcpg4F.

Builds on PR #51 (branch `ccr-cc285211-5jqocu`): the event bus, the SSE server and the `ui/` page.

## Goal

The page's header becomes the orb. It should react to California as things happen:

- her **state**;
- her **voice**, syllable by syllable, while she speaks;
- **your voice** while she listens;
- her **words**, revealed as she says them.

It must stay smooth on a phone over Wi-Fi and must never add latency to a turn.

## The constraint that shapes everything

Her voice plays through `sounddevice` on the laptop speaker, not in the browser. The page therefore cannot measure it with Web Audio, which is what every Jarvis project does. Loudness has to come from Python.

Streaming 25 levels a second would stutter on late packets and flood SSE. Instead we send **one event per clip** carrying the whole loudness curve, and the page plays that curve on its own clock.

## Event contract (new kinds)

| kind | when | data | replayed? |
|---|---|---|---|
| `clip_start` | a clip starts playing: a TTS sentence, the acknowledgement line, bootup | `{clip, kind: "reply"\|"ack"\|"boot", hz: 25, env: [0..1 ×N], ms, text}` | no, live only |
| `clip_end` | the clip finished or was cut | `{clip, stopped: bool}` | no, live only |
| `mic` | while listening (idle, recording, reply listener) | `{level: 0..1, phase}` at ≤15 Hz, keeping the peak | no, live only |

- **Text:** `text` is dropped when `logging.include_transcripts` is false, the same rule as `sentence`. The existing `sentence` event stays, so the turn panel is unchanged.
- **Replay:** all three are live-only, like `wake`. A page opened mid-sentence should not replay an animation for audio that has already finished.
- **Barge-in:** on a barge-in, `stop_playback()` leads to `clip_end {stopped: true}`, and the page cuts the animation within one frame.

## Python changes

### 1. `core/audio_levels.py` (new, pure functions, no I/O)

- `envelope(audio, sr, hz=25) -> list[float]`
  - Splits the clip into windows and takes the RMS of each window.
  - Converts RMS to dBFS and maps -60..-10 dB onto 0..1, so quiet speech still moves the orb.
  - Rounds values to 2 decimals, so a 4 s sentence is about 100 numbers, roughly 500 bytes.
- `level(chunk_int16) -> float`: the same mapping for one mic chunk.
- `MicLevelPublisher`: built like `WakeScorePublisher` (throttled, carries the peak since the last publish). It could share one base class with `WakeScorePublisher` in `core/event_bus.py`.

### 2. `core/audio_pipeline.py`: clip hooks, no bus knowledge

- Add optional callbacks:
  - `on_clip_start(data, sr, text, kind) -> clip_id`
  - `on_clip_end(clip_id, stopped)`
- Both default to `None`, and every call is wrapped so a raising hook can never break playback.
- `play_audio(..., text="", kind="reply")`:
  - fires the start hook just before the first block is written, in both the session path and the `sd.play` fallback;
  - fires the end hook when the clip returns, with `stopped = self._abort.is_set()`.
- `play_activation_sound`: fires the start hook for the acknowledgement line (`kind="ack"`, the line's text) in both the blocking and non-blocking paths.
- `stop_playback()` sets the abort flag as it does now. The end hook reports `stopped` from that flag.
- **Why hooks and not a bus import:** the pipeline stays testable with no bus, the same reason `GeminiLiveAgent` takes `on_line`.

### 3. `core/orchestrator.py`

- **`_start_live_ui`:** when the UI is on, set `self.audio.on_clip_start/on_clip_end` to methods that compute `envelope()` and publish.
  - The envelope costs about 0.3 ms per sentence in numpy, on the player thread, before the first write. That is still far below the 40 ms block.
  - A test asserts it stays under 5 ms for a 10 s clip.
- **`_audio_player_worker`:** pass `text=` to `play_audio` (it already has the text for `_speaking_text`).
- **Mic levels:** publish `mic` levels wherever a chunk is already in hand:
  - `_idle_loop` (phase `idle`),
  - `_record_speech` (phase `recording`, reusing the existing `_chunk_rms` value),
  - `_reply_listener` (phase `reply`).

  These go through `MicLevelPublisher`. A null meter keeps `__new__`-built orchestrators working.
- **Clip timing:** `ui.voice_offset_ms` (default 120) goes into `hello` so the page delays each envelope by the speaker's output latency. Tune it once by eye on the real laptop.

### 4. `core/live_ui.py`

- Add `vendor/three.min.js` (Three.js r149, MIT, about 600 KB, with its licence header kept) to the static whitelist.
- **Why vendor it:** the page's CSP is `script-src 'self'`, and the Pi may have no internet. It stays a fixed whitelist entry with no path joining.
- Serve it with `Cache-Control: max-age=86400` so a phone downloads it once. Everything else stays `no-store`.

### 5. Config

Add to the `ui:` block:

- `orb: true`, which lets the page fall back to the plain state pill;
- `voice_offset_ms: 120`;
- `mic_publish_hz: 15`.

## Page changes (`ui/`)

### 1. `ui/orb.js` (new), ported from the draft and split from `app.js`

- **Interface:** `createOrb(canvas, {small})` returns `{setState(s), setThreshold(t), voice(level), mic(level), wake(score), resize(), dispose()}`.
- **Contents:** the sunset core shader, the noise-displaced wireframe shell, the additive glow, the ember halo, pulse rings and drag-to-turn, all as in the draft.
- **Inputs only:** the orb never touches the network. That is the LiveKit-style contract (state plus levels).

### 2. `ui/app.js`

- **Voice playback:**
  - On `clip_start`, schedule the envelope: `start = now + voice_offset_ms`. Each frame reads `env[floor((t - start) * hz)]` through the same attack/release smoothing as the draft.
  - On `clip_end {stopped}`, drop it immediately.
  - Overlapping clips: the latest one wins.
- **Caption:** reveals the clip's words in step with its envelope (word index ∝ elapsed / `ms`). It replaces the plain "She is saying" line in the Now panel header area.
- **Other inputs:** `mic` → `orb.mic()`, `wake` → `orb.wake()`, `state` → `orb.setState()`.
- **Without WebGL** (or `orb: false`): the orb container is hidden and the current header pill shows as now.

### 3. Layout

- The orb becomes the top section, full width, about 46 dvh on a phone and 56 vh on desktop.
- The state, voice/mic/wake readouts and caption overlay its corners and bottom, as in the draft.
- Below it the existing panels follow (Now, call, session, log).
- The wake panel keeps its graph but loses the big number, which is now on the orb.

### 4. Ambient mode

- A "Full screen" button in the orb corner shows just the orb and caption. It uses the Fullscreen API where allowed and also requests a Screen Wake Lock (both optional, rejection tolerated) for a tablet propped by the TV.
- `#ambient` deep-links straight into it.

### 5. Battery

- Rendering drops to about 30 fps after 10 s idle with no voice, and returns to full rate on any state change.
- `requestAnimationFrame` already pauses in a hidden tab.
- Pixel ratio is capped at 1.75 and the ember count is halved under 640 px, as in the draft.
- `prefers-reduced-motion` renders one still frame per state change.

## Tests

- `tests/test_audio_levels.py`:
  - silence gives all zeros;
  - a full-scale tone is near 1;
  - the envelope length is `ceil(duration × hz)`;
  - clipping never goes above 1;
  - int16 and float32 inputs agree;
  - a 10 s clip runs in under 5 ms.
- `tests/test_speaker_session.py` (extend):
  - the start hook fires before the first write and the end hook after the last;
  - a stop mid-clip gives `stopped=True`;
  - a raising hook does not stop playback;
  - the `sd.play` fallback fires both hooks.
- `tests/test_event_bus.py` (extend):
  - `clip_start`, `clip_end` and `mic` are live-only and never replayed;
  - `MicLevelPublisher` throttles but keeps the peak.
- `tests/test_reply_barge_in.py` (extend): a barge-in produces `clip_end {stopped: true}` for the clip that was playing.
- Orchestrator:
  - the player worker passes text through;
  - `include_transcripts: false` strips `text` from `clip_start`;
  - a `__new__`-built orchestrator with no UI publishes nothing and does not raise.
- `tests/test_live_ui.py` (extend):
  - `vendor/three.min.js` is served under the token and 404s without it;
  - its cache header differs from the page's.
- All new tests go through the existing guards (no real audio device, no adb, no LAN).

## Verification

1. Run `uv run python -m unittest discover -s tests`. The failure set must match master's.
2. `tools/demo_live_ui.py` (new, committed): runs the real `EventBus` + `LiveUI` and feeds a scripted turn whose envelopes come from real WAVs in `sounds/` (or synthetic speech when absent). A Playwright pass then:
   - screenshots the orb in each state at phone and desktop width;
   - checks for no console errors and no horizontal scroll;
   - checks that a `clip_end {stopped}` resets the voice level within one frame.
3. On the real laptop:
   - Run `uv run python main.py` and open the page on the phone. Say "California, put on Fallout".
   - Check that the rim follows your voice while recording, the halo spins while thinking, and the shell ripples in step with her sentence.
   - Tune `voice_offset_ms` until the ripple lands on her syllables.
   - Barge in, and check that the animation stops when she does.
4. Compare `turns.jsonl` timings with `ui.enabled` on and off over 5 turns each. `first_audio` must not move by more than its normal spread.

## Order of work

Each step is shippable on its own.

1. `audio_levels.py` + tests.
2. Pipeline hooks + tests.
3. Orchestrator wiring + mic levels + tests.
4. Vendor Three.js + `live_ui` whitelist + tests.
5. `orb.js` + `app.js` + layout + ambient mode.
6. Demo tool + Playwright pass.
7. CLAUDE.md / AGENTS.md: in the Live UI section, add the clip contract and why levels come from Python.

## Out of scope

- Word-level timing from the TTS. Chirp 3 HD has no timepoints, so words are revealed proportionally to the clip's length.
- Spectrum from real FFT bins. The strip is shaped from the envelope.
- A Three.js halo for the phone call agent's voice.
- Typed turns.
