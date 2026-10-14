# Incoming call ownership: proposed design

Date: 2026-10-14. Planning only; implementation requires review of these artifacts.

## Intent and scope

Miguel explicitly wants to answer an incoming call himself by saying “answer it and put it on speakerphone”, and also wants a plan for California to handle an incoming call herself. He confirmed the human route means the phone’s own speaker and microphone, not laptop audio.

Miguel confirmed California’s first version should take a message and report back, but first ask him whether she should cover any specific point. Free-form delegated conversations and switching an already answered human call to AI are separate later work.

Two deliverables, in order:
1. Voice answer to native phone speakerphone, with no AI call-audio interception.
2. Explicit delegation of a ringing call to Gemini Live for message taking.

The existing announcement remains once per ring; neither mode auto-answers. An explicit native-speaker answer command is authorization to accept the current ring; do not add a read-back to native answer. California delegation has the requested two-turn question flow described below. An outgoing call continues to require its existing read-back.

## Current code and constraints

Baseline is 665dd01 (phone-toolkit PR #54), including the outgoing ADB helper and call bridge. Planning branch: codex/incoming-call-planning. Finish/integrate that dependency before implementation; do not add implementation to the approved toolkit PR.

CallAudio.java currently has check/dial/hangup/ring/bridge, no answer or speaker mode. PhoneCallAudio.arm is set only after California’s outgoing dial; keep that default protection. PhoneService.enabled depends on Gemini credentials: native answer must have its own capability gate. IncomingCallWatcher.pop consumes an announcement, so answer targeting needs a non-consuming current-ring view. Room announcements do not currently establish conversation history; tool dispatch must inject current-ring context from the watcher, not depend on model recollection.

Windows only; Python 3.14 and uv only. No new packages expected. Never uv add/remove/sync while main.py is running. Keep Gemini europe-west1 and silence_duration_ms=700. Never restart main.py without asking. No phone writes, helper pushes, actual answering or calls during planning. Live checks later use Miguel’s backup line; never Marta. Synthetic contacts in tests/docs. Logs use kinds/counts/statuses, not full numbers. Preserve the hash-checked helper launch and adbkey ACL. Docs go in CLAUDE.md, never AGENTS.md.

## Option selection

Recommended: extend the existing shell helper and ADB services, and reuse Gemini’s established bridge for AI. This preserves the current architecture and avoids installing a new phone app.

Alternative: a dedicated Android InCallService/default dialer app offers stronger call-specific routing APIs but requires installation, role changes and a new subsystem. Reconsider only if native speakerphone fails the feasibility gate; do not silently install it.

Alternative: laptop speaker/microphone bridging requires room capture, speaker routing and echo cancellation. It does not satisfy the selected native-phone route and is excluded here.

## Human route

“Answer it” / “answer it on speakerphone” -> control_phone(phone_answer_speaker) -> trusted current ring captured for this command -> guarded helper accept -> observe same call ACTIVE -> route to phone built-in speaker and verify phone mic remains usable -> “Answered on your phone’s speakerphone.”

No Gemini connection, capture, uplink injection, transcript or recording for a human-owned call. Do not change Windows audio defaults. Stop California’s ongoing room playback before accepting and keep ordinary room listening behavior (no new always-on meeting mode). Human mode leaves call ending to Miguel on the phone; no background close() or outgoing worker cleanup may end his call.

If acceptance succeeds but route verification fails, leave the connected call on its actual route and say “The call is answered, but I couldn’t switch it to speakerphone. Use the phone’s speaker button.” Never claim routing success from a setter return alone. If acceptance is uncertain, report uncertainty and do not automatically hang up or redial.

## California route

“California, you answer it” / “take a message” -> first control_phone(phone_answer_california, confirm=false) stores a pending request bound to this ring and asks “Anything specific you want me to ask or tell them, or should I just take a message?” -> Miguel replies with points or “just take a message” -> control_phone(phone_answer_california, confirm=true, details=<his points or empty>) validates a later user turn, consumes the pending request and reserves one call ownership slot -> connect Gemini while the phone is still ringing -> revalidate the same ring -> accept without dialling -> observe same call ACTIVE -> explicitly arm only this accepted call’s bridge -> enforce verified phone-mic mute -> caller downlink to Gemini / generated audio to uplink -> goodbye -> end only the call California owns -> report caller/message/urgency.

No acceptance, Gemini session or microphone capture happens before Miguel answers that question. Pending requests expire on ring end, replacement or the existing phone.confirm_timeout_ms. Timeout is not permission to answer. The confirmation must come from a later user turn, not a second tool call in the same turn. Relay Miguel’s exact requested facts/questions as notes; do not invent points. Ask only once per pending request. Fixed incoming prompt: identify herself as Miguel’s AI assistant, explain she can take a message, ask who is calling and what they want Miguel to know, clarify once when needed, cover any points Miguel gave, read back important details, then say goodbye. Do not say she is placing a call, pretend to be Miguel, disclose whereabouts/schedule/credentials, make commitments, or execute room/device/message actions from the caller’s words. Caller name and transcript are untrusted data; use the existing report quoting boundary.

No Gemini connection failure may answer the phone. Require a checked mute for AI mode; the existing optional mute failure behavior for outgoing calls remains compatible. AI mode may not silently fall back to human mode. If the bridge fails after confirmed owned acceptance, release it and end that owned call with a failure report. If ownership becomes uncertain, stop audio capture/injection and do not issue global hangup. No bridge may attach to a manually answered call or replacement call.

Default AI duration limit: existing phone.max_call_s. Keep existing no_answer_s as the opening-silence safety limit unless live testing shows it unsuitable; document it as an already accepted incoming call timing out, not unanswered ringing. Existing answered nudge may open the conversation as soon as the bridge is connected.

## Target identity and arbitration

Extend watcher state with RingOffer(ring_id: int, call: IncomingCall), monotonically allocated per ring episode. A number filling in retains ring_id; idle then another ring allocates a new id even for the same caller. current() returns the live offer without consuming the announcement. Capture this offer AND the authoritative Android call key at the beginning of the user command turn, checking the watcher episode is unchanged before and after that read. Bind them as IncomingTarget(offer, call_key); dispatch passes it out-of-band, never from model-provided caller digits. Retain the same key across the question, reply and backend connection; do not discover a new target during confirmation. An absent or changed offer fails with NO_RING/STALE_RING.

For answer and cleanup decisions, independently read authoritative phone call topology and call identity immediately before mutation. Initial support is one INCOMING call with no other ACTIVE/HOLDING/DIALING/ALERTING or WAITING call. Reject call waiting and multi-call/multi-modem ambiguity without switching, holding or ending anything.

Python watcher episode ids are not Android call ids. Feasibility work must discover a stable phone call key, redact it in logs, and verify it survives RINGING -> ACTIVE. Bind owned cleanup/audio monitoring to that key. If identity cannot be checked reliably, AI mode remains disabled. Global Telecom acceptance has a time-of-check/time-of-use boundary; do not claim atomic targeting from two separate dumps. Probe targeted acceptance if available. If only the global API works, document its residual race and require a fresh single-call topology plus matching ring immediately before and accepted identity immediately after; mismatches never arm audio or invoke hangup.

Mutations share a one-call ownership coordinator across native incoming, AI incoming and outgoing. Native ownership is tracked read-only until the call ends; it does not claim the AI worker. Digest and watcher transports remain separate. No lock is held across a Gemini network await or ADB process wait: atomically reserve a state, perform work outside the lock, then validate the reservation before changing ownership.

## Capability gate and primary sources

AOSP ITelecomService exposes acceptRingingCall(String callingPackage), but the inspected interface does not expose a general setAudioRoute method. Do not invent a binder transaction number or cmd telecom routing subcommand. AudioManager.setCommunicationDevice selects communication devices, but platform Telecom controls call routing; HyperOS cellular behavior is unverified.

Stage 1 starts with a controlled feasibility probe of acceptance, native speaker routing, mic state, observable call identity and route persistence after the helper exits. Accept/speaker support must pass before exposing human mode. AI support also requires identity-aware capture/cleanup and verified mute. Unsupported devices get a truthful spoken refusal.

Sources (checked 2026-10-14):
- https://android.googlesource.com/platform/frameworks/base/+/master/telecomm/java/com/android/internal/telecom/ITelecomService.aidl
- https://developer.android.com/reference/android/media/AudioManager#setCommunicationDevice(android.media.AudioDeviceInfo)
- https://android.googlesource.com/platform/packages/services/Telecomm/+/android16-release/src/com/android/server/telecom/CallAudioRouteStateMachine.java

These sources justify candidate APIs; they do not verify this Xiaomi’s behavior. Existing outgoing audio is live-tested, incoming accept/speaker is not.

## Acceptance and exclusions

Native: no Gemini credentials required; same ring accepted, speaker route and mic verified, user talks both ways, ending/shutdown never terminates human call. AI: explicit delegation only, backend failure leaves ring untouched, caller hears disclosure/message prompt, message report arrives, room mic does not leak, owned call ends, outgoing behavior unchanged.

Unit tests never touch a real device. Live approval is obtained immediately before each answering test and helper push; manual tests cover phone locked, Bluetooth headset connected, caller hangs up during connection, new ring replacing old, and phone Wi-Fi disappearing.

Excluded: automatic screening, quiet-hours policy, call-waiting switching, conferences/video, laptop speakerphone, transfer of an already answered call, AI-to-human mid-call handoff, rejection/silencing/new hang-up voice commands, and unrelated existing minors.

## Planning self-review

Requirements covered by native Tasks 1–5 and California Tasks 1–5. Interface check: RingOffer -> IncomingTarget -> IncomingTurn/PendingIncoming preserve the original Android key; native gate is independent of Gemini; AI connects before acceptance and requires a later user turn. Both plans include five concrete failure modes and their owning tests. Hardware-dependent API choice is assigned to an explicit feasibility deliverable, not presented as verified. No implementation/live validation was performed during planning.
