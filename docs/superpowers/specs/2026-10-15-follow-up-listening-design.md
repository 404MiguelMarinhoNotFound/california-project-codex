# Follow-up listening: answer her question without the wake word

Date: 2026-10-15. Status: design, approved in conversation, not built.

## Problem

When California asks Master Miguel something ("Send Marta 'I'm late'?", "Costa or
Silva?", "Want the first source?", "Want me to find something similar?"), the turn
ends and she goes back to the wake word. He has to say "California, yes" to answer
her own question. That is a bad UX buffer.

The wake word is also a guardrail he wants to keep: she must not answer the TV,
other people in the room, or him talking to someone else.

## Goal and non-goals

- After a reply that **asks him something**, the mic stays open for a short window
  and his answer is taken without the wake word.
- After a plain statement ("Done, the lights are off") nothing changes: back to
  the wake word.
- Speech in the window that is not an answer to her (TV, other people) is
  recognised by the LLM and dropped silently.
- Not in scope: an always-on follow-up after every reply, an audio cue for the
  window (no cue for now; revisit if it feels ambiguous), speaker identification.

## When the window opens

At the end of a turn, `follow_up` is true when **either**:

1. **A tool asked for an answer in this turn.** `_TOOL_AWAITING` is reset after
   each tool call, so a turn-level flag (`self._turn_awaiting`) is set whenever a
   tool reports `awaiting`. Covers WhatsApp/phone read-backs, "which one?",
   Stremio's source question and the volume cap.
2. **The last spoken chunk ends in `?`.** The sentence chunker keeps terminal
   punctuation, so this is a check on the final chunk only. A `?` mid-reply
   followed by a statement does not count.

It never opens when:

- the reply was cut off by a barge-in (that chains as today),
- the turn ended in an error, or the spoken text was the error line,
- the turn was a phone-report turn (`_report_turn`) or an incoming-call announcement,
- `conversation.follow_up_enabled` is false.

## Flow

`_handle_activation` returns a small result instead of a bool: one of `barged_in`,
`follow_up` or neither. `_idle_loop`'s existing chain loop handles both on the same
open speaker session:

```text
reply ends -> follow_up? -> drain mic -> _handle_follow_up(mic_stream)
                                          record (window = follow_up_timeout_s)
                                          -> no_speech: back to idle (see "Going quiet")
                                          -> STT -> LLM (follow-up marker) -> reply
                                          -> follow_up again? (up to the cap)
```

`_handle_follow_up` is `_run_activation` without the acknowledgement clip and
without the `_wake_count` bump. It uses its own `TurnTimer` (`chained: true`,
`follow_up: true`), the same STT path, the same Whisper hallucination filter,
the same command check and the same `_stream_response` (with its wake-word reply
listener, so barge-in still works inside a follow-up).

**The window** is the existing pre-speech clock (`vad.speech_timeout` in
`should_stop_recording`), overridden for follow-up recordings by
`conversation.follow_up_timeout_s`, default **3.0s**. That is shorter than the
wake-word path's 4.0s on purpose: it only measures how long until he *starts*
talking (the silence timer takes over once he does, so a long answer is never
cut), people answer a question in well under a second, and a shorter window gives
the TV less room. The clock starts when the mic is drained after her last clip
finished playing, not when the LLM finished. Silence means `no_speech`: no
Whisper call, no LLM call.

Saying "California, ..." inside the window is just speech and is handled as a
normal answer.

## "That wasn't for me": the `[ignore]` sentinel

- The user message for a follow-up turn is prefixed, in the message rather than
  the system prompt so the system prompt stays unchanged:
  `[Heard without the wake word, right after your question. If this is not him
  answering you, reply with exactly [ignore].]` followed by the transcript.
- In `_stream_response`, for follow-up turns only, a token gate buffers the
  start of the stream until it can tell whether the reply begins with `[ignore]`
  (whitespace-trimmed, case-insensitive). Anything that cannot be the sentinel
  is released to the chunker at once, so a real answer has no audible delay.
- A reply that **starts** with `[ignore]` is ignored even if the model keeps
  writing after it: the stream is closed, nothing is spoken,
  `LLMService.drop_last_exchange()` removes that exchange from history (by the
  existing exchange boundaries `_trim_history` uses), the turn is logged with
  `outcome: "ignored"`, and the conversation ends (back to the wake word, no
  spoken line).
- Tools: a follow-up that is ignored runs no tool. If the model calls a tool
  instead of answering `[ignore]`, that is a normal turn.

The confirmation tokens are the real safety net and are unchanged: a WhatsApp send
or a phone call needs a pending read-back of the same recipient and message, so a
misheard "yes" from the TV can only confirm what she just read back, never
something else.

## Chain cap and going quiet

- `conversation.max_follow_ups: 10` consecutive follow-ups per conversation (the
  count resets at every wake word).
- She says a short fixed line that she has stopped listening, through TTS with no
  LLM call and nothing added to history, in exactly two cases:
  1. **The cap is reached**: the 10th follow-up's reply would open another window.
  2. **A window times out in a chain**: no reply in a window that follows a
     follow-up turn (chain depth >= 1).
- She says **nothing** when the first window after a wake-word turn times out
  (he ignored one question), and nothing after an `[ignore]`.
- Two fixed lines, one per case, **pre-recorded** (not synthesized at runtime):
  - `timeout`: "I'll take that as a no. Say my name if you need me."
  - `cap`: "Okay, I'll stop listening now. Say California if you need me."
- The text lives in a new generator, `generate_stop_lines.py`, a sibling of
  `generate_activation_phrases.py` and `generate_bootup_sounds.py`. It
  synthesizes with the `tts` block in config.yaml (Aoede today), applies the same
  `trim_silence`, and writes `sounds/stop_lines/<voice_slug>/{timeout,cap}.wav`
  plus a `manifest.json` with the text. Like every other generated clip, the WAVs
  are not committed; run it on a fresh clone and after a voice change.
- `sounds.stop_lines_dir` in config.yaml points at the folder, next to
  `activation_dir` and `bootup_dir`. The orchestrator loads both clips once at
  startup and plays them on the open speaker session (`kind: "stop_listening"`,
  with the manifest text for the live UI), then drains the mic.
- A missing clip is logged once at startup and plays nothing at runtime; the
  conversation still ends normally. Never a live synthesis fallback.

## Config

New top-level block in `config.yaml`:

```yaml
conversation:
  follow_up_enabled: true
  follow_up_timeout_s: 3.0
  max_follow_ups: 10
```

And under `sounds:`, next to `bootup_dir`:

```yaml
  stop_lines_dir: "sounds/stop_lines/google_en-US-Chirp3-HD-Aoede"
```

`tests/config_fixture.py` needs no override. `follow_up_enabled: false` must
reproduce today's behaviour exactly.

## Logging and live UI

- `turns.jsonl`: follow-up turns carry `chained: true, follow_up: true` and a
  `follow_up_index`. New outcome `ignored`; an unanswered window is the existing
  `no_speech`. A spoken stop line is recorded as `stopped_listening: "cap" |
  "timeout"` on that turn.
- The live page shows state `listening` during the window through the existing
  `_set_state`; no new event type is needed. No audio cue.
- `include_transcripts: false` still governs transcripts of follow-up turns.

## Error handling

- Any exception in a follow-up turn ends the conversation (back to idle), logged
  like any turn error, with no stop line.
- The token gate never raises into the reply: if it fails, the reply is spoken
  as-is.
- `drop_last_exchange()` on a history that does not end with the exchange is a
  no-op, logged.

## Testing

New `tests/test_follow_up.py`, with the orchestrator built the way the existing
barge-in tests do (no real mic, STT, LLM or TTS):

- a reply ending in `?` opens a window; a statement does not; an awaiting tool
  opens one without a `?`; a `?` mid-reply followed by a statement does not
- barge-in still chains as before and wins over follow-up; error and phone-report
  turns never open a window
- silence in the window: no STT, no LLM; first window silent, chained window speaks
  the stop line once
- the follow-up recording uses `follow_up_timeout_s`, the wake-word path keeps
  `vad.speech_timeout`
- `[ignore]` (with leading whitespace, different case, trailing text): nothing
  spoken, history unchanged, outcome `ignored`, no stop line
- a reply that merely contains "ignore" later is spoken normally; a real answer's
  first audio is not delayed beyond the gate
- the follow-up message carries the marker; the system prompt is unchanged
- the cap: the 10th follow-up's question speaks the stop line and opens no window;
  the count resets after a wake word
- no acknowledgement clip and no `_wake_count` change in a follow-up
- `follow_up_enabled: false` reproduces today's flow
- `LLMService.drop_last_exchange()` removes exactly the last exchange, including
  tool rounds
