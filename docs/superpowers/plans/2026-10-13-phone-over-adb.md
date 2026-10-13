# Phone Over ADB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `control_phone` places, hears, speaks into, hangs up and rings Master Miguel's phone entirely over ADB, and the Phone Link / VB-CABLE path moves to `deprecated/`.

**Architecture:** A transport, `AdbPhone` (`services/phone_adb.py`), keeps the dialer interface `PhoneService` already uses (`dial`, `in_call`, `hang_up`, `play_sound`) and adds `audio()`, which returns a `PhoneCallAudio` bridge (`services/phone_call_audio.py`) that looks like the old loopback (ears) and cable (mouth) to `GeminiLiveAgent`. Both drive a dex helper (`android/callaudio/`) run as the shell user through `app_process`: Telecom for dial/hang-up, `getCallDownlinkExtractionAudioRecord` / `getCallUplinkInjectionAudioTrack` for audio over an `adb forward` TCP socket, and an alarm-usage tone for find-my-phone.

**Tech Stack:** Python 3.14 (uv), adb (platform-tools), Java 11 source compiled with the portable Microsoft JDK 21 + r8/d8 into a dex, Gemini Live on Vertex (`europe-west1`).

**Spec:** this conversation, 2026-10-13, summarized here because no separate spec file exists:
- Route every phone action through ADB "like we just did" (`tools/phone_adb_call.py` spike, verified live with Marta at 18:51).
- Move the legacy Phone Link code somewhere, do not delete it (`deprecated/`, which mirrors the live layout).
- Gemini on `europe-west1`. Do NOT shorten `silence_duration_ms` (stays 700).
- Windows only; no Raspberry Pi paths.

## Global Constraints

- uv only: `uv add` / `uv remove` / `uv run`; commit `pyproject.toml` and `uv.lock` together.
- `phone.vertex.location: "europe-west1"`; `phone.silence_duration_ms: 700` unchanged.
- HyperOS needs "USB debugging (Security settings)" on: `getprop persist.security.adbinput` == `1`. Without it the shell is denied `CALL_PHONE` and input events.
- Dial and hang up only through the helper's Telecom modes, never `am start -a android.intent.action.CALL` or `input keyevent`.
- Injection track: `setPreferredDevice(TYPE_TELEPHONY == 18)` and 48000 Hz stereo PCM16; laptop sends 24000 Hz mono and the helper upsamples. Downlink: 16000 Hz mono PCM16.
- Discovery never selects a `:5555` device (that is the Mi Box) and accepts an attached `IP:port` when mDNS is silent (phone screen off).
- `PhoneService(...)` and `AdbPhone(...)` construction never run adb or open a socket.
- Every failure has its own spoken line; none collapses into a generic "couldn't".
- Nothing under `deprecated/` is imported or executed; tests there are not discovered (`-s tests`).
- Docs: update `CLAUDE.md` first, `README.md` if there is a natural place, never `AGENTS.md`.

## Review Focus

1. **The Mi Box is also on adb.** `adb devices` lists `192.168.1.200:5555` next to the phone; discovery must return the phone, never the box (Task 3 test `test_discovery_never_picks_the_mi_box`).
2. **Phone rebooted / Wireless debugging off.** No phone found must speak "turn Wireless debugging back on", not raise (Task 3 `test_no_phone_is_a_status_not_an_exception`, Task 5 `test_no_phone_gets_its_own_line`).
3. **Security setting reverted.** Checked before dialing, so no half-placed call: `LOCKED_DOWN` and nothing dialled (Task 3 `test_locked_down_dials_nothing`).
4. **They hang up mid-sentence.** The bridge must stop within seconds (socket shut, helper waited/killed, forward removed) and `hang_up` must report `NO_CALL` (Task 4 `test_stop_unblocks_a_reader_and_removes_the_forward`, Task 3 `test_hang_up_without_a_call_is_no_call`).
5. **A stale helper on the phone** from an older build: pushed again when its sha256 differs, not when it matches (Task 3 `test_helper_is_pushed_only_when_its_hash_differs`).

---

### Task 1: Branch, and commit the spike as the baseline

