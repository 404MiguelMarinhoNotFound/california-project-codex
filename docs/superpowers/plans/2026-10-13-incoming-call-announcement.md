# Incoming-Call Announcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Master Miguel's phone rings, California says who is calling in the room ("Mama's calling."), once per ring, within about two seconds.

**Architecture:** A background `IncomingCallWatcher` (`services/incoming_call.py`) holds one long-running read-only `adb shell` loop that prints the phone's call state every second, detects the start of a ring, resolves the number to a name against the contact book, and queues an `IncomingCall`. `Orchestrator._idle_loop` pops it between mic reads (exactly like a phone-call report) and speaks one fixed line through TTS, with no LLM turn. Answering by voice is NOT part of this plan.

**Tech Stack:** Python 3.14 (uv), adb, the existing `AdbPhone`, `TTSService`, `AudioPipeline`, `EventBus`.

**Spec:** idea 6 of `docs/ideas/2026-10-13-phone-toolkit.md` ("Incoming calls ... she announces 'Mama's calling' in the room. Next step, separately: 'answer it'"), as reprioritised with Master Miguel on 2026-10-13 (third of: contact sync, missed calls + digest, incoming-call announcement).

**Build order:** after `2026-10-13-weekly-contact-sync.md` and `2026-10-13-phone-missed-digest.md`. This plan consumes `caller_phrase` from the digest plan and the contact snapshot from the sync plan; if either is not built yet, see the rulings in Task 3.

## What the phone shows (probed read-only, 2026-10-13)

`dumpsys telephony.registry` prints one block per modem (two on this phone), each with:

```text
    mCallState=0
    mPreciseCallState=Ringing call state: 0, Foreground call state: 0, Background call state: 0, Disconnect cause: -1, Precise disconnect cause: -1
    mCallIncomingNumber=
```

The second block reads `-1` throughout (no SIM). `Ringing call state` uses `PreciseCallState`: `5` INCOMING, `6` WAITING (a second call during one), `0` idle. `com.android.shell` holds `READ_PRIVILEGED_PHONE_STATE` and `READ_CALL_LOG`, so `mCallIncomingNumber` should carry the number while ringing. **Not verified during a real ring**: Task 5 checks it live; if it stays empty, the line falls back to "Someone's calling you." (never silence, never a guess).

## Global Constraints

- uv only; tests `PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests`; fixtures from `tests/config_fixture.py`; no test reaches a real adb (guard as in `tests/test_phone_adb.py`).
- Read-only on the phone: the watcher's command is exactly `dumpsys telephony.registry | grep -E 'mPreciseCallState|mCallIncomingNumber'` in a loop; no helper, no Security-settings gate needed.
- Never speak a full phone number: unnamed is "a number ending <last 4>", withheld/empty/`-1`/`-2`/`-3` is "a private number" (same rule and function as the digest plan).
- One announcement per ring episode. A ring that ended before she could speak is dropped, not announced late.
- The mic keeps exactly one reader: announcements happen on the idle-loop thread, like `_deliver_phone_report`.
- Windows only. Docs: CLAUDE.md first, never AGENTS.md. Fake numbers/names in tests and docs (the repo is public).

## Review Focus

1. **The watcher's adb loop dies** (phone left the Wi-Fi, reboot, port change). Expected: restarted with backoff (5 s, doubling to 60 s), rediscovering through `AdbPhone`; never a tight respawn loop, never an exception in the orchestrator (Task 2 `test_a_dead_stream_restarts_with_backoff`).
2. **She is mid-reply or mid-call when it rings.** Expected: queued, spoken at the next idle point only if still ringing; skipped entirely while `PhoneService` has a call of its own (Task 4 `test_no_announcement_during_her_own_call`, Task 2 `test_a_ring_that_ended_is_not_announced`).
3. **Call waiting during his own call** (state 6) is announced as "<who> is calling on the other line." (Task 1 `test_waiting_is_its_own_kind`).
4. **Two modems, one ringing in the second block.** Expected: detected (Task 1 `test_the_ringing_modem_can_be_the_second_block`).
5. **A number the book does not know vs. a withheld one** must give different words and never the full number (Task 3 `test_unknown_and_private_callers`).

