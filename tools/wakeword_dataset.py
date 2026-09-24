"""
Builds the wake-word training set, and keeps junk out of it.

Replaces tools/record_wakeword.py (in deprecated/ since 2026-09-23). That tool
recorded a fixed 2.5s window through the default device, trimmed it
destructively at 6% of peak, kept anything 200-2200ms with a peak over 1500,
and wrote no record of how or where a take was made. Holdouts were split by
hand, take by take. What went into training could not be audited afterwards.

Everything here goes through one manifest,
training/recordings/manifest.jsonl, one row per take. A take is only ever
exported for training if it passed every automatic check or a human kept it.

  record       one session: a language, a distance, a background, ~15 takes.
               Enter starts a take and Enter stops it. --hands-free instead
               cuts one take per utterance, for when the keyboard is out of
               reach (mid, couch)
  review       listen to flagged takes and keep or drop them
  audit        import the pre-manifest recordings and check them the same way
  import-live  copy her own activation captures in as candidates for review
  summary      counts per cell, what still needs review, cells with no holdout
  export       write the train / holdout / backgrounds sets for Modal

  uv run python tools/wakeword_dataset.py record --lang pt --distance couch --background tv
  uv run python tools/wakeword_dataset.py record --lang en --distance near --background quiet --holdout
  uv run python tools/wakeword_dataset.py review
  uv run python tools/wakeword_dataset.py export --name v3

What makes a take junk, and what only makes it suspicious:

  Rejected (never exported unless review overrides): clipping, no sound
  above the room, a word shorter than 0.3s, a word that runs into either
  edge of the window (cut off), and in a quiet session a "word" over 1.4s.

  Flagged (exported only after a human keeps it): more than one sound, SNR
  under 10 dB, Silero hearing no speech in it, or a transcript that is not
  "California". Whisper mishears a lone word often enough that it cannot be
  allowed to reject on its own, and a TV in the background makes "one sound"
  and "too long" unreliable, so those are left to the ear.

The model's own score is recorded on every take and is NEVER a gate. The
takes the current model scores lowest are the ones it most needs to learn
from; filtering on them would train it on what it already knows.

Why the whole take is kept, not just the word: trimming is decided at
export, from the segment stored in the manifest, so a better trim later does
not need a re-record.

Holdout is decided per SESSION, at record time (--holdout), and never
re-rolled. Takes from one sitting share a mic position, a mood and a room, so
splitting a session between train and holdout leaks. Record holdout sessions
on a different day from the train sessions of the same cell.
"""

import argparse
import difflib
import hashlib
import io
import json
import os
import shutil
import sys
import time
import unicodedata
import wave
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

SR = 16000
REC_DIR = ROOT / "training" / "recordings"
MANIFEST_PATH = REC_DIR / "manifest.jsonl"
# Everything recorded with this tool, one folder per session. Named apart from
# the pre-manifest folders (positive/, holdout_*/) so the two never mix.
SESSIONS_DIR = REC_DIR / "new"
LIVE_DIR = REC_DIR / "live"
# Her long replies for --background her; written by generate_her_monologues.py
# in the live voice (config.yaml tts.google.voice).
HER_MONOLOGUE_DIR = ROOT / "sounds" / "her_monologues" / "google_en-US-Chirp3-HD-Aoede"
EXPORT_DIR = REC_DIR / "export"

LANGS = ("en", "pt")
DISTANCES = ("near", "mid", "couch")
BACKGROUNDS = ("quiet", "tv", "music", "her")
NOISY_BACKGROUNDS = {"tv", "music", "her"}

MAX_TAKE_S = 10.0       # a take that runs this long stops on its own
KEY_GUARD_START_S = 0.25  # push-to-talk: the start key's release click
KEY_GUARD_END_S = 0.10    # push-to-talk: the stop key's click
HANDS_FREE_FACTOR = 3.0   # hands-free: a take opens this far above the room
HANDS_FREE_PREROLL_S = 0.6
HANDS_FREE_HANG_S = 0.7   # hands-free: quiet this long closes the take
ROOMTONE_S = 20.0       # recorded before the first take of every session
EDGE_S = 0.05           # speech closer than this to an edge was cut off
MIN_WORD_S = 0.3
MAX_WORD_S = 1.4
MAX_CLIP_FRAC = 0.001   # share of samples at full scale
FULL_SCALE = 32000
MIN_SNR_DB = 10.0     # must sit above SEGMENT_FACTOR, or no detected word can ever fail it
FRAME_S = 0.02
SEGMENT_FACTOR = 2.0    # a frame is sound when its RMS is 2x the room (~6 dB)
SEGMENT_MIN_RMS = 150.0
MERGE_GAP_S = 0.25      # "Cali ... fornia" is one word, not two sounds
MIN_SOUND_S = 0.06      # shorter blips are ignored
EXPORT_PAD_S = 0.04     # livekit's align_clip_to_end wants a tight clip
REVIEW_CONTEXT_S = 0.3

REJECTED, FLAGGED, OK, ACCEPTED, DROPPED = "rejected", "flagged", "ok", "accepted", "dropped"
EXPORTABLE = {OK, ACCEPTED}