**Files:**
- Modify: `services/gemini_live_call.py` (already has the `answered` parameter and `_ANSWERED_GRACE_S = 0.8`)
- Modify: `config.yaml` (already `location: "europe-west1"`)
- Add: `tools/phone_adb_call.py`, `tools/android_call_audio/` (spike, as is)
- Test: `tests/test_gemini_live_call.py`

**Interfaces:**
- Produces: `GeminiLiveAgent.run(prompt, max_call_s, no_answer_s, still_connected=None, stop=None, dial=None, mute=None, answered=None) -> CallResult`

- [ ] **Step 1:** `git switch -c feat/phone-over-adb`
- [ ] **Step 2: Write the failing test** `test_an_answered_call_is_nudged_before_anyone_speaks` in `tests/test_gemini_live_call.py`: with the file's existing fake session, `answered=lambda: True`, nothing ever heard from them; assert one `send_client_content` carrying `_NUDGE_TEXT` within ~1.5 s of start and `result.stats["nudges"] == 1`. Also `test_without_answered_the_nudge_waits_for_them`: `answered=None`, nothing heard, run 2 s, assert no nudge sent.
- [ ] **Step 3:** Run `uv run python -m unittest tests.test_gemini_live_call -v`. Expected: both pass (the code exists); if the first fails, fix the nudge loop in `_run` so `answered` is polled only until it first returns True.
- [ ] **Step 4:** Add `debug/phone_adb/` to `.gitignore` (call recordings). Do not commit `tools/android_call_audio/build/`.
- [ ] **Step 5: Commit** `feat(phone): ADB call spike, answered nudge, Gemini on europe-west1`

### Task 2: The phone-side helper, in its permanent home, with ring and mic mute

**Files:**
- Move: `tools/android_call_audio/` → `android/callaudio/` (`git mv`; keep `src/`, `stubs/`, `build.ps1`)
- Modify: `android/callaudio/src/CallAudio.java`
- Create: `android/callaudio/callaudio.dex` (committed build output, ~11 KB, so a fresh clone needs no JDK) and `android/callaudio/README.md`
- Modify: `.gitignore` (`android/callaudio/build/`)

**Interfaces (command line of the helper, all run as `CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio <mode>`):**
- `check` → prints `INTERCEPTABLE=true|false`, `AUDIO_MODE=<n>`, `TELEPHONY_TX=<type/name/addr|none>`, exits 0.
- `dial <digits>` → prints `DIALED`, exit 0; any exception → prints `ERROR <class>: <message>` and exits 1 (today it dies with an uncaught exception and exit 137).
- `hangup` → prints `ENDED=true|false`.
- `ring <seconds>` → sets `STREAM_ALARM` to its max, prints `RINGING`, plays a two-tone beep (880/660 Hz, 300 ms on / 200 ms off) with `USAGE_ALARM` for `<seconds>`, restores the previous alarm volume in a `finally`, exits 0. Prints `ERROR ...` and exits 1 if the track cannot start.
- `bridge <port> 16000 24000 [mutemic]` → as today; with `mutemic`, `AudioManager.setMicrophoneMute(true)` after the streams start and the previous state restored on exit, logging `mic muted (was <bool>)`.

- [ ] **Step 1:** `git mv tools/android_call_audio android/callaudio`; make `build.ps1` copy the dex to `android/callaudio/callaudio.dex`.
- [ ] **Step 2:** Wrap `telecomCommand` so failures print `ERROR <cause>` and exit 1; add `ring` and the `mutemic` option.
- [ ] **Step 3:** Run `powershell -NoProfile -ExecutionPolicy Bypass -File android/callaudio/build.ps1`. Expected: `built ...\android\callaudio\callaudio.dex`.
- [ ] **Step 4:** Push and run `check` against the phone (`adb -s <serial> push android/callaudio/callaudio.dex /data/local/tmp/callaudio.dex`, then `... CallAudio check`). Expected: `INTERCEPTABLE=true` and `TELEPHONY_TX=18/...`.
- [ ] **Step 5:** `README.md`: what the helper is, the modes above, how to rebuild (JDK path + r8 path, as `build.ps1` resolves them), and the three hardware findings (Security settings gate, Telecom instead of `am start`, Telephony Tx + 48 kHz stereo).
- [ ] **Step 6: Commit** `feat(phone): callaudio helper in android/, ring + mic mute modes`