---

### Task 1: Parse the ring state (pure)

**Files:**
- Create: `services/incoming_call.py`
- Test: `tests/test_incoming_call.py`

**Interfaces:**
- Produces:
  - `INCOMING = 5`, `WAITING = 6`
  - `@dataclass(frozen=True) class RingState: kind: str  # "incoming" | "waiting" | "none"; number: str  # "" when the phone gave none`
  - `parse_ring_state(text: str) -> RingState | None` — `None` when the text has no `mPreciseCallState` line at all (unreadable, never "not ringing"). Pairs each `mPreciseCallState` line with the next `mCallIncomingNumber` line of the same block; the first block whose ringing state is 5 or 6 wins; otherwise `RingState("none", "")`.

- [ ] **Step 1: Write the failing tests** with an invented two-block fixture in the probed layout:
  - `test_idle_is_none_kind` → `RingState("none", "")`
  - `test_incoming_with_number` (block 1 ringing 5, `mCallIncomingNumber=+351910004567`) → `RingState("incoming", "+351910004567")`
  - `test_the_ringing_modem_can_be_the_second_block`
  - `test_waiting_is_its_own_kind` (ringing 6) → `kind == "waiting"`
  - `test_unreadable_text_is_none` (`""`, `"error: closed"`) → `None`
  - `test_crlf_output_parses` (Windows adb gives `\r\n`)
- [ ] **Step 2:** `uv run python -m unittest tests.test_incoming_call -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `RingState` and `parse_ring_state` in `services/incoming_call.py`.
- [ ] **Step 4:** Same command → PASS.
- [ ] **Step 5: Commit** `feat(phone): parse the phone's ring state`

### Task 2: `IncomingCallWatcher`, one streaming read-only shell

**Files:**
- Modify: `services/phone_adb.py` (one method), `services/incoming_call.py`
- Test: `tests/test_incoming_call.py`, `tests/test_phone_adb.py`

**Interfaces:**
- Consumes: `AdbPhone.from_config` and `AdbPhone.reachable()` (contact-sync / digest plans); `parse_ring_state` (Task 1).
- Produces:
  - `AdbPhone.stream(command: str) -> subprocess.Popen` — `adb -s <serial> shell <command>` through the injected `popen`, after `reachable() == "OK"`; raises nothing, returns `None` when the phone is not reachable.
  - `RING_LOOP = "while true; do echo ---; dumpsys telephony.registry | grep -E 'mPreciseCallState|mCallIncomingNumber'; sleep 1; done"`
  - `@dataclass(frozen=True) class IncomingCall: kind: str; number: str; name: str; started: float`
  - `class IncomingCallWatcher:`
    - `__init__(self, phone, resolve_name: Callable[[str], str] = lambda n: "", clock=time.monotonic, sleep=time.sleep, backoff_s=(5.0, 60.0))` — runs nothing.
    - `start(self, stop: threading.Event) -> None` — daemon thread `incoming-call`.
    - `pop(self) -> IncomingCall | None` — the queued call only if the watcher's latest state is still ringing (same kind and number); otherwise drops it and returns `None`.
    - `ringing(self) -> bool`
  - Behaviour: blocks are split on `---`; a transition from not-ringing to ringing queues one `IncomingCall` (name from `resolve_name(number)`, `""` on any exception); further blocks of the same ring queue nothing; the queue holds at most one call (a newer ring replaces an older one). EOF or an unreadable stream ends the process (`kill`), sleeps the current backoff (start 5 s, ×2, cap 60 s, reset after a stream that produced a readable block), and reopens.
