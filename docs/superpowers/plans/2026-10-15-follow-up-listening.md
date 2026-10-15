# Follow-up Listening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When California's reply asks Master Miguel something, keep the mic open for a short window and take his answer without the wake word; the LLM drops speech that was not meant for her.

**Architecture:** A turn ends by setting `self._next_follow_up`. A new `_run_conversation` loop in `core/orchestrator.py` (called from `_idle_loop` and `_deliver_phone_report`) chains barge-ins as today and follow-ups through a new `_handle_follow_up`, which is `_run_activation` minus the acknowledgement, with a shorter pre-speech window. Follow-up messages carry a marker; a token gate in `_stream_response` recognises an `[ignore]` reply before anything reaches TTS.

**Tech Stack:** Python 3.14, `unittest`, uv. Run everything through `uv run`.

**Spec:** `docs/superpowers/specs/2026-10-15-follow-up-listening-design.md`

## Global Constraints

- Config block, verbatim:
  ```yaml
  conversation:
    follow_up_enabled: true
    follow_up_timeout_s: 3.0
    max_follow_ups: 10
  ```
  and under `sounds:`: `stop_lines_dir: "sounds/stop_lines/google_en-US-Chirp3-HD-Aoede"`.
- Stop lines are **pre-recorded**, never synthesized at runtime. Text, verbatim: `timeout` = "I'll take that as a no. Say my name if you need me.", `cap` = "Okay, I'll stop listening now. Say California if you need me." Generated WAVs are not committed.
- Sentinel is exactly `[ignore]`; matched after `lstrip()`, case-insensitive, on the *start* of the reply.
- Follow-up message prefix, verbatim: `[Heard without the wake word, right after your question. If this is not him answering you, reply with exactly [ignore].] ` followed by the transcript.
- The system prompt is not changed.
- `follow_up_enabled: false` reproduces today's behaviour exactly. Class-level defaults on `Orchestrator` keep follow-up **off** so `Orchestrator.__new__` test orchestrators behave as today.
- Never a `pip` command; tests run with `uv run python -m unittest ...`.
- No model identifiers in commits or docs.

## Review Focus

1. **The LLM's own error line ends in "?"** ("Sorry, I had trouble thinking about that. Could you try again?"). It must not open a window, or one API outage becomes a loop. Pinned in Task 3.
2. **A tool boundary or a tool call before any text in a follow-up** (he answered "yes" and she confirms the WhatsApp send). The gate must release on `TOOL_BOUNDARY` so tools run normally. Pinned in Task 4.
3. **A reply that is only a partial sentinel at stream end** (`"[ign"`). It must be spoken, not swallowed. Pinned in Task 4.
4. **A barge-in during a follow-up reply** resets the chain count to 0 (a wake word starts a new conversation) and never speaks the stop line. Pinned in Task 5.
5. **A follow-up reply that asks another question at index 10** speaks the cap line once and opens no window, and the speaker session is still closed exactly once by `_idle_loop`. Pinned in Task 5.

---

### Task 1: Per-recording pre-speech window

**Files:**
- Modify: `core/vad.py` (`start_recording`, `should_stop_recording`)
- Modify: `core/orchestrator.py` (`_record_speech`)
- Test: `tests/test_vad_silence.py`

**Interfaces:**
- Produces: `VAD.start_recording(speech_timeout: float | None = None) -> None`. `None` uses `self.speech_timeout`; a value applies to this recording only.
- Produces: `Orchestrator._record_speech(mic_stream, playback=None, speech_timeout: float | None = None) -> np.ndarray | None`. It passes `speech_timeout` to **both** `self.vad.start_recording(...)` calls (the gate re-arm calls it again), and the "Nothing said within %.1fs" log uses the effective value.

