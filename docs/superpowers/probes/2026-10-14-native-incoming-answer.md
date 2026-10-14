# Native incoming answer feasibility probe

Date: 2026-10-14. Device: Xiaomi / HyperOS, Android SDK 36.

**Verdict (updated 15:39): PASS by tapping Google Phone's own Speaker button** (see the last section). The two AudioManager setters below stay UNSUPPORTED from the shell. Original verdict: **UNSUPPORTED for the tested shell-helper native-speaker route.** Guarded native acceptance and stable identity are observed, but neither tested public AudioManager speaker setter produced a speaker route. Do not enable native answer+speaker from this report. A separately designed Telecom-owned route/application or an explicitly chosen narrower answer-only feature needs a new design decision.

## Initial idle read-only observations

- One attached Wireless debugging TLS phone; no Mi Box or `:5555` target used.
- `dumpsys telecom` exposes a current `CallsManager` `mCalls` section. It was empty: zero nonempty rows and zero current call key candidates. A stable ringing-to-active key has **not** been observed.
- `dumpsys telephony.registry` contained two modem records. Both coarse call states were `0`; precise ringing/foreground/background triples were `(0, 0, 0)` and `(-1, -1, -1)`. Coarse/precise states do not establish call identity or prove unambiguous live topology.
- `dumpsys audio` exposes `Computed Preferred communication device:`, `Applied Preferred communication device:`, `Active communication device:`, `Audio mode:` and `mic mute FromSwitch`. Computed and applied preferred devices were `null` while idle. These are candidate readback surfaces; their authority for cellular routing remains unproved.
- Telecom contains `mCallAudioManager` and `mCallAudioRouteAdapter` sections, but no verified current Telecom route record was obtained at idle. Historical audio/event rows are not evidence of current route or incoming-call success.

Raw dumps were held in memory and were not saved in reports. Caller names, numbers, contact payload, device identifiers, addresses and historical call keys are omitted.

## Prepared local artifacts

Ignored `debug/phone_adb/` contains:

- `NativeIncomingProbe.java`: reflection-only `inspect` mode; reads AudioManager mode, mute, speaker flag, current communication device type and available device types. Prints API signatures from the installed `ITelecomService` and `AudioManager`. No accept, route setter, dial, hangup, capture, playback or mic setter is callable.
- `build-native-probe.ps1`: compiles with existing portable JDK 21 and existing r8, using existing compile-only Android stubs. No dependencies added or synchronized.
- `native-probe-build/dex/classes.dex`: successfully compiled. SHA256 `1D9B559404DB2B49749F6D321BA549FFC70B952181DCC0B8A0C77CC907AFCA1B`.
- `native_snapshot.py`: read-only dumpsys summarizer. Selects exactly one attached TLS phone, excludes `:5555`, refuses ambiguity, prints only static field presence/counts/state values. Does not save raw dumps or print subprocess command failures.

The production `android/callaudio/callaudio.dex` and source remain untouched. An attempted read-only pull of the installed 49 MB framework archive timed out after 60 seconds; its ignored partial local archive is not usable evidence.

## Approved installed API inspection

Miguel explicitly approved "Yes, run the read-only probe". The local hash was reverified, one TLS phone selected, and the reviewed dex pushed to `/data/local/tmp/california-native-probe-1d9b5594.dex`, made read-only, then launched through the same-command hash check and `exec`. Actual installed signatures:

```text
void ITelecomService.acceptRingingCall(java.lang.String) throws RemoteException
void ITelecomService.acceptRingingCallWithVideoState(java.lang.String,int) throws RemoteException
List<AudioDeviceInfo> AudioManager.getAvailableCommunicationDevices()
AudioDeviceInfo AudioManager.getCommunicationDevice()
boolean AudioManager.isMicrophoneMute()
boolean AudioManager.setCommunicationDevice(AudioDeviceInfo)
void AudioManager.setSpeakerphoneOn(boolean)
```

