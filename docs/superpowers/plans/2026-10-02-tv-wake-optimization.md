# TV Wake Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure and shorten the deep-standby wake and "put on X from a dark room" on the real Mi Box + Samsung pair, keeping only the changes the measurements prove.

**Architecture:**
- **Two observers record every benchmark run on one clock:**
  - Phase marks from inside the code (`services/phase_marks.py`, a no-op unless a bench run is recording).
  - Port observers outside it, polling TV :8001/:9197 and box :5555.
- **Each hypothesis ships behind its own config flag**, with a value that reproduces today's behaviour. One bench run therefore measures it alone with `--set`, and the winning values become the shipped config.

**Tech Stack:** Python 3.14, uv, unittest, adb, samsungtvws (all existing).

**Spec:** `docs/superpowers/specs/2026-10-02-tv-wake-optimization-design.md`

## Global Constraints

- **Tooling:** uv only. `uv run python -m unittest discover -s tests -v` stays green after every task, and no new dependencies.
- **Never send these:** `KEYCODE_POWER` in any state; `KEY_POWER` to the TV except behind a fresh "on" reading (`hdmi_state().tv_power == "on"`).
- **Never reboot the box.** No root, no factory reset, and no Samsung service-menu change.
- **Restore CEC settings on every bench run.**
  - Captured before each run: `hdmi_control_enabled`, `hdmi_control_one_touch_play_enabled`, `hdmi_control_auto_device_off_enabled`.
  - Restored in a `finally` and read back to verify.
- **Hardware runs are strictly serial.** Never two bench processes at once.
- **Win rule:** `median(candidate) < median(baseline) - (max(baseline) - min(baseline))`, per scenario, on the `total` phase.
- **Bench output** goes to `bench/results/`, which is gitignored and never committed.
- **New config keys follow the existing house style:**
  - Each lives under its block with a comment giving the measured reason.
  - Each defaults to today's behaviour in code (`cfg.get(key, <old value>)`), so an older `config.yaml` behaves as before.
- **Unit tests stay off the hardware:** no real ADB, sockets, or HTTP. Patch at the boundary, as the existing guards in `tests/test_media_power.py` and `tests/test_stremio_service.py` do.
- **Docs:** CLAUDE.md first, README optional, and AGENTS.md is never touched.

## Deliberate deviations from the spec

- **Two spec phases are not recorded:**
  - `tv_cec_first` is dropped: the box's CEC log runs on the box's clock, and aligning it to the host clock is not worth the code.
  - `boot_completed` is folded into `box_ready`, which `_wait_for_box` only returns once boot has completed.
  - Both are bracketed by `box_on_lan` (observer) and `box_ready` (mark).
- **Losing spikes stay in the code, switched off in config,** instead of being reverted. The flag value that reproduces today's behaviour is what makes per-spike measurement possible, and it keeps the measurement reproducible later.
- **Phase names differ from the spec's table.** This plan's mark and observer names are authoritative.

## Review Focus

1. **A prepare thread that raises** (title not found, TMDB down) must be spoken as "I couldn't find X in Stremio or TMDB." after the wake. It must never surface as a traceback or a hang. Test: Task 8, `test_prepare_error_is_spoken_after_the_wake_and_nothing_launches`.
2. **A prepare thread still running when the wake fails** must not launch anything. The dispatcher returns the wake's line. Test: Task 8, `test_failed_wake_returns_its_line_and_never_launches`.
3. **Pre-wake WoL with a box that is reachable** (awake under a dark TV, the normal `tv_only_standby` state) must send no WoL and must not change which branch `turn_on` takes. Test: Task 6, `test_reachable_box_sends_no_early_wol`.
4. **A box that came back on a new address during `_wait_for_box`** must still be found within N passes, because the full rescan still runs every Nth pass and on the final one. Test: Task 7, `test_full_rescan_runs_every_nth_pass_and_on_the_last`.
5. **A bench run killed mid-run** (Ctrl-C) must still restore CEC settings or say loudly that it could not. Test: Task 3, `test_restore_runs_on_keyboard_interrupt`.

