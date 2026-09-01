"""
Prints the raw wake-word score in real time, so misses and false fires become
numbers instead of impressions.

`WakeWordDetector` only ever answers yes or no, which is the wrong instrument
for tuning: "it did not hear me" and "it heard me at 0.55 and the threshold is
0.59" feel identical and need opposite fixes. This prints every score, flags
anything above --floor as an event, and can dump the audio that caused it.

  uv run python tools/score_wakeword.py
  uv run python tools/score_wakeword.py --floor 0.05 --save-clips debug/wakeword
  uv run python tools/score_wakeword.py --wav recording.wav

Two things to measure before retraining:

  Misses. Say the wake word ten times, normally, from where you usually stand.
  Peaks in the 0.4-0.6 band mean the model hears you and the threshold is
  wrong, so lower it and stop. Peaks under 0.1 mean the model genuinely does
  not recognise your voice, which no threshold fixes and only retraining will.

  False fires. Leave it running with --save-clips while the TV plays. Every
  event writes a WAV of the two seconds that caused it. Listening to those is
  how you learn what to put in custom_negative_phrases, instead of guessing.

The score comes from the same openWakeWord model the assistant loads, at the
same 16kHz, so the numbers transfer directly to config.yaml.
"""

import argparse
import os
import sys
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio_pipeline import AudioPipeline  # noqa: E402
from core.wake_word import WakeWordDetector  # noqa: E402

WINDOW_SECONDS = 2.0


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_model(config: dict):
    """Reuse WakeWordDetector's loading so this measures exactly what runs live."""
    detector = WakeWordDetector(config)
    if detector._backend != "oww":
        raise SystemExit(
            f"This tool only supports the openWakeWord backend, got "
            f"'{detector._backend}'. Point wake_word.model at an .onnx model."
        )
    return detector


def save_clip(out_dir: str, ring: deque, sample_rate: int, score: float) -> str:
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    path = os.path.join(out_dir, f"{stamp}_{score:.3f}.wav")
    audio = np.concatenate(list(ring)) if ring else np.array([], dtype=np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.astype(np.int16).tobytes())
    return path


def run_wav(detector, path: str, floor: float) -> None:
    with wave.open(path, "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1:
            print(
                f"warning: expected 16kHz mono, got {wf.getframerate()}Hz "
                f"{wf.getnchannels()}ch - scores may be meaningless"
            )
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16)

    chunk = 1280  # 80ms, openWakeWord's native step
    peak, peak_at = 0.0, 0.0
    for start in range(0, len(audio) - chunk + 1, chunk):
        score = float(
            list(detector._oww_model.predict(audio[start : start + chunk]).values())[0]
        )
        t = start / 16000
        if score >= floor:
            print(f"  [{t:6.2f}s] {score:.4f}")
        if score > peak:
            peak, peak_at = score, t

    print(f"\npeak {peak:.4f} at {peak_at:.2f}s   (threshold is {detector.threshold})")
    print("verdict:", verdict(peak, detector.threshold))


def score_wav(detector, path: str) -> float:
    """Peak score over a WAV file. Returns 0.0 for unreadable or empty audio."""
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16)

    chunk = 1280
    peak = 0.0
    for start in range(0, len(audio) - chunk + 1, chunk):
        score = float(
            list(detector._oww_model.predict(audio[start : start + chunk]).values())[0]
        )
        peak = max(peak, score)
    detector._oww_model.reset()
    return peak


