"""
Records real utterances of the wake word to fold into training as positives.

Why this exists: every positive the model has seen so far was synthesized by
Piper's `en-us-libritts-high` checkpoint, and `synthesis.py` takes its espeak
voice from that checkpoint's own config, so it is en-us and not configurable.
The model has therefore never heard the wake word said with a Portuguese
accent, which is exactly the reported failure. No threshold or model-size
change fixes that; only hearing it does.

  uv run python tools/record_wakeword.py
  uv run python tools/record_wakeword.py --count 60 --manual
  uv run python tools/record_wakeword.py --out training/recordings/miguel

Say it the way you actually say it. The prompts cycle through delivery styles
on purpose: a model trained only on careful pronunciations learns to need one.

Clips are trimmed tightly at both ends, which matters more than it looks.
livekit's `align_clip_to_end` places each positive at the END of the 2s window
with up to 200ms of jitter, so trailing silence would push the word earlier and
misalign it against the Piper clips, which are themselves VAD-trimmed.

Output is 16kHz mono 16-bit, one file per utterance. Upload and merge with the
commands in training/README.md.
"""

import argparse
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE_RATE = 16000
RECORD_SECONDS = 2.5
SILENCE_PAD_MS = 40

# Cycled so the set covers real delivery rather than 120 careful repetitions.
STYLES = [
    "normally, like you would to get her attention",
    "quickly, a bit clipped",
    "quietly, like it is late",
    "louder, like you are across the room",
    "slowly and clearly",
    "casually, half-swallowed",
]


def trim_silence(audio: np.ndarray, floor_ratio: float = 0.06) -> np.ndarray:
    """Trim to the spoken part, leaving a short pad. Energy gate on abs amplitude."""
    if audio.size == 0:
        return audio
    envelope = np.abs(audio.astype(np.float32))
    threshold = max(envelope.max() * floor_ratio, 200.0)
    loud = np.flatnonzero(envelope > threshold)
    if loud.size == 0:
        return np.array([], dtype=np.int16)
    pad = int(SAMPLE_RATE * SILENCE_PAD_MS / 1000)
    start = max(0, loud[0] - pad)
    end = min(len(audio), loud[-1] + pad)
    return audio[start:end]


def record_once(sd) -> np.ndarray:
    frames = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    return frames.reshape(-1)


def describe(audio: np.ndarray) -> str:
    if audio.size == 0:
        return "silent"
    peak = int(np.abs(audio).max())
    ms = int(len(audio) / SAMPLE_RATE * 1000)
    if peak >= 32000:
        return f"{ms}ms, peak {peak} - CLIPPING, back off the mic"
    if peak < 1500:
        return f"{ms}ms, peak {peak} - very quiet"
    return f"{ms}ms, peak {peak}"


def usable(audio: np.ndarray) -> bool:
    """Reject silence, blips, and anything that ran the whole window."""
    if audio.size == 0:
        return False
    ms = len(audio) / SAMPLE_RATE * 1000
    return 200 <= ms <= 2200 and int(np.abs(audio).max()) >= 1500


def save(audio: np.ndarray, path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.astype(np.int16).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--word", default="California", help="what to say")
    parser.add_argument("--count", type=int, default=120, help="utterances to keep")
    parser.add_argument("--out", default="training/recordings/positive")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="wait for Enter before each take instead of auto-advancing",
    )
    args = parser.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        raise SystemExit("sounddevice not installed. Run: uv sync")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("*.wav"))
    start_index = len(existing)

    print(f"Recording {args.count} takes of \"{args.word}\" -> {out_dir}/")
    if existing:
        print(f"{len(existing)} already there, continuing from {start_index}")
    print("Rejected takes do not count, so just say it again.\n")
    print("Ctrl+C to stop early. Whatever is saved is still usable.\n")

    kept = 0
    rejected = 0
    try:
        while kept < args.count:
            style = STYLES[(start_index + kept) % len(STYLES)]
            print(f"[{kept + 1}/{args.count}] Say it {style}")

            if args.manual:
                input("      press Enter, then speak: ")
            else:
                time.sleep(0.35)
                print("      recording...", end="", flush=True)

            raw = record_once(sd)
            clip = trim_silence(raw)
            info = describe(clip)

            if not usable(clip):
                rejected += 1
                print(f"\r      rejected ({info})            ")
                continue

            path = out_dir / f"take_{start_index + kept:04d}.wav"
            save(clip, path)
            kept += 1
            print(f"\r      kept {path.name}  ({info})        ")
    except KeyboardInterrupt:
        print("\n\nstopped early")

    total = len(list(out_dir.glob("*.wav")))
    print(f"\nkept {kept} this session, {rejected} rejected, {total} total in {out_dir}/")
    if total:
        print("\nNext, from training/README.md:")
        print(f"  uvx modal volume put california-wakeword-data {out_dir} /recordings/positive")


if __name__ == "__main__":
    main()
