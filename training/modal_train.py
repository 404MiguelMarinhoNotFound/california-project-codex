"""Train the California wake word on Modal.

Why Modal: livekit-wakeword needs apt packages (espeak-ng, sox, libsndfile1) and a
GPU, neither of which exists on Windows. Modal's Starter plan gives $30/month of
credit, which is roughly 15 L40S-hours or 50 T4-hours.

Layout, and the reason for it:

    /data          Modal Volume. The ~16GB ACAV100M feature file, RIRs, MUSAN
                   backgrounds, and the Piper checkpoint. Downloaded once by
                   `setup`, reused by every later run.
    /root/output   Container-local scratch. Generation writes ~30k individual
                   .wav files here. Modal Volume v1 degrades past 50,000 inodes,
                   so this deliberately never touches the volume — only the
                   handful of final artifacts get copied back.

Usage (uv-only, per AGENTS.md — modal is a CLI tool, not a project dependency):

    uv tool install modal
    uvx modal setup                                     # one-time browser auth

    uvx modal run training/modal_train.py::setup        # ~16GB download, run once
    uvx modal run training/modal_train.py::smoke        # cheap end-to-end check
    uvx modal run training/modal_train.py::train        # the real run

    uvx modal volume get california-wakeword-data \
        /artifacts/california/california.onnx models/

Then point config.yaml at models/california.onnx and set wake_word.threshold to the
optimal_threshold reported in california_metrics.json — not the current 0.6.
"""

from pathlib import Path

import modal

APP_NAME = "california-wakeword"
VOLUME_NAME = "california-wakeword-data"

DATA_DIR = "/data"
OUTPUT_DIR = "/root/output"
CONFIG_PATH = "/root/config.yaml"

# Real recordings uploaded with `modal volume put`, folded in as extra positives.
RECORDINGS_DIR = "/data/recordings"

# Artifacts worth keeping, mirroring livekit's own skypilot/train.yaml.
ARTIFACT_SUFFIXES = (".pt", ".onnx", "_metrics.json", "_det.png", "_eval.json")

app = modal.App(APP_NAME)

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    # espeak-ng does the phonemization; the rest are livekit's documented system deps.
    # portaudio is skipped on purpose — that is only for the microphone listener.
    .apt_install("espeak-ng", "libsndfile1", "ffmpeg", "sox")
    .pip_install("livekit-wakeword[train,eval,export]")
)


def _read_config(name: str) -> str:
    """Read a config next to this file. Sent as text so no local-file mount is needed."""
    return (Path(__file__).parent / name).read_text(encoding="utf-8")


def _model_name(config_text: str) -> str:
    """Pull model_name out of the YAML without importing yaml.

    This runs locally under `uvx modal`, whose isolated environment has modal and
    nothing else, so a regex is the portable option.
    """
    import re

    match = re.search(r"^model_name:\s*[\"']?([\w.-]+)", config_text, re.M)
    if not match:
        raise SystemExit("config has no model_name")
    return match.group(1)


def _write_config(config_text: str) -> str:
    Path(CONFIG_PATH).write_text(config_text, encoding="utf-8")
    return CONFIG_PATH


def _sh(*args: str) -> None:
    """Run a command, streaming output, and fail loudly."""
    import subprocess

    print(f"$ {' '.join(args)}", flush=True)
    subprocess.run(args, check=True)


@app.function(
    image=image,
    volumes={DATA_DIR: volume},
    cpu=8,
    memory=32768,
    timeout=4 * 60 * 60,
)
def _setup_remote(config_text: str) -> None:
    """Download the Piper checkpoint, ACAV100M features, RIRs, and MUSAN into the volume."""
    cfg = _write_config(config_text)
    _sh("livekit-wakeword", "setup", "--config", cfg)
    volume.commit()
    print("Setup complete. Volume contents:", flush=True)
    _sh("du", "-sh", DATA_DIR)


