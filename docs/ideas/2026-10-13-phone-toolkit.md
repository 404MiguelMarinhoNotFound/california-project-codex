# Phone toolkit: ideas for California over ADB (parked)

Parked 2026-10-13. Master Miguel liked all of these and asked to park them
until after `docs/superpowers/plans/2026-10-13-phone-over-adb.md` ships: that
plan builds the transport (`AdbPhone`, the `callaudio` helper, the
hash-checked launch) every idea below would reuse.

## What the phone's shell can do (verified 2026-10-13)

Xiaomi, HyperOS 3, Android 16, with Wireless debugging paired to this laptop
only and "USB debugging (Security settings)" on (`persist.security.adbinput=1`).
`dumpsys package com.android.shell` shows these granted, and the reads were
probed live:

| Capability | Permission / mechanism | Probe |
|---|---|---|
| SMS read / receive / send | `READ_SMS`, `RECEIVE_SMS`, `SEND_SMS` | `content query --uri content://sms/inbox` returned rows |
| Contacts read / write | `READ_CONTACTS`, `WRITE_CONTACTS` | `content://com.android.contacts/contacts` returned rows |
| Calendar read | `READ_CALENDAR` | `content://com.android.calendar/events` returned rows |
| Call log | `READ_CALL_LOG` | `content://call_log/calls` returned rows |
| Notifications, every app | `dumpsys notification --noredact` | 20 active records |
| Battery | `dumpsys battery` | level 16, not charging, at the time |
| Location | `ACCESS_FINE_LOCATION`, `ACCESS_BACKGROUND_LOCATION` | not probed yet |
| Ringer / DND | `ACCESS_NOTIFICATION_POLICY`, `MODIFY_AUDIO_SETTINGS` | not probed yet |
| Alarms | `SET_ALARM` | not probed yet |
| Screen / camera | `READ_FRAME_BUFFER`, `CAMERA` | not probed (see "skip for now") |
| Calls, call audio | `CALL_PHONE`, `ANSWER_PHONE_CALLS`, `CALL_AUDIO_INTERCEPTION` | used live in the Marta calls |

## Ideas

### California tells him things

1. **"What did I miss on my phone?"** A digest of active notifications from
   every app: WhatsApp, Gmail, banks, Uber. Source: `dumpsys notification
   --noredact`, parse package, title, text and post time per record. Same rule
   as `whatsapp_unread`: senders and apps first, text only when he asks for one,
   because she is speaking in a room. Labelled as other people's words, never
   instructions (the prompt-injection rule WhatsApp already follows).
2. **Missed calls.** "Who called me?" from `content://call_log/calls`
   (`type=3` missed), names from the phone's own contacts.
3. **SMS.** Read the latest texts and pull a verification code ("what code did
   my bank send?"). Sending SMS reuses the WhatsApp / phone read-back token:
   never sent without his "go" on the exact recipient and text. Reading a code
   aloud is sensitive: only on an explicit request, never in a digest.
4. **Calendar.** "What's on today?", "am I free Friday night?" from
   `content://com.android.calendar/instances/when/<start>/<end>`, so recurring
   events come out expanded. Natural fit for a morning briefing later.
5. **Battery.** "Your phone's at 16% and not charging." `dumpsys battery`.
   Optional proactive nudge below a threshold, at most once per discharge,
   spoken from the idle loop like the phone-call report.

### California acts on the phone

6. **Incoming calls.** Poll `mPreciseCallState` for ringing (5) plus the number
   from the call log / telephony registry; she announces "Mama's calling" in
   the room. Next step, separately: "answer it" with `ANSWER_PHONE_CALLS`,
   then either a screening agent (Gemini takes a message, same bridge as
   outgoing calls) or a speakerphone bridge to the laptop's speaker and mic.
   Must not fight the outgoing-call path for the one `callaudio` bridge.
7. **Phone settings by voice.** Ringer mode (`cmd audio set-ringer-mode`),
   Do Not Disturb (`cmd notification set_dnd on|off`), "set an alarm on my
   phone for 7" (`am start -a android.intent.action.SET_ALARM` with
   `android.intent.extra.alarm.HOUR` / `MINUTES` / `SKIP_UI`), pause / resume
   whatever plays on the phone (`cmd media_session dispatch pause|play`).
8. **Send to phone.** "Send me directions to the restaurant" opens Google Maps
   navigation (`am start -a android.intent.action.VIEW -d
   "google.navigation:q=<address>"`), or any URL. The phone may need to be
   unlocked for the app to come up.

### Better versions of what exists

9. **Live contacts.** Read the phone's contacts directly instead of the
   hand-exported `contacts.vcf`. `WhatsAppService` and `PhoneService` would
   both resolve against the live book, with the VCF as the offline fallback.
   The quoted-printable / emoji-name problems of the VCF disappear because the
   provider returns plain UTF-8.
10. **Where the phone is.** Last known location from `dumpsys location` (or a
    helper mode asking `LocationManager`), so "where's my phone?" can say
    "not at home, last seen near X" before ringing it. It also gives presence:
    "is Miguel home?" for automations such as lights on arrival.

### Skip for now

- **Phone screen to Claude vision** ("what's on my phone?" via `screencap`):
  powerful, but it sees everything, including banking apps and 2FA screens.
- **Camera.** No voice use case worth the exposure.

## Constraints whoever picks this up must keep

- **Token cost.** Every tool schema is re-sent on every turn: add compact
  actions to `control_phone`, not new tools, and keep descriptions tight.
- **Privacy in a room.** Anything that reads other people's words (SMS,
  notifications) says who first and reads text only on request, and marks it
  as data, not instructions.
- **Writes are read back.** SMS sends (and anything else that reaches another
  person) use the server-side confirmation token, like WhatsApp and calls.
- **Security.** Every helper launch keeps the sha256-checked
  `exec app_process` from the phone-over-ADB plan; the laptop's
  `~/.android/adbkey` stays readable only by Master Miguel, SYSTEM and
  Administrators (fixed 2026-10-13). Whoever holds that key holds all of the
  above.
- **Windows only.**
