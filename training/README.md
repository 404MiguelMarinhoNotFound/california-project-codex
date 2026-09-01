# Training the California wake word

The assistant currently answers to `hey_jarvis_v0.1`. That is a placeholder, not a
choice — Picovoice disabled all Free Tier AccessKeys on 2026-06-30, which killed the
custom Porcupine `California_*.ppn` models, and openWakeWord ships no "California".

**No pretrained California wake word exists anywhere.** LiveKit ships exactly two
models (`hey_livekit`, `nihao_livekit`); openWakeWord ships `hey_jarvis`, `alexa`,
`hey_mycroft`. The word only ever comes from training one.

This directory trains one with [livekit-wakeword](https://github.com/livekit/livekit-wakeword)
(Apache-2.0) on Modal.

## Why livekit-wakeword and not openWakeWord

Same audio front-end — mel spectrogram into the frozen Google speech embedding model,
producing a `(16, 96)` feature matrix. The difference is the classifier on top.
openWakeWord flattens it into 1536 values and runs a dense net, which has no
inductive bias for temporal order. livekit's `conv_attention` head runs 1D convs plus
multi-head self-attention across the 16 timesteps, so it sees the *order* of phonemes.

On livekit's own "hey livekit" validation set (15k positives, 45k negatives, 25 hours):

| | openWakeWord DNN | livekit conv-attention |
|---|---:|---:|
| False positives/hour | 8.50 | **0.08** |
| Recall | 68.6% | **86.1%** |
| Optimal threshold | 0.01 | **0.68** |

That threshold row is the one that matters. openWakeWord's optimum collapsing to 0.01
means no operating point met the false-positive target at all. For a wake word sitting
next to a TV that plays Hotel California, that is the whole problem.

There is a tempting shortcut: `--format tflite` exports an openWakeWord-compatible
model that would need **zero** code changes in `core/wake_word.py`. Don't take it. That
path supports the `dnn` head only, and the dnn head is the one that fails above.

## Cost

Modal Starter is $30/month in credits, reset monthly, no rollover. Note the fine print:
**$1 lands at signup and the remaining $29 requires adding a payment method** (plus a
one-time $0.50 verification charge). It is free to run, but not card-free.

At Modal's rates that budget is roughly:

| GPU | $/hr | hours on $30 |
|---|---:|---:|
| T4 | 0.59 | ~50 |
| L4 | 0.80 | ~37 |
| A10 | 1.10 | ~27 |
| L40S | 1.95 | ~15 |

The volume also costs a little to keep parked — it holds ~16GB, billed on daily
snapshots. Delete it with `uvx modal volume delete california-wakeword-data` when the
model is trained and you are done retraining.

Neither livekit's README nor `docs/training.md` documents wall-clock training time or
VRAM anywhere. That is why the smoke test below exists.

## Steps

```bash
uv tool install modal
```

```bash
uvx modal setup
```

One-time volume population. Downloads the Piper VITS checkpoint (~166MB), the
ACAV100M feature file (~16GB, ~2000 hours of negative embeddings), room impulse
responses, and the MUSAN noise subset. CPU-only, so it costs almost nothing.

```bash
uvx modal run training/modal_train.py::setup
```

Smoke test on a T4. A few hundred clips, a tiny DNN, 500 steps. The model it produces
is useless — the point is to prove espeak-ng phonemizes "california", the checkpoint
loads, the volume mounts, and export writes a file, before spending real GPU hours.
**Time this run.** It is the only way to get the wall-clock number the docs omit.

```bash
uvx modal run training/modal_train.py::smoke
```

The real run on an L40S. Use `--detach` — without it the app is tied to your
terminal session, so closing the window kills a multi-hour run.

```bash
uvx modal run --detach training/modal_train.py::train
```

`train` defaults to `california_v2.yaml`. Pass `--config california.yaml` to
re-run an older one. `model_name` is read out of the YAML, so runs land in
separate directories on the volume and can be compared rather than overwriting
each other.

Pull the model down:

```bash
uvx modal volume get california-wakeword-data /artifacts/california/california.onnx models/
```

## What comes back

In `/artifacts/california/` on the volume:

| File | What it is |
|---|---|
| `california.onnx` | the classifier — this is what ships |
| `california.pt` | PyTorch checkpoint, for re-exporting or quantizing later |
| `california_metrics.json` | **contains `optimal_threshold`** — use it, do not keep 0.6 |
| `california_eval.json` | AUT, FPPH, recall |
| `california_det.png` | DET curve |

## After training

**One change, and it is two lines of `config.yaml`.** No code change.

```yaml
wake_word:
  model: "models/california.onnx"
  threshold: <optimal_threshold from california_metrics.json>
```

This works because livekit's exported ONNX has the same I/O contract as an
openWakeWord custom model:

```
inputs : [('embeddings', ['batch', 16, 96], 'tensor(float)')]
outputs: [('score', ['batch', 1], 'tensor(float)')]
```

and `openwakeword/model.py` runs it as
`onnx_model.run(None, {onnx_model.get_inputs()[0].name: x})` — it reads the input name
off the model rather than hardcoding one, and only checks `get_inputs()[0].shape[1] == 16`
and `get_outputs()[0].shape[1] == 1`. Both hold. Verified end to end against a real
exported model on onnxruntime 1.29: it loads, keys off the filename stem, and scores.

Worth recording because the obvious reading of livekit's docs says otherwise. Their
inference API (`WakeWordModel.predict`) is **stateless** — "the caller manages the audio
window" — and wants a full 32,000-sample window per call, which would need a ring buffer
and a stride on top of this project's 40ms chunk feed. That is only true if you use
*livekit's* runtime. Going through openWakeWord's instead, its rolling embedding buffer
over the last 16 embeddings already is that 2-second window, so `_process_oww`,
`_check_debounce`, and the consecutive-frame counter all work unchanged.

Both runtimes drive the same frozen Google speech embedding front-end — livekit's package
just bundles its own copy of `melspectrogram.onnx` and `embedding_model.onnx`. So there is
no accuracy argument for adding `livekit-wakeword` as a runtime dependency here. Don't.

One caveat: this was verified with the smoke model, which uses the `dnn` head. The real
model uses `conv_attention`. livekit documents all three heads as sharing one ONNX
contract, and their own eval stage runs the exported file through onnxruntime, so this is
expected to hold — but confirm it loads before assuming.

## When the model is not good enough

Run 1 (`california.yaml`) scored 92.4% recall at 0.096 FPPH and still both missed
Master Miguel and fired on non-wake-words in the actual room. That is not a
contradiction — those numbers were measured against synthetic Piper audio, where
904 LibriTTS speakers read calmly into a clean channel. A European Portuguese
speaker saying one clipped word across a room is out of that distribution.

**Measure before spending money.** A retrain is roughly 4-5 hours and $9, so it is
worth ten minutes first:

```bash
uv run python tools/score_wakeword.py
```

Say the wake word ten times, normally, from where you actually stand. The tool
prints the raw score rather than a yes/no, and that distinction decides everything:

| Peak score | Meaning | Fix |
|---|---|---|
| above threshold | fires | nothing |
| 0.4 - 0.6 | heard, threshold too high | lower `wake_word.threshold`, stop |
| under 0.1 | genuinely not recognised | retrain, no threshold helps |

For false fires, leave it running with the TV on:

```bash
uv run python tools/score_wakeword.py --save-clips debug/wakeword
```

Every event writes the two seconds of audio that caused it. Listening to those is
how you learn what belongs in `custom_negative_phrases`. Adding guesses instead is
how you spend $9 on a model that fails the same way.

### What run 2 changes, and why

`california_v2.yaml` targets separation rather than either failure mode alone,
because when a model both misses and false-fires, the classes are not far enough
apart and pushing on one side only moves the problem:

- **`model_size: large`** (256d, 3 blocks, up from medium). Inference cost is
  irrelevant here — this runs once per 2-second window on ~1500 floats, not per
  audio frame
- **`n_samples: 25000`** and **`steps: 100000`**, up from 10000/50000. Run 1 sat in
  livekit's own "quick experiments" band
- **wider TTS spread** — this is the recall lever, and the most overlooked one. Run
  1 used a single `noise_scales` and `noise_scale_ws` value, so all 10,000 positives
  shared one timbre and one phoneme-duration setting, and `length_scales` only
  spanned 0.75-1.25. Run 2 spreads all three and widens `slerp_weights` at both
  ends. A wake word said fast, flat, or half-swallowed only counts if the model has
  seen it said that way
- **`max_negative_weight` deliberately unchanged at 3000.** Raising it is the
  obvious reflex for false fires and the wrong one here: the trainer already doubles
  it between phases when FPPH misses target, and pushing higher buys quiet by giving
  up the recall that is already the louder complaint

### The accent problem, and the fix

Reported symptom: said with an English accent it fires, said with a Portuguese
accent it does not. That is not a tuning problem, it is a data problem, and the
cause is fixed in the toolchain:

- the Piper checkpoint is **`en-us-libritts-high`**
- `synthesis.py` reads its espeak voice from that checkpoint's own JSON
  (`config["espeak"]["voice"]`), so phonemization is **`en-us`** and is not
  settable from the wake-word YAML

So every synthetic positive is American English. The model has never heard
*/ka.li.ˈfɔɾ.ni.ɐ/*, only */ˌkæ.lɪ.ˈfɔɹ.njə/*. Raising `model_size` would only
teach it the English pronunciation more thoroughly.

The fix is real audio. Record yourself, and the pipeline folds it in as extra
positives:

```bash
uv run python tools/record_wakeword.py --count 140
```

About ten minutes. The prompts cycle through delivery styles deliberately —
a model trained only on careful pronunciations learns to require one. Clips are
trimmed tightly at both ends, which matters because livekit's `align_clip_to_end`
places positives at the END of the 2s window with 200ms jitter, so trailing
silence would shift the word out of position relative to the Piper clips.

**Hold some back.** Move roughly 20 takes into a separate directory and do not
upload them. They are the only honest measure of whether this worked, because
livekit's eval scores against synthetic Piper audio and therefore cannot tell you
anything about your own voice.

```bash
uvx modal volume put california-wakeword-data training/recordings/positive /recordings/positive
```

```bash
uvx modal run --detach training/modal_train.py::train
```

Then score the held-out takes against the old model and the new one:

```bash
uv run python tools/score_wakeword.py --dir training/recordings/holdout
```

### How the merge works

`train` runs the stages separately when `--replicate` is above 0 (default 25),
because `livekit-wakeword run` does everything in one go and leaves no seam:

```
generate -> merge recordings -> augment -> train -> export -> eval
```

Two details are load-bearing, and both fail silently if got wrong:

- **Naming.** `augment.py` selects sources with a strict `^clip_\d{6}\.wav$`
  regex. Anything else is ignored, so wrongly-named clips would sit in the
  directory, never train, and the run would look completely healthy. The merge
  renames into that pattern, continuing past the highest index Piper wrote.
- **Replication.** 120 real clips against 25,000 synthetic is under half a
  percent, which the model would barely notice. Each take is copied 25 times.
  The copies are identical on disk, but augmentation applies independently random
  reverb, noise, and EQ per clip per round, so each becomes a distinct example.
  120 takes at x25 lands around 10% real.

Pass `--replicate 0` to train on synthetic audio only. Uploading no recordings is
not an error either; the merge simply no-ops and says so.

### If that is still not enough

`augmentation.background_paths` accepts extra directories, so recordings of the
actual living room with the TV playing can go in as negatives — the same trick,
applied to the false-positive side.