def _merge_recordings(model_name: str, replicate: int) -> int:
    """Fold real recordings into the generated positives, before augmentation.

    Two details here are load-bearing:

    Naming. `augment.py` selects source clips with a strict `^clip_\d{6}\.wav$`
    regex, so anything named otherwise is silently ignored — the clips would sit
    in the directory, never train, and the run would look perfectly healthy.
    Files are therefore renamed into that pattern, continuing past the highest
    index Piper already wrote so nothing is overwritten.

    Replication. A hundred-odd real clips against 25,000 synthetic ones is under
    half a percent and the model would barely notice them. Each take is copied
    `replicate` times; the copies are byte-identical here, but augmentation
    applies independently random reverb, noise, and EQ per clip per round, so
    each one becomes a genuinely distinct training example.
    """
    import re
    import shutil

    src_dir = Path(RECORDINGS_DIR)
    dest_dir = Path(OUTPUT_DIR) / model_name / "positive_train"
    if not src_dir.is_dir():
        print(f"No recordings at {src_dir}, training on synthetic positives only.", flush=True)
        return 0

    # Recursive so accents can live in sibling directories (positive/, positive_pt/)
    # and the share of each is controlled simply by how many are recorded.
    takes = sorted(src_dir.glob("**/*.wav"))
    if not takes:
        print(f"{src_dir} is empty, training on synthetic positives only.", flush=True)
        return 0

    pattern = re.compile(r"^clip_(\d{6})\.wav$")
    used = [
        int(match.group(1))
        for path in dest_dir.glob("*.wav")
        if (match := pattern.match(path.name))
    ]
    index = max(used) + 1 if used else 0
    synthetic = len(used)

    written = 0
    for take in takes:
        for _ in range(replicate):
            shutil.copy2(take, dest_dir / f"clip_{index:06d}.wav")
            index += 1
            written += 1

    share = written / (synthetic + written) * 100 if synthetic + written else 0
    print(
        f"Merged {len(takes)} recordings x{replicate} = {written} clips into "
        f"{dest_dir.name} alongside {synthetic} synthetic ({share:.1f}% real).",
        flush=True,
    )
    return written


def _run_pipeline(config_text: str, model_name: str, replicate: int = 0) -> dict:
    """generate -> augment -> train -> export -> eval, then save artifacts to the volume."""
    import json
    import shutil

    import torch

    print(f"CUDA available: {torch.cuda.is_available()}", flush=True)
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}", flush=True)

    cfg = _write_config(config_text)

    if replicate > 0:
        # Stages run separately so recordings can be injected between generation
        # and augmentation. `livekit-wakeword run` would do all of it in one go
        # and leave no seam to merge into.
        _sh("livekit-wakeword", "generate", cfg)
        _merge_recordings(model_name, replicate)
        _sh("livekit-wakeword", "augment", cfg)
        _sh("livekit-wakeword", "train", cfg)
        _sh("livekit-wakeword", "export", cfg)
        _sh("livekit-wakeword", "eval", cfg)
    else:
        _sh("livekit-wakeword", "run", cfg)

    src = Path(OUTPUT_DIR) / model_name
    dest = Path(DATA_DIR) / "artifacts" / model_name
    dest.mkdir(parents=True, exist_ok=True)

    saved = []
    for path in sorted(src.iterdir()):
        # Files only — the positive_train/ negative_train/ etc. wav directories stay behind.
        if path.is_file() and path.name.endswith(ARTIFACT_SUFFIXES):
            shutil.copy2(path, dest / path.name)
            saved.append(path.name)

    volume.commit()
    print(f"Saved to {dest}: {saved}", flush=True)

    metrics_path = dest / f"{model_name}_metrics.json"
    metrics = {}
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        print(f"metrics.json: {json.dumps(metrics, indent=2)}", flush=True)
    return metrics


@app.function(
    image=image,
    volumes={DATA_DIR: volume},
    gpu="T4",
    cpu=8,
    memory=32768,
    timeout=2 * 60 * 60,
)
def _run_smoke(config_text: str, model_name: str, replicate: int = 0) -> dict:
    return _run_pipeline(config_text, model_name, replicate)


@app.function(
    image=image,
    volumes={DATA_DIR: volume},
    gpu="L40S",
    cpu=8,
    memory=32768,
    timeout=24 * 60 * 60,
)
def _run_prod(config_text: str, model_name: str, replicate: int = 0) -> dict:
    return _run_pipeline(config_text, model_name, replicate)


@app.local_entrypoint()
def setup(config: str = "california.yaml"):
    """One-time: populate the volume. ~16GB, mostly the ACAV100M feature file.

    Every config shares the same data_dir, so this only needs running once no
    matter how many wake words get trained afterwards.
    """
    _setup_remote.remote(_read_config(config))


@app.local_entrypoint()
def smoke(config: str = "california_smoke.yaml"):
    """Cheap end-to-end pipeline check on a T4. The resulting model is not usable."""
    text = _read_config(config)
    _run_smoke.remote(text, _model_name(text))


@app.local_entrypoint()
def train(config: str = "california_v2.yaml", replicate: int = 60):
    """The real run on an L40S.

        uvx modal run --detach training/modal_train.py::train
        uvx modal run --detach training/modal_train.py::train --config california.yaml
        uvx modal run --detach training/modal_train.py::train --replicate 0

    Defaults to the current best config. model_name comes from the YAML, so runs
    do not overwrite each other on the volume and can be compared.

    `replicate` controls how many times each real recording in /data/recordings
    is copied into the positive set before augmentation (60 puts ~175 takes at ~41%
    of the positive set against n_samples: 15000, chosen because the baseline holdout
    showed the synthetic English was carrying far less than assumed). 0 disables the
    merge and
    trains on synthetic audio only. Having no recordings uploaded is not an error;
    the merge just no-ops.
    """
    text = _read_config(config)
    _run_prod.remote(text, _model_name(text), replicate)
