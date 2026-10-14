# California Incoming Message Taking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** California asks Miguel about any specific points, then explicitly accepts the same ringing call, takes a message through Gemini and reports back.

**Architecture:** Reuse stage 1’s ring identity, guarded acceptance and ownership. Add a two-turn pending delegation bound to a trusted user turn and the ring episode; connect Gemini before acceptance, then arm the call bridge only for the confirmed owned call with mandatory verified phone-mic mute. Add an incoming-specific prompt, nudge and report without changing outgoing behavior.

**Tech Stack:** Windows, Python 3.14/uv, existing Gemini Live on Vertex europe-west1, existing ADB callaudio bridge, unittest/fakes.

**Spec:** docs/superpowers/specs/2026-10-14-incoming-call-ownership-design.md (proposed; review before implementation).

## Global Constraints

- Dependency: docs/superpowers/plans/2026-10-14-native-incoming-speakerphone.md Tasks 1–5 and its call identity/ownership contracts. No repeat of completed stage 1 tasks.
- Windows only; uv only; no new dependencies. Keep europe-west1, silence_duration_ms=700, existing phone.max_call_s and phone.confirm_timeout_ms. Never sync dependencies while main.py runs.
- Planning is not live answering permission. Ask immediately before test acceptance/helper push and before restarting main.py; backup line only, never Marta.
- First delegation ALWAYS asks “Anything specific you want me to ask or tell them, or should I just take a message?” No answer/network session/capture before a later user reply. No timeout auto-answer.
- Accept only same INCOMING episode/key and single-call topology. No human-call takeover, call waiting, conferences or transferring an already active call.
- Mandatory verified phone-mic mute for AI mode; no silent human fallback. Preserve optional outgoing mute behavior. Capture/injection never start without confirmed owned acceptance.
- Reuse hash-checked helper and privacy flags. No raw numbers/names/transcripts in status logs or bus events with transcripts off. Call report labels caller-provided words as data; no caller text may invoke room tools.
- All tests fake ADB/sockets/Gemini; invented contacts. CLAUDE.md updated, AGENTS.md untouched; default-off AI flag until live validation.

## Review Focus

1. First tool call or same-turn fabricated confirmation: ask only, no answer/Gemini/mute/capture (Task 2).
2. Ring ends or is replaced while Miguel replies/Gemini connects: pending authorization invalid, never accept replacement or attach audio (Tasks 2/4).
3. Gemini startup fails: phone continues ringing untouched; bridge/mute failure after owned acceptance ends only that owned call (Tasks 3/4).
4. Human answers manually or identity becomes unknown: stop audio, never capture or issue unscoped hangup (Tasks 3/4).
5. Caller requests device control, private facts or different persona: incoming prompt and report keep their words as data, preserve no-tools boundary; outgoing prompts/logs remain compatible (Tasks 1/5).

## File Map

| Files | Responsibility |
|---|---|
| services/phone_prompts.py, services/call_outcome.py | Incoming prompt, optional points and direction-aware message outcome |
| services/phone_service.py | Pending request/turn binding, incoming worker, ownership-safe cleanup, direction-aware report |
| services/phone_adb.py | Identity-specific in-call/hang-up methods |
| services/phone_call_audio.py | Explicit incoming ownership arm and strict bridge handshake |
| services/gemini_live_call.py | Incoming accept callback after backend connection; incoming opening nudge |
| android/callaudio/src/CallAudio.java, callaudio.dex, README.md | Strict mute/identity bridge variant, checked cleanup |
| core/orchestrator.py, services/llm.py | Trusted turn token and two-turn tool flow; incoming report wording |
| config.yaml, tests/config_fixture.py, CLAUDE.md | AI capability flag, offline guards and documented behavior |
| tests/test_phone_prompts.py, test_phone_service.py, test_phone_adb.py, test_phone_call_audio.py, test_gemini_live_call.py, test_orchestrator_phone.py | Coverage for callbacks, targeting, prompt and ownership |