def run_dir(detector, directory: str, floor: float) -> None:
    """Score every WAV in a directory and summarise. This is the retrain scorecard.

    Hold a slice of recordings back from training and run them through here before
    and after. Recall on livekit's own eval is measured against synthetic Piper
    audio, so it cannot tell you whether the model learned *your* voice; this can.
    """
    paths = sorted(Path(directory).glob("*.wav"))
    if not paths:
        raise SystemExit(f"no .wav files in {directory}")

    peaks = []
    for path in paths:
        peak = score_wav(detector, str(path))
        peaks.append(peak)
        flag = "fires" if peak >= detector.threshold else "MISS "
        print(f"  {flag}  {peak:.4f}  {path.name}")

    peaks_arr = np.array(peaks)
    fired = int((peaks_arr >= detector.threshold).sum())
    print()
    print(f"  files      : {len(peaks)}")
    print(f"  would fire : {fired}/{len(peaks)}  ({fired / len(peaks):.1%} recall at {detector.threshold})")
    print(f"  peak score : min {peaks_arr.min():.4f}  median {np.median(peaks_arr):.4f}  max {peaks_arr.max():.4f}")

    # The threshold that would catch 90% of these, if one exists.
    ninety = float(np.percentile(peaks_arr, 10))
    if ninety > 0.02:
        print(f"  a threshold of {ninety:.2f} would catch 90% of them")
    else:
        print("  no threshold catches 90% of these - the model does not know this voice")


def verdict(peak: float, threshold: float) -> str:
    if peak >= threshold:
        return "would fire"
    if peak >= threshold * 0.6:
        return "near miss - the model heard it, the threshold is too high"
    return "not recognised - lowering the threshold will not fix this, retrain"


def run_live(detector, config: dict, floor: float, save_dir: str | None) -> None:
    audio = AudioPipeline(config)
    sample_rate = audio.sample_rate
    ring = deque(maxlen=int(WINDOW_SECONDS * sample_rate / audio.chunk_samples) + 1)

    print(f"model      : {config['wake_word']['model']}")
    print(f"threshold  : {detector.threshold}   floor: {floor}")
    print(f"save clips : {save_dir or 'no'}")
    print("\nListening. Say the wake word. Ctrl+C to stop.\n")

    session_peak = 0.0
    in_event = False
    event_peak = 0.0

    stream = audio.create_mic_stream()
    stream.start()
    try:
        while True:
            data, _ = stream.read(audio.chunk_samples)
            chunk = audio.bytes_to_numpy(data)
            ring.append(chunk.astype(np.int16))

            score = float(list(detector._oww_model.predict(chunk).values())[0])
            session_peak = max(session_peak, score)

            if score >= floor:
                in_event = True
                event_peak = max(event_peak, score)
                bar = "#" * int(score * 40)
                mark = "  <-- FIRES" if score >= detector.threshold else ""
                print(f"  {score:.4f} |{bar:<40}|{mark}")
            elif in_event:
                print(f"  event peak {event_peak:.4f} - {verdict(event_peak, detector.threshold)}")
                if save_dir:
                    print(f"  saved {save_clip(save_dir, ring, sample_rate, event_peak)}")
                print()
                in_event, event_peak = False, 0.0
    except KeyboardInterrupt:
        print(f"\n\nsession peak: {session_peak:.4f}   threshold: {detector.threshold}")
    finally:
        stream.stop()
        stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--model",
        help="override wake_word.model, for A/B-ing a new model against the live one",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        help="override wake_word.threshold (use the trained optimal_threshold)",
    )
    parser.add_argument("--wav", help="score a WAV file instead of the microphone")
    parser.add_argument(
        "--dir",
        metavar="DIR",
        help="score every WAV in DIR and summarise; use on held-out recordings",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=0.15,
        help="report anything scoring at or above this (default 0.15)",
    )
    parser.add_argument(
        "--save-clips",
        metavar="DIR",
        help="write the 2s of audio behind each event to DIR, for listening back",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.model:
        config["wake_word"]["model"] = args.model
    if args.threshold is not None:
        config["wake_word"]["threshold"] = args.threshold
    detector = build_model(config)

    if args.dir:
        run_dir(detector, args.dir, args.floor)
    elif args.wav:
        run_wav(detector, args.wav, args.floor)
    else:
        run_live(detector, config, args.floor, args.save_clips)


if __name__ == "__main__":
    main()
