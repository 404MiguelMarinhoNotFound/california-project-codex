# TV Wake Optimization: Design

**Date:** 2026-10-02
**Branch:** `feat/wake-optimization`
**Status:** approved in conversation, awaiting written-spec review

## Goal

Cut the time from a voice command to a usable screen on two paths that Master
Miguel hits day to day:

1. **Deep-standby wake.** The box has suspended and dropped off the LAN,
   usually because the Samsung remote's power button broadcast `<Standby>`.
   Measured 34-42s on 2026-09-25.
2. **Dark room to playing.** "Put on <show>" from a dark room, through the wake,
   the Stremio launch and confirmed playback. About 25s from a shallow start
   (`tv_only_standby`); more from deep standby.

Success is either a measured median improvement on either path, or a measured
explanation of why a lever cannot move it. Both outcomes are written into
CLAUDE.md.

## Constraints

- **Allowed:** unattended TV/box power cycling during benchmarks; box settings
  changed over ADB, provided originals are captured and restored; TV
  user-menu settings flipped by hand by Master Miguel.
- **Not allowed:**
  - Root, bootloader unlock and factory reset.
  - Any box reboot. `persist.adb.tcp.port` is empty, so ADB over Wi-Fi may
    not come back after one.
  - `KEYCODE_POWER`, in any state.
- **Samsung service-menu changes need their own explicit yes.** This approval
  does not cover them.
- **One TV and one box.** Hardware runs are strictly serialized; parallel
  subagents do research and analysis only.

## Research inputs (2026-10-02, read-only subagents)

- **Samsung (UE49M5505, 2017).**
  - No user-menu warm-standby option.
  - No Anynet+ sub-option to stop powering connected devices off; Anynet+ is
    On/Off only.
  - The only lead is "Instant On / Always Instant On Support" in the service
    menu (*Option > MRT Option*). Evidence that it exists is medium, and there
    is none on its effect on CEC start time.
  - No published boot or CEC timings exist; the 37s is ours.
- **Android 11 AOSP (`services/core/java/com/android/server/hdmi/`).**
  - `hdmi_control_enabled` is the only shell-writable gate on TV-to-box
    `<Standby>`.
  - `HdmiControlService.standby()` refuses while `canGoToStandby()` is false.
    For a playback device that means "the keep_awake wakelock is not held",
    and the wakelock is held while the box is the active source with
    `persist.sys.hdmi.keep_awake` true (the AOSP default).
  - CLAUDE.md currently says that property "is not consulted on the
    `<Standby>` path". This contradicts it and has to be checked on the box
    (H10).
  - With CEC off, a box wake sends nothing on the bus. Switching CEC from 0
    to 1 while awake broadcasts `<Active Source>` but no `<Text View On>`.
  - The forced suspend is vendor code; nothing in shell speeds the resume.
- **Code audit.**
  - Stremio prep (library sync, TMDB lookup, `build_deep_link`) uses no ADB
    and could overlap the wake.
  - `_wait_for_box` passes cost ~3.7-4s each, so detection lags ~2s on
    average.
  - `is_awake()` rediscovery and `tv_power()` probes run before the WoL
    packet is sent.
  - On the play path, `_wait_for_stremio_foreground` is redundant with
    `list_ready`, and the pre-OK `_first_card_label` dump blocks the OK press.

## Hypotheses

| id | path | hypothesis | kind |
|---|---|---|---|
| H1 | deep | Samsung service-menu Instant On lowers the ~37s CEC start | TV setting, **optional, separate yes** |
| H6 | deep | Sending WoL (and speaking) before the reachability scan and UPnP probes saves ~2-4s | code |
| H7 | deep | `_wait_for_box` probing the known static IP every ~0.5s, full rescan only every Nth pass, saves ~1.5-3s | code + config |
| H8 | play | Preparing the Stremio request (sync, resolve, link) on a thread during the wake hides ~1-3s, and up to 25s on a slow network | code |
| H9 | play | Dropping `_wait_for_stremio_foreground`, and skipping or capping the pre-OK dump when a source is remembered, saves ~1-3s | code |
| H10 | deep (avoidance) | The box sleeps on the Samsung remote's `<Standby>` because it was not the active source or `keep_awake` is false; if the former, claiming the active source keeps it awake | read-only measurement, then one experiment |

Code-audit estimates are hypotheses. None counts until it shows up in the
timeline.

## §1 Measurement

### Per-phase timeline

`tools/bench_tv_power.py` gains `--json`, which writes one record per run with
millisecond offsets from `t0`. A phase that did not happen is absent, never
zero, the same rule as `logs/turns.jsonl`.

| phase | meaning |
|---|---|
| `wol_sent` | first Wake-on-LAN packet |
| `tv_8001_up` | TV REST answers |
| `tv_9197_up` | TV UPnP answers (powered) |
| `tv_cec_first` | first TV-originated CEC message after `t0`, read off the box's dump |
| `box_on_lan` | port 5555 answers |
| `boot_completed` | `sys.boot_completed` = 1 |
| `tv_showing_box` | `tv_power == on` and the box is the active source |
| `deeplink_fired` | Stremio `am start` returned |
| `stream_list_visible` | `list_ready` true |
| `ok_pressed` | the one OK |
| `player_up` | ExoPlayer views present |
| `playing` | Stremio session `state=3` |

Each record also stores the scenario, git SHA, the spike id, the captured box
settings, and the outcome.

Records append to `bench/results/<date>-<scenario>.jsonl`; `bench/` is added to
`.gitignore`. `tools/bench_report.py` prints the median, minimum and maximum per
phase per scenario, and a diff against a named baseline file.

