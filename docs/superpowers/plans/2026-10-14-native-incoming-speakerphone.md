# Native Incoming Speakerphone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** “Answer it on speakerphone” accepts the current ring for Miguel on the phone’s speaker and microphone, without Gemini or audio interception.

**Architecture:** Extend the hash-checked shell helper only after proving native answer/routing on the Xiaomi. Keep a non-consuming ring offer for trusted command targeting, and add native incoming ownership to PhoneService independently of its Gemini capability gate.

**Tech Stack:** Windows, Python 3.14/uv, unittest, ADB, existing Java reflection helper and portable JDK/r8 build.

**Spec:** docs/superpowers/specs/2026-10-14-incoming-call-ownership-design.md (proposed; review before implementation).

## Global Constraints

- Dependency: phone-toolkit PR #54, planning baseline 665dd01. Integrate it before execution; implement on a new branch, not the approved PR.
- Windows only; uv only; no new dependencies. Never uv add/remove/sync while main.py runs. Verification uses PYTHONIOENCODING=utf-8 and uv run --no-sync python -m unittest.
- Native audio is PHONE speaker/microphone. No Gemini, bridge, recording, transcript, phone-mic mute or Windows default-device changes.
- Keep hash-checked helper launch, adbkey ACL, europe-west1 and silence_duration_ms=700. Update CLAUDE.md; never AGENTS.md.
- Explicit native answer needs no read-back. Accept only the same live INCOMING ring; refuse call waiting, another active/held/outgoing call and unknown topology.
- Tests inject runner/popen/clock, never real ADB; synthetic names/numbers. Live answer/helper push needs approval immediately beforehand; backup line only, never Marta. Ask before main.py restart.
- Task 1 is a feasibility stop/go gate. Never invent binder transaction numbers or unverified cmd telecom subcommands.

## Review Focus

1. A stops and B rings during command processing: reject old offer, never target B (Tasks 2/4).
2. Miguel answers manually: stale request causes no capture/mute/hangup or routing mutation (Tasks 3/4).
3. Acceptance succeeds but speaker route fails: keep connected call and report partial success (Tasks 3/4).
4. Missing Gemini credentials: native answer works independently (Tasks 4/5).
5. Shutdown, ADB loss, concurrent outgoing call: never end human-owned call; unknown ownership blocks mutations until resolved (Task 4).

## File Map

| Files | Responsibility |
|---|---|
| services/incoming_call.py | Non-consuming RingOffer with ring-episode identity |
| services/phone_adb.py | Phone topology/identity, guarded native answer result |
| android/callaudio/src/CallAudio.java, callaudio.dex, README.md | Proven answer/route helper operation |
| services/phone_service.py | Native capability gate and incoming/outgoing ownership |
| core/orchestrator.py | Trusted turn ring context and native dispatch |
| services/llm.py, config.yaml, tests/config_fixture.py | One action; default-off answer flag |
| tests/test_incoming_call.py, test_phone_adb.py, test_phone_service.py, test_orchestrator_phone.py, test_llm_prompt.py | Fake-device regression coverage |
| docs/superpowers/probes/2026-10-14-native-incoming-answer.md, CLAUDE.md | Sanitized probe evidence, setup and live limitations |

### Task 1: Prove native acceptance, identity and speaker routing

**Files:** Create probe report above. Temporary scripts/helpers stay in ignored debug/phone_adb; retained implementation belongs to Task 3.
**Interfaces:** Produces report with actual API signatures, stable ringing-to-active Android call key, topology/route/mic readback shapes, persistence after helper exit, and PASS/UNSUPPORTED verdict.

- [x] Step 1: Inspect installed methods and sanitized dumpsys telecom/audio/telephony output read-only. Candidate answer: ITelecomService.acceptRingingCall with com.android.shell; reflect actual signature. Determine stable call key and observable route.
- [x] Step 2: With approval for each write/test, Miguel rings from backup line. Probe guarded acceptance and native built-in-speaker selection; setCommunicationDevice is a candidate, not assumed success. Verify actual Telecom route, both-way audio, mic unmuted and routing persistence after helper exits. Check locked phone and connected Bluetooth headset. Do not change default dialer or permissions automatically.
- [x] Step 3: PASS requires all observed behavior and stable identity. Record exact selected mechanism, redacted evidence and partial-failure behavior. If unsupported, stop remaining tasks and recommend a separately designed InCallService app or narrower answer-only feature for user choice.
- [x] Step 4: Commit redacted report only: docs(phone): verify native incoming answer capability.