---

## File Structure

| file | responsibility |
|---|---|
| `services/phase_marks.py` (new) | Process-wide phase recorder: `mark()`, `recording()`. No-op unless recording |
| `tools/bench_report.py` (new) | Pure summarise / win-rule functions plus a CLI over `bench/results/*.jsonl` |
| `tools/bench_tv_power.py` (modify) | `--runs/--json/--spike/--set`, `--from-deep` start state, settings capture/restore, port observer, `standby-probe` subcommand |
| `services/cec_wake.py` (modify) | Public `send_wol()`; `wol_sent` mark |
| `services/media_service.py` (modify) | `prewake()` (H6); `_wait_for_box` cadence (H7); `dump_ui_hierarchy(timeout_s, retries)`; marks |
| `services/stremio_service.py` (modify) | `StremioPlan`, `prepare()`, `launch()` (H8); watch-state lock; H9 waits; marks |
| `core/orchestrator.py` (modify) | `_ensure_playable` uses `prewake()`; `_dispatch_tv` prepares Stremio during the wake |
| `config.yaml` (modify) | New flags with comments |
| `.gitignore` (modify) | `bench/` |
| `tests/test_phase_marks.py`, `tests/test_bench_report.py`, `tests/test_bench_safety.py`, `tests/test_stremio_prepare.py` (new) | as named |
| `tests/test_media_power.py`, `tests/test_stremio_ready_list.py` (modify) | H6, H7, H9 cases |

---

### Task 1: Phase marks

Instrumentation only, with no behaviour change, so that the Task 4 baseline runs on code that behaves exactly like master.

**Files:**
- Create: `services/phase_marks.py`, `tests/test_phase_marks.py`
- Modify: `services/cec_wake.py` (`_send_wol`), `services/media_service.py` (`_wait_for_box`, `_wake_and_wait`), `services/stremio_service.py` (`_play_deep_link`, `_play_when_ready`)

**Interfaces:**
- Produces:
  - `phase_marks.mark(name: str) -> None` records the first occurrence of each name, in ms since the recording started. It is thread-safe and a no-op when nothing is recording.
  - `phase_marks.recording() -> contextmanager[dict[str, float]]` yields the live dict. Nested use raises `RuntimeError`.
- Mark names, used verbatim in Tasks 3, 4 and 10:
  - `wol_sent`, in `_send_wol`, at the end
  - `box_ready`, when `_wait_for_box` returns True
  - `tv_showing_box`, in `_wake_and_wait` after `ensure_active_source()` returns True
  - `deeplink_fired`, after `_launch_uri`
  - `stream_list_visible`, after the `list_ready` poll succeeds
  - `ok_pressed`, after `_keyevent(23)` in `_play_when_ready`
  - `player_up`, after `player_opened` succeeds
  - `playing`, when the `state == 3` poll succeeds
  - `stremio_prepared`, used by Task 8

- [ ] **Step 1: Write failing tests** in `tests/test_phase_marks.py`:
  - `test_mark_outside_recording_is_a_noop`: `mark("x")` raises nothing; a later `recording()` dict is empty.
  - `test_first_occurrence_wins`: under `patch("services.phase_marks.time.monotonic", side_effect=[0.0, 1.0, 2.5])` (start, mark, mark), `mark("a"); mark("a")` gives `{"a": 1000.0}`.
  - `test_marks_from_another_thread_are_recorded`.
  - `test_nested_recording_raises`.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_phase_marks -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `services/phase_marks.py` with a module-level `threading.Lock` and `_active: dict | None`. Add the `mark(...)` calls listed above; each is one line plus `from services import phase_marks`.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: all PASS, including the untouched power and Stremio suites.
- [ ] **Step 5: Commit** with message `Phase marks for wake/play benchmarking (no behaviour change)`.

---

### Task 2: Bench report and win rule

**Files:**
- Create: `tools/bench_report.py`, `tests/test_bench_report.py`
- Modify: `.gitignore` (add a `bench/` block with a comment saying the runs are local measurements)

