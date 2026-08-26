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


def _run_pipeline(config_text: str, model_name: str) -> dict:
    """generate -> augment -> train -> export -> eval, then save artifacts to the volume."""
    import json
    import shutil

    import torch

    print(f"CUDA available: {torch.cuda.is_available()}", flush=True)
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}", flush=True)

    cfg = _write_config(config_text)
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
def _run_smoke(config_text: str, model_name: str) -> dict:
    return _run_pipeline(config_text, model_name)


@app.function(
    image=image,
    volumes={DATA_DIR: volume},
    gpu="L40S",
    cpu=8,
    memory=32768,
    timeout=24 * 60 * 60,
)
def _run_prod(config_text: str, model_name: str) -> dict:
    return _run_pipeline(config_text, model_name)


@app.local_entrypoint()
def setup():
    """One-time: populate the volume. ~16GB, mostly the ACAV100M feature file."""
    _setup_remote.remote(_read_config("california.yaml"))


@app.local_entrypoint()
def smoke():
    """Cheap end-to-end pipeline check on a T4. The resulting model is not usable."""
    _run_smoke.remote(_read_config("california_smoke.yaml"), "california_smoke")


@app.local_entrypoint()
def train():
    """The real run on an L40S."""
    _run_prod.remote(_read_config("california.yaml"), "california")