No `ITelecomService.setAudioRoute` appeared among reflected public methods. This is method availability, not evidence that shell may invoke acceptance or that either speaker setter controls cellular routing.

Idle readback: `getMode=0`, `isMicrophoneMute=false`, `isSpeakerphoneOn=false`, `communicationType=1`; available communication types `1` and `2` (earpiece/speaker candidates). No setter was invoked.

The inspect Java main printed all expected output but the `app_process` remained alive, consistent with Binder runtime threads preventing process exit. The exact owned process command `app_process / NativeIncomingProbe inspect` was matched, its PID and command rechecked, and only that process received TERM. The host command then ended nonzero because of this explicit cleanup; do not describe this as a clean helper exit. Before another helper build/run, add explicit process exit and review its new hash. No native route persistence was tested.

Prepared `debug/phone_adb/observe-native-ring.py` performs a bounded 180-second dumpsys-only observation window, outputting changes only. It records current call-key family and stable in-memory aliases (`call_1`, etc.), whitelisted state labels, route device type labels and mic flags. It refuses phone replacement/ambiguity. Synthetic privacy/identity checks passed. Its completed manual observation is recorded below. Unknown call-key syntax is emitted as `unrecognized`, not guessed as authoritative identity.

## Approved manual incoming-call observation

Miguel later requested the deferred observation. A 180-second read-only window completed with exit 0 and one unique candidate key. Initial transport was detached; the one mDNS-advertised TLS phone was reconnected. Selection accepts that exact TLS-advertised endpoint or its TLS serial, excludes `:5555`, and rejects ambiguity.

Observed sequence for one current Telecom row:

```text
empty mCalls / MODE_NORMAL
call_1 (key family TC@numeric), state RINGING
same call_1, state ACTIVE / MODE_IN_CALL
same call_1 ACTIVE, manual speaker selection
empty mCalls / MODE_NORMAL after manual ending
```

The actual key token matched `TC@` followed by digits and remained identical from ringing to active. Call row field-presence checks found `id`, `state`, `handle`; caller handle was never emitted. Literal row punctuation was not retained, so `id[:=] TC@<key>` and `state[:=] RINGING` are conservative parser candidates, **not verified exact row templates**. The next helper dry-run must capture a synthetic row shape while preserving punctuation and validate a sole real ringing row before mutation.

Audio's actual route grammar was observed as `Active communication device: ... type:speaker addr:<redacted> ...`. Before the call and after ending the type was `earpiece`. During manual speaker selection, computed/applied preferred device changed from `null` to non-null, active device type was `speaker`, call remained ACTIVE, and microphone flags `FromSwitch=false`, `FromRestrictions=false`, `FromApi=false` persisted. The observer's first whitelist expected a longer device spelling; a separate narrow read captured the actual short `type:speaker` token without device addresses/names. Its whitelist is now corrected for future windows.

Miguel explicitly confirmed that manual speakerphone audio worked both ways. This proves a usable manual native speaker route for the observed call. It does not prove that shell acceptance or speaker setters work, or that a helper-selected route persists after process exit.

At idle, the current Telecom audio-manager block contains `mCallAudioRouteAdapter`, but no `CallAudioState`, `route`, `isMuted` or current route field was observed there. Telecom supplies current-call topology/identity; the phone's AudioService supplies current active communication-device route. Do not fabricate Telecom route evidence.

## Locally prepared guarded candidate — not pushed/run

Ignored artifacts:

- `debug/phone_adb/GuardedNativeAnswerProbe.java`
- `debug/phone_adb/build-guarded-native-probe.ps1`
- `debug/phone_adb/guarded-native-probe-build/dex/classes.dex`
- SHA256: `9ADEC888A421B9C7538FF12AAF06F0AA4CBA015234D94A8328CD8F1F0111CE2B`