**Interfaces:**
- Consumes the record shape that Task 3 writes, one JSON object per line:
  `{"scenario": str, "spike": str, "git_sha": str, "run": int, "phases": {name: ms}, "total_ms": float | None, "outcome": str, "settings_before": dict, "settings_restored": bool}`
- Produces:
  - `load(paths: list[Path]) -> list[dict]`
  - `summarize(records) -> dict[str, dict[str, tuple[float, float, float, int]]]`: scenario+spike → phase → (median, min, max, n). Phases are ms; `total` comes from `total_ms`.
  - `is_win(baseline: list[float], candidate: list[float]) -> bool`, the Global Constraints win rule. False if either list has fewer than 2 values.
  - CLI: `uv run python tools/bench_report.py bench/results/*.jsonl [--baseline SPIKE]` prints a table per scenario and a WIN / no gain verdict per spike against the baseline.

- [ ] **Step 1: Write failing tests:**
  - `test_summarize_groups_by_scenario_and_spike`
  - `test_absent_phase_is_not_zero`: a run missing `box_ready` does not drag the median toward 0, and `n` counts only the runs that have it.
  - `test_is_win_needs_more_than_the_baseline_spread`: baseline `[40000, 42000, 44000]` (spread 4000, median 42000, so a win needs a median below 38000).
    - Candidate `[36000, 37000, 37900]` → True.
    - Candidate `[38000, 38500, 39000]` → False.
  - `test_is_win_refuses_single_runs`.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_bench_report -v`. Expected: FAIL.
- [ ] **Step 3:** Implement with `statistics.median`. The CLI uses argparse and globs.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `Bench report: per-phase summary and the win rule`.

---

### Task 3: Bench runner, JSON records, deep start state, safety

**Files:**
- Modify: `tools/bench_tv_power.py`
- Create: `tests/test_bench_safety.py`

**Interfaces:**
- Consumes `phase_marks.recording()` (Task 1) and the Task 2 record shape.
- Produces these helpers in `tools/bench_tv_power.py`, importable for tests:
  - `SETTING_KEYS = ("hdmi_control_enabled", "hdmi_control_one_touch_play_enabled", "hdmi_control_auto_device_off_enabled")`
  - `capture_settings(svc) -> dict[str, str]` reads `settings get global <key>`.
  - `restore_settings(svc, before: dict[str, str]) -> bool` runs `settings put global` for every key that differs, then reads back. True only if all match. False if the box is unreachable, with a WARNING naming each key.
  - `run_recorded(svc, scenario: str, spike: str, run: int, action: Callable[[], object], out: Path) -> dict` captures settings, starts `PortObserver`, and enters `phase_marks.recording()`. It times `action()` into `total_ms`, then in a `finally` stops the observer, merges its phases, restores settings, and appends the record to `out`.
  - `class PortObserver(threading.Thread)`, built with `(tv_ip: str, box_ip: str, interval_s=0.5)`. It records first-open times as `tv_8001_up`, `tv_9197_up` and `box_on_lan` using `services.device_finder.port_open(ip, port, 0.3)`. `.phases -> dict[str, float]` uses the same clock origin as the recording; pass `t0` in. It never runs ADB.
  - `put_room_in_deep_standby(svc, idle_s: float) -> bool` puts the room into S1's start state:
    - Precondition: `svc.is_awake() is True` and `svc.hdmi_state().tv_power == "on"`. Otherwise it prints why and returns False.
    - Calls `svc._finish_pending_standby()`, then sends `svc.cec_waker._send_keys(["KEY_POWER"])` with the box's CEC **left on**.
    - Polls `port_open(svc.ip, svc.port, 0.3)` every 2s until closed, giving up after 120s.
    - Then sleeps `idle_s`.
- CLI additions:
  - On `wake` and `stremio`: `--json PATH` (default `bench/results/<YYYY-MM-DD>-<scenario>.jsonl`), `--spike LABEL` (default `baseline`), `--from-deep`, `--idle SECONDS` (default 120).
  - Global: `--set dotted.key=value` (repeatable), deep-merged into the loaded config before services are built. Values are parsed with `yaml.safe_load`.
  - New subcommand `standby-probe [--claim-first] [--watch 30]`, for H10 (Task 5):
    - prints `getprop persist.sys.hdmi.keep_awake` and `hdmi_state().active_source`
    - with `--claim-first`, calls `svc.claim_active_source()` and prints the result
    - prompts "Press the Samsung remote's power button now", then prints `is_awake()` once a second for `--watch` seconds and the final verdict "box stayed awake" or "box slept"
- Scenario names, used verbatim: `S1` (`wake --via-turn-on --from-deep`), `S2-deep` (`stremio --from-deep`), `S2-shallow` (`stremio --from-off --tv-only-standby`).
- Before each run after the first, the room must be brought back up. For `--from-deep` this means running `svc.turn_on()` untimed, then `put_room_in_deep_standby`.

- [ ] **Step 1: Write failing tests** in `tests/test_bench_safety.py`. `svc` is a `Mock` whose `_adb` answers from a dict.
  - `test_restore_puts_back_only_changed_keys_and_verifies`.
  - `test_restore_reports_false_when_the_box_is_unreachable`.
  - `test_restore_runs_on_keyboard_interrupt`: `action` raises `KeyboardInterrupt`. `restore_settings` is still called and a record with `outcome == "interrupted"` is written before the exception propagates.
  - `test_deep_standby_refuses_without_an_on_reading`: with `hdmi_state().tv_power == "standby"`, `_send_keys` is never called.
  - `test_bench_never_sends_keycode_power`: run each helper with `_adb` recording commands, then assert no command contains `KEYCODE_POWER`.
  - `test_set_override_deep_merges`: `--set media.cec_wake.poll_interval_ms=500` changes only that key.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_bench_safety -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Rework `cmd_wake --via-turn-on` and `cmd_stremio` so they loop `--runs` times through `run_recorded`. The existing printed output stays.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS.
- [ ] **Step 5:** Smoke-test against the real room with no power change: `uv run python tools/bench_tv_power.py standby-probe --watch 1`, answering nothing. Expected: it prints keep_awake and active_source, then exits.
- [ ] **Step 6: Commit** with message `Bench: recorded runs, deep-standby start state, settings restore, standby-probe`.

---

### Task 4: Baseline (hardware, no code)

Run on the Task 3 commit. Behaviour equals master; only marks were added. Unattended. The TV and box start on, with the box awake.

- [ ] **Step 1:** Run `uv run python tools/bench_tv_power.py wake --via-turn-on --from-deep --runs 3 --spike baseline`. Expected: 3 records in `bench/results/<date>-S1.jsonl`, all with `settings_restored: true`.
- [ ] **Step 2:** Run `uv run python tools/bench_tv_power.py stremio --from-deep --runs 3 --spike baseline`. Expected: 3 records, with `playing` present.
- [ ] **Step 3:** Run `uv run python tools/bench_tv_power.py stremio --from-off --tv-only-standby --runs 3 --spike baseline`. Expected: 3 records.
- [ ] **Step 4:** Run `uv run python tools/bench_report.py bench/results/*.jsonl`. Paste the table into the task's report to the user. Nothing is committed in this task.
- [ ] **Stop condition:** a run fails to bring the box back within 120s after a full wake attempt. Stop the series and report; do not retry.

---

### Task 5: H10, does an active-source box survive the Samsung remote? (hardware, needs Master Miguel)

- [ ] **Step 1:** With the TV on and the box awake, run `uv run python tools/bench_tv_power.py standby-probe`. Master Miguel presses the Samsung remote's power button when prompted. Record keep_awake, active_source and the verdict.
- [ ] **Step 2:** Only if keep_awake is `true` (or empty) and Step 1 showed `active_source` not True: turn the room back on, then run `standby-probe --claim-first`. Record the verdict.
- [ ] **Step 3:** Report the result. If the box stayed awake with `--claim-first`, write it up as a follow-up proposal (claim the active source after each wake). Do **not** implement it in this plan; it changes what the Samsung remote does and gets its own approval.

---

### Task 6: H6, Wake-on-LAN before the reachability scan

**Files:**
- Modify: `services/cec_wake.py`, `services/media_service.py`, `core/orchestrator.py` (`_ensure_playable`), `config.yaml` (`media.cec_wake.prewake_wol: true`, with a comment)
- Test: `tests/test_media_power.py` (new class `PrewakeTests`, plus additions to `EnsurePlayableTests`)

**Interfaces:**
- Produces:
  - `CecWaker.send_wol() -> None`: a public wrapper around `_send_wol`, so `Mock(spec=CecWaker)` accepts it.
  - `MediaService.prewake() -> bool`:
    - Pings once with `self._adb("shell echo ping")` and returns True if that succeeded.
    - Otherwise, when `self.prewake_wol is True` and `self.cec_waker.available is True`, calls `self.cec_waker.send_wol()`, then returns False.
    - Never rediscovers and never touches `_last_fail_time`.
  - `MediaService.prewake_wol: bool` comes from `media.cec_wake.prewake_wol`, defaulting to `False` in code; the config ships `true`.
- `_ensure_playable` change: before the `is_awake()` check, `if getattr(media_svc, "prewake", None) is not None and media_svc.prewake() is False:` say the "Hold on" line now and go straight to the `turn_on()` branch, skipping the awake-box branch. `turn_on()` itself is unchanged; its `is_awake()` does the one rediscovery.

- [ ] **Step 1: Write failing tests:**
  - `test_unreachable_box_sends_wol_before_anything_else`: `_adb` fails; `send_wol` is called once and `_finder.resolve` is not called.
  - `test_reachable_box_sends_no_early_wol` (Review Focus 3).
  - `test_prewake_off_sends_nothing`: `prewake_wol=False`.
  - `test_ensure_playable_speaks_before_turn_on_when_prewake_misses`: with a spec'd media mock where `prewake` returns False, `say_now` is called before `turn_on`, and `is_awake` is not called by `_ensure_playable`.
  - `test_bare_mock_media_keeps_the_old_order`: a plain `Mock()` media service behaves as today, because `prewake()` returns a Mock, not False.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_media_power -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Wake-on-LAN the TV before the reachability scan when the box is off the LAN`.

---

### Task 7: H7, faster `_wait_for_box` cadence

**Files:**
- Modify: `services/media_service.py` (`__init__`, `_wait_for_box`), `config.yaml` (`media.cec_wake.box_probe_interval_ms: 500`, `full_rescan_every: 4`, with comments)
- Test: `tests/test_media_power.py` (new class `WaitForBoxCadenceTests`)

**Interfaces:**
- `self.box_probe_interval_s` comes from `box_probe_interval_ms`, defaulting in code to the old `poll_interval_ms` value. `self.full_rescan_every` defaults to `1` in code, which is today's behaviour: rescan on every pass.
- In `_wait_for_box`, pass `i` (0-based) clears `_last_discovery_t = 0` only when `i % full_rescan_every == 0` or the pass is the last one before the deadline. Otherwise it sets `_last_discovery_t = time.monotonic()`, so `ensure_connected()` does the port-gated connect at the known IP and skips the scan. `_last_fail_time = 0` on every pass, as today. Sleep `box_probe_interval_s`.
- "Last pass" means `time.monotonic() + box_probe_interval_s >= deadline` at the top of the pass.

- [ ] **Step 1: Write failing tests**, using a fake monotonic clock and stubbing `ensure_connected` to record whether `_last_discovery_t == 0` on entry:
  - `test_full_rescan_runs_every_nth_pass_and_on_the_last` (Review Focus 4): `full_rescan_every=4` over 10 passes allows a scan on passes 0, 4, 8 and the final pass.
  - `test_default_cadence_is_unchanged`: `full_rescan_every=1` allows a scan on every pass, sleeping `poll_interval`.
  - `test_box_found_on_a_probe_pass_returns_immediately`.
  - Existing `DeepStandbyWakeTests` stay unchanged and green.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_media_power -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement. Update the `_wait_for_box` comment block to say why the scan is now periodic, citing the code audit and that the box has a static IP since 2026-09-22.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Probe the known box address between periodic rescans while waiting out a wake`.

---

### Task 8: H8, prepare the Stremio request during the wake

**Files:**
- Modify: `services/stremio_service.py`, `core/orchestrator.py` (`_dispatch_tv`, `stremio_play` and `stremio_continue` branches), `config.yaml` (`stremio.prepare_during_wake: true`, with a comment)
- Create: `tests/test_stremio_prepare.py`

**Interfaces:**
- Produces:
  - `@dataclass class StremioPlan` with fields `imdb_id: str`, `media_type: str`, `season: int | None`, `episode: int | None`, `title_key: str`, `title_label: str`, `remembered_source: str | None`, `started_from_first_episode: bool`.
  - `StremioService.prepare(title, media_type=None, season=None, episode=None) -> StremioPlan` is exactly lines 506-532 of today's `play()`. It raises what `play()` raised before the launch, and marks `stremio_prepared`.
  - `StremioService.launch(plan, allow_unknown_source=False) -> StremioPlayResult` is today's `_play_deep_link(...)` call plus `result.started_from_first_episode = plan.started_from_first_episode`.
  - `StremioService.play(...)` keeps its signature and becomes `return self.launch(self.prepare(...), allow_unknown_source)`.
  - `StremioService.prepare_during_wake: bool` comes from `stremio.prepare_during_wake` (code default `False`).
  - A module-level `_WATCH_STATE_LOCK = threading.RLock()` is held across `sync_library`'s read-merge-write, `_remember_successful_source`, and `_write_watch_state`. `_write_watch_state` writes `<path>.tmp` and then `os.replace`s it.
- `_dispatch_tv` change: for `stremio_play` / `stremio_continue`, if `isinstance(stremio_svc, StremioService) and stremio_svc.prepare_during_wake is True`, start `prepare` on a daemon thread **before** `_ensure_playable`:
  - Hold its result or exception in a small holder.
  - If `_ensure_playable` returns a problem line, return it and never launch (Review Focus 2).
  - Otherwise join the thread with no timeout. Every network call inside already has its own.
  - On an exception, log it and return `f"I couldn't find {title} in Stremio or TMDB."`.
  - Then call `stremio_svc.launch(plan, allow_unknown_source=...)`; everything after is unchanged.
  - Otherwise, including the Mock-based tests, keep today's `play()` call.
  - `stremio_continue` passes `media_type="series"`, as today.

- [ ] **Step 1: Write failing tests** in `tests/test_stremio_prepare.py`, using `config_for_tests` and stubbing network and adb as `tests/test_stremio_service.py` does (including its `subprocess.run` guard):
  - `test_play_still_equals_prepare_then_launch`: `play()` calls `_play_deep_link` with the same kwargs as before for a tracked series, an untracked series (S1E1 plus `started_from_first_episode`), and a movie.
  - `test_prepare_runs_during_the_wake` (barrier test): `prepare` and the media mock's `turn_on` each wait on a shared `threading.Barrier(2, timeout=2)`. The dispatch completes, which proves they overlapped.
  - `test_prepare_error_is_spoken_after_the_wake_and_nothing_launches` (Review Focus 1).
  - `test_failed_wake_returns_its_line_and_never_launches` (Review Focus 2).
  - `test_flag_off_uses_play_as_before`.
  - `test_watch_state_write_is_atomic_and_locked`: two threads, each doing `_remember_successful_source` for a different title, both survive in the file.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_stremio_prepare -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS, including the existing `test_stremio_service`, `test_orchestrator_vpn_routing` and the dispatch tests in `test_media_power`.
- [ ] **Step 5: Commit** with message `Prepare Stremio requests while the room wakes`.

---

### Task 9: H9, drop redundant waits on the play path

**Files:**
- Modify: `services/stremio_service.py` (`_play_deep_link`, `_play_when_ready`, `_first_card_label`), `services/media_service.py` (`dump_ui_hierarchy`), `config.yaml` (`stremio.skip_foreground_wait: true`, `stremio.first_card_dump_timeout_ms: 2000`, with comments)
- Test: `tests/test_stremio_ready_list.py`

**Interfaces:**
- `MediaService.dump_ui_hierarchy(timeout_s: float | None = None, retries: int | None = None) -> str`. Defaults keep today's `ui_dump_timeout_s` and `ui_dump_retry_count`.
- `_play_when_ready(target_mode, title, remembered_source: str | None = None)`:
  - With a `remembered_source`, skip the dump and use `source = remembered_source`.
  - Otherwise `_first_card_label()` calls `dump_ui_hierarchy(timeout_s=first_card_dump_timeout_s, retries=1)` through `_dump_ui_hierarchy`.
  - `first_card_dump_timeout_s` code default: `media.ui_dump_timeout_ms`.
- `_play_deep_link` skips `_wait_for_stremio_foreground` when `skip_foreground_wait is True` (code default False), and passes `remembered_source` into `_play_when_ready`.

- [ ] **Step 1: Write failing tests:**
  - `test_remembered_source_skips_the_dump`.
  - `test_first_card_dump_is_capped`: asserts `dump_ui_hierarchy` was called with `timeout_s=2.0, retries=1`.
  - `test_no_foreground_wait_when_skipped`: `_wait_for_stremio_foreground` is not called, and the launch is still exactly one `am start`.
  - Update the existing `test_the_first_cards_source_is_read_before_ok_and_reported` lambda to accept `**kw`.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_stremio_ready_list -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS.
- [ ] **Step 5: Commit** with message `Skip the redundant foreground wait and cap the pre-OK dump`.

---

### Task 10: Measure each spike, keep the winners (hardware)

Run on the Task 9 commit. Each spike is isolated with `--set`; the others are held at their old-behaviour values.

| spike | scenario | `--set` overrides (others at old values) |
|---|---|---|
| `H6` | S1 | `media.cec_wake.prewake_wol=true` |
| `H7` | S1 | `media.cec_wake.box_probe_interval_ms=500 media.cec_wake.full_rescan_every=4` |
| `H8` | S2-deep | `stremio.prepare_during_wake=true` |
| `H9` | S2-shallow | `stremio.skip_foreground_wait=true stremio.first_card_dump_timeout_ms=2000` |
| `all` | S1, S2-deep, S2-shallow | every winner from the rows above |

"Old values" means `prewake_wol=false`, `full_rescan_every=1`, `box_probe_interval_ms=2000`, `prepare_during_wake=false`, `skip_foreground_wait=false`, and `first_card_dump_timeout_ms=6000`. Every spike run passes all six explicitly.

- [ ] **Step 1:** For each row, run N=3 with `--spike <id>`. The same stop condition as Task 4 applies.
- [ ] **Step 2:** Run `uv run python tools/bench_report.py bench/results/*.jsonl --baseline baseline`.
- [ ] **Step 3:** For each losing spike, set its flag in `config.yaml` to the old-behaviour value with a one-line comment giving the measured result. The code stays, since it is inert at that value.
- [ ] **Step 4:** Run `all` with only the winners on.
- [ ] **Step 5: Commit** the `config.yaml` values with message `Ship the wake/play settings that measured faster`, with the table in the commit body.

---

### Task 11: Docs and PR

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md:**
  - Add before/after rows to the tables under "turn_on, rebuilt" and "The ready-list path".
  - Add one line per losing spike, so nobody retries it.
  - Record the H10 result. Correct or confirm the `persist.sys.hdmi.keep_awake` paragraph, citing `HdmiControlService.standby()` → `canGoToStandby()`.
  - Document the new config keys under their blocks.
  - Add `services/phase_marks.py`, `tools/bench_report.py` and the four new test files to Project Structure and the test-coverage list.
  - Add a Development Session Log entry for this session.
- [ ] **Step 2:** Run `uv run python -m unittest discover -s tests -v`. Expected: PASS.
- [ ] **Step 3: Commit** with message `CLAUDE.md: measured wake/play optimization results`, then `git push -u origin feat/wake-optimization` and open a PR against `master`. The body holds the before/after table and ends with the attribution line.
