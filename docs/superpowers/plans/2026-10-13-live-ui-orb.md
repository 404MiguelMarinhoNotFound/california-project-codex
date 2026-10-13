# Live UI halo: bring California to life

Implementation plan. Status: **approved design, not started.**

- Branch: `ccr-cc285211-5jqocu`, restarted from master after PR #51 merged.
- Builds on the live UI that PR #51 shipped: `core/event_bus.py`, `core/live_ui.py` and `ui/`.
- Also on master since then: `core/live_ui_announce.py`, which WhatsApps the URL and opens it in Firefox on boot, and hex tokens. Nothing in this plan touches those; keep `tests/test_live_ui_announce.py` green.

## Contents

1. [Context and decisions](#1-context-and-decisions)
2. [What the halo does](#2-what-the-halo-does)
3. [Architecture](#3-architecture)
4. [Event contract](#4-event-contract)
5. [Python changes, file by file](#5-python-changes-file-by-file)
6. [Mood rules](#6-mood-rules)
7. [Page changes, file by file](#7-page-changes-file-by-file)
8. [Config](#8-config)
9. [Performance budgets](#9-performance-budgets)
10. [Failure modes and guards](#10-failure-modes-and-guards)
11. [Tests](#11-tests)
12. [Verification](#12-verification)
13. [Work breakdown with acceptance criteria](#13-work-breakdown-with-acceptance-criteria)
14. [Rollout and rollback](#14-rollout-and-rollback)
15. [Risks and open questions](#15-risks-and-open-questions)
16. [Out of scope](#16-out-of-scope)

---

## 1. Context and decisions

PR #51 gave California a live, read-only page on the home Wi-Fi: state, wake score, turns building in real time, phone calls and the log. Master Miguel asked for her to be brought to life visually on that page, reacting to her voice.

### Design history

All drafts were simulated, and each was checked against what the code can actually feed it.

| draft | link | verdict |
|---|---|---|
| Research + three concepts (sun, horizon, halo) | https://claude.ai/artifact/X9YVoUXW9xFTfQkCLcpg4F | Liked the sun (A) and the halo (C) |
| Sun core + wireframe shell (openclaw-jarvis-ui) + halo | https://claude.ai/artifact/Y3ctkviHKV4WeeCX5cwuuQ | Liked the motion; the sun was questioned |
| Eyes, and a sun with eyes | https://claude.ai/artifact/4a24vPtQU9SiN8Qf5oRidj | Eyes read as a generic robot; the sun with eyes reads as the grimacing emoji |
| **Halo, Dusk palette** | **https://claude.ai/artifact/N3yAShAhQPWHWksWtjTk1s** | **Chosen** |

### Why the halo

- **The sun was the wrong symbol.** It is a synthwave cliché, a place rather than a personality, and it cannot show expression.
- **Eyes were expressive but generic.** They read as Cozmo or EVE: an assistant, not *her*.
- **The halo keeps what worked.** The ember ring and the voice-reactive core survive, and the moods from the eye drafts are acted out by the ring itself.

### Why Dusk

She is a female voice (Google Chirp 3 HD *Aoede*, named after the Greek muse of song). The yellow-orange belonged to the sun, not to her. Dusk (violet, rose, coral) reads as a California evening without going cliché pink. Pacific (blue/teal) was the runner-up.

### Hard constraints

These come from CLAUDE.md and the code.

- **Never add latency to a turn.** Everything on the hot path is O(1) or a single numpy pass, and publishing never blocks (see `core/event_bus.py`).
- **Her voice never plays in the browser.** It goes out through `sounddevice` on the laptop. The page therefore cannot use Web Audio, and loudness must come from Python. This is the one way California differs from every Jarvis project researched.
- **The page is watch-only.** Nothing added here changes what she does.
- **No new Python dependency.** Three.js is vendored as a static file; `uv.lock` is untouched.
- **Respect `logging.include_transcripts: false`.** No spoken or heard text reaches the page when it is off.
- **Tests stay hermetic.** No audio device, no adb, no LAN socket. The shared fixture already sets `ui.enabled: false`.

---

## 2. What the halo does

Each input changes a small set of animation targets. The halo eases toward them, so changes never jump.

### By state

| state | ring | core | other |
|---|---|---|---|
| idle | Loosens into a dim spherical cloud drifting slowly (`loose → 1`) | Dim, breathing (period about 5.7 s) | Spin 0.08 rad/s |
| wake | Cloud snaps to the ring (`loose → 0` over about 0.2 s); scale pops 12 % | Flash (`wakeFlash = 1`, decays in about 0.6 s) | Fires on the **wake-word hit** itself (see the note below), not only on leaving idle |
| listening | Tightens (`push = -0.12 - mic × 0.12`), leans toward the viewer (x −0.22 rad) | Brightens with the mic level | Spin 0.28 |
| thinking | Spins up (1.6 rad/s); a comet head runs round the ring | Slightly brighter (`heat`) | Comet speed 4.2 rad/s |
| speaking | Pushes out per syllable (`push = voice × 0.22`); embers grow | Scales and flares with the voice | A ripple leaves when the voice crosses 0.55 upward (cooldown 0.32 s) |

**The wake sound.** `_run_activation` plays the acknowledgement line *before* it calls `_set_state("listening")`. With `sounds.activation_blocking: true` (the shipped value), the line plays out entirely while the published state is still `idle`. If the halo keyed the wake on `state` alone, she would say "Sup?" over a dim idle cloud.

So the halo also wakes on:

- `wake_hit` with `phase: "idle"` and no `ignored`. This is the earliest signal, published in `_idle_loop` the instant the detector fires.
- `clip_start` with `kind: "ack"`. It drives the voice level like any clip, so the ring pushes out with the acknowledgement line.

Waking twice is harmless: `wake()` is idempotent while `loose` is already below 0.5. The bootup line (`kind: "boot"`) also drives the voice level, so the halo visibly speaks its greeting at startup.

### By mood

Moods layer on top of the state.

| mood | ring | colour |
|---|---|---|
| happy | Scale 1.06, spin ×1.3, embers twinkle | Warms toward coral |
| smug | Cocked sideways (z 0.38 rad), spin ×0.45 | Slightly warm |
| unimpressed | Flattens toward edge-on (x tilt −0.5), sinks 0.25, spin ×0.25 | Dims 60 % |
| sheepish | Shrinks to 0.82, sinks 0.35, tilts −0.1, spin ×0.5 | Cools toward violet, dims 35 % |
| eye-roll | The whole ring rolls one full turn about x over 1.3 s (eased), then becomes *unimpressed* | as unimpressed |

All of these values come straight from the approved draft. Port them as named constants, not magic numbers.

### Dusk palette

| token | value | use |
|---|---|---|
| `core` | `#ffe1f0` | the light at the centre |
| `ember-cool` | `#b388ff` violet | embers, cool end; sheepish shift |
| `ember-mid` | `#ff6fa8` rose | embers; aura; ripples; **page accent** |
| `ember-warm` | `#ff9a6b` coral | embers, warm end; happy shift |
| `accent-2` | `#b388ff` | secondary page accent |
| `sky` | `#0a0912` | stage background |

The rest of the page (`ui/app.css`) moves its accent from orange `#f28c38` to rose, so the page is one palette. Light-theme counterparts must keep AA contrast on the light background: rose `#c2185b` and violet `#6a3fc8` are the starting points, to be checked with a contrast tool.

---

## 3. Architecture

```text
                         laptop (Python)                                      phone (browser)
 ┌──────────────────────────────────────────────────────────────┐   SSE   ┌───────────────────────────┐
 │ _tts_worker ─(audio, sr, text)─► _audio_player_worker         │         │ app.js                    │
 │                                    │ play_audio(…, text, kind)│         │  HANDLERS                 │
 │ AudioPipeline.play_audio           ▼                          │         │   clip_start → VoiceClock │
 │   on_clip_start(data,sr,text,kind) ──► Orchestrator._on_clip_* │         │   clip_end   → VoiceClock │
 │   on_clip_end(id, stopped)         │   envelope() → publish    │──────►  │   mic        → halo.mic   │
 │ _idle_loop / _record_speech /      │                           │ /events │   state      → halo.state │
 │ _reply_listener ── mic chunk ──► MicLevelPublisher ─► publish  │         │   mood       → halo.mood  │
 │ _handle_tool_call ── tool_ok ──► mood rules ──────► publish    │         │                           │
 │                       EventBus (live-only kinds bypass ring)   │         │ halo.js (Three.js r149)   │
 └──────────────────────────────────────────────────────────────┘         └───────────────────────────┘
```

**Threads.** No new threads are added; every publish runs on a thread that already exists.

| publish | thread |
|---|---|
| `clip_start` / `clip_end` | the audio player thread, or the main thread for the acknowledgement and bootup |
| `mic` | the main thread (idle loop, recorder) or the reply-listener thread |
| `mood` | the main thread |

`EventBus.publish` is already thread-safe and non-blocking.

**Separation.**

- `AudioPipeline` gets callbacks, never the bus. It stays testable without the bus, the same pattern as `GeminiLiveAgent(on_line=)`.
- Dispatchers get no new parameters; tool outcomes travel through a small context helper (§6.2).
- `halo.js` takes inputs only and never touches the network. `app.js` translates events into those inputs.

---

## 4. Event contract

All payloads are JSON in SSE `data:`. The bus adds `id`, `kind` and `ts` (wall-clock epoch seconds).

### 4.1 `clip_start` (live only)

```json
{"clip": 412, "kind": "reply", "hz": 25, "ms": 2840,
 "env": [0.0, 0.12, 0.55, 0.71, 0.48, 0.0, 0.33, ...],
 "text": "Fallout is up on the TV, Master Miguel."}
```

| field | meaning |
|---|---|
| `clip` | Monotonic int per process (`itertools.count`) |
| `kind` | `reply` (a TTS sentence), `ack` (the activation line), `boot` (the bootup line) or `error` (the error sound) |
| `hz` | Envelope sample rate. Always 25 for now, but the page must read it, not assume it |
| `ms` | Clip duration, `round(len(samples) / sr × 1000)` |
| `env` | `ceil(ms / 1000 × hz)` values in [0, 1], 2 decimals |
| `text` | Omitted entirely when `include_transcripts` is false |

### 4.2 `clip_end` (live only)

```json
{"clip": 412, "stopped": false}
```

- `stopped: true` means the clip was cut by `stop_playback()` (barge-in, "stop", shutdown).
- Clips played with `blocking=False` (the non-blocking acknowledgement) never get a `clip_end`. The page ends them by `ms`.

### 4.3 `mic` (live only)

```json
{"level": 0.43, "phase": "recording"}
```

- `phase` is `idle`, `recording` or `reply`.
- At most `ui.mic_publish_hz` (15) per second. Each publish carries the **peak** level since the previous one.

### 4.4 `mood` (replayed)

```json
{"mood": "sheepish", "reason": "control_lights.light_on failed"}
```

- `mood` is `none`, `happy` or `sheepish` in phase 1. Phase 2 adds `smug`, `unimpressed` and `roll`.
- It is replayed so a page opened mid-turn shows the current mood. It is cheap: at most a few per turn.
- The page clears the mood to `none` on `turn_start` and after `hello.mood_hold_ms` of idle.

### 4.5 `hello` additions

Sent per connection.

```json
{"voice_offset_ms": 120, "palette": "dusk", "mood_hold_ms": 6000, "halo": true}
```

### 4.6 Live-only kinds

`core/event_bus.py` already has `_LIVE_ONLY = {"wake", "ping"}`. Extend it to `{"wake", "ping", "clip_start", "clip_end", "mic"}`.

Rationale:

- Replaying a clip would animate audio that finished long ago.
- At 15 Hz the mic would push every turn out of the 500-event ring, the same bug the wake score had (found 2026-10-13).

---

## 5. Python changes, file by file

### 5.1 `core/audio_levels.py` (new)

Pure functions, numpy only, no I/O, no logging on the hot path.

```python
DB_FLOOR = -60.0     # dBFS at or below which the level is 0
DB_CEIL = -10.0      # dBFS at or above which the level is 1

def _to_float(audio) -> np.ndarray:
    """int16 -> float32 in [-1, 1]; float passes through; stereo -> mono mean."""

def _db_to_level(rms: np.ndarray) -> np.ndarray:
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    return np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0, 1.0)

def envelope(audio, sample_rate: int, hz: int = 25) -> list[float]:
    """Loudness per 1/hz s window, 0..1, rounded to 2 decimals.

    Vectorised: pad to a whole number of windows, reshape (n, win), take
    sqrt(mean(x**2, axis=1)). Never raises on empty input: returns [].
    """

def level(chunk) -> float:
    """One mic chunk's loudness on the same 0..1 scale."""
```

**Why dBFS and not raw RMS.** Speech RMS spans about 30 dB. Linear RMS leaves quiet syllables near zero, and the ring would only move on the loudest words. Measured RMS in this project (CLAUDE.md, wake word notes):

| source | RMS (int16) | dBFS | level |
|---|---|---|---|
| room tone | ~5 | about −76 dBFS | 0 |
| her voice through the mic | ~480 | about −37 dBFS | 0.46 |
| his voice | ~840 | about −32 dBFS | 0.56 |

TTS output is near full scale, so `reply` clips sit around 0.7–0.9. **Calibrate the floor and ceiling once on real clips** in step 7. The constants live at module top for that reason.

### 5.2 `core/event_bus.py`

1. Extend `_LIVE_ONLY` (§4.6).
2. Factor the throttle-with-peak logic out of `WakeScorePublisher` into `class PeakThrottle(bus, kind, hz, clock)`, with `offer(value, **extra)` publishing `{key: peak, **extra}`.
   - `WakeScorePublisher` becomes a thin subclass that keeps its current behaviour, so existing tests pass untouched.
   - `MicLevelPublisher(PeakThrottle)` publishes `mic` with key `level`.

### 5.3 `core/audio_pipeline.py`

Add to `__init__`:

```python
# Called for every clip that actually starts playing, so the live UI can
# animate her voice. Set by the orchestrator; None means nobody is watching.
self.on_clip_start = None   # (data, sample_rate, text, kind) -> clip_id | None
self.on_clip_end = None     # (clip_id, stopped: bool) -> None
```

Two private helpers that can never raise:

```python
def _clip_started(self, data, sr, text, kind):
    hook = self.on_clip_start
    if hook is None:
        return None
    try:
        return hook(data, sr, text, kind)
    except Exception:
        logger.debug("on_clip_start failed", exc_info=True)
        return None

def _clip_ended(self, clip_id, stopped):
    ...  # same shape; no-op when clip_id is None
```

`play_audio(self, audio_data, sample_rate, blocking=True, *, text="", kind="reply")` has three exit paths. Each must fire the hooks correctly:

| path | start hook | end hook |
|---|---|---|
| `not blocking or self._speaker is None` → `sd.play(...)` | before `sd.play` | after it **only when blocking**, `stopped=self._abort.is_set()` |
| abort already set → `return` | **not fired**: the clip never plays | — |
| reopen failed → `sd.play(..., blocking=True)` | before | after |
| session write loop | before the first `stream.write` (after the `data.size == 0` early return) | after the loop; `stopped=True` on the abort `return` and on the abort-in-except path |
| write failed → fallback `sd.play` | (already fired) | after the fallback |

Use one `try/finally` around the session branch so every return reports exactly once. A `fired` flag prevents a double end on the fallback path.

`play_activation_sound` passes `text=text, kind="ack"` on the blocking path. On the non-blocking `sd.play(data, sr, blocking=False)` path it calls `_clip_started(data, sr, text, "ack")` directly, and no end.

`play_error_sound` passes `kind="error"`. `Orchestrator._play_bootup_sound` passes `kind="boot"`.

### 5.4 `core/orchestrator.py`

**Live UI wiring (in `_start_live_ui`, after the bus exists):**

```python
self._mic_meter = MicLevelPublisher(self.bus, hz=float(ui_cfg.get("mic_publish_hz", 15)))
self._clip_ids = itertools.count(1)
self._include_text = (config.get("logging") or {}).get("include_transcripts", True) is not False
self.audio.on_clip_start = self._on_clip_start
self.audio.on_clip_end = self._on_clip_end
```

Set them *after* phase 1 builds `self.audio`. `_start_live_ui` currently runs first in `__init__`, so split it:

- `_start_live_ui()` stays at the top. It creates the bus and the server, so the boot log reaches the page.
- `_attach_live_ui()` runs at the end of `__init__` and installs the hooks.

**Handlers:**

```python
def _on_clip_start(self, data, sr, text, kind):
    clip = next(self._clip_ids)
    payload = {"clip": clip, "kind": kind, "hz": 25,
               "ms": int(round(len(data) / sr * 1000)),
               "env": envelope(data, sr, 25)}
    if text and self._include_text:
        payload["text"] = text
    self.bus.publish("clip_start", payload)
    return clip

def _on_clip_end(self, clip, stopped):
    self.bus.publish("clip_end", {"clip": clip, "stopped": bool(stopped)})
```

Class-level defaults keep `__new__`-built test orchestrators working: `_mic_meter = None`.

**Text through to the pipeline.** In `_audio_player_worker`, change `self.audio.play_audio(audio_data, sample_rate, blocking=True)` to pass `text=text`. The `sentence` event already published there stays: the turn panel uses it.

**Mic levels.** Add one line where a chunk already exists:

| where | chunk variable | phase |
|---|---|---|
| `_idle_loop`, after `bytes_to_numpy` | `audio_chunk` | `idle` |
| `_record_speech`, inside the read loop | `audio_chunk` (`_chunk_rms` is already computed there) | `recording` |
| `_reply_listener`, after `bytes_to_numpy` | `chunk` | `reply` |

```python
def _offer_mic(self, chunk, phase):
    meter = self._mic_meter
    if meter is not None:
        meter.offer(level(chunk), phase=phase)
```

`level()` costs about 10 µs on a 640-sample chunk, which is negligible against the 40 ms chunk period.

**Mood rules** live in `_handle_tool_call` and `_handle_activation`; see §6.

### 5.5 `core/live_ui.py`

- **Whitelist:** add `"vendor/three.min.js": ("vendor/three.min.js", "text/javascript; charset=utf-8")` to `_STATIC`. The handler's `rest` lookup is an exact dict match, so a path with a subdirectory needs no joining logic. The existing `../` tests stay valid.
- **Caching:** per-file headers. The vendored file gets `Cache-Control: public, max-age=86400, immutable`; everything else keeps `no-store`. Implement as an optional third tuple element in `_STATIC` instead of a special case.
- **CSP:** unchanged (`script-src 'self'` already allows the vendored file). Add `worker-src 'none'` for clarity.
- **`hello`:** `_hello(config)` adds `voice_offset_ms`, `palette` (`"dusk"`), `mood_hold_ms` and `halo` from the `ui` config.

### 5.6 `ui/vendor/three.min.js` (new, vendored)

- Source: `three@0.149.0/build/three.min.js`, the last release shipping the UMD `three.min.js` global build.
- Size: about 600 KB minified, about 150 KB gzipped. The stdlib server does not gzip, so it is a one-off 600 KB download per phone, cached for a day.
- Keep the MIT licence header at the top of the file.
- Add `ui/vendor/README.md` naming the version, the source URL and the SHA-256, so a later upgrade is deliberate.

---

## 6. Mood rules

### 6.1 Phase 1 rules

| trigger | mood | reason string |
|---|---|---|
| A tool call raised (`result is None` in `_handle_tool_call`'s `finally`) | sheepish | `"<tool>.<action> raised"` |
| A dispatcher reported `ok=False` | sheepish | `"<tool>.<action> failed"` |
| A dispatcher reported `ok=True` | happy | `"<tool>.<action> worked"` |
| The turn outcome is `error` (`_handle_activation` `finally`) | sheepish | `"turn error"` |
| `turn_start` | none (page side) | — |
| Several tools in one turn | **the last report wins**, except that any failure in the turn beats a later success | — |

Questions are never failures. A read-back ("Send it to Marta?"), "which one?", `requires_confirmation` and "I couldn't find X" for a lookup report **nothing**, so she does not look sheepish for asking.

### 6.2 How dispatchers report: a context helper, no signature changes

All dispatchers are module-level and return strings. CLAUDE.md requires them to stay module-level and service-injected. Threading a `turn` through every signature would touch dozens of call sites and tests. Instead:

```python
# core/orchestrator.py, module level
_TOOL_OUTCOME: contextvars.ContextVar[bool | None] = contextvars.ContextVar("tool_outcome", default=None)

def _report(ok) -> None:
    """Record whether the device action this tool call asked for actually happened.

    Compared with `is True` / `is False`: tests pass bare Mocks, whose results
    are truthy Mocks, and a Mock must never be read as a success.
    """
    if ok is True or ok is False:
        _TOOL_OUTCOME.set(ok)
```

`_handle_tool_call` resets the var before dispatch and reads it after:

```python
token = _TOOL_OUTCOME.set(None)
try:
    result = self._dispatch_tool(tool_name, tool_input)
    return result
finally:
    ok = _TOOL_OUTCOME.get()
    _TOOL_OUTCOME.reset(token)
    self._turn.tool(tool_name, action, started, ..., ok=ok)   # TurnTimer.tool gains ok=
    self._publish_mood_for_tool(tool_name, action, result, ok)
```

Tool calls run synchronously on the main thread inside the LLM stream. A context var therefore cannot leak between calls, and needs no locking.

### 6.3 Where `_report` is called (phase 1 allow-list)

Only device actions whose success is already a typed, checked result:

| dispatcher | action(s) | success signal already in the code |
|---|---|---|
| `_dispatch_lights` | `light_on`, `light_off`, `light_brightness`, `light_color` | `result = govee_svc.set_…(…)`, then `if result:`. Report `bool(result) is True` only when `isinstance(result, GoveeCommandResult)` |
| `_dispatch_vacuum` | `vacuum_clean_all`, `vacuum_clean_rooms`, `vacuum_stop`, `vacuum_dock` | `DeebotCommandResult`, same pattern |
| `_dispatch_tv` | `stremio_play`, `stremio_continue` | `result.success` and `not result.requires_confirmation`; report True only for `target_mode == "episode"` (actually playing) |
| `_dispatch_tv` | `youtube_playlist`, `youtube_search` | the confirmed-play branches (session stamp moved); the results-page fallback reports nothing |
| `_dispatch_tv` | `turn_on`, `turn_off` | `media_svc.last_wake_result.success` for on, the standby result for off. `tv_confirmed is False` → report False |
| `_dispatch_tv_volume` | `volume_*`, `mute`, `unmute` | the `TvVolume` result is truthy on success; `needs_confirm` reports nothing |
| `_dispatch_tv` | `switch_hdmi` | `result = media_svc.switch_hdmi(port)`, `if result:` |
| `_guard_whatsapp_send` | `whatsapp_send` | already computes `line.startswith(("Sent to", "Set."))`. Reuse: True on sent; False only on an outright failure line (`timeout`, `unconfirmed`, `not_linked`), never on a read-back |
| `_dispatch_phone` | — | not in phase 1: a call's outcome arrives minutes later as a report |

Status and read actions (`get_status`, `light_status`, `vacuum_status`, `whatsapp_unread`/`read`/`find_contact`, `phone_status`/`log`, `stremio_get_progress`) report nothing.

Each `_report` call sits **next to** an existing `if result:` and changes no spoken line. A reviewer can check the diff line by line.

### 6.4 Phase 2 (deferred): she tags her own mood

Only if phase 1 feels flat after a week of use.

- **Prompt:** add one line to `llm.system_prompt`: an optional leading tag `[happy] [smug] [unimpressed] [sheepish] [roll]`, at most one per reply.
- **Parsing:** in `_timed_tokens` (already the one wrapper around the token stream), buffer the first ≤ 16 characters.
  - If they match `^\s*\[(\w+)\]\s*`, strip the tag, publish `mood` and pass the rest through.
  - A `[` anywhere later that forms a known tag is also stripped, as a safety net, before `sentence_chunker`.
- **Measure before shipping** with `tools/eval_phone_conversation.py`-style runs over 30 real prompts:
  - token cost per turn (expected under 5);
  - the share of replies carrying a tag;
  - any tag reaching TTS. **The target is zero.** One leaked "bracket smug bracket" spoken aloud is a worse bug than no moods.

---

## 7. Page changes, file by file

### 7.1 `ui/halo.js` (new)

A port of the approved draft's renderer with the palette switcher, shell and demo controls removed. It exposes one factory on `window`. The page has no module bundler, and an ES module would need its own CSP and whitelist thought; a global keeps it simple.

```js
window.createHalo = function createHalo(canvas, opts) -> {
  setState(state),            // "idle" | "listening" | "thinking" | "speaking" | "unknown"
  setVoice(level),            // 0..1, already smoothed by VoiceClock
  setMic(level),              // 0..1, raw; smoothed inside
  setMood(mood),              // "none" | "happy" | "smug" | "unimpressed" | "sheepish" | "roll"
  wake(),                     // snap the idle cloud into the ring with the flash; idempotent
  resize(), dispose(),
  get ok()                    // false when WebGL failed; caller shows the pill instead
}
```

`opts` is `{small, reducedMotion, palette}`.

**Contents**, each a named section in the file:

1. **Renderer setup:** pixel ratio ≤ 1.75; transparent clear; `try/catch` around WebGL creation.
2. **Embers:** a `BufferGeometry` of N points (1800, or 900 when `small`). Per-point attributes: `aAngle`, `aRad`, `aSpeed`, `aY`, `aSize`, `aPhase` and `aCloud` (vec3). The shader mixes ring and cloud positions by `uLoose`, and implements the comet (`pow(fract((ang - uComet)/2π), 10) × 1.3 × uHeat`) and the twinkle (`uSparkle`).
3. **Core and aura:** two additive sprites sharing one radial-gradient `CanvasTexture`.
4. **Ripples:** a pool of 6 thin `RingGeometry` meshes, camera-facing.
5. **Mood targets:** the `M` object from the draft (`tiltZ, droop, scale, spinMul, warm, cool, dim, sparkle, flat`), eased with attack 0.25 s / release 0.4 s. The roll uses `since = now - moodStart`: `angle = (1 - cos(π·k))/2 · 2π` for `k = since/1.3`.
6. **Drag-to-turn with spring-back:** pointer events on the canvas only. `touch-action: none` on the canvas, so a page scroll on the phone still works outside it.
7. **The frame loop:** `requestAnimationFrame`, `dt` capped at 50 ms.
   - **Idle throttle:** after 10 s in idle with voice and mic below 0.02, render every other frame (about 30 fps). Any `set*` call restores full rate.
   - **Reduced motion:** with `reducedMotion`, there is no loop; each `set*` call renders about 30 settled frames once.

**Smoothing constants** (from the draft): voice attack 0.06 s / release 0.2 s; mic 0.05 / 0.25; heat 0.4 / 0.6; push 0.08 / 0.3; loose 0.18 / 0.9.

### 7.2 `ui/app.js`

Add a `VoiceClock` that turns `clip_start` / `clip_end` into a per-frame voice level:

```js
const VoiceClock = {
  clip: null,                                   // {id, start, hz, env, ms}
  begin(d) { this.clip = { id: d.clip, start: performance.now() + offsetMs,
                           hz: d.hz, env: d.env, ms: d.ms }; },
  end(d)   { if (this.clip && this.clip.id === d.clip && d.stopped) this.clip = null; },
  level(now) {
    const c = this.clip; if (!c) return 0;
    const t = (now - c.start) / 1000;
    if (t < 0) return 0;                         // waiting out voice_offset_ms
    if (t * 1000 > c.ms + 150) { this.clip = null; return 0; }
    const i = Math.floor(t * c.hz);
    return c.env[Math.min(i, c.env.length - 1)] || 0;
  },
};
```

**Rules:**

- A newer `clip_start` replaces the current clip. Sentences are sequential; the acknowledgement overlapping the recorder is fine.
- A non-stopped `clip_end` does nothing. The clip ends by `ms`, which already accounts for `voice_offset_ms` drift.
- A stopped `clip_end` drops the clip at once, so the ring settles within one release time (0.2 s).
- `offsetMs` comes from `hello.voice_offset_ms`.

**New `HANDLERS` entries:** `clip_start`, `clip_end`, `mic`, `mood`.

- The existing `state` handler also calls `halo.setState`.
- The existing `wake_hit` handler calls `halo.wake()` when `phase === "idle" && !ignored`.
- `clip_start` with `kind === "ack"` also calls `halo.wake()` (§2, the wake sound).

**Caption.** On `clip_start` with `text`, the halo caption shows the sentence. Words are revealed at `index = ceil(words × elapsed / ms)` on each animation frame, the same calculation as the draft's `renderCaption`. Without `text`, the caption stays empty.

**One rAF loop drives the halo.** Each frame calls `halo.setVoice(VoiceClock.level(now))`. The halo does its own smoothing.

**Mood hold.** On `mood`, call `setMood`. Clear to `none` on `turn_start`. In idle, clear after `mood_hold_ms`.

**Reconnect.**

- `resetAll()` (already called on a new `hello.started`) also calls `halo.setMood("none")` and `VoiceClock.clip = null`.
- When the link is not live, the state goes to `unknown`, which the halo renders as idle but at 40 % brightness, so a stale page never looks alive.

### 7.3 `ui/index.html`

- **Orb section.** A new first section, `<section class="halo" id="halo">`, contains:
  - `<canvas aria-label="California">`;
  - the HUD (`CALIFORNIA`, state, mood) and a caption `<div aria-live="polite">`;
  - a "Full screen" button.
- **Scripts:** `<script src="vendor/three.min.js"></script>` before `halo.js`, then `app.js`. All are relative, so they inherit the token path.
- **The existing header pill stays.** It is hidden by CSS when the halo is OK, and shown when `halo.ok` is false or `hello.halo` is false.

### 7.4 `ui/app.css`

- **Tokens:** replace `--accent: #f28c38` (and its light value) with the Dusk rose. Add `--accent-2` violet. Check every component that used `--accent` (state pill, meter, links, chips, follow button) for contrast in both themes.
- **Halo section:** full width; `height: 46dvh` under 640 px and `56vh` above (`min-height: 320px`). Radial-gradient background from `--sky`. The HUD is absolutely positioned like the draft.
- **Ambient mode:** `body.ambient` hides everything except the halo section, which becomes `position: fixed; inset: 0`.
  - The button toggles it: `requestFullscreen()`, then `navigator.wakeLock.request("screen")`. Both are optional; a rejection is ignored.
  - `#ambient` in the URL turns it on at load.
  - `Escape` and `visibilitychange` re-request the wake lock as the spec requires.
- **Reduced motion** is already globally handled for CSS; `halo.js` handles the canvas.

---

## 8. Config

`config.yaml`, `ui:` block. Each key gets a comment saying what it does and why the default.

| key | default | meaning |
|---|---|---|
| `halo` | `true` | `false` shows the plain state pill instead |
| `voice_offset_ms` | `120` | Delay before the page animates a clip, matching the laptop speaker's output latency (CLAUDE.md measured about 100–180 ms of PortAudio buffering on MME). Tune by eye in step 7 |
| `mic_publish_hz` | `15` | Mic level rate. Each publish carries the peak since the last |
| `mood_hold_ms` | `6000` | How long a mood lingers in idle before it clears |
| `palette` | `"dusk"` | Reserved for later palettes; only `dusk` ships |

`tests/config_fixture.py` needs nothing new: `ui.enabled` is already forced off.

---

## 9. Performance budgets

| item | budget | how it is checked |
|---|---|---|
| `envelope()` on a 10 s, 24 kHz clip | < 5 ms (expected < 1 ms) | unit test with `time.perf_counter` |
| `level()` per 640-sample chunk | < 0.1 ms | unit test |
| `clip_start` payload | < 1 KB per 4 s sentence | unit test on the JSON length |
| `first_audio` in `turns.jsonl`, UI on vs off | no change beyond its normal spread over 5 turns | manual (§12.4) |
| Page frame time on a mid-range phone | 60 fps active, about 30 fps idle | manual, Chrome DevTools remote |
| First page load over Wi-Fi | three.min.js about 600 KB once, then cached | manual |

---

## 10. Failure modes and guards

| failure | effect | guard |
|---|---|---|
| A hook raises | Playback must continue | `_clip_started` / `_clip_ended` swallow and log at DEBUG |
| The bus is a `NULL_BUS` (UI off) | No hooks installed; `on_clip_*` stay `None` | `_attach_live_ui` returns early when `live_ui is None` |
| WebGL unavailable on the phone | No halo | `halo.ok === false` → the existing pill and panels show; an inline line says "This browser can't draw the halo" |
| The three.min.js request fails | `createHalo` undefined | `app.js` checks `typeof window.createHalo === "function" && window.THREE`; same fallback |
| Wi-Fi drops mid-clip | The clip plays out on its own clock and ends by `ms` | `VoiceClock` ends by duration; the stalled indicator already exists |
| Clock skew between laptop and phone | None: the page uses its own `performance.now()` from receipt | No cross-device timestamps are used for animation |
| Barge-in while a clip animates | The ring keeps pulsing after she stopped | stopped `clip_end` drops the clip at once (test in §11) |
| `include_transcripts: false` | Text must not reach the page | `_on_clip_start` omits `text` (test) |
| A bare `Mock` service in a dispatcher test | Could be read as success | `_report` accepts only `True`/`False`, plus an `isinstance` check on typed results (test) |
| The tool context var leaks across tool calls | A wrong mood | set/reset with a token in `_handle_tool_call` (test with two sequential calls) |

---

## 11. Tests

All new tests run under the existing hermetic guards. Each name states the behaviour it pins.

### `tests/test_audio_levels.py` (new)

- `test_silence_is_all_zeros`
- `test_full_scale_tone_is_near_one`
- `test_never_exceeds_one_even_when_clipping`
- `test_length_is_duration_times_hz_rounded_up`
- `test_int16_and_float32_agree`
- `test_stereo_is_mixed_to_mono`
- `test_empty_input_returns_empty_list`
- `test_quiet_speech_is_visible` (a −40 dBFS tone gives a level above 0.3)
- `test_ten_second_clip_is_fast` (< 5 ms)
- `test_level_matches_envelope_scale`

### `tests/test_event_bus.py` (extend)

- `test_clip_and_mic_events_are_live_only`
- `test_mood_events_are_replayed`
- `test_mic_publisher_keeps_the_peak_between_publishes`
- `test_wake_score_publisher_unchanged_after_refactor` (the existing tests keep passing; this one guards the subclass)

### `tests/test_speaker_session.py` (extend)

- `test_start_hook_fires_before_the_first_write_and_end_after_the_last`
- `test_stop_mid_clip_reports_stopped_true`
- `test_clip_refused_by_an_existing_stop_fires_no_hooks`
- `test_raising_hook_never_stops_playback`
- `test_sd_play_fallback_fires_both_hooks`
- `test_nonblocking_activation_fires_start_only`
- `test_each_clip_reports_end_exactly_once` (including the write-failure fallback path)

### `tests/test_reply_barge_in.py` (extend)

- `test_barge_in_publishes_stopped_clip_end_for_the_clip_playing`

### `tests/test_activation_phrases.py` or the wiring test file (extend)

- `test_activation_line_publishes_an_ack_clip_before_state_listening` pins the order the page relies on: `wake_hit` → `clip_start(kind=ack)` → `state: listening`.

### `tests/test_live_ui_orchestrator.py` (new)

Orchestrator wiring, built with `__new__` like the others.

- `test_player_worker_passes_text_to_play_audio`
- `test_clip_start_carries_envelope_duration_and_kind`
- `test_include_transcripts_false_omits_clip_text`
- `test_mic_levels_offered_from_idle_recording_and_reply`
- `test_orchestrator_without_ui_publishes_nothing_and_never_raises`

### `tests/test_mood_rules.py` (new)

- `test_raising_tool_is_sheepish`
- `test_turn_error_is_sheepish`
- `test_light_on_success_is_happy` / `test_light_on_failure_is_sheepish` (`GoveeCommandResult` true/false)
- `test_vacuum_dock_success_is_happy` / `..._failure_is_sheepish`
- `test_stremio_episode_playing_is_happy`, `test_stremio_detail_page_reports_nothing`, `test_stremio_requires_confirmation_reports_nothing`
- `test_whatsapp_read_back_reports_nothing`, `test_whatsapp_sent_is_happy`
- `test_status_actions_report_nothing` (parametrised over every status/read action)
- `test_bare_mock_result_is_never_a_success`
- `test_outcome_does_not_leak_between_tool_calls`
- `test_failure_in_a_turn_beats_a_later_success`
- `test_turn_record_carries_tool_ok`

### `tests/test_live_ui.py` (extend)

- `test_vendored_three_is_served_under_the_token`
- `test_vendored_three_is_404_without_the_token`
- `test_vendored_three_is_cacheable_and_the_page_is_not`
- `test_hello_carries_voice_offset_palette_mood_hold_and_halo`

**Page JavaScript.** The repo has no JS test runner and this plan does not add one. `VoiceClock` and the halo are covered by the Playwright pass in §12.2. That gap is acknowledged; a later PR could add Node's built-in `node:test` for `VoiceClock`, which is pure.

---

## 12. Verification

### 12.1 Suite

```bash
uv run python -m unittest discover -s tests
```

The failure set must equal master's in the same environment. In the cloud container that is 83 failures from missing extras. Diff the sorted `FAIL:`/`ERROR:` lines, as done for PR #51.

### 12.2 `tools/demo_live_ui.py` (new, committed)

- Starts a real `EventBus` + `LiveUI` on `127.0.0.1` with token `demo`.
- Publishes three scripted turns using the **real** `envelope()` on WAVs found under `sounds/` (bootup and activation clips), or synthetic speech when none exist:
  - "put on Fallout" ends happy;
  - a failed light ends sheepish;
  - a barge-in ends with a stopped `clip_end`.
- Flags: `--port`, `--loop`, `--no-text`.

A Playwright script (`--check`) then opens the page at 390×844 and 1280×900:

- screenshots each state and mood into `debug/live_ui/`;
- asserts no console errors and no horizontal scroll;
- asserts that within 300 ms of a stopped `clip_end` the halo's exposed debug level (`window.__halo?.voice`, set only when `?debug` is in the URL) is below 0.05.

### 12.3 On the real laptop (with Master Miguel)

1. Run `uv run python main.py` and open the boot-log URL on the phone.
2. Idle: a dim drifting cloud. Say "California": the cloud snaps into a ring with a flash **as the acknowledgement line starts**, and the ring moves with that line.
3. "Put on Fallout":
   - the ring edge follows his voice while recording;
   - the comet runs while she thinks;
   - the ring pushes out in step with her syllables, and the caption keeps pace;
   - it ends happy (warm, sparkling).
4. Tune `voice_offset_ms`: adjust until the ripples land on her syllables. Record the value in `config.yaml` with the date.
5. "Turn on the attic lights" with the strip unplugged: it ends sheepish.
6. Barge in mid-reply: the ring settles as she stops.
7. Ambient mode on the phone: full screen, and the screen stays awake for 5 minutes.

### 12.4 Latency

Five turns each with `ui.enabled: true` (phone connected) and `false`. Compare `first_audio` and `reply_done` medians in `logs/turns.jsonl`. Any shift beyond the spread of the off-runs is a regression to investigate before merging.

---

## 13. Work breakdown with acceptance criteria

Each step is a separate commit, and each leaves the app working.

| # | step | files | done when |
|---|---|---|---|
| 1 | Loudness functions | `core/audio_levels.py`, `tests/test_audio_levels.py` | 10 tests pass; budgets met |
| 2 | `PeakThrottle`, `MicLevelPublisher`, live-only kinds | `core/event_bus.py`, `tests/test_event_bus.py` | old and new bus tests pass; `WakeScorePublisher` behaviour unchanged |
| 3 | Clip hooks in the pipeline | `core/audio_pipeline.py`, `tests/test_speaker_session.py` | 7 hook tests pass; every exit path reports exactly once |
| 4 | Orchestrator wiring: `_attach_live_ui`, `_on_clip_*`, text pass-through, mic levels | `core/orchestrator.py`, `tests/test_live_ui_orchestrator.py`, `tests/test_reply_barge_in.py` | wiring tests pass; a full suite diff equals master's |
| 5 | Mood rules: `_TOOL_OUTCOME`, `_report` calls (§6.3), `tool_ok` on the turn, `mood` publishing | `core/orchestrator.py`, `core/turn_log.py`, `tests/test_mood_rules.py` | mood tests pass; every `_report` sits next to an existing success check, and no spoken line changes |
| 6 | Server: vendored Three.js, cache headers, `hello` fields, config keys | `core/live_ui.py`, `ui/vendor/`, `config.yaml`, `tests/test_live_ui.py` | live UI tests pass; the vendor README has a SHA-256 |
| 7 | The page: `halo.js`, `VoiceClock`, handlers, layout, Dusk tokens, ambient mode, fallback | `ui/*` | §12.2 Playwright pass is clean at both widths in both themes |
| 8 | Demo tool | `tools/demo_live_ui.py` | runs standalone; `--check` passes |
| 9 | Live calibration with Master Miguel | `config.yaml` (`voice_offset_ms`), `core/audio_levels.py` (floor/ceiling if needed) | §12.3 checklist done; §12.4 shows no latency shift |
| 10 | Docs | CLAUDE.md + AGENTS.md (Live UI section, project structure, test coverage list, session log) | both files updated in the same commit, as the repo's convention requires |

---

## 14. Rollout and rollback

- **Kill switches, from smallest to largest:**
  - `ui.halo: false` restores the PR #51 page exactly (pill and panels);
  - `ui.enabled: false` removes the UI and every hook: `on_clip_*` stay `None`, and the mic and mood meters are `None`.
- **No migration.** Nothing persists: no new files on disk, and `turns.jsonl` gains one optional `tool_ok` field per tool, which readers already ignore if unknown.
- **Reverting** any single step's commit leaves the app working, because each step is additive (§13).

---

## 15. Risks and open questions

| risk / question | likelihood | mitigation |
|---|---|---|
| The `voice_offset_ms` sync feels off because MME latency varies per device | medium | Tunable; the ripples tolerate ±60 ms visually. If needed, measure actual latency with `sd.OutputStream.latency` and publish it in `hello` |
| The dBFS mapping leaves her TTS pinned near 1, making the ring move too little | medium | Floor and ceiling are constants calibrated in step 9 against real Chirp clips. Fallback: normalise each clip's envelope to its own 95th percentile |
| 600 KB of Three.js on a slow phone | low | One-off and cached. A 2D-canvas halo is possible later if the Pi page must be lighter (the research draft C was 2D) |
| Phase-1 moods too sparse to notice | medium | Expected: happy and sheepish only. Phase 2 exists for this, gated on measurement |
| Moods feel judgemental ("she's always sheepish") | low | Moods are short (cleared at the next turn, 6 s hold); failures are rare with the current reliability work |
| **Open:** should the phone-call agent's lines also drive the halo while a call is on? | — | Not in this plan; decide after using it |
| **Open:** a second palette later? | — | `ui.palette` is reserved; only Dusk ships |

---

## 16. Out of scope

- Word-level timing. Chirp 3 HD returns no timepoints, so words are spread over the clip's duration.
- Phase 2 moods (`smug`, `unimpressed`, `roll`) until measured (§6.4).
- A real frequency spectrum.
- A halo for the phone-call agent.
- Typed turns and any write path from the page.
- A JS unit-test runner (see §11).
