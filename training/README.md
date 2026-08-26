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

The real run on an L40S.

```bash
uvx modal run training/modal_train.py::train
```

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

## Tuning

The plan if it false-triggers: play Hotel California and Californication at the TV at
normal volume and count activations. If it fires, add what fired to
`custom_negative_phrases` in `california.yaml` and retrain — that is what the loop is
for, and why sample counts here sit in livekit's "quick experiments" band rather than
prod's. Once a config proves itself, scale `n_samples` to 25000 and `steps` to 100000.