### Task 1: Incoming prompt and report contracts

**Files:** Modify services/phone_prompts.py, services/phone_service.py, services/call_outcome.py, core/orchestrator.py; tests/test_phone_prompts.py, test_phone_service.py, test_call_outcome.py, test_orchestrator_phone.py.
**Interfaces:** build_incoming_prompt(owner: str, points: str = '') -> str. CallReport gains direction: str='outgoing' and answer_status: str=''; metadata/call_record include them with backwards-compatible outgoing defaults. Incoming worker still supplies existing CallBrief (kind general, goal take a message, details points) to existing outcome reader. Incoming status message_taken only when outcome evidence supports it; otherwise no_message/failed. Add summarize_call(brief: CallBrief, transcript: str, model: str="", client=None, *, direction: str="outgoing") -> dict | None in services/call_outcome.py. Select incoming statuses (message_taken/no_message/failed) for incoming direction and existing OUTCOME_STATUSES for outgoing; do not add incoming statuses globally to the outgoing enum. PhoneService._read_outcome forwards direction to its internal wrapper while keeping injected two-argument summarizers compatible.

- [ ] Step 1: Tests test_incoming_prompt_identifies_ai_and_takes_message, test_points_are_notes_not_invented_facts, test_caller_words_do_not_authorize_room_tools, test_incoming_prompt_has_no_outgoing_opening, test_incoming_report_says_answered_not_called, test_silent_accepted_call_is_no_message_not_no_answer, test_old_log_defaults_outgoing. Assert required semantic prompt clauses and report formatting; no evaluation of a live model.
- [ ] Step 2: Focused prompt/service/orchestrator tests fail on new contracts.
- [ ] Step 3: Add fixed incoming persona/flow: disclose AI, ask identity/message/urgency, cover Miguel’s points, clarify once, read back important facts, goodbye. Do not reuse outgoing “say why you are calling” flow. Keep common safety rules but exclude inappropriate claims about owner whereabouts. Never reveal bank/credentials/location or commit Miguel to arrangements. Report introduction: “You answered an incoming call from ... to take a message.” Mask unknown numbers. Transcript is labelled caller data, and respect include_transcripts for persistent logs/events. Make no_message and accepted-silence wording direction-specific.
- [ ] Step 4: Focused tests pass; existing outgoing prompt/outcome tests unchanged and green.
- [ ] Step 5: Commit feat(phone): incoming message-taking prompt and report direction.

### Task 2: Ask Miguel once and bind the reply to the same ring

**Files:** Modify services/phone_service.py; tests/test_phone_service.py.
**Interfaces:** Frozen IncomingTurn(target: IncomingTarget | None, turn_id: int). PendingIncoming(ring_id: int, call_key: str, created_turn_id: int, expires_at: float). PhoneService.answer_california(turn: IncomingTurn, points: str='', confirm: bool=False) -> PhoneCommandResult. Config phone.incoming.california_enabled=false; Gemini enabled still required for actual delegation. Trusted turn_id comes from orchestrator, never tool params.

- [ ] Step 1: Tests test_first_request_only_asks_points_question, test_same_turn_confirm_cannot_accept, test_later_reply_with_points_starts_once, test_just_message_passes_empty_points, test_timeout_does_not_answer, test_ring_end_or_replacement_invalidates_pending, test_duplicate_confirm_is_one_shot, test_calls_disabled_does_not_accept. Fake clock and agent factory; no actual thread/network unless explicitly controlled.
- [ ] Step 2: Run uv run --no-sync python -m unittest tests.test_phone_service -v; missing interface fails.
- [ ] Step 3: First call stores/reuses one pending question for current ring and returns exact question above. Confirmation requires later trusted user turn, same live episode AND original Android key, unexpired phone.confirm_timeout_ms and enabled Gemini capability. Consume once before worker reservation; preserve points verbatim as notes. Pending is cancelled by native answer, any ended/replacement ring, service close or expiration. Expiration only refuses, never grants consent. Reserving outgoing/AI/native remains atomic through stage 1 ownership state.
- [ ] Step 4: Focused tests pass, outgoing read-back tokens remain independent and unchanged.
- [ ] Step 5: Commit feat(phone): ask for incoming talking points before delegation.