**Task 1 outcome (2026-10-14, binding for Tasks 3-5):** PASS by a different route than planned. Acceptance is the helper's `acceptRingingCall` (proven). Speaker is NOT an AudioManager setter (both UNSUPPORTED from the shell): it is a tap on the default dialer's own Speaker button -- `exec-out uiautomator dump /dev/tty`, the nearest clickable ancestor of the node labelled Speaker in package `com.google.android.dialer`, `input tap` at the centre of its bounds, tap only when its `checked` disagrees with the wanted state, verify `dumpsys audio` `Active communication device ... type:speaker` and mic unmuted. Evidence and exact commands: the probe report's last section. Miguel also wants **speaker off** by voice: the same button, same guards.

### Task 2: Episode-bound ring offers independent of announcements

**Files:** Modify services/incoming_call.py; tests/test_incoming_call.py.
**Interfaces:** Frozen RingOffer(ring_id: int, call: IncomingCall); IncomingCallWatcher.current() -> RingOffer | None; keep pop() -> IncomingCall | None compatible.

- [ ] Step 1: Write test_current_survives_announcement_pop (same id), test_number_fill_keeps_ring_id, test_same_number_after_idle_gets_new_id, test_dead_stream_has_no_current_offer. Assert current does not consume announcement.
- [ ] Step 2: Run uv run --no-sync python -m unittest tests.test_incoming_call -v; missing interface fails.
- [ ] Step 3: Allocate monotonic episode ids under watcher lock. Non-consuming current returns live latest call; number fill keeps id, idle-to-ring creates id, stream gap invalidates answerability. Keep once-per-ring announcement behavior.
- [ ] Step 4: Same test module passes, including existing watchdog/backoff/announcement tests.
- [ ] Step 5: Commit feat(phone): expose episode-bound incoming ring offers.

### Task 3: Guarded helper acceptance and verified transport result

**Files:** Modify services/phone_adb.py and android/callaudio/{src/CallAudio.java,callaudio.dex,README.md}; tests/test_phone_adb.py; tests/fixtures/ (a synthetic in-call `uiautomator` XML shaped like the real one: label TextView inside a clickable resource-id-less android.view.View; no names or numbers). Task 1 probe is binding for API choice: helper accepts, the speaker is the dialer-button tap from Python (`set_speaker`), never an AudioManager setter.
**Interfaces:** Frozen CallLeg(key: str, state: str, modem: int), CallTopology(legs: tuple[CallLeg,...]); parse_call_topology(text: str) -> CallTopology | None; AdbPhone.call_topology() -> CallTopology | None; AnswerResult(status: str, call_key: str = '', accepted: bool = False, speaker_verified: bool = False); AdbPhone.answer_incoming(expected_key: str, speaker: bool = True) -> AnswerResult; AdbPhone.set_speaker(expected_key: str, on: bool) -> SpeakerResult(status: str, speaker_verified: bool) with statuses SPEAKER_ON, SPEAKER_OFF, ALREADY, NO_BUTTON, ROUTE_FAILED, STALE_CALL, UNREADABLE, NO_PHONE; parse_speaker_button(xml: str) -> SpeakerButton(center: tuple[int,int], checked: bool) | None (refuses zero or two candidates, wrong package, unparseable bounds). State names ringing/active/holding/dialing/alerting/waiting/idle; unknown dump -> None.

Statuses: ANSWERED_SPEAKER, ANSWERED (speaker=False), ANSWERED_ROUTE_FAILED, NO_RING, STALE_RING, BUSY, UNREADABLE, NO_PHONE, LOCKED_DOWN, HELPER_MISMATCH, NOT_ANSWERED, ANSWER_UNCERTAIN, UNSUPPORTED. accepted=True only after matching authoritative ACTIVE identity.

- [ ] Step 1: Fake-runner tests test_answer_requires_same_single_key, test_manual_answer_does_not_mutate, test_waiting_or_multiple_calls_are_busy, test_answer_requires_matching_active_identity, test_route_setter_alone_is_not_success, test_route_failure_preserves_call, test_hash_mismatch_runs_no_answer, test_answer_never_dials_mutes_or_bridges.
- [ ] Step 2: Run uv run --no-sync python -m unittest tests.test_phone_adb -v; missing methods/types fail.
- [ ] Step 3: Implement probe-selected parser/helper operation. Validate keys with ^[A-Za-z0-9@_.:-]{1,128}$ before shell construction; unsupported key shapes are refused. Preflight helper/Security gate without interception. Recheck expected key/topology inside helper immediately before accepting. Observe matching ACTIVE for at most 5 s and route/mic for at most 3 s. Fixed status markers only, no personal output. Never infer speaker success from a tap alone: success is the audio route read back. The tap runs only for the same sole ACTIVE key re-read immediately before it, at most one tap per request, no retry tap (a second tap toggles back). If only global acceptance is available, preserve the spec’s residual TOCTOU limitation; post-accept mismatch never permits bridge arm or global cleanup. No automatic hangup after partial success.
- [ ] Step 4: Rebuild android/callaudio/build.ps1; focused tests pass; dex nonempty, rebuild instructions accurate. No device push in unit verification.
- [ ] Step 5: Commit feat(phone): guarded incoming answer and native speaker route.

