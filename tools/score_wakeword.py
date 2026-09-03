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
  uv run python tools/score_wakeword.py --dir holdout/ --framed
  uv run python tools/score_wakeword.py --negatives debug/activations --sweep

Two things to measure before retraining:

  Misses. Say the wake word ten times, normally, from where you usually stand.
  Peaks in the 0.4-0.6 band mean the model hears you and the threshold is
  wrong, so lower it and stop. Peaks under 0.1 mean the model genuinely does
  not recognise your voice, which no threshold fixes and only retraining will.

  False fires. Leave it running with --save-clips while the TV plays. Every
  event writes a WAV of the two seconds that caused it. Listening to those is
  how you learn what to put in custom_negative_phrases, instead of guessing.

The score comes from the same openWakeWord model the assistant loads, at the
same 16kHz, and with the same `wake_word.dither_rms` noise floor applied, so the
numbers transfer directly to config.yaml. That last part matters: undithered,
this tool reports ~0.80 on a silent room and would send you straight back to
raising the threshold, which is the fix that does not work.

Two ways to score, and they answer different questions:

  Peak model score (default). The highest single-frame score in the file.
  This measures the MODEL, and is what the recall table in config.yaml is
  built from.

  --framed. Pushes the file through detector.process_audio() in the same
  640-sample chunks the assistant feeds it, so threshold, consecutive_frames
  and debounce all apply. This measures the DECISION, which is what Master
  Miguel actually experiences. Since the 1280-sample framing fix the two
  genuinely differ: a lone spike now scores high and does not fire.