### Task 3: Identity-bound bridge with strict mute and safe hang-up

**Files:** Modify services/phone_adb.py, services/phone_call_audio.py and android/callaudio/{src/CallAudio.java,callaudio.dex,README.md}; tests/test_phone_adb.py, test_phone_call_audio.py.
**Interfaces:** AdbPhone.in_call_for(call_key: str) -> bool | None (True matching owned active/held, False reliably gone, None unreadable/mismatched ambiguity); AdbPhone.hang_up_owned(call_key: str) -> str (ENDED/NO_CALL/NOT_OWNED/UNREADABLE). PhoneCallAudio.arm_incoming(call_key: str) -> None and strict_incoming: bool=False constructor option; keep arm() outgoing behavior. AdbPhone.audio(record_path: str='', *, strict_incoming: bool=False) returns configured bridge. Helper strict variant is bridge_incoming <port> 16000 24000 <call_key>; existing bridge mode unchanged.

- [ ] Step 1: Tests test_unarmed_bridge_never_captures_manual_call, test_incoming_arm_requires_matching_active_key, test_mute_failure_never_opens_capture_or_uplink, test_mute_is_observed_before_ready_handshake, test_ownership_loss_stops_audio_without_global_hangup, test_owned_hangup_rejects_replacement, test_stop_restores_prior_mute_and_removes_forward, test_outgoing_optional_mute_remains_compatible. Use real bridge + fake socketpair/process runners.
- [ ] Step 2: Focused ADB/bridge tests fail on new methods/mode.
- [ ] Step 3: Python and helper both validate the call key. Strict mode checks matching ACTIVE and single-call topology, saves prior mic state, mutes and reads back true before opening call audio; emit INCOMING_READY only after those gates. Outgoing handshake remains LISTENING. Incoming “answered” flag becomes true only after owned strict handshake/socket connection. Recheck identity during stream polling; mismatch/unreadable state stops capture and injection. Owned cleanup must target the call if available or recheck same sole key inside helper before global endCall; if not verifiable refuse mutation. Restore only mic state this helper changed, and never overwrite a replacement call’s user-controlled mute; document route/control lifetime from live probe. Stop closes socket/process/forward idempotently and joins bounded threads.
- [ ] Step 4: Rebuild dex; focused tests pass including outgoing bridge timing/cleanup regression coverage. No real helper push in tests.
- [ ] Step 5: Commit feat(phone): own incoming bridge and require verified mic mute.

### Task 4: Connect backend first, then accept this ring

**Files:** Modify services/gemini_live_call.py, services/phone_service.py; tests/test_gemini_live_call.py, test_phone_service.py.
**Interfaces:** Append accept: Callable[[], bool] | None=None and incoming: bool=False to GeminiLiveAgent.run and internal _run. Reject supplying dial and accept together. Existing dial path unchanged; incoming invokes accept once only inside successfully connected session. New ended_by accept_failed; incoming no-speech timeout ended_by incoming_silence. PhoneService._run_incoming(turn: IncomingTurn, points: str) -> None handles pending-confirmed worker only.