- [ ] **Step 1: Write the failing tests** in `GraceWindowTests`:
  - `test_a_per_recording_timeout_overrides_the_configured_one`: `_vad(speech_timeout=4.0)`, `start_recording(speech_timeout=3.0)`; at +2.9s QUIET → `(False, "continue")`; at +3.1s → `(True, "no_speech")`.
  - `test_the_override_does_not_leak_into_the_next_recording`: after the above, `start_recording()`; at +3.1s QUIET → `(False, "continue")`.
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_vad_silence -v`. Expected: the two new tests FAIL (TypeError on the keyword).
- [ ] **Step 3: Implement.** Store the effective value in `self._active_speech_timeout` in `start_recording`, and compare against it in the pre-speech branch. Add the `_record_speech` parameter.
- [ ] **Step 4: Run** `uv run python -m unittest tests.test_vad_silence tests.test_mic_drain tests.test_activation_phrases -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat(vad): per-recording pre-speech window`.

---

### Task 2: LLM error constant and dropping the last exchange

**Files:**
- Modify: `services/llm.py`
- Test: `tests/test_follow_up.py` (create; class `DropLastExchangeTests`)

**Interfaces:**
- Produces: module constant `services.llm.ERROR_LINE = "Sorry, I had trouble thinking about that. Could you try again?"`, used by `stream_response` in place of the inline literal.
- Produces: `LLMService.drop_last_exchange() -> bool`. Deletes from the last message for which `_starts_exchange` is true to the end of `history`. Returns `True` if it removed anything; with no exchange start it logs a warning, leaves history alone, and returns `False`.

- [ ] **Step 1: Write the failing tests.** Build the service with `LLMService.__new__` and set `history` directly:
  - `test_removes_the_last_exchange_and_its_tool_rounds`: history = `[user "a", assistant "x", user "b", assistant [tool_use], user [tool_result], assistant "y"]` → after the call it equals the first two messages, and returns True.
  - `test_a_partial_ignore_reply_is_removed_with_its_question`: `[user "a", assistant "x", user "[Heard…] tv", assistant "[ignore]"]` → the first two remain.
  - `test_no_exchange_is_a_logged_no_op`: history `[]` → returns False, stays `[]`.
  - `test_error_line_constant_is_what_she_says`: `ERROR_LINE.endswith("?")` (documents why Task 3 excludes it).
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** the constant and method in `services/llm.py`.
- [ ] **Step 4: Run** `uv run python -m unittest tests.test_follow_up tests.test_llm_claude_streaming -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(llm): drop_last_exchange and a named error line`.

---

### Task 2b: Pre-recorded stop lines

**Files:**
- Create: `generate_stop_lines.py` (repo root, alongside the other two generators)
- Create: `services/stop_lines.py`
- Modify: `config.yaml` (`sounds.stop_lines_dir`, with a comment like the `bootup_dir` one)
- Modify: `.gitignore` (add `sounds/stop_lines/**/manifest.json` next to the activation one; `*.wav` is already ignored)
- Test: `tests/test_follow_up.py` (class `StopLinesTests`)

**Interfaces:**
- Produces: `generate_stop_lines.STOP_LINES = {"timeout": "...", "cap": "..."}` (the text from Global Constraints). `main()` follows `generate_activation_phrases.main`: load `.env` and `config.yaml`, build `TTSService`, synthesize each line, apply `generate_activation_phrases.trim_silence` (imported, not copied), and write `sounds/stop_lines/<tts.voice_slug()>/<name>.wav` plus `manifest.json` (`{name: text}`). Print each duration and the `sounds.stop_lines_dir` line to paste. Module docstring in the house style of the sibling scripts.
- Produces: `services.stop_lines.load_stop_lines(directory: str) -> dict[str, tuple[np.ndarray, int, str]]`. Relative paths resolve against the repo root, as `_play_bootup_sound` does. For each of `"timeout"` and `"cap"`: read the WAV with `soundfile` and the text from `manifest.json` (`""` if absent). A missing or unreadable file is skipped with one `logger.warning` naming `generate_stop_lines.py`. Never raises.

- [ ] **Step 1: Write the failing tests** (tmpdir, WAVs written with `soundfile`):
  - `test_loads_both_clips_with_their_text`
  - `test_a_missing_clip_is_skipped_with_a_warning` (`assertLogs`)
  - `test_a_missing_directory_returns_empty_and_never_raises`
  - `test_generator_text_matches_the_spec`: `STOP_LINES` equals the two verbatim strings
  - `test_shipped_config_points_at_the_aoede_folder`: `config_for_tests()["sounds"]["stop_lines_dir"] == "sounds/stop_lines/google_en-US-Chirp3-HD-Aoede"`
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** `services/stop_lines.py`, `generate_stop_lines.py` and the config line. Importing `generate_stop_lines` must not synthesize anything (all work happens in `main()`).
- [ ] **Step 4: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sounds): pre-recorded stop-listening lines`.

---

### Task 3: Deciding a follow-up at the end of a turn