# Cycled so a session covers real delivery rather than careful repetitions.
STYLES = [
    "normally, like you would to get her attention",
    "quickly, a bit clipped",
    "quietly, like it is late",
    "louder, like she did not hear you",
    "slowly and clearly",
    "casually, half-swallowed",
]

# Pre-manifest recordings: directory, language, split.
LEGACY = [
    ("positive", "en", "train"),
    ("positive_pt", "pt", "train"),
    ("holdout_en", "en", "holdout"),
    ("holdout_pt", "pt", "holdout"),
]


# ─── Audio helpers ───────────────────────────────────────────────────


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SR or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16kHz mono 16-bit")
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(audio.astype(np.int16).tobytes())


def wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


def rms(audio: np.ndarray) -> float:
    if len(audio) == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def frame_rms(audio: np.ndarray, frame_s: float = FRAME_S) -> np.ndarray:
    frame = int(SR * frame_s)
    n = len(audio) // frame
    if n == 0:
        return np.zeros(0)
    x = audio[: n * frame].astype(np.float64).reshape(n, frame)
    return np.sqrt(np.mean(x ** 2, axis=1))


def sha1(audio: np.ndarray) -> str:
    return hashlib.sha1(audio.astype(np.int16).tobytes()).hexdigest()


def rel(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


# ─── Segmentation and checks (pure, tested) ─────────────────────────


def sound_segments(audio: np.ndarray, floor_rms: float) -> list[tuple[float, float]]:
    """
    (start_s, end_s) of every sound louder than the room, merged across short gaps.

    Relative to the session's own room tone rather than to the take's peak, so
    a soft word stays whole and a click elsewhere in the window shows up as a
    second sound instead of silently stretching the word.
    """
    levels = frame_rms(audio)
    threshold = max(floor_rms * SEGMENT_FACTOR, SEGMENT_MIN_RMS)
    active = levels > threshold
    runs = []
    start = None
    for i, on in enumerate(active):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start * FRAME_S, i * FRAME_S))
            start = None
    if start is not None:
        runs.append((start * FRAME_S, len(active) * FRAME_S))

    merged = []
    for seg in runs:
        if merged and seg[0] - merged[-1][1] <= MERGE_GAP_S:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)
    return [(round(s, 3), round(e, 3)) for s, e in merged if e - s >= MIN_SOUND_S]