- [ ] Step 1: Tests test_connect_failure_does_not_accept, test_accept_runs_only_after_live_connect, test_incoming_never_calls_dial, test_stale_ring_during_connect_refuses, test_accept_failure_never_arms_bridge, test_incoming_nudge_does_not_say_why_calling, test_bridge_failure_hangs_up_only_owned_call, test_no_unowned_hangup_in_finally, test_shutdown_ends_owned_ai_but_not_human_call.
- [ ] Step 2: Run focused Gemini/service tests; new interfaces fail.
- [ ] Step 3: Worker reserves AI ownership, builds incoming prompt and strict bridge, connects backend before acceptance. Accept callback rechecks original turn ring and authoritative key, invokes answer_incoming(speaker=False), records confirmed accepted key and then arm_incoming. If ring/manual answer changes, refuse and release reservation without phone mutation. Invoke strict checks before PCM flow; no speakerphone or room-mic fallback. Incoming nudge: “The caller is connected and waiting. Introduce yourself as Miguel’s AI assistant and offer to take a message.” Keep 0.8 s answered grace and outgoing nudge unchanged. Existing no_answer_s is incoming opening-silence limit; incoming_silence reports no message. Finally stop/release bridge then hang_up_owned only when acceptance and identity are confirmed; log/report failures and ownership uncertainty without global hangup. Clear active slot only when topology resolves ended; unknown physical ownership remains blocked until resolved. Publish status only. Use existing max_call_s.
- [ ] Step 4: Gemini/service/bridge tests pass, including outgoing dial/nudge/end-call behavior. Full suite before committing integrated worker.
- [ ] Step 5: Commit feat(phone): delegate incoming calls after backend readiness.

### Task 5: Two-turn voice flow, docs and live acceptance

**Files:** Modify core/orchestrator.py, services/llm.py, config.yaml, tests/config_fixture.py, CLAUDE.md; tests/test_orchestrator_phone.py, test_llm_prompt.py, test_mood_rules.py.
**Interfaces:** Add trailing incoming_turn=None to _dispatch_phone (keep stage 1 incoming_target compatibility). IncomingTurn carries captured target plus current user turn_id; pending retains its original Android key across turns, never retargets. Increment trusted turn_id only for actual user commands; incoming report/system/tool turns do not count as human confirmation. Add phone_answer_california enum; reuse details for optional points and confirm for the later reply. Clarify confirm schema applies to outgoing read-back OR incoming points reply.

- [ ] Step 1: Tests test_first_delegation_relays_question_only, test_user_points_reply_confirms_current_ring, test_same_turn_tool_loop_cannot_confirm, test_report_turn_cannot_confirm, test_no_extra_outgoing_readback_for_incoming, test_schema_distinguishes_human_and_ai_answer, test_transcripts_off_has_no_private_event_payload.
- [ ] Step 2: Focused orchestrator/schema tests fail on action and trusted turn plumbing.
- [ ] Step 3: First action asks points; next human reply “just a message” passes details empty, or relay requested points as notes with confirm true. No details invented from caller title/notification. Service gate owns timing/ownership; model instructions do not replace it. Room playback must finish/stop before accepted call so helper mic-mute timing is predictable; AI room listening keeps its single reader. Update CLAUDE.md command examples, two-turn timing, accepted-call silence, strict mute, startup/identity failures, report and deferred capabilities. Fixtures disable both incoming answer flags.
- [ ] Step 4: Full uv run --no-sync python -m unittest discover -s tests exits 0. With approved restart and each live answer test, backup caller verifies disclosure/message taking, supplied point covered, summary correct, no room/phone-mic leakage, ended call and restored mic. Test “just a message”, specific point, cancel during question, backend unavailable, manual answer and Wi-Fi loss. Prove no acceptance before the answer to the points question. Enable AI flag only after success; record measured results and limitations.
- [ ] Step 5: Commit feat(phone): voice incoming message taking with optional points; independent review before merge.

## Handoff

Review shared design and both proposed plans before implementation. Subagent-driven execution is recommended and was the previously selected workflow: the ownership boundaries, helper APIs and Gemini startup span several files, and a bad cleanup could terminate a real call. Automatic answer, call waiting, free-form negotiation and mid-call AI/human transfer remain outside this plan.