Local javac, synthetic self-test and D8 all succeeded. The candidate exposes `dry-run` and `accept-speaker <expected-key> --approved-live-test`; only the latter can mutate. Both require uniquely parsed actual current `id`/`state` row and refuse all unknown/non-sole row shapes. `dry-run` prints a synthetic key and replaces every nonstructural token including handle/name/Unicode payload while retaining punctuation. Its actual output still requires live validation before accepting.

Acceptance invokes only observed `acceptRingingCall(String)` after a fresh sole-RINGING-key check. It waits for the same key ACTIVE, refuses identity changes before routing, selects exactly one available type-2 communication device, invokes only observed `setCommunicationDevice(AudioDeviceInfo)`, and checks same ACTIVE identity, getter type 2, current AudioService `type:speaker` and unmuted mic. No dial, hangup, capture, playback, mute setter, legacy fallback or automatic restoration is present. It explicitly exits via `System.exit`.

Success status is deliberately `ANSWERED_AUDIO_ROUTE_OBSERVED_PENDING_EXIT_AND_HUMAN_CHECK`; it cannot claim full gate PASS. False setter/readback results leave the connected call untouched and report `ANSWERED_ROUTE_FAILED_LEAVE_CONNECTED`. Uncertain identity leads to no further mutations. Global acceptance retains a check-to-use race; matching pre/post keys do not make it atomic.

Next proposed unique remote path: `/data/local/tmp/california-native-answer-probe-9adec888.dex`. Controller must review source/hash and obtain specific approval before any push/run. During that future backup-line test: hash-check local and remote bytes; run `dry-run` first, inspect synthetic row punctuation/state; retain the originally read real call key privately; revalidate same single RINGING key; invoke the approved accept-speaker mode; independently observe the same ACTIVE key and speaker route **after helper exit**, then obtain Miguel's two-way speech check. If route disappears at process exit, gate does not pass. No call-ending operation is part of the probe.

## First approved automatic attempt — acceptance, no speaker attempt

Miguel approved "Yes, run the answer-and-speaker test" for the exact v1 hash above and one backup-line ring. Preflight found two host transport representations of the same advertised TLS service; it stopped before any push/acceptance. The duplicate IP host transport was disconnected while retaining TLS serial, then the unchanged launcher proceeded with one phone.

Actual dry-run grammar was captured and validated:

```text
[X id=TC@101, state=RINGING, ...]
```

`X` and `TC@101` are synthetic replacements. Actual observed field separators are `id=` and `state=`; nonstructural fields and handle/name values were scrubbed while punctuation was retained. Both dry-run and preaccept check established the same sole live ring.

Candidate output: `ACCEPT_REQUESTED`, then `STATE_CHANGED_NO_MORE_MUTATIONS`, explicit helper exit `3`. The helper saw a same-key state other than RINGING/ACTIVE and exited before reaching `setCommunicationDevice`. It did **not** attempt routing; this is not a speaker setter failure.

Independent post-exit reads at approximately 1s and 5s showed the original key ACTIVE, MODE_IN_CALL, active communication type `earpiece`, preferred/applied device `null`, and all observed mic flags false. Miguel confirmed "accept worked but speakerphone didnt". Thus shell acceptance connected the expected ringing call; speaker selection remains untested. Miguel ended manually; no hangup was sent.

The helper did not retain the exact transient state token. Minimum redacted Telecom event-vocabulary inspection later showed recent `REQUEST_ACCEPT -> SET_ANSWERED -> SET_ACTIVE` sequences, but attribution of the helper's first poll to a specific state was not retained. Do not claim the exact first-poll label as verified.

## Revised local candidate v2 — pending new approval

Controller ruling: after the one acceptance has been issued, a sole same-key non-ACTIVE state may permit **only bounded read-only polling** until the existing seven-second deadline. It cannot authorize routing or another acceptance. Terminal DISCONNECTED/DISCONNECTING/ABORTED, replacement, multicall or unreadable topology exit immediately. ACTIVE alone unlocks the existing fresh identity check and one speaker setter.

- Source/build paths unchanged; new preserved dex: `debug/phone_adb/guarded-native-probe-v2.dex`.
- SHA256: `1F4C9C9645EA074D30FEBDEA58740659D1099395CF044F3D8A984F3E1AFE2DF6`.
- New unique remote path: `/data/local/tmp/california-native-answer-probe-1f4c9c96.dex`.
- Launcher is pinned to this new hash/path and relays `POST_ACCEPT_STATE=<state-token>` diagnostics; it has not run with v2.
- Local compile/self-test/D8 passed. Tests exercise unknown same-key intermediate -> ACTIVE, unknown timeout, replacement, ended and unreadable topology. The wait routine contains no mutation calls; timeout/refusal cannot issue another accept or route.

V1 approval does not cover v2. No new helper push/run or second acceptance has occurred. The revised source/hash require review and a new specific live-test approval.

## Next gate and required evidence

1. Completed: approved inspection push/run at its unique path; installed signatures and idle readback recorded above.
2. Completed: approved manual backup-line ring -> answer -> speaker -> end observation. Stable TC@numeric key and short audio route type spelling observed. Literal call-row grammar still requires dry-run validation.
3. Obtain approval immediately before each acceptance/route mutation test. Confirm exactly one current INCOMING ring and no other active/held/outgoing/waiting calls, validate the original key immediately before acceptance, and verify that key is ACTIVE afterward. No global hangup is part of the probe.
4. Verify real phone built-in-speaker route and unmuted microphone through independent readback and Miguel's two-way speech check. A setter returning true is insufficient. Observe route after the helper process exits, including locked phone and a connected Bluetooth headset in separately approved tests.

Installed `ITelecomService.acceptRingingCall` and `AudioManager.setCommunicationDevice` signatures are observed, but their acceptance/routing behavior is unverified. Do not infer shell authorization, call-specific targeting, routing persistence, or success from method availability. If acceptance works but routing fails, leave the human call connected and record partial success. If identity is uncertain, issue no route mutation, capture, injection or hangup.

PASS requires observed stable ring-to-active identity, validated topology, acceptance, actual native speaker route, usable unmuted phone mic, two-way audio and route persistence after process exit. Unsupported APIs/behavior require UNSUPPORTED and halt further production work. Pending approval or an idle phone is neither PASS nor UNSUPPORTED.

## Corrected v2 live result (controller execution)

User approved corrected test, then explicitly requested retry after manually answering the first attempt. Subagent hit account usage limit before execution; controller ran reviewed unchanged v2 launcher directly. Reviewed hash remained 1F4C9C9645EA074D30FEBDEA58740659D1099395CF044F3D8A984F3E1AFE2DF6.

First v2 window: dry-run exited 1 before any acceptance/route action; user reported accidentally picking up. Retry was directly authorized by user (“try again”).

Corrected retry: dry-run PASS with actual synthetic id=TC@101/state=RINGING grammar; exactly one original call. acceptRingingCall -> POST_ACCEPT_STATE=ANSWERED -> ACTIVE. MATCHING_ACTIVE_OBSERVED. setCommunicationDevice returned true, but communication getter speaker=false and AudioService speaker=false; mic muted=false. Helper exited4 with ANSWERED_ROUTE_FAILED_LEAVE_CONNECTED. At approximately1s and5s after helper exit, original key remained ACTIVE, actual route earpiece, preferred/applied null, all mic flags false. Launcher exited0; this is observer completion, not helper success. Controller instructed manual end.

Conclusion: automatic guarded acceptance is verified; temporary ANSWERED state is now observed. The current AudioManager.setCommunicationDevice candidate does not switch this cellular call to speaker. Full native answer+speaker feasibility has NOT passed. No production tasks or enable flags may proceed on this evidence. Alternative installed setSpeakerphoneOn mechanism or a Telecom-owned route remains untested. No dial/hangup/mute/capture was performed.

## Local legacy route-only candidate — not pushed/run

Primary documentation describes `setSpeakerphoneOn(boolean)` as deprecated since API34 and intended for telephony/platform audio management. Communication-device requests prioritize the audio-mode owner. [AudioManager documentation](https://developer.android.com/reference/android/media/AudioManager#setSpeakerphoneOn(boolean))

Android16 source shows legacy setter has its own AudioService entry and constructs fixed speaker AudioDeviceAttributes; the newer setter uses a chosen device/port. Both use MODIFY_PHONE_STATE privilege classification and converge on the same communication-route client. Thus the legacy experiment can distinguish an OEM/port-path difference, but there is **no source-backed expectation it bypasses the observed routing limitation**. [AudioManager source](https://android.googlesource.com/platform/frameworks/base/+/android16-release/media/java/android/media/AudioManager.java), [AudioService source](https://android.googlesource.com/platform/frameworks/base/+/android16-release/services/core/java/com/android/server/audio/AudioService.java), [AudioDeviceBroker source](https://android.googlesource.com/platform/frameworks/base/+/android16-release/services/core/java/com/android/server/audio/AudioDeviceBroker.java)

Narrow read-only installed shell permission inspection found granted flags for MODIFY_AUDIO_SETTINGS, MODIFY_PHONE_STATE and MODIFY_AUDIO_ROUTING. Installed public `setSpeakerphoneOn(boolean)` was already reflected earlier. Grants and method availability do not prove cellular route control.

Artifacts, all ignored/local:

- `debug/phone_adb/LegacySpeakerRouteProbe.java`
- `debug/phone_adb/build-legacy-route-probe.ps1`
- `debug/phone_adb/legacy-route-probe-build/dex/classes.dex`
- SHA256 `9F629B2081E4256CC46F8D9D3EA0B3435B2F276DF8173135639EDB89E962BF99`
- `debug/phone_adb/run-legacy-route-test.py`, pinned to unique remote `/data/local/tmp/california-legacy-route-probe-9f629b20.dex`

Local compile, `LEGACY_ROUTE_SELF_TEST_PASS`, and D8 succeeded. Synthetic tests prove only same-key/sole ACTIVE reaches the setter once; RINGING, replacement, DISCONNECTED, unknown state, multiple rows and unknown topology make zero setter calls. Launcher syntax check passed; launcher has not run.

Proposed isolated procedure, requiring **new explicit approval**: push reviewed fixed dex/hash to unique path; launcher waits for backup-line ring and privately retains key; Miguel answers manually; only the same sole ACTIVE key allows one `setSpeakerphoneOn(true)`. Readback checks speaker flag, getter type2, actual AudioService speaker, unmuted mic and matching ACTIVE identity. Helper explicitly exits. Independent host observations at1s/5s check persistence before human two-way audio check and manual ending. No automatic acceptance, dial, hangup, mic setter, capture, fallback setter or restoration is part of this route-only invocation.

The dex includes previously reviewed shared parser/context classes, including the older acceptance main; the fixed launcher invokes **only LegacySpeakerRouteProbe route-active**, which has no acceptance call. No production change and no new phone mutation has occurred. Original subagent waiting window ended with NO_RING_WINDOW_FINISHED; the reported v2 acceptance/routing result came from the controller's separately authorized direct execution.

## Approved legacy route-only live result

Miguel approved "Yes—run the route-only test" for the reviewed exact hash `9F629B2081E4256CC46F8D9D3EA0B3435B2F276DF8173135639EDB89E962BF99`. Hash and one TLS phone were reverified; unique-path push/chmod/hash-checked launch performed. The launcher captured one backup-line RINGING key, then Miguel answered manually. Only the same sole ACTIVE key reached one `setSpeakerphoneOn(true)` invocation. No automatic acceptance occurred in this test.

Actual helper output:

```text
LEGACY_SPEAKER_REQUESTED
LEGACY_SPEAKER_FLAG=false
COMMUNICATION_TYPE_SPEAKER=false
ACTIVE_AUDIO_ROUTE_SPEAKER=false
MIC_MUTED=false
ROUTE_FAILED_LEAVE_CONNECTED
helper_exit=4
```

The void setter returned without throwing; that is not success. Independent observations after explicit process exit at1s/5s retained the original ACTIVE key, MODE_IN_CALL, active route `earpiece`, computed/applied preferred device `null`, mic FromSwitch/Restrictions/Api false. Launcher exited0 after finishing observation; helper exited4 because route readback failed. Call left connected for Miguel to end manually. No retry, fallback setter, route restoration, mic mutation, capture, dial or hangup occurred.

The observed same-key topology, readback and manual speaker success distinguish native audio availability from shell route-control failure. This evidence supports UNSUPPORTED for these two public shell AudioManager mechanisms, not a claim that every possible Telecom-owned application mechanism is impossible. Locked-phone/Bluetooth/permanent helper-route checks were not pursued after both setters failed the basic route gate. No production tasks or flags proceed. The plan's next choice is a separately designed InCallService/default-dialer application or explicitly approved narrower answer-only scope; no such app was installed and no role/permission changed.

## Speaker by tapping the dialer's own button: PASS (2026-10-14 15:37-15:39)

The setters fail because Android gives the speaker route to the app that owns call
audio, which is the default dialer: Google Phone (`com.google.android.dialer`, the
holder of `android.app.role.DIALER`). Pressing its own Speaker button over adb asks
that app to do it.

Miguel approved the test. Backup line rang the main phone, Miguel answered by hand
(acceptance is already proven above), phone locked throughout. Script:
`debug/phone_adb/speaker_tap.py` (ignored); every adb command is logged to
`debug/phone_adb/speaker-tap-commands.log` with exit code and duration only.

Commands, in order:

```text
adb devices                                   # one TLS phone, never :5555
adb -s <phone> shell dumpsys telecom          # mCalls section only: id=TC@<n>, state=ACTIVE
adb -s <phone> exec-out uiautomator dump /dev/tty   # in-call screen, readable over the keyguard
adb -s <phone> shell dumpsys telecom          # same TC@ key, still the sole ACTIVE row
adb -s <phone> shell input tap <x> <y>        # centre of the Speaker button's bounds
adb -s <phone> shell dumpsys audio            # Active communication device ... type:speaker
```

What the screen looks like: focus `com.google.android.dialer/com.android.dialer.incall.activity.ui.InCallActivity`
with `isKeyguardShowing=true`. The word "Speaker" is a non-clickable label inside a
**clickable `android.view.View` with no resource-id**; the tap target is that nearest
clickable ancestor, its centre computed from `bounds` on every read (775,2121 here,
never hardcoded). Its `checked` attribute is the on/off state; the same button turns
the speaker off again, so a tap is only sent when `checked` disagrees with the wanted
state.

Result:

| | before | +1 s | +5 s |
|---|---|---|---|
| active communication device | earpiece | speaker | speaker |
| mic mute FromSwitch | false | false | false |
| call | sole ACTIVE | same key ACTIVE | same key ACTIVE |
| button `checked` | false | true | true |

Miguel: "worked very well" (two-way audio on speaker). Timings: screen read ~2.5 s,
tap 0.14 s, telecom/audio reads 1-2 s. Miguel ended the call by hand; no hang-up sent.

Not yet observed: helper acceptance followed directly by the tap (each half proven
separately), a connected Bluetooth headset (the button may become an audio-route
menu), and a non-English dialer locale (the label matcher also accepts Portuguese
"Altifalante"/"Alta-voz"). Any of these failing gives the partial-success line and
leaves the call connected.