- [ ] **Step 1: Write the failing tests** (fake `phone.stream` returning a fake process whose `stdout` yields scripted lines; injected `sleep` records waits):
  - `test_a_new_ring_is_queued_once` — blocks idle, ringing ×3, idle → exactly one `pop()` result before the idle block
  - `test_a_ring_that_ended_is_not_announced` — ringing, then idle, then `pop()` → `None`
  - `test_the_name_comes_from_the_book` and `test_a_failing_resolver_gives_no_name`
  - `test_a_dead_stream_restarts_with_backoff` — three EOFs → `sleep` called with 5.0, 10.0, 20.0; a good block then resets to 5.0
  - `test_no_phone_waits_instead_of_spinning` — `stream` returns `None` → `sleep(5.0)` before retrying
  - `test_stop_ends_the_thread` — `stop.set()` → thread exits within 2 s and the process is killed
  - In `tests/test_phone_adb.py`: `test_stream_is_read_only_and_needs_no_helper` — no `push`, no `CallAudio`, no `adbinput` command; the Popen argv ends with the given command.
- [ ] **Step 2:** Run both test modules → FAIL.
- [ ] **Step 3:** Implement `AdbPhone.stream` and the watcher.
- [ ] **Step 4:** Run → PASS, three times (thread timing).
- [ ] **Step 5: Commit** `feat(phone): watch the phone for incoming calls`

### Task 3: Who is calling, in words

**Files:**
- Modify: `services/incoming_call.py`, `services/whatsapp_service.py`
- Test: `tests/test_incoming_call.py`, `tests/test_whatsapp_service.py`

**Interfaces:**
- Consumes: `caller_phrase(name: str | None, number: str | None) -> str` from `services/phone_digest.py` (digest plan); the contact snapshot loaded by `WhatsAppService` (contact-sync plan).
- Produces:
  - `WhatsAppService.name_for_number(self, number: str) -> str` — the saved name of the contact owning that number after `normalize_phone(..., self.default_country)` on both sides, aliases ignored; `""` when none or ambiguous (two contacts with the number).
  - `incoming_line(call: IncomingCall) -> str`, exact copy:
    - incoming: `f"{Who} is calling."` → `"Mama is calling."`, `"A number ending 4567 is calling."`, `"A private number is calling."`
    - waiting: `f"{Who} is calling on the other line."`
    - number `""` from a phone that gave none: `"Someone's calling you."`
    - `Who` = `caller_phrase(call.name, call.number)` with the first letter upper-cased.