**Files:**
- Modify: `core/orchestrator.py` (class defaults, `__init__`, `_handle_tool_call`, `_handle_activation`, `_report_turn`, `_stream_response`)
- Modify: `config.yaml` (add the `conversation:` block from Global Constraints, after `vad:`, with one comment line pointing at the spec)
- Test: `tests/test_follow_up.py` (class `FollowUpDecisionTests`)

**Interfaces:**
- Consumes: `services.llm.ERROR_LINE` (Task 2), `services.stop_lines.load_stop_lines` (Task 2b), called in `__init__` with `config["sounds"].get("stop_lines_dir", "")`.
- Produces, as class-level defaults on `Orchestrator` (overwritten in `__init__` from `config["conversation"]`):
  - `_follow_up_enabled: bool = False`, `_follow_up_timeout_s: float = 3.0`, `_max_follow_ups: int = 10`, `_stop_lines: dict = {}` (kind → `(audio, sample_rate, text)`, filled in `__init__` by Task 2b's `load_stop_lines`)
  - `_next_follow_up: bool = False`: set at the end of every turn, and read by Task 5
  - `_turn_awaiting: bool = False`: reset to False at the start of every turn (`_handle_activation`, `_report_turn`, Task 5's `_handle_follow_up`); set True in `_handle_tool_call`'s `finally` when `awaiting` is True
  - `_follow_up_index: int = 0`: the current turn's position in the chain (0 = a wake-word turn)
- Produces: `Orchestrator._play_stop_line(which: str) -> None` (`which` is `"timeout"` or `"cap"`). Looks up `self._stop_lines[which]`; if it is missing, does nothing. Otherwise, on the already-open speaker: `reset_playback()`, then `play_audio(audio, sr, blocking=True, text=text, kind="stop_listening")`. Never raises (logs). No TTS call, no history, no LLM.
- Produces: `_wants_follow_up(reply_parts: list[str], interrupted: bool) -> bool`, a module-level pure function. True iff not interrupted, the parts are non-empty, `ERROR_LINE` is not in the joined reply, and `reply_parts[-1].rstrip().endswith("?")`.

At the end of `_stream_response` (after `reply_done`), set:

```text
wanted = enabled and outcome != "error" and (self._turn_awaiting or _wants_follow_up(parts, interrupted))
          and not (an awaiting tool that was interrupted)
if wanted and self._follow_up_index >= self._max_follow_ups:
    self._play_stop_line("cap"); turn.set(stopped_listening="cap"); wanted = False
self._next_follow_up = wanted
```

`_report_turn` forces `_next_follow_up = False` after streaming. `_handle_activation` and `_report_turn` reset `_next_follow_up = False` and `_turn_awaiting = False` at their start.

- [ ] **Step 1: Write the failing tests.** Reuse the fakes from `tests/test_reply_barge_in.py` by importing `_orchestrator`, `_FakeMic` and `SENTENCES` from it, and set `_follow_up_enabled = True`, `_stop_lines = {"timeout": (_tts_synthesize("timeout line")[0], 24000, "timeout line"), "cap": (_tts_synthesize("cap line")[0], 24000, "cap line")}` (the fake audio plays back as its text), `_turn = NULL_TURN` (or a `Mock`):
  - `test_a_reply_ending_in_a_question_wants_a_follow_up` (last sentence `"…want the first source?"`)
  - `test_a_statement_does_not`
  - `test_a_question_mid_reply_followed_by_a_statement_does_not`
  - `test_an_awaiting_tool_wants_one_without_a_question_mark`: set `_turn_awaiting = True`, statement reply → True
  - `test_the_llm_error_line_never_opens_a_window` (sentences = `[ERROR_LINE]`)
  - `test_a_barged_in_reply_never_wants_one` (`fire_on=8`, question at the end)
  - `test_disabled_never_wants_one`
  - `test_the_tenth_follow_up_asking_again_says_the_stop_line_once`: `_follow_up_index = 10`, question reply → `_next_follow_up` False, "cap line" appears exactly once in `audio.played`, "timeout line" never, and `tts.synthesize` was never called with either line
  - `test_a_missing_stop_clip_plays_nothing_and_does_not_raise`: `_stop_lines = {}`, cap reached → nothing extra played, `_next_follow_up` False
  - `test_handle_tool_call_marks_the_turn_awaiting`: patch `_dispatch_tool` to call `core.orchestrator._await_answer()` and return `"ok"` → `_turn_awaiting` True
  - `test_shipped_config_carries_the_conversation_block`: `config_for_tests()["conversation"]` equals the Global Constraints values
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the defaults, config read, `_play_stop_line`, `_wants_follow_up` and the end-of-turn decision. Read config with `.get` and fall back to the defaults above.
- [ ] **Step 4: Run** `uv run python -m unittest tests.test_follow_up tests.test_reply_barge_in tests.test_mood_rules tests.test_turn_log -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(orchestrator): decide a follow-up when she asks something`.

---

### Task 4: The `[ignore]` gate

**Files:**
- Modify: `core/orchestrator.py` (module function plus `_stream_response`)
- Test: `tests/test_follow_up.py` (classes `IgnoreGateTests`, `IgnoredFollowUpTests`)

**Interfaces:**
- Consumes: `LLMService.drop_last_exchange()` (Task 2).
- Produces: `SENTINEL = "[ignore]"` and `FOLLOW_UP_PREFIX` (Global Constraints text) as module constants.
- Produces: `_ignore_gate(tokens, ignored: threading.Event)`, a generator. Buffer the leading `str` tokens while `SENTINEL.startswith(buf.lstrip().lower())`. Once `buf.lstrip().lower().startswith(SENTINEL)`: set `ignored` and return without yielding. On the first token that rules the sentinel out, a non-`str` token (`TOOL_BOUNDARY`), or the end of the stream: yield the buffer (if non-empty), then pass everything through unchanged.
- Produces: `_stream_response(user_text, mic_stream=None, follow_up: bool = False) -> bool`. With `follow_up=True` it sends `FOLLOW_UP_PREFIX + user_text` to `self.llm.stream_response` and wraps the token stream in `_ignore_gate` **inside** `_timed_tokens`. When the gate fires: `token_stream.close()` (the LLM saves `"[ignore]"` as a partial reply), then `self.llm.drop_last_exchange()`, `self._turn.set(outcome="ignored")`, `self._next_follow_up = False`, nothing queued for TTS, no `speaking` state, and it returns False.

- [ ] **Step 1: Write the failing tests:**
  - `IgnoreGateTests` (pure, list in → list out):
    - `["[ign", "ore]"]` → yields nothing, event set
    - `["  [IGNORE]", " anyway"]` → nothing, set
    - `["Yes", ", sending"]` → same tokens, not set
    - `["[ign"]` at stream end → `["[ign"]`, not set
    - `[TOOL_BOUNDARY, "Done."]` → unchanged, not set
    - `["I'll ignore that"]` → unchanged
  - `IgnoredFollowUpTests`, using `_orchestrator` with a fake LLM that records the text it was given and has a `drop_last_exchange` Mock:
    - `test_a_follow_up_reply_of_ignore_is_silent_and_dropped`: sentences `["[ignore]"]`, `follow_up=True` → `audio.played == []`, `drop_last_exchange` called once, `_next_follow_up` False
    - `test_the_follow_up_message_carries_the_prefix`: the LLM received `FOLLOW_UP_PREFIX + "yes"`
    - `test_a_normal_turn_never_gates`: `follow_up=False` with `["[ignore] hm"]` → spoken
    - `test_a_real_answer_is_spoken`
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the constants, `_ignore_gate` and the `follow_up` path.
- [ ] **Step 4: Run** `uv run python -m unittest tests.test_follow_up tests.test_reply_barge_in -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(orchestrator): [ignore] gate for follow-up turns`.

---

### Task 5: The follow-up turn and the conversation loop

**Files:**
- Modify: `core/orchestrator.py` (`_idle_loop`, `_deliver_phone_report`, new `_handle_follow_up`, `_run_follow_up`, `_run_conversation`)
- Test: `tests/test_follow_up.py` (class `ConversationLoopTests`)

**Interfaces:**
- Consumes: Task 1 `_record_speech(..., speech_timeout=)`, Task 3 attributes and `_play_stop_line`, Task 4 `_stream_response(..., follow_up=True)`.
- Produces: `_handle_follow_up(mic_stream, index: int) -> bool` (barged_in). It mirrors `_handle_activation`'s `TurnTimer` wrapper (`chained=True`, plus `turn.set(follow_up=True, follow_up_index=index)`), resets `_next_follow_up`/`_turn_awaiting`, sets `_follow_up_index = index`, and runs `_run_follow_up`.
- Produces: `_run_follow_up(mic_stream, index) -> bool`. `reset_playback()`, `_set_state("listening")`, `_record_speech(mic_stream, None, speech_timeout=self._follow_up_timeout_s)`, then STT, the empty check, `_handle_command`, `_capture_incoming_target()`, and `_stream_response(transcript, mic_stream, follow_up=True)`. No activation clip, no `_wake_count` change, no `strip_activation_echo`. **No reply** (recording `None` → outcome `_last_record_outcome`, or an empty transcript → `empty_transcript`): call `wake_word.reset()`, and when `index >= 2` call `_play_stop_line("timeout")` and `turn.set(stopped_listening="timeout")`. Return False with `_next_follow_up` False.
- Produces: `_run_conversation(mic_stream, barged_in: bool) -> None`:

```text
index = self._follow_up_index    # 0 after a wake turn
loop:
  if barged_in: drain("barge-in"); self._follow_up_index = 0; barged_in = _handle_activation(mic); continue
  if not self._next_follow_up: break
  drain("follow-up"); barged_in = _handle_follow_up(mic, self._follow_up_index + 1)
finally: self._follow_up_index = 0; self._next_follow_up = False
```

`_handle_activation` sets `_follow_up_index = 0`. `_idle_loop` replaces its `while barged_in` loop with `self._run_conversation(mic_stream, barged_in)`, inside the existing `try/finally close_speaker()`. `_deliver_phone_report` does the same.

- [ ] **Step 1: Write the failing tests.** Patch `_handle_activation`, `_record_speech`, `stt.transcribe` and `llm` with fakes that script each turn. Count `audio.speaker_opens`/`speaker_closes` through `_idle_loop` with a `_FakeWake` that fires on chunk 1.
  - `test_an_answer_in_the_window_is_a_follow_up_turn_with_no_acknowledgement`: the wake turn asks; the follow-up records audio "yes"; `play_activation_sound` not called; `_wake_count` unchanged; `_record_speech` got `speech_timeout=3.0`
  - `test_silence_after_the_first_question_ends_quietly`: the follow-up `_record_speech` returns None at index 1 → stop line not played, one speaker open and one close
  - `test_silence_in_a_chained_window_says_the_stop_line_once`: follow-up 1 answered and asks again; follow-up 2 silent → "timeout line" played once, "cap line" never
  - `test_ten_follow_ups_then_the_cap_line`: every reply asks → exactly 10 follow-up turns, then "cap line" once, then idle
  - `test_a_barge_in_inside_a_follow_up_resets_the_chain`: follow-up 1 barged in → `_handle_activation` runs next, and its turn starts at index 0
  - `test_an_ignored_follow_up_ends_the_conversation_without_a_line`
  - `test_disabled_is_todays_flow`: `_follow_up_enabled = False`, the reply asks → no follow-up turn, `_record_speech` called once
  - `test_a_phone_report_question_opens_no_window`
- [ ] **Step 2: Run** `uv run python -m unittest tests.test_follow_up -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the three methods and the two call sites.
- [ ] **Step 4: Run** the full suite: `uv run python -m unittest discover -s tests`. Expected: OK, with no new failures against the baseline taken before Task 1.
- [ ] **Step 5: Commit** `feat(orchestrator): take his answer without the wake word`.

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` and `AGENTS.md` (kept in sync): a new subsection "Answering Her Questions Without The Wake Word" after "Interrupting Her Is the Wake Word", covering the trigger, the 3s window and why it is shorter, `[ignore]`, the cap and the two pre-recorded stop lines (and that `generate_stop_lines.py` must be run on a fresh clone and after a voice change), and that confirmation tokens remain the safety net. Add `tests/test_follow_up.py`, `services/stop_lines.py` and `generate_stop_lines.py` to the project tree, `sounds/stop_lines/` to the "generated audio, not committed" runtime note, and a coverage bullet under "Current automated coverage".

- [ ] **Step 1:** Write the section (about 25 lines, house style: what, why, measured or not yet measured).
- [ ] **Step 2:** `git diff --stat` shows only the two docs. Confirm the two files' new sections are identical: `diff <(sed -n '/Answering Her Questions/,/^### /p' CLAUDE.md) <(sed -n '/Answering Her Questions/,/^### /p' AGENTS.md)` produces no output.
- [ ] **Step 3: Commit** `docs: follow-up listening`.

**Not done here, listed in the section as pending:** live tuning of `follow_up_timeout_s` from `turns.jsonl`, and how often the model actually answers `[ignore]` to TV speech.