### Scenarios

| id | start state | action | N |
|---|---|---|---|
| S1 | Both off via the Samsung remote, then at least 120s idle (box suspended) | `turn_on()` | 3 |
| S2-deep | as S1 | `stremio_play` Fallout | 3 |
| S2-shallow | California's `turn_off` (`tv_only_standby`), then at least 60s idle | `stremio_play` Fallout | 3 |

Reaching S1's start state unattended means getting the box into deep standby
without Master Miguel. The runner does it with what already works: `KEY_POWER`
to the TV, sent behind a fresh "on" reading, with the box's HDMI-CEC **left
on**, then a wait until port 5555 closes. That reproduces the
Samsung-remote case on the bus.

### Win rule

A spike is a win only if its median beats the baseline median by more than the
baseline's min-max spread on the same scenario. Anything else is reverted and
recorded as "no measurable gain".

## §2 Spike protocol

The order is fixed:

1. **Baseline**, on unmodified master code: S1, S2-deep, S2-shallow, N=3 each.
2. **H10**, read-only first. With the TV on and the box awake, record
   `getprop persist.sys.hdmi.keep_awake` and `mIsActiveSource`. Master Miguel
   presses the Samsung remote's power button; the runner records whether the
   box sleeps, and the CEC tail. If `keep_awake` is true and the box was not
   the active source, a second run makes it claim the active source first
   (`claim_active_source`) and repeats. This needs Master Miguel at the remote
   both times.
3. **H6, H7, H8, H9** on the branch. Each is measured alone against the
   baseline, then all the winners together.
4. **H1** only after a separate explicit yes, with the original service-menu
   values photographed before any change and exactly one option touched.

### Safety rules for every run

- Before each run, capture `hdmi_control_enabled`,
  `hdmi_control_one_touch_play_enabled` and
  `hdmi_control_auto_device_off_enabled`. Restore them in a `finally`, and
  verify the restore by reading them back.
- Never send `KEYCODE_POWER`. `KEY_POWER` to the TV goes only behind a fresh
  "on" reading, as in `CecWaker.standby_tv`.
- No reboots. If a run leaves the box unreachable for more than 120s after a
  full wake attempt, stop the series and report it rather than retry blindly.

## §3 Productionizing the winners

### H8: split Stremio `play()` into `prepare()` + `launch()`

- **`prepare(title, season, episode)`** does the library sync, the watch-state
  lookup, the TMDB fallback and `build_deep_link`. It touches no ADB and
  returns a plan object, or an error carrying the existing "I couldn't find
  X" line.
- **`launch(plan)`** is today's `_play_deep_link` path.
- **`play()`** keeps its signature and calls both, so every existing caller and
  test still works.
- **`_dispatch_tv`** starts `prepare` on a thread before `_ensure_playable` and
  joins it before `launch`. A prepare error is spoken after the wake, not
  before. Waking the room for a title she then cannot find is accepted: it
  matches today's order, and the room is usually wanted on anyway.
- **`watch_state.json` writes take a module-level lock**, because the
  background sync thread and the prepare thread can now overlap.

### H6, H7, H9

- **H6.** `turn_on` sends WoL to the TV before `is_awake()` rediscovery and
  before the `tv_power()` probes, when the cached state says the box was
  last seen off the LAN. This is safe because WoL is a no-op on a TV that is
  already on. The interim "Hold on" line moves ahead of the scan.
- **H7.** `_wait_for_box` port-probes the cached/static IP every
  `media.cec_wake.box_probe_interval_ms` (proposed 500). It runs the full
  ladder only every `media.cec_wake.full_rescan_every` passes (proposed 4)
  and on the final pass. Every config default is commented with its measured
  reason.
- **H9.** Remove `_wait_for_stremio_foreground` from the ready-list path.
  Skip `_first_card_label` when a remembered source exists, otherwise cap it
  at `stremio.first_card_dump_timeout_ms` (proposed 2000).

### Tests

These extend `tests/test_media_power.py` and `tests/test_stremio_ready_list.py`,
plus a new `tests/test_stremio_prepare.py`:

- prepare runs concurrently with the wake (barrier test)
- a prepare error produces the "I couldn't find X" line and still no launch
- `play()` behaves exactly as before for direct callers
- the wait loop probes the known IP before any scan, and rescans on the Nth
  pass and the final pass
- WoL is sent before rediscovery when the box was last seen off the LAN, and
  never `KEY_POWER` without an "on" reading
- the ready-list path no longer waits for the foreground, and skips the dump
  with a remembered source
- the existing guards stay green: no real ADB, sockets or HTTP from the suite

### Docs

- **CLAUDE.md** first (README optional; AGENTS.md untouched unless asked):
  - new before/after rows in "turn_on, rebuilt" and "The ready-list path"
  - the `persist.sys.hdmi.keep_awake` claim corrected or confirmed by H10
  - the service-menu result, if H1 runs
  - a session-log entry
- **Spike losers** get a sentence each, so they are not retried.

## §4 Out of scope

- Root, bootloader unlock, factory reset, Bluetooth wake
- Torrent buffering time and stream-source choice
- Samsung firmware changes, and any service-menu change beyond the single H1
  option
- Anything that requires a box reboot
- A permanent `hdmi_control_enabled=0`: research confirms the Xiaomi remote's
  wake would then stop turning the TV on

## Deliverables

1. `bench_tv_power.py --json`, `tools/bench_report.py`, and `bench/` gitignored
2. Baseline results and per-spike results, summarized in CLAUDE.md
3. The winning code changes with tests, on `feat/wake-optimization`
4. A PR against `master`