def pick_word(audio: np.ndarray, segments: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The segment carrying the most energy is taken to be the word."""
    if not segments:
        return None

    def energy(seg):
        a = audio[int(seg[0] * SR) : int(seg[1] * SR)].astype(np.float64)
        return float(np.sum(a ** 2))

    return max(segments, key=energy)


def analyze(audio: np.ndarray, floor_rms: float) -> dict:
    """Everything the gates look at, stored on the row so a decision can be re-checked."""
    segments = sound_segments(audio, floor_rms)
    word = pick_word(audio, segments)
    metrics = {
        "duration_s": round(len(audio) / SR, 3),
        "floor_rms": round(float(floor_rms), 1),
        "peak": int(np.abs(audio.astype(np.int32)).max()) if len(audio) else 0,
        "clip_frac": round(float(np.mean(np.abs(audio.astype(np.int32)) >= FULL_SCALE)), 5)
        if len(audio) else 0.0,
        "segments": [list(s) for s in segments],
        "word": list(word) if word else None,
        "word_s": None,
        "snr_db": None,
    }
    if word:
        speech = audio[int(word[0] * SR) : int(word[1] * SR)]
        metrics["word_s"] = round(word[1] - word[0], 3)
        metrics["snr_db"] = round(20 * np.log10(max(rms(speech), 1.0) / max(floor_rms, 1.0)), 1)
    return metrics


def check(metrics: dict, *, noisy: bool, check_edges: bool) -> tuple[list[str], list[str]]:
    """
    (reject reasons, flag reasons). Rejects are unambiguous junk; flags need an ear.

    `noisy`: the session had TV, music or her voice playing. The background
    itself then rises over the room level all through the take, so the loudest
    "sound" can run into an edge or past 1.4s, and there is always more than
    one. Measured on the first TV session (2026-09-24): 17 of 52 takes rejected
    as cut off at the start and 14 at the end, while the transcripts read
    "California". So with a background those only flag, extra sounds are not
    even flagged, and the transcript is what decides.
    `check_edges`: False for clips that were trimmed before they got here
    (legacy recordings, live captures), whose edges prove nothing.
    """
    rejects, flags = [], []
    if metrics["clip_frac"] > MAX_CLIP_FRAC:
        rejects.append("clipping")
    word = metrics["word"]
    if word is None:
        rejects.append("no_sound")
        return rejects, flags

    if metrics["word_s"] < MIN_WORD_S:
        rejects.append("too_short")
    if metrics["word_s"] > MAX_WORD_S:
        (flags if noisy else rejects).append("too_long")
    if check_edges:
        if word[0] < EDGE_S:
            (flags if noisy else rejects).append("cut_off_start")
        if word[1] > metrics["duration_s"] - EDGE_S:
            (flags if noisy else rejects).append("cut_off_end")
    if len(metrics["segments"]) > 1 and not noisy:
        flags.append("extra_sounds")
    if metrics["snr_db"] is not None and metrics["snr_db"] < MIN_SNR_DB:
        flags.append("low_snr")
    return rejects, flags


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return "".join(c if c.isalpha() or c.isspace() else " " for c in text).strip()


def transcript_matches(text: str | None) -> bool:
    """Is this Whisper's rendering of "California" / "Califórnia"?"""
    if not text:
        return False
    words = normalize_text(text).split()
    if not words:
        return False
    joined = "".join(words)
    if "californ" in joined:
        return True
    return any(
        difflib.SequenceMatcher(None, w, "california").ratio() >= 0.75
        for w in words + [joined]
    )


def status_for(rejects: list[str], flags: list[str]) -> str:
    if rejects:
        return REJECTED
    if flags:
        return FLAGGED
    return OK


# ─── Manifest ────────────────────────────────────────────────────────


class Manifest:
    """One JSON object per line. Rewritten whole, atomically, on every save."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else MANIFEST_PATH
        self.rows: list[dict] = []
        if self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                self.rows = [json.loads(line) for line in fh if line.strip()]

    def ids(self) -> set[str]:
        return {r["id"] for r in self.rows}

    def paths(self) -> set[str]:
        return {r["path"] for r in self.rows}

    def hashes(self) -> set[str]:
        return {r["sha1"] for r in self.rows if r.get("sha1")}

    def add(self, row: dict) -> None:
        if row["id"] in self.ids():
            raise ValueError(f"duplicate take id {row['id']}")
        self.rows.append(row)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in self.rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)


def cell(row: dict) -> str:
    return f"{row['lang']}-{row['distance']}-{row['background']}"


# ─── Leak guard and export planning (pure, tested) ──────────────────


def leaks(rows: list[dict]) -> list[str]:
    """Why these rows cannot be split as given. Empty means safe."""
    problems = []
    by_session = defaultdict(set)
    by_hash = defaultdict(set)
    for r in rows:
        by_session[r["session"]].add(r["split"])
        if r.get("sha1"):
            by_hash[r["sha1"]].add(r["split"])
    for session, splits in sorted(by_session.items()):
        if len(splits) > 1:
            problems.append(f"session {session} has takes in both {sorted(splits)}")
    for h, splits in by_hash.items():
        if len(splits) > 1:
            problems.append(f"identical audio {h[:10]} is in both train and holdout")
    ids = Counter(r["id"] for r in rows)
    problems += [f"take id {i} appears {n} times" for i, n in ids.items() if n > 1]
    return problems


def exportable(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] in EXPORTABLE and r.get("split") in ("train", "holdout")]


def train_clip(row: dict, audio: np.ndarray) -> np.ndarray:
    """The word plus a tight pad, cut from the raw window. Legacy clips are already trimmed."""
    if row["source"] == "legacy_v1" or not row["metrics"].get("word"):
        return audio
    start, end = row["metrics"]["word"]
    a = max(0, int((start - EXPORT_PAD_S) * SR))
    b = min(len(audio), int((end + EXPORT_PAD_S) * SR))
    return audio[a:b]


def holdout_gaps(rows: list[dict]) -> list[str]:
    """Cells with training takes and no holdout to measure them against."""
    live = [r for r in rows if r["status"] in EXPORTABLE and r["source"] == "session"]
    train = {cell(r) for r in live if r["split"] == "train"}
    held = {cell(r) for r in live if r["split"] == "holdout"}
    return sorted(train - held)


def safe_name(take_id: str) -> str:
    return take_id.replace("/", "__")


# ─── Services used by the interactive commands ──────────────────────


def load_config() -> dict:
    import yaml

    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Transcriber:
    """Groq Whisper, called raw: no filler filter, the language given per take, no prompt.

    A prompt mentioning "California" would bias Whisper toward hearing it and
    make this check meaningless, so there is none.
    """

    def __init__(self, config: dict, min_interval_s: float = 3.0):
        self.client = None
        self.model = config["stt"]["groq"]["model"]
        self.min_interval_s = min_interval_s
        self._last = 0.0
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            print("  GROQ_API_KEY not set: transcripts skipped, every take will be flagged")
            return
        from groq import Groq

        self.client = Groq(api_key=key)

    def __call__(self, audio: np.ndarray, lang: str) -> str | None:
        if self.client is None:
            return None
        for attempt in range(2):
            wait = self.min_interval_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                result = self.client.audio.transcriptions.create(
                    file=("take.wav", wav_bytes(audio)),
                    model=self.model,
                    language=lang if lang in LANGS else None,
                    response_format="text",
                )
                return str(getattr(result, "text", result)).strip()
            except Exception as exc:  # rate limit or network: one retry, then flag
                if attempt == 0:
                    time.sleep(20)
                    continue
                print(f"  transcript failed ({exc.__class__.__name__}), take flagged")
                return None
        return None


class Scorer:
    """The live model's peak score, through score_wakeword's bed. Information only."""

    def __init__(self, config: dict):
        import score_wakeword as sw

        self.sw = sw
        self.model = config["wake_word"]["model"]
        self.detector = sw.build_model(config)

    def __call__(self, path: Path) -> float:
        return round(self.sw.score_wav(self.detector, str(path)), 4)


class SpeechCheck:
    """Silero, if the silero extra is installed. None when it cannot tell."""

    def __init__(self, config: dict):
        self.get_ts = None
        try:
            from core.vad import VAD

            vad = VAD({**config, "vad": {**config["vad"], "engine": "silero"}})
            if vad._silero_model is not None:
                self.model = vad._silero_model
                self.get_ts = vad._silero_get_speech
        except Exception as exc:
            print(f"  Silero unavailable ({exc}); speech check skipped")

    def __call__(self, audio: np.ndarray) -> bool | None:
        if self.get_ts is None:
            return None
        import torch

        stamps = self.get_ts(
            torch.from_numpy(audio.astype(np.float32) / 32768.0), self.model, sampling_rate=SR
        )
        return bool(stamps)


def inspect_take(audio, floor_rms, *, noisy, check_edges, lang, transcriber, speech):
    """Run every check on one take. Returns (status, reasons, metrics, transcript)."""
    metrics = analyze(audio, floor_rms)
    rejects, flags = check(metrics, noisy=noisy, check_edges=check_edges)
    transcript = None
    if not rejects and metrics["word"]:
        s, e = metrics["word"]
        around = audio[max(0, int((s - REVIEW_CONTEXT_S) * SR)) : int((e + REVIEW_CONTEXT_S) * SR)]
        if speech is not None and speech(around) is False:
            flags.append("not_speech")
        transcript = transcriber(around, lang) if transcriber is not None else None
        if not transcript_matches(transcript):
            # No transcript (--no-stt, no key, a failed call) is not a pass either.
            flags.append("transcript" if transcript is not None else "no_transcript")
    return status_for(rejects, flags), rejects + flags, metrics, transcript


def describe(row: dict) -> str:
    m = row["metrics"]
    bits = []
    if m.get("word_s") is not None:
        bits.append(f"{m['word_s']:.2f}s")
    if m.get("snr_db") is not None:
        bits.append(f"snr {m['snr_db']:.0f}dB")
    if row.get("transcript") is not None:
        bits.append(repr(row["transcript"]))
    if row.get("score") is not None:
        bits.append(f"score {row['score']:.2f}")
    if row["reasons"]:
        bits.append("[" + ", ".join(row["reasons"]) + "]")
    return "  ".join(bits)


def load_dotenv_quietly() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


# ─── record ──────────────────────────────────────────────────────────


def read_seconds(stream, seconds: float, chunk: int) -> np.ndarray:
    parts, need = [], int(seconds * SR)
    while need > 0:
        data, _ = stream.read(chunk)
        a = np.frombuffer(data, dtype=np.int16)
        parts.append(a)
        need -= len(a)
    return np.concatenate(parts)[: int(seconds * SR)]


def trim_key_clicks(audio: np.ndarray) -> np.ndarray:
    """
    Cut the Enter presses off a push-to-talk take.

    The start key clicks again on its way back up, ~100ms after it registers,
    and the stop key clicks as it registers. Left in, a laptop mic hears both as
    sounds of their own and every take would be flagged extra_sounds.
    """
    a = int(KEY_GUARD_START_S * SR)
    b = len(audio) - int(KEY_GUARD_END_S * SR)
    return audio[a:b] if b > a else audio[:0]


def record_push_to_talk(stream, chunk: int) -> np.ndarray:
    """Read the mic until Enter is pressed, or MAX_TAKE_S."""
    import threading

    pressed = threading.Event()
    threading.Thread(target=lambda: (input(), pressed.set()), daemon=True).start()
    parts, n, limit = [], 0, int(MAX_TAKE_S * SR)
    while not pressed.is_set() and n < limit:
        data, _ = stream.read(chunk)
        parts.append(np.frombuffer(data, dtype=np.int16))
        n += len(parts[-1])
    if not pressed.is_set():
        print(f"      stopped at {MAX_TAKE_S:.0f}s - press Enter", flush=True)
        pressed.wait()  # or the waiting input() would eat the next prompt's Enter
    return trim_key_clicks(np.concatenate(parts))


class UtteranceCutter:
    """
    Hands-free takes: cut one take per utterance out of a continuous stream.

    A take opens after two chunks louder than the room and closes after
    HANDS_FREE_HANG_S of quiet, with HANDS_FREE_PREROLL_S kept from before the
    onset, so the word never touches either edge of its window.
    """

    def __init__(self, floor_rms: float, chunk: int):
        from collections import deque

        self.threshold = max(floor_rms * HANDS_FREE_FACTOR, SEGMENT_MIN_RMS)
        self.chunk = chunk
        self.preroll = deque(maxlen=max(1, int(HANDS_FREE_PREROLL_S * SR / chunk)))
        self.parts = None
        self.loud_run = 0
        self.quiet_s = 0.0

    def feed(self, audio: np.ndarray) -> np.ndarray | None:
        loud = rms(audio) > self.threshold
        if self.parts is None:
            self.preroll.append(audio)
            self.loud_run = self.loud_run + 1 if loud else 0
            if self.loud_run >= 2:
                self.parts = list(self.preroll)
                self.quiet_s = 0.0
            return None
        self.parts.append(audio)
        self.quiet_s = 0.0 if loud else self.quiet_s + len(audio) / SR
        length = sum(len(p) for p in self.parts) / SR
        if self.quiet_s >= HANDS_FREE_HANG_S or length >= MAX_TAKE_S:
            take = np.concatenate(self.parts)
            self.parts = None
            self.loud_run = 0
            self.preroll.clear()
            return take
        return None


def her_voice() -> np.ndarray:
    """
    Her long replies, back to back, for the 'her' background.

    He interrupts a reply, not an acknowledgement: long, continuous speech. The
    first 'her' session looped her short activation clips instead, which is
    not what the barge-in case sounds like. generate_her_monologues.py writes
    the replies, in the live voice, and none of them says her name: over a
    line that did, a take whose transcript reads "California" might be her
    saying it, and her voice would be trained in as a positive.
    """
    import soundfile as sf

    clips = []
    for f in sorted(HER_MONOLOGUE_DIR.glob("*.wav")):
        x, sr = sf.read(str(f), dtype="float32", always_2d=True)
        x = x.mean(axis=1)
        if sr != 24000:
            x = np.interp(np.arange(0, len(x), sr / 24000), np.arange(len(x)), x)
        clips += [x, np.zeros(int(0.6 * 24000), dtype=np.float32)]
    if not clips:
        raise SystemExit(
            f"no monologues in {rel(HER_MONOLOGUE_DIR)} - run: uv run python generate_her_monologues.py"
        )
    return np.concatenate(clips)


def cmd_record(args) -> None:
    import sounddevice as sd

    from core.audio_pipeline import AudioPipeline

    load_dotenv_quietly()
    config = load_config()
    pipeline = AudioPipeline(config)
    if pipeline.sample_rate != SR or pipeline.channels != 1:
        raise SystemExit("audio.sample_rate must be 16000 mono, as the wake word runs")
    device = sd.query_devices(pipeline.device, "input")["name"]
    manifest = Manifest()

    split = "holdout" if args.holdout else "train"
    session = f"{datetime.now():%Y%m%d_%H%M}_{args.lang}_{args.distance}_{args.background}_{split}"
    sdir = SESSIONS_DIR / session
    noisy = args.background in NOISY_BACKGROUNDS

    print(f"\nSession {session}")
    print(f"  mic: {device}   {args.takes} takes to keep   split: {split}")
    if args.hands_free and noisy:
        print("  note: hands-free cuts a take at every sound, so with a background")
        print("  playing expect extra takes; the checks and review sort them out")
    print("  loading checks...")
    transcriber = None if args.no_stt else Transcriber(config)
    speech = SpeechCheck(config)
    scorer = None if args.no_score else Scorer(config)

    her = her_voice() * args.her_volume if args.background == "her" else None

    def background_on():
        if her is not None:
            # A random point each time, or every restart (after a beep or a
            # replay) would put the same opening sentence under the next take.
            sd.play(np.roll(her, -int(np.random.randint(len(her)))), 24000, loop=True)

    if args.background in ("tv", "music"):
        input(f"  Put the {args.background} on at its usual level, then press Enter ")
    background_on()

    kept = rejected = index = 0
    last = None  # (row, audio) of the previous take, for replay / undo

    def process(audio: np.ndarray, style: str | None) -> dict:
        nonlocal kept, rejected, index
        path = sdir / f"take_{index:03d}.wav"
        write_wav(path, audio)
        status, reasons, metrics, transcript = inspect_take(
            audio, floor, noisy=noisy, check_edges=True,
            lang=args.lang, transcriber=transcriber, speech=speech,
        )
        if sha1(audio) in manifest.hashes():
            status, reasons = REJECTED, reasons + ["duplicate"]
        row = {
            "id": f"{session}/{index:03d}",
            "source": "session",
            "session": session,
            "path": rel(path),
            "lang": args.lang,
            "distance": args.distance,
            "background": args.background,
            "style": style,
            "mode": "hands_free" if args.hands_free else "push_to_talk",
            "speaker": args.speaker,
            "device": device,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "split": split,
            "status": status,
            "reasons": reasons,
            "metrics": metrics,
            "transcript": transcript,
            "score": scorer(path) if scorer else None,
            "model": scorer.model if scorer else None,
            "sha1": sha1(audio),
            "note": "",
        }
        manifest.add(row)
        index += 1
        if status == REJECTED:
            rejected += 1
            print(f"      rejected  {describe(row)}  - say it again")
        else:
            kept += 1
            print(f"      {status:<8}  {describe(row)}")
        return row

    def beep(times: int) -> None:
        """Cues for hands-free, where the screen is out of sight. Never recorded:
        every beep is followed by a drain of the mic buffer."""
        t = np.arange(int(0.15 * SR)) / SR
        tone = (0.3 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
        gap = np.zeros(int(0.15 * SR), dtype=np.float32)
        sd.play(np.concatenate([np.concatenate([tone, gap])] * times), SR, blocking=True)
        background_on()

    stream = pipeline.create_mic_stream()
    stream.start()
    chunk = pipeline.chunk_samples
    try:
        if args.hands_free:
            print(f"\n  Walk to your spot. In {args.walk_time:.0f}s:")
            print("    1 beep  = stay silent (room tone)")
            print(f"    2 beeps = start saying \"{args.word}\", ~2s apart, varying how you say it")
            print(f"    3 beeps = done ({args.takes} takes kept), come back")
            time.sleep(args.walk_time)
            beep(1)
            pipeline.drain_mic_stream(stream)

        print(f"\n  Room tone: stay silent for {ROOMTONE_S:.0f}s (leave the background on)")
        tone = read_seconds(stream, ROOMTONE_S, chunk)
        write_wav(sdir / "roomtone.wav", tone)
        floor = float(np.median(frame_rms(tone)))
        print(f"  room level RMS {floor:.0f}\n")

        if args.hands_free:
            print(f"  Hands-free: say \"{args.word}\", pause, say it again. Ctrl+C to stop early.\n")
            cutter = UtteranceCutter(floor, chunk)
            beep(2)
            pipeline.drain_mic_stream(stream)
            while kept < args.takes:
                data, _ = stream.read(chunk)
                take = cutter.feed(np.frombuffer(data, dtype=np.int16))
                if take is not None:
                    print(f"[{kept + 1}/{args.takes}]", end="")
                    process(take, None)
            beep(3)
        else:
            while kept < args.takes:
                style = STYLES[index % len(STYLES)]
                print(f"[{kept + 1}/{args.takes}] say \"{args.word}\" {style}")
                choice = input("      Enter = record   p = replay last   u = undo last   q = quit > ")
                choice = choice.strip().lower()
                if choice == "q":
                    break
                if choice == "p" and last:
                    sd.play(last[1], SR, blocking=True)
                    background_on()
                    continue
                if choice == "u" and last:
                    row = last[0]
                    if row["status"] != REJECTED:
                        kept -= 1
                    row["status"], row["note"] = DROPPED, "undone while recording"
                    manifest.save()
                    print(f"      dropped {row['id']}")
                    last = None
                    continue
                if choice:
                    continue
                pipeline.drain_mic_stream(stream)
                print("      ● recording - Enter to stop", flush=True)
                audio = record_push_to_talk(stream, chunk)
                last = (process(audio, style), audio)
    except KeyboardInterrupt:
        print("\n  stopped; everything recorded so far is in the manifest")
    finally:
        stream.stop()
        stream.close()
        if her is not None:
            sd.stop()

    flagged = sum(1 for r in manifest.rows if r["session"] == session and r["status"] == FLAGGED)
    print(f"\nkept {kept}, rejected {rejected}, flagged for review {flagged}")
    if flagged:
        print("next: uv run python tools/wakeword_dataset.py review")


# ─── review ──────────────────────────────────────────────────────────


def cmd_review(args) -> None:
    import sounddevice as sd

    manifest = Manifest()
    wanted = {FLAGGED}
    if args.all:
        wanted |= {OK}
    if args.rejected:
        wanted |= {REJECTED}
    queue = [
        r for r in manifest.rows
        if r["status"] in wanted and (not args.session or r["session"] == args.session)
        and (not args.new or r["source"] == "session")
    ]
    # New sessions first: the ~200 imported legacy takes would otherwise bury them.
    queue.sort(key=lambda r: r["source"] != "session")
    if not queue:
        print("nothing to review")
        return
    print(f"{len(queue)} take(s). k keep, d drop, r replay, f full window, s skip, q quit.")
    print("Keep only what is clearly you saying the wake word, however it sounds.\n")

    for n, row in enumerate(queue, 1):
        audio = read_wav(ROOT / row["path"])
        word = row["metrics"].get("word")
        if word:
            region = audio[max(0, int((word[0] - REVIEW_CONTEXT_S) * SR)) : int((word[1] + REVIEW_CONTEXT_S) * SR)]
        else:
            region = audio
        print(f"[{n}/{len(queue)}] {row['id']}  {row['status']}  {describe(row)}")
        sd.play(region, SR, blocking=True)
        while True:
            choice = input("   > ").strip().lower()
            if choice == "r":
                sd.play(region, SR, blocking=True)
            elif choice == "f":
                sd.play(audio, SR, blocking=True)
            elif choice in ("k", "d"):
                if choice == "k" and row["lang"] not in LANGS:
                    lang = ""
                    while lang not in LANGS:
                        lang = input("   language, en or pt? ").strip().lower()
                    row["lang"] = lang
                row["status"] = ACCEPTED if choice == "k" else DROPPED
                row["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
                note = input("   note (Enter for none): ").strip()
                if note:
                    row["note"] = note
                manifest.save()
                break
            elif choice == "s":
                break
            elif choice == "q":
                print("stopped; decisions so far are saved")
                return
    print("review done")


# ─── audit (legacy) and import-live ──────────────────────────────────


def cmd_recheck(args) -> None:
    """
    Re-grade recorded takes after the checks change. Nothing is re-recorded.

    Takes a human kept or dropped are never touched. A transcript already on
    the row is reused, so only takes that never got one (they were rejected
    before Whisper ran) cost a call.
    """
    load_dotenv_quietly()
    config = load_config()
    manifest = Manifest()
    transcriber = None if args.no_stt else Transcriber(config)
    speech = SpeechCheck(config)

    changes = Counter()
    for row in manifest.rows:
        if row["source"] != "session" or row["status"] in (ACCEPTED, DROPPED):
            continue
        if args.session and row["session"] != args.session:
            continue
        cached = row.get("transcript")

        def transcribe(audio, lang, cached=cached):
            if cached is not None:
                return cached
            return transcriber(audio, lang) if transcriber is not None else None

        status, reasons, metrics, transcript = inspect_take(
            read_wav(ROOT / row["path"]), row["metrics"]["floor_rms"],
            noisy=row["background"] in NOISY_BACKGROUNDS, check_edges=True,
            lang=row["lang"], transcriber=transcribe, speech=speech,
        )
        if "duplicate" in row["reasons"]:
            status, reasons = REJECTED, reasons + ["duplicate"]
        if status != row["status"]:
            changes[(row["status"], status)] += 1
        row.update(status=status, reasons=reasons, metrics=metrics,
                   transcript=transcript if transcript is not None else cached)
        manifest.save()

    for (before, after), n in sorted(changes.items()):
        print(f"  {before:>8} -> {after:<8} {n}")
    if not changes:
        print("  no take changed")


def cmd_audit(args) -> None:
    load_dotenv_quietly()
    config = load_config()
    manifest = Manifest()
    transcriber = Transcriber(config) if args.stt else None
    speech = SpeechCheck(config)
    scorer = None if args.no_score else Scorer(config)

    added = Counter()
    for dirname, lang, split in LEGACY:
        for path in sorted((REC_DIR / dirname).glob("*.wav")):
            if rel(path) in manifest.paths():
                continue
            audio = read_wav(path)
            # A trimmed clip has no room tone; its quietest frames are the best guess.
            levels = frame_rms(audio)
            floor = max(float(np.percentile(levels, 10)), 5.0) if len(levels) else 5.0
            status, reasons, metrics, transcript = inspect_take(
                audio, floor, noisy=False, check_edges=False,
                lang=lang, transcriber=transcriber, speech=speech,
            )
            if transcriber is None and status == OK:
                status = FLAGGED  # nobody has checked these by ear or by transcript
                reasons = reasons + ["unverified"]
            row = {
                "id": f"legacy_{dirname}/{path.stem}",
                "source": "legacy_v1",
                "session": f"legacy_{dirname}",
                "path": rel(path),
                "lang": lang,
                "distance": "unknown",
                "background": "unknown",
                "style": None,
                "speaker": "miguel",
                "device": None,
                "recorded_at": None,
                "split": split,
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
                "transcript": transcript,
                "score": scorer(path) if scorer else None,
                "model": scorer.model if scorer else None,
                "sha1": sha1(audio),
                "note": "",
            }
            manifest.add(row)
            added[(dirname, status)] += 1
            if status != OK:
                print(f"  {status:<8} {row['id']}  {describe(row)}")

    print()
    for dirname, _, _ in LEGACY:
        counts = {s: added[(dirname, s)] for s in (OK, FLAGGED, REJECTED) if added[(dirname, s)]}
        print(f"  {dirname:<12} {counts or 'nothing new'}")
    print("\nnext: uv run python tools/wakeword_dataset.py review")


def cmd_import_live(args) -> None:
    config = load_config()
    capture = config["wake_word"].get("capture", {}) or {}
    pre_s = float(capture.get("pre_seconds", 2.0))
    manifest = Manifest()
    scorer = None if args.no_score else Scorer(config)

    sources = [
        ("live_ok", sorted((ROOT / "debug" / "activations").glob("*_ok.wav")), pre_s),
        ("live_nearmiss", sorted((ROOT / "debug" / "nearmiss").glob("*.wav")), None),
    ]
    added = Counter()
    for source, paths, keep_s in sources:
        for src in paths:
            take_id = f"{source}/{src.stem}"
            if take_id in manifest.ids():
                continue
            audio = read_wav(src)
            # An ok capture is the pre-roll, then his command. The wake word is in the pre-roll.
            if keep_s:
                audio = audio[: int(keep_s * SR)]
            dest = LIVE_DIR / source / src.name  # copied: debug/ is pruned at max_files
            write_wav(dest, audio)
            levels = frame_rms(audio)
            floor = max(float(np.percentile(levels, 10)), 5.0) if len(levels) else 5.0
            status, reasons, metrics, _ = inspect_take(
                audio, floor, noisy=True, check_edges=False,
                lang="?", transcriber=None, speech=None,
            )
            if status != REJECTED:
                status = FLAGGED  # a live capture only ever enters training by ear
            row = {
                "id": take_id,
                "source": source,
                "session": f"{source}_{src.stem[:8]}",
                "path": rel(dest),
                "lang": "?",
                "distance": "live",
                "background": "live",
                "style": None,
                "speaker": "miguel",
                "device": None,
                "recorded_at": None,
                "split": "train",
                "status": status,
                "reasons": reasons,
                "metrics": metrics,
                "transcript": None,
                "score": scorer(dest) if scorer else None,
                "model": scorer.model if scorer else None,
                "sha1": sha1(audio),
                "note": "",
            }
            manifest.add(row)
            added[(source, status)] += 1
    for key, n in sorted(added.items()):
        print(f"  {key[0]:<14} {key[1]:<8} {n}")
    if not added:
        print("  nothing new")


# ─── summary and export ──────────────────────────────────────────────


def cmd_summary(args) -> None:
    rows = Manifest().rows
    if not rows:
        print("manifest is empty")
        return
    print("status  :", dict(Counter(r["status"] for r in rows)))
    table = defaultdict(Counter)
    for r in rows:
        if r["status"] in EXPORTABLE:
            table[cell(r)][r["split"]] += 1
    print(f"\n  {'cell':<28} {'train':>6} {'holdout':>8}")
    for c in sorted(table):
        print(f"  {c:<28} {table[c]['train']:>6} {table[c]['holdout']:>8}")
    pending = sum(1 for r in rows if r["status"] == FLAGGED)
    if pending:
        print(f"\n  {pending} flagged take(s) still need review")
    for c in holdout_gaps(rows):
        print(f"  no holdout for {c}: record one with --holdout on another day")


def cmd_export(args) -> None:
    manifest = Manifest()
    rows = exportable(manifest.rows)
    problems = leaks(rows)
    if problems:
        for p in problems:
            print("  LEAK:", p)
        raise SystemExit("refusing to export until the manifest is fixed")
    pending = sum(1 for r in manifest.rows if r["status"] == FLAGGED)
    if pending:
        print(f"  note: {pending} flagged take(s) are unreviewed and left out")

    out = EXPORT_DIR / args.name
    if out.exists():
        if not args.force:
            raise SystemExit(f"{rel(out)} exists; pass --force to rebuild it")
        shutil.rmtree(out)

    counts = defaultdict(Counter)
    for r in rows:
        audio = read_wav(ROOT / r["path"])
        name = f"{safe_name(r['id'])}.wav"
        if r["split"] == "train":
            write_wav(out / "train" / name, train_clip(r, audio))
        else:
            write_wav(out / "holdout" / cell(r) / name, audio)
        counts[cell(r)][r["split"]] += 1

    # Room tone from train sessions only: a holdout room must stay unseen.
    train_sessions = {r["session"] for r in rows if r["split"] == "train"}
    for session in sorted(train_sessions):
        tone = SESSIONS_DIR / session / "roomtone.wav"
        if tone.exists():
            (out / "backgrounds").mkdir(parents=True, exist_ok=True)
            shutil.copy2(tone, out / "backgrounds" / f"{session}.wav")

    with open(out / "export.json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "name": args.name,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "train": sorted(r["id"] for r in rows if r["split"] == "train"),
                "holdout": sorted(r["id"] for r in rows if r["split"] == "holdout"),
            },
            fh,
            indent=1,
        )

    print(f"\n  {'cell':<28} {'train':>6} {'holdout':>8}")
    for c in sorted(counts):
        print(f"  {c:<28} {counts[c]['train']:>6} {counts[c]['holdout']:>8}")
    for c in holdout_gaps(manifest.rows):
        print(f"  warning: no holdout for {c}")
    print(f"\nwritten to {rel(out)}/")
    print("\nUpload (check `uvx modal volume ls california-wakeword-data /recordings` first:")
    print("the trainer merges EVERY wav under /recordings, including old uploads):")
    print(f"  uvx modal volume put california-wakeword-data {rel(out / 'train')} /recordings/{args.name}")
    if (out / "backgrounds").exists():
        print(f"  uvx modal volume put california-wakeword-data {rel(out / 'backgrounds')} /backgrounds_{args.name}")
    print("Score a model against the holdout, one cell at a time:")
    print(f"  uv run python tools/score_wakeword.py --dir {rel(out / 'holdout')}/<cell> --framed")


# ─── CLI ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="record one session")
    p.add_argument("--lang", choices=LANGS, required=True)
    p.add_argument("--distance", choices=DISTANCES, required=True)
    p.add_argument("--background", choices=BACKGROUNDS, required=True)
    p.add_argument("--takes", type=int, default=15, help="takes to keep (default 15)")
    p.add_argument("--holdout", action="store_true", help="this whole session is holdout")
    p.add_argument(
        "--hands-free",
        action="store_true",
        help="no keys: one take per utterance, for when you are away from the keyboard",
    )
    p.add_argument(
        "--walk-time",
        type=float,
        default=15.0,
        help="hands-free: seconds to get to your spot before the first beep (default 15)",
    )
    p.add_argument("--word", default="California")
    p.add_argument("--speaker", default="miguel")
    p.add_argument("--her-volume", type=float, default=0.6, help="for --background her")
    p.add_argument("--no-stt", action="store_true", help="skip the Whisper check (takes get flagged)")
    p.add_argument("--no-score", action="store_true", help="skip scoring with the live model")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("review", help="listen to flagged takes")
    p.add_argument("--all", action="store_true", help="also re-check takes that passed")
    p.add_argument("--rejected", action="store_true", help="also offer rejected takes")
    p.add_argument("--session")
    p.add_argument("--new", action="store_true", help="only takes recorded with this tool")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("recheck", help="re-grade recorded takes after the checks change")
    p.add_argument("--session")
    p.add_argument("--no-stt", action="store_true")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("audit", help="import and check the pre-manifest recordings")
    p.add_argument("--stt", action="store_true", help="transcribe them too (~3s per take)")
    p.add_argument("--no-score", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("import-live", help="import her activation captures for review")
    p.add_argument("--no-score", action="store_true")
    p.set_defaults(func=cmd_import_live)

    p = sub.add_parser("summary", help="counts per cell and what is missing")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("export", help="write train / holdout / backgrounds for Modal")
    p.add_argument("--name", required=True, help="e.g. v3")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