### Task 3: `AdbPhone`, the transport

**Files:**
- Create: `services/phone_adb.py`
- Test: `tests/test_phone_adb.py`

**Interfaces:**
- Consumes: the helper modes from Task 2.
- Produces:
  - Call-state ints `IDLE=0, ACTIVE=1, HOLDING=2, DIALING=3, ALERTING=4` (PreciseCallState foreground).
  - Status strings: `"OK"`, `"NO_PHONE"` (not found / not attached), `"LOCKED_DOWN"` (`persist.security.adbinput != "1"`), `"NO_INTERCEPTION"` (`check` says false), `"DIALED"`, `"NOT_PLACED"` (helper said DIALED but no DIALING/ALERTING/ACTIVE within 10 s, or helper ERROR), `"ENDED"`, `"NO_CALL"`, `"PLAYING"`, `"RING_FAILED"`.
  - `class AdbPhone:`
    - `__init__(self, adb_path: str = "adb", serial: str = "", helper_dex: str = <repo>/android/callaudio/callaudio.dex, port: int = 27183, mute_phone_mic: bool = True, runner: Callable[[list[str], float], subprocess.CompletedProcess] | None = None, popen: Callable[..., subprocess.Popen] | None = None, sleep=time.sleep, clock=time.monotonic)` — stores only; no adb.
    - `ready(self) -> str` — discover serial, push the helper if `sha256sum /data/local/tmp/callaudio.dex` differs from the local file's sha256 (`rm -f` first, then push, then always `chmod 444`), check the Security-settings gate, run `check` once per process. Returns `"OK"` or the first failing status.
    - Every helper launch is ONE shell command: `[ "$(sha256sum F | cut -d" " -f1)" = "<local sha256>" ] && CLASSPATH=F exec app_process / CallAudio <args> || echo HELPER_MISMATCH` (as in the spike's `_helper_cmd`). `HELPER_MISMATCH` is its own status, logged as an error, and nothing runs. Security review 2026-10-13: `/data/local/tmp` is shared by everything running as shell and the dex runs with the shell's call/audio permissions.
    - `call_state(self) -> int | None`
    - `dial(self, number: str) -> str` — `ready()` first; digits only (`^\+?\d{3,15}$`) else `"NOT_PLACED"`; resets the `seen` flag.
    - `in_call(self) -> bool | None` — True while DIALING/ALERTING/ACTIVE/HOLDING; False only once a live state was seen since the last dial; else None; None when the state is unreadable.
    - `hang_up(self) -> str` — `"ENDED"` / `"NO_CALL"` / `""` (unreachable).
    - `play_sound(self, seconds: int = 20) -> str` — `ready()` first; starts `ring` with `popen` and returns `"PLAYING"` once the helper prints `RINGING` (≤5 s), else `"RING_FAILED"`.
    - `helper(self, *args: str) -> subprocess.Popen`, `forward(self, port: int) -> None`, `unforward(self, port: int) -> None`
    - `audio(self, record_path: str = "") -> "PhoneCallAudio"` (Task 4)
  - Discovery rule (`_discover`): from `adb mdns services` take `_adb-tls-connect._tcp` → `IP:port`; from `adb devices` accept a `device` row whose name is that mDNS name, that `IP:port`, contains `_adb-tls-connect._tcp`, or is any `IP:port` not ending `:5555`; if only mDNS has it, `adb connect IP:port`. `phone.adb.serial` in config, when set, skips discovery.

- [ ] **Step 1: Write the failing tests** with a scripted fake `runner` (maps the joined argv to stdout; records every call) and fake `popen`:
  - `test_construction_runs_no_adb` — `AdbPhone()` with a runner that raises; no call recorded.
  - `test_discovery_finds_the_mdns_name`; `test_discovery_accepts_an_attached_ip_port_when_mdns_is_silent` (`adb devices` has `192.168.1.101:38731 device`, mDNS empty → that serial).
  - `test_discovery_never_picks_the_mi_box` (`192.168.1.200:5555 device` only → `"NO_PHONE"`).
  - `test_no_phone_is_a_status_not_an_exception`.
  - `test_locked_down_dials_nothing` — gate prop empty → `dial()` returns `"LOCKED_DOWN"` and no `CallAudio dial` in the recorded calls.
  - `test_helper_is_pushed_only_when_its_hash_differs` (matching sha → no `push`; different → one `push`; `chmod 444` either way).
  - `test_a_swapped_helper_is_never_run` (every launch command carries the local sha256 check and `exec app_process`; the phone answering `HELPER_MISMATCH` → `dial()` returns `"HELPER_MISMATCH"`, `play_sound()` returns `"RING_FAILED"`).
  - `test_call_state_parses_precise_state` from `mPreciseCallState=Ringing call state: 0, Foreground call state: 4, ...` → 4; garbage → None.
  - `test_dial_waits_for_the_call_to_register` — states idle, idle, dialing → `"DIALED"`; idle for 10 s of fake clock → `"NOT_PLACED"`; helper prints `ERROR SecurityException...` → `"NOT_PLACED"`.
  - `test_in_call_is_unknown_until_a_call_was_seen` — idle after dial → None; dialing → True; idle → False.
  - `test_hang_up_without_a_call_is_no_call` (`ENDED=false`) and `ENDED=true` → `"ENDED"`.
  - `test_play_sound_reports_playing_once_the_tone_starts` and `..._ring_failed` on `ERROR`.
  - `test_unit_tests_never_run_a_real_adb` — patch `subprocess.run` and `subprocess.Popen` to raise for the whole class and drive every method above.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_phone_adb -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `services/phone_adb.py` (move `PhoneAdb` from `tools/phone_adb_call.py`, apply the interface above).
- [ ] **Step 4:** Run the same command. Expected: all pass.
- [ ] **Step 5: Commit** `feat(phone): AdbPhone transport`

### Task 4: `PhoneCallAudio`, the call's ears and mouth

**Files:**
- Create: `services/phone_call_audio.py`
- Test: `tests/test_phone_call_audio.py`

**Interfaces:**
- Consumes: `AdbPhone.call_state`, `.helper`, `.forward`, `.unforward`, `.mute_phone_mic`, `ACTIVE` (Task 3).
- Produces: `class PhoneCallAudio` with `__init__(self, phone, record_path: str = "", connect=socket.create_connection, sleep=time.sleep)`; `capture()` / `speaker()` faces (as in the spike's `_Face`); `start(on_block)`; `write(pcm)`, `flush()`, `idle() -> bool`, `stop()` (idempotent); `answered() -> bool` (True once the bridge connected); attributes `peak`, `blocks`, `source = "phone downlink"`, `injected_s`, `gaps: list[float]`. Constants `REC_RATE = 16000`, `PLAY_RATE = 24000`, `_LEAD_S = 0.12`, `_SPEECH_RMS = 0.01`. The helper is started with `bridge <port> 16000 24000` plus `mutemic` when `phone.mute_phone_mic`.

- [ ] **Step 1: Write the failing tests** with a fake phone (scripted `call_state`, `helper` returning a fake process whose stdout yields `LISTENING 27183`) and a `socket.socketpair()` handed out by `connect`:
  - `test_the_helper_starts_only_once_the_call_is_answered` (states dialing, alerting, active → one `helper` call, after active).
  - `test_downlink_blocks_reach_the_agent_in_20ms_pieces` (write 2000 bytes into the far end → `on_block` gets 640-byte pieces; `blocks` counts them; `peak` > 0 for a loud block).
  - `test_her_voice_is_paced_to_real_time` (write 1 s of PCM; after 0.3 s of fake-free real time the far end holds ≤ 0.3 s + `_LEAD_S` of audio).
  - `test_flush_drops_what_has_not_been_sent` (queue 2 s, flush after 0.1 s → far end receives < 0.5 s total; `idle()` True).
  - `test_stop_unblocks_a_reader_and_removes_the_forward` (stop returns within 3 s while the reader is blocked; `unforward(27183)` called; second `stop()` is a no-op).
  - `test_mutemic_is_passed_when_configured` (helper args end with `mutemic`).
  - `test_answered_is_false_until_connected`.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_phone_call_audio -v`. Expected: FAIL.
- [ ] **Step 3:** Implement by moving `PhoneCallAudio` / `_Face` from the spike, with `connect`/`sleep` injected and the gap log kept.
- [ ] **Step 4:** Run the same command. Expected: all pass.
- [ ] **Step 5: Commit** `feat(phone): PhoneCallAudio bridge`

### Task 5: PhoneService on ADB

**Files:**
- Modify: `services/phone_service.py`, `core/orchestrator.py:1944-1949`, `config.yaml` (`phone:` block), `services/llm.py:567` (comment only)
- Test: `tests/test_phone_service.py`, `tests/test_orchestrator_phone.py`

**Interfaces:**
- Consumes: `AdbPhone` (Task 3), `PhoneCallAudio` via `AdbPhone.audio()` (Task 4), `GeminiLiveAgent.run(..., answered=...)` (Task 1).
- Produces:
  - `PhoneService.__init__(self, config, resolve_contact=None, dialer=None, agent_factory=None, summarize=None, publish=None)` — `mic_route_factory`, `speaker_route_factory` and `room_speaking` are removed (the phone's downlink never carries the room).
  - Config: remove `cable_output_device`, `cable_mic_endpoint`, `call_speaker_endpoint`; add
    ```yaml
    adb:
      path: "adb"          # or media.adb_path's value if that is an absolute path
      serial: ""           # empty: discover over mDNS / attached IP:port
      port: 27183
      mute_phone_mic: true # the phone's own mic stays out of the call
    ```
  - `_probe()`: no Windows check, no `soundcard`; requires `google.genai` (find_spec) and credentials as today, and `shutil.which(adb.path)` or an existing file. Never runs adb.
  - `_run_call`: `transport = self._get_dialer()`; `audio = transport.audio(record_path)` when the transport has `audio`, recorded to `debug/phone_adb/<call_id>-downlink.wav` only when `logging.include_transcripts` is true; agent built with `capture_factory=audio.capture, speaker_factory=audio.speaker`; `run(..., still_connected=transport.in_call, dial=dial, answered=audio.answered)`. Drop `with self._mic_route(), self._speaker_route():`. `_DIALED = ("DIALED",)`.
  - New `_DIAL_FAILURES` keys and lines: `HELPER_MISMATCH` "The call helper on your phone isn't the one I installed, so I won't run it. Something may have changed it.", `NO_PHONE` "I can't reach your phone over Wi-Fi. Turn Wireless debugging back on in Developer options.", `LOCKED_DOWN` "Your phone is blocking me from calling. Turn on USB debugging (Security settings) in Developer options.", `NO_INTERCEPTION` "Your phone won't let me into the call audio, so I can't talk on it.", `NOT_PLACED` "I asked your phone to call but the call never started.", `""` "I couldn't reach your phone to place the call."
  - New `_FIND_LINES`: `PLAYING` (keep today's text), `NO_PHONE`, `LOCKED_DOWN` (same fixes as above, phrased for ringing), `RING_FAILED` "Your phone didn't start the sound. Try again in a moment."; `_MSG_FIND_FAILED` drops "Phone Link".
- [ ] **Step 1: Update tests first.** In `tests/test_phone_service.py`: `FakeDialer` keeps `dial/in_call/hang_up/play_sound` and gains `audio()` returning a stub with `capture/speaker/answered`; delete `test_mic_is_routed_for_the_call_and_restored`, `test_the_room_speaking_hook_reaches_the_agent`, the Phone Link `_DIAL_FAILURES` test and every `PhoneLink*`/`RepairCallingLink` class (they move in Task 6); add `test_no_phone_gets_its_own_line`, `test_locked_down_gets_its_own_line`, `test_find_phone_no_phone_says_wireless_debugging`, `test_the_agent_gets_the_phone_audio_and_the_answered_signal` (agent factory receives nothing; assert the real `_make_agent` path wires `audio.capture`/`audio.speaker` by patching `GeminiLiveAgent`), `test_construction_never_runs_adb` (patch `subprocess.run`/`Popen` to raise). Rename `NoRealPhoneTests.test_construction_never_runs_powershell` accordingly.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_phone_service tests.test_orchestrator_phone -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the interface above in `services/phone_service.py`, `config.yaml` and the orchestrator call site.
- [ ] **Step 4:** Run `uv run python -m unittest discover -s tests -q`. Expected: OK, same count as before minus the removed tests plus the new ones.
- [ ] **Step 5: Commit** `feat(phone): control_phone runs over ADB`

### Task 6: Move the Phone Link path to `deprecated/`

**Files:**
- Move (`git mv`, same relative path under `deprecated/`): `services/phone_link.py`, `tools/fix_phone_link.py`
- Create: `deprecated/services/phone_link_audio.py` holding `LoopbackCapture` and `CableSpeaker` cut from `services/gemini_live_call.py` (with their imports and `_SILENT_PEAK`/`_BLOCK_MS` if only they use them)
- Create: `deprecated/tests/test_phone_link.py` holding the removed `PhoneLink*`, `RepairCallingLink` and loopback-device tests (not discovered; kept for reference)
- Modify: `services/gemini_live_call.py` (module docstring: the audio path is the phone's; `capture_factory` and `speaker_factory` become required keyword arguments; drop `cable_device`, `listen_device`), `deprecated/README.md`, `pyproject.toml` + `uv.lock`

- [ ] **Step 1:** Make the moves; remove `soundcard` from the `phone` extra with `uv remove --optional phone soundcard` (keep `google-genai`).
- [ ] **Step 2:** Run `uv run python -m unittest discover -s tests -q` and `uv run python -c "import services.phone_service, services.gemini_live_call, core.orchestrator"`. Expected: OK, and `git grep -n "phone_link\|LoopbackCapture\|CableSpeaker\|soundcard" -- ':!deprecated' ':!docs'` prints nothing but CLAUDE.md history lines.
- [ ] **Step 3:** `deprecated/README.md`: a dated (2026-10-13) table row per moved item, "why" = replaced by the ADB path, Phone Link's calling link would not reconnect (six fixes tried live); note VB-CABLE can be uninstalled and the default devices it changed should be checked.
- [ ] **Step 4: Commit** `refactor(phone): move the Phone Link path to deprecated/`

### Task 7: The CLI over the services, and the docs

**Files:**
- Modify: `tools/phone_adb_call.py` (thin CLI: `check`, `state`, `ring [--seconds 20]`, `call <who> --kind --goal --details` building the real `PhoneService` path minus the read-back; `probe` removed)
- Modify: `CLAUDE.md` (Project Structure entries for `android/callaudio/`, `services/phone_adb.py`, `services/phone_call_audio.py`, the new tests and the tool; "Credentials" phone bullet; rewrite the PhoneService section's transport parts: setup = Wireless debugging paired once + Security settings, the three hardware findings, the answered nudge, europe-west1 numbers, what moved to deprecated; Development Session Log entry), `README.md` (phone setup lines, if it has a phone section)

- [ ] **Step 1:** Rewrite the CLI on `AdbPhone` / `PhoneCallAudio` / `PhoneService` internals; no duplicated transport code left in `tools/`.
- [ ] **Step 2:** Run `uv run python tools/phone_adb_call.py check`. Expected: `INTERCEPTABLE=true`, `TELEPHONY_TX=18/...`, `call state: idle`.
- [ ] **Step 3:** Update the docs as listed.
- [ ] **Step 4: Commit** `docs(phone): ADB call path in CLAUDE.md, CLI over the services`

### Task 8: Live verification on the real phone

No code unless something fails. Ask Master Miguel before each outward call.

- [ ] **Step 1:** `uv run python tools/phone_adb_call.py ring --seconds 5`. Expected: the phone beeps for ~5 s even on silent; alarm volume back to what it was (`adb shell cmd media_session volume --stream 4 --get` before/after).
- [ ] **Step 2:** With his go-ahead, a call through California by voice ("call Marta and ask ...") with `mute_phone_mic: true`. Expected: she is heard (callee confirms), no room audio reaches the callee, report spoken afterwards, `logs/calls.jsonl` row has `dial_status: DIALED` and `hang_up_status` `ENDED` or `NO_CALL`. If the callee hears nothing with the mic muted, set `mute_phone_mic: false`, note it in CLAUDE.md, and retest.
- [ ] **Step 3:** "Where is my phone?" by voice. Expected: `phone_find` → `PLAYING` line, the phone beeps.
- [ ] **Step 4:** Commit any config/doc correction from the live run: `fix(phone): <what the live run showed>`.