### Task 4: Native ownership independent of Gemini

**Files:** Modify services/phone_service.py, config.yaml, tests/config_fixture.py; tests/test_phone_service.py.
**Interfaces:** Frozen IncomingTarget(offer: RingOffer, call_key: str); PhoneService.bind_incoming(offer: RingOffer | None) -> IncomingTarget | None; PhoneService.answer_speaker(target: IncomingTarget | None) -> PhoneCommandResult; native_answer_enabled: bool. Config phone.incoming.answer_speaker_enabled=false until live gate passes. Shared _active gains owner (outgoing/human/california), ring_id and call_key; lock protects reservations only, never ADB/network waits. Own human-monitor AdbPhone is injectable/lazy and separate from digest/watcher transports.

- [ ] Step 1: Tests test_native_answer_without_gemini, test_native_answer_uses_no_agent_or_audio, test_same_ring_only, test_outgoing_cannot_overlap_reservation, test_close_never_hangs_up_human, test_adb_loss_keeps_unknown_ownership, test_route_failure_is_partial_success. Assert negative side effects as well as result text.
- [ ] Step 2: Run test_phone_service; new interface fails.
- [ ] Step 3: Add platform/ADB/flag gate without credentials or google.genai import. bind_incoming reads authoritative single-ring topology on its dedicated control transport and binds its key to the unchanged watcher episode; it never mutates the phone. Capture before command processing and retain this target. Reserve then operate outside lock; independently revalidate both episode and original Android key before mutation. Monitor human call read-only on own transport, release slot only when known ended; unreadable state retains unknown ownership and blocks new mutations until resolved. close stops monitor only. Native calls never enter outgoing _run_call cleanup, recording or CallReport.

Exact copy: success “Answered on your phone’s speakerphone.” Partial success “The call is answered, but I couldn’t switch it to speakerphone. Use the phone’s speaker button.” No ring “Your phone isn’t ringing now.” Stale “That call stopped ringing. I haven’t answered the new one.” Busy “There’s already another call on your phone. Answer this one on the phone.” Uncertain “I couldn’t confirm whether the call answered. Check your phone.” Other hardware statuses retain specific fixes.

- [ ] Step 4: Run test_phone_service and test_phone_adb; outgoing confirmation/ownership regressions remain green.
- [ ] Step 5: Commit feat(phone): native incoming ownership independent of Gemini.

### Task 5: Voice action and live validation

**Files:** Modify core/orchestrator.py, services/llm.py, CLAUDE.md; tests/test_orchestrator_phone.py, test_llm_prompt.py, test_orchestrator_incoming.py.
**Interfaces:** Add trailing incoming_target=None to _dispatch_phone. At user command start, read watcher.current, call bind_incoming, then check watcher.current still has the same episode. Store this target in trusted turn context; _handle_tool_call supplies it out-of-band. If no stable target was captured, refuse; do not acquire a replacement during dispatch. Enum phone_answer_speaker and phone_speaker (with on: boolean, for a call already connected, answered by California or by hand) and one short description sentence; no model-supplied caller/id field. phone_speaker never answers and needs the same sole-ACTIVE check.

- [ ] Step 1: Tests test_answer_dispatch_before_calls_gate, test_model_cannot_override_turn_target, test_old_turn_cannot_answer_new_ring, test_playback_stops_before_native_accept, test_native_call_has_no_ai_report. Existing dispatcher calls retain defaults.
- [ ] Step 2: Focused orchestrator/schema run fails on missing action.
- [ ] Step 3: Native action before Gemini enabled gate; preserve other actions. Caller name remains untrusted display data. Stop ongoing room playback before accepting. Status-only bus events when transcripts off; no UI redesign. Update CLAUDE.md setup/actions/ownership/status and live limitations.
- [ ] Step 4: Full uv run --no-sync python -m unittest discover -s tests must exit 0. After approved restart and individual live acceptance tests, verify phone speaker/mic both ways, no Gemini/recording, no acceptance after cancellation, Bluetooth override and helper-exit persistence. Enable flag only on live success; document actual results.
- [ ] Step 5: Commit feat(phone): answer incoming calls on phone speakerphone; independent review before merge.

## Handoff

Proposed plan only. Hardware capability is intentionally gated. Review this plan and shared design before implementation; preserve subagent-driven execution if approved.