# callaudio: California's helper on the phone

A tiny dex that runs on Master Miguel's Xiaomi as the ADB **shell** user,
started from the laptop:

```text
adb shell 'CLASSPATH=/data/local/tmp/callaudio.dex app_process / CallAudio <mode> ...'
```

The shell holds permissions no normal app gets (CALL_PHONE,
CALL_AUDIO_INTERCEPTION, CAPTURE_AUDIO_OUTPUT, MODIFY_AUDIO_SETTINGS...), but
the APIs that use them are `@SystemApi`, so there is no shell command for them.
Everything below the small `FakeContext` is reflection; the `stubs/` exist only
so `javac` compiles and are left out of the dex.

## Modes

| Mode | Prints | What it does |
|---|---|---|
| `check` | `INTERCEPTABLE=`, `AUDIO_MODE=`, `TELEPHONY_TX=` | Can call audio be intercepted, and is the Telephony Tx output visible |
| `dial <digits>` | `DIALED` or `ERROR ...` (exit 1) | `ITelecomService.placeCall` |
| `hangup` | `ENDED=true\|false` or `ERROR ...` | `ITelecomService.endCall` |
| `ring <seconds>` | `RINGING` or `ERROR ...` | Find my phone: 880/660 Hz beeps on the alarm stream at max volume, previous alarm volume restored in a `finally` (stderr: `alarm volume A -> max -> restored A`) |
| `bridge <port> 16000 24000 [mutemic]` | `LISTENING <port>` | One TCP client on 127.0.0.1:port (reach it with `adb forward`): the other side of the call goes out as 16 kHz mono PCM16, 24 kHz mono PCM16 coming in is upsampled to 48 kHz stereo and injected into the call. `mutemic` mutes the phone's own mic for the call and restores it after |

## Build

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File android/callaudio/build.ps1
```

Needs a JDK (`javac`; `build.ps1` also looks in `~/tools/jdk-*` for the portable
Microsoft build) and Google's `r8.jar` (`~/tools/r8/r8.jar`, from
`dl.google.com/android/maven2/com/android/tools/r8/`). The built
`callaudio.dex` is committed so a fresh clone does not need either. The laptop
pushes it read-only and checks its sha256 in the same shell command that runs
it, so a swapped file is never executed.

## What the phone needed (measured 2026-10-13)

- **HyperOS "USB debugging (Security settings)" on.** Without it Xiaomi's
  `PKMSImpl` denies the shell CALL_PHONE and INJECT_EVENTS system-wide, even
  though stock Android grants them.
- **Telecom, not intents.** `am start -a android.intent.action.CALL` and
  `input keyevent` are the hooked paths; the binder calls above are not.
- **Pin the injection track to Telephony Tx, in 48 kHz stereo.** Without
  `setPreferredDevice(TYPE_TELEPHONY)` MediaTek's audio policy opened the track
  on TELEPHONY_TX, resolved its start to the earpiece and invalidated it
  ("Device doesn't sync between startOutput[0x1] and getOutputForAttr"), 51
  times in 3 s: nothing ever played. `incall_music_uplink` only takes 44.1/48 kHz
  stereo.
- **Android 16's AudioTrack.Builder needs a Context** (it reads the attribution
  tag), hence `setContext(new FakeContext())` in `ring`.