- Rulings if the sibling plans are not built yet: if `phone_digest.caller_phrase` does not exist, create it in `services/phone_digest.py` with the digest plan's exact contract (so that plan's Task 2 finds it done); if the contact snapshot does not exist, `name_for_number` works on `contacts.vcf` unchanged.
- [ ] **Step 1: Write the failing tests:** `test_named_caller`, `test_unknown_and_private_callers` (`+351910004567` → "A number ending 4567 is calling."; `""` from a withheld `-2` → "A private number is calling."; never the full number in any line), `test_waiting_line`, `test_no_number_at_all_says_someone`; in `tests/test_whatsapp_service.py`: `test_name_for_number_matches_normalised_numbers`, `test_name_for_number_is_empty_when_two_contacts_share_it`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(phone): say who is calling`

### Task 4: Announce from the idle loop

**Files:**
- Modify: `core/orchestrator.py`, `config.yaml`, `tests/config_fixture.py`
- Test: `tests/test_orchestrator_incoming.py` (new; `Orchestrator.__new__` + attributes, as `tests/test_live_ui_orchestrator.py` does)

**Interfaces:**
- Consumes: `IncomingCallWatcher`, `incoming_line` (Tasks 2-3); `WhatsAppService.name_for_number`.
- Produces:
  - Config:
    ```yaml
    phone:
      incoming:
        enabled: true      # announce who is calling, once per ring
    ```
    `tests/config_fixture.py` sets `phone.incoming.enabled: false` (a live watcher would stream from the real phone).
  - `Orchestrator.incoming_calls: IncomingCallWatcher | None`, built in `__init__` when `phone.incoming.enabled` and the phone transport can be built (`AdbPhone.from_config(config)`), started with `self._background_stop`; `resolve_name = whatsapp_service.name_for_number` when WhatsApp is up, else `lambda n: ""`.
  - `_idle_loop`: after the phone-report check, `call = self.incoming_calls.pop()`; if not `None` and `phone_service` has no active call (`phone_service.status_line()` is not consulted; use a new `PhoneService.busy() -> bool` reading `self._active is not None`), `self._announce(incoming_line(call), mic_stream)` and return.
  - `Orchestrator._announce(self, text: str, mic_stream) -> None`: `open_speaker` → `tts.synthesize(text)` → `play_audio` with clip kind `"announce"` → `close_speaker` → `_drain_mic(mic_stream, "announcement")`. No LLM call, no history entry. Publishes `("incoming_call", {"kind": ..., "label": <Who or "">})` on the bus, label omitted when `logging.include_transcripts` is false.
- [ ] **Step 1: Write the failing tests:**
  - `test_a_ringing_call_is_announced_between_mic_reads` — fake watcher `pop()` returns a call → `tts.synthesize` called with `"Mama is calling."`, speaker opened then closed, mic drained, and no mic read in that pass
  - `test_no_announcement_during_her_own_call` — `phone_service.busy()` True → nothing synthesized, the call is dropped
  - `test_nothing_queued_reads_the_mic_as_usual`
  - `test_an_announcement_is_not_an_llm_turn` — `llm` mock never called, history unchanged
  - `test_a_tts_failure_does_not_take_the_idle_loop_down` — `synthesize` raises → logged, loop continues
  - `test_disabled_builds_no_watcher` — fixture config → `incoming_calls is None`
  - `test_PhoneService.busy` in `tests/test_phone_service.py`: False before, True with `_active` set.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `PYTHONIOENCODING=utf-8 uv run python -m unittest discover -s tests` → OK.
- [ ] **Step 5: Commit** `feat(phone): announce incoming calls in the room`

### Task 5: Live check and docs

No code unless the live run shows a gap. Master Miguel places the test calls himself, from his backup phone; nothing here dials.

- [ ] **Step 1:** With California running, he rings his main phone from the backup line. Expected within ~2 s of the first ring: "<name or 'A number ending 0055'> is calling." once; nothing more while it keeps ringing; nothing after he rejects it.
- [ ] **Step 2:** Check `mCallIncomingNumber` was filled during the ring (`logs/california.log` line from the watcher at DEBUG: kind + last four digits only). If it was empty, the line said "Someone's calling you."; record that in CLAUDE.md and open a follow-up (the number can then come from the call log right after the ring, or from `dumpsys telecom`).
- [ ] **Step 3:** Ring while she is replying to something: the announcement comes after her reply if the phone is still ringing, not at all if it stopped.
- [ ] **Step 4:** CLAUDE.md: a short "Incoming calls" subsection under PhoneService (what is read, once per ring, private-number rule, read-only, the live result of Step 2); Project Structure and test-coverage lines.
- [ ] **Step 5: Commit** `docs(phone): incoming-call announcements`

## Shared touch points (for reconciling with the sibling plans)

- `services/phone_adb.py`: adds `AdbPhone.stream(command)`. Consumes `from_config` (contact-sync plan) and `reachable()` (digest plan).
- `services/phone_digest.py`: consumes `caller_phrase` (digest plan); creates it only if that plan has not.
- `services/whatsapp_service.py`: adds `name_for_number`; reads whatever book the contact-sync plan loads.
- `services/phone_service.py`: adds `busy()`.
- `core/orchestrator.py`: `incoming_calls` built in `__init__`, a pop in `_idle_loop` after the phone report, `_announce`. Does not touch `_dispatch_phone` or the `control_phone` schema (no tool change, no token cost per turn).
- `config.yaml`: `phone.incoming.enabled`. `tests/config_fixture.py`: forces it off.

## Assumptions to confirm

1. Announce every incoming call, at any hour (no quiet hours yet).
2. A fixed line through TTS, not an LLM turn: faster and free, but she cannot comment ("your mum again?"). Answering ("answer it") stays a separate later plan.
3. Call waiting during his own call is announced; ringing during a call California is making is not.