Recall is only half the picture, and until --negatives existed it was the only
half this tool could see. Turn on wake_word.capture in config.yaml, live with
her for a day, and every debug/activations/*_no_speech.wav is a false fire she
recorded herself. Then:

  uv run python tools/score_wakeword.py --negatives debug/activations --sweep

gives false fires per hour at each threshold, next to recall from --dir. That
pair is what a threshold should be chosen from.

Gotcha when building a negatives corpus: openWakeWord zeroes its first five
predictions after a reset, so any clip shorter than about 0.5s scores 0.0 no
matter what is in it, and would silently understate the false-positive rate.
The 2s clips written by wake_word.capture are safely clear of this.
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
            list(
                detector._oww_model.predict(
                    detector._apply_dither(audio[start : start + chunk])
                ).values()
            )[0]
        )
        t = start / 16000
        if score >= floor:
            print(f"  [{t:6.2f}s] {score:.4f}")
        if score > peak:
            peak, peak_at = score, t

    detector._oww_model.reset()
    print(f"\npeak {peak:.4f} at {peak_at:.2f}s   (threshold is {detector.threshold})")
    print("verdict:", verdict(peak, detector.threshold))


def read_wav(path: str) -> np.ndarray:
    """int16 samples from a WAV, mono assumed (the whole pipeline is 16kHz mono)."""
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def score_wav(detector, path: str) -> float:
    """Peak score over a WAV file. Returns 0.0 for unreadable or empty audio."""
    audio = read_wav(path)

    chunk = 1280
    peak = 0.0
    for start in range(0, len(audio) - chunk + 1, chunk):
        score = float(
            list(
                detector._oww_model.predict(
                    detector._apply_dither(audio[start : start + chunk])
                ).values()
            )[0]
        )
        peak = max(peak, score)
    detector._oww_model.reset()
    return peak


def fires_framed(detector, path: str, chunk_samples: int = 640) -> bool:
    """
    Would the live detector actually fire on this file?

    Runs process_audio() in the assistant's own chunk size, so threshold,
    consecutive_frames and debounce all apply. Peak score answers "did the
    model see it"; this answers "would she wake up", and since the 1280-frame
    fix those are different questions.
    """
    audio = read_wav(path)
    detector.reset()
    detector._last_activation_time = 0.0
    for start in range(0, len(audio) - chunk_samples + 1, chunk_samples):
        if detector.process_audio(audio[start : start + chunk_samples]):
            detector.reset()
            return True
    detector.reset()
    return False


def score_dir(detector, directory: str) -> list[tuple[Path, float, float]]:
    """
    (path, peak score, duration in seconds) for every WAV in a directory.

    Shared by the recall (--dir) and false-positive (--negatives) modes so both
    are measured the same way.
    """
    paths = sorted(Path(directory).glob("*.wav"))
    if not paths:
        raise SystemExit(f"no .wav files in {directory}")

    rows = []
    for path in paths:
        peak = score_wav(detector, str(path))
        rows.append((path, peak, len(read_wav(str(path))) / 16000))
    return rows


def run_dir(detector, directory: str, floor: float, framed: bool = False) -> list[tuple[Path, float, float]]:
    """Score every WAV in a directory and summarise. This is the retrain scorecard.

    Hold a slice of recordings back from training and run them through here before
    and after. Recall on livekit's own eval is measured against synthetic Piper
    audio, so it cannot tell you whether the model learned *your* voice; this can.
    """
    rows = score_dir(detector, directory)

    for path, peak, _ in rows:
        hit = fires_framed(detector, str(path)) if framed else peak >= detector.threshold
        flag = "fires" if hit else "MISS "
        print(f"  {flag}  {peak:.4f}  {path.name}")

    peaks_arr = np.array([peak for _, peak, _ in rows])
    if framed:
        fired = sum(1 for path, _, _ in rows if fires_framed(detector, str(path)))
        label = f"live decision at {detector.threshold}, {detector.consecutive_required} frames"
    else:
        fired = int((peaks_arr >= detector.threshold).sum())
        label = f"peak score at {detector.threshold}"

    print()
    print(f"  files      : {len(rows)}")
    print(f"  would fire : {fired}/{len(rows)}  ({fired / len(rows):.1%} recall, {label})")
    print(f"  peak score : min {peaks_arr.min():.4f}  median {np.median(peaks_arr):.4f}  max {peaks_arr.max():.4f}")

    # The threshold that would catch 90% of these, if one exists.
    ninety = float(np.percentile(peaks_arr, 10))
    if ninety > 0.02:
        print(f"  a threshold of {ninety:.2f} would catch 90% of them")
    else:
        print("  no threshold catches 90% of these - the model does not know this voice")

    return rows


def run_negatives(detector, directory: str, framed: bool = False) -> list[tuple[Path, float, float]]:
    """
    False-positive rate over audio that should never have woken her.

    Point this at debug/activations (see wake_word.capture in config.yaml):
    every *_no_speech.wav there is a wake that nobody spoke into, which is the
    definition of a false fire. Anything else with no wake word in it works too.
    """
    rows = score_dir(detector, directory)

    short = [p.name for p, _, dur in rows if dur < 0.5]
    fired_rows = []
    for path, peak, dur in rows:
        hit = fires_framed(detector, str(path)) if framed else peak >= detector.threshold
        if hit:
            fired_rows.append((path, peak))
            print(f"  FIRES  {peak:.4f}  {path.name}")

    total_hours = sum(dur for _, _, dur in rows) / 3600
    peaks_arr = np.array([peak for _, peak, _ in rows])

    print()
    print(f"  files       : {len(rows)}  ({total_hours * 60:.1f} minutes of audio)")
    print(f"  false fires : {len(fired_rows)}/{len(rows)} at threshold {detector.threshold}")
    if total_hours > 0:
        print(f"              : {len(fired_rows) / total_hours:.1f} per hour of this audio")
    print(f"  worst peak  : {peaks_arr.max():.4f}")
    clean = float(peaks_arr.max()) + 0.01
    if clean <= 1.0:
        print(f"  a threshold of {clean:.2f} would silence every one of these")
    else:
        print("  no threshold silences all of these - these need to be training negatives")
    if short:
        print(
            f"  warning: {len(short)} clip(s) under 0.5s always score 0.0 "
            "(openWakeWord zeroes its first 5 predictions after a reset) - "
            f"ignore them: {', '.join(short[:5])}"
        )
    return rows


def run_sweep(detector, positives, negatives, framed: bool = False) -> None:
    """
    Recall and false fires per hour across the threshold range.

    This is the table config.yaml hand-writes, except measured. Pick a
    threshold off the row where the two columns trade acceptably, rather
    than off an eval computed against synthetic Piper audio.
    """
    steps = [round(x, 2) for x in np.arange(0.15, 0.96, 0.05)]
    neg_hours = sum(dur for _, _, dur in negatives) / 3600 if negatives else 0.0

    print()
    print("  threshold   recall        false fires/hour")
    print("  " + "-" * 44)
    original = detector.threshold
    try:
        for step in steps:
            detector.threshold = step

            if positives:
                if framed:
                    hits = sum(1 for p, _, _ in positives if fires_framed(detector, str(p)))
                else:
                    hits = sum(1 for _, peak, _ in positives if peak >= step)
                recall = f"{hits}/{len(positives)} ({hits / len(positives):5.1%})"
            else:
                recall = "     -"

            if negatives:
                if framed:
                    fp = sum(1 for p, _, _ in negatives if fires_framed(detector, str(p)))
                else:
                    fp = sum(1 for _, peak, _ in negatives if peak >= step)
                per_hour = f"{fp / neg_hours:8.1f}" if neg_hours > 0 else f"{fp:8d} total"
            else:
                per_hour = "       -"

            marker = "  <- current" if abs(step - original) < 1e-9 else ""
            print(f"     {step:.2f}     {recall:<16}{per_hour}{marker}")
    finally:
        detector.threshold = original


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

            score = float(
                list(detector._oww_model.predict(detector._apply_dither(chunk)).values())[0]
            )
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
    parser.add_argument(
        "--negatives",
        metavar="DIR",
        help="score audio that should NOT wake her (try debug/activations, "
             "written by wake_word.capture) and report false fires per hour",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="with --dir and/or --negatives, print recall vs false-fires-per-hour "
             "across the threshold range instead of guessing from one value",
    )
    parser.add_argument(
        "--framed",
        action="store_true",
        help="decide with the live detector (threshold + consecutive_frames + "
             "debounce) instead of peak model score - what she actually does",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.model:
        config["wake_word"]["model"] = args.model
    if args.threshold is not None:
        config["wake_word"]["threshold"] = args.threshold
    detector = build_model(config)

    if args.dir or args.negatives:
        positives, negatives = [], []
        if args.dir:
            print(f"\npositives - should fire ({args.dir})\n")
            positives = run_dir(detector, args.dir, args.floor, framed=args.framed)
        if args.negatives:
            print(f"\nnegatives - should stay quiet ({args.negatives})\n")
            negatives = run_negatives(detector, args.negatives, framed=args.framed)
        if args.sweep:
            run_sweep(detector, positives, negatives, framed=args.framed)
    elif args.wav:
        run_wav(detector, args.wav, args.floor)
    else:
        run_live(detector, config, args.floor, args.save_clips)


if __name__ == "__main__":
    main()
