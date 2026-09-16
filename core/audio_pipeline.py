"""
Audio Pipeline — Microphone capture and speaker playback.

Handles:
- Continuous mic streaming (16kHz mono int16)
- Recording to WAV buffer
- Audio playback via sounddevice
- Chime/sound effect playback
"""

import io
import json
import wave
import struct
import math
import os
import random
import logging
import threading
from typing import NamedTuple

import numpy as np
import sounddevice as sd
import soundfile as sf

from services.activation_phrases import COLD, TIERS, WARM

logger = logging.getLogger(__name__)


class ActivationPlayback(NamedTuple):
    """What `play_activation_sound` just started playing."""

    name: str
    text: str
    duration: float
    blocking: bool


class AudioPipeline:
    def __init__(self, config: dict):
        audio_cfg = config["audio"]
        self.sample_rate = audio_cfg["sample_rate"]
        self.channels = audio_cfg["channels"]
        self.chunk_ms = audio_cfg["chunk_duration_ms"]
        self.chunk_samples = int(self.sample_rate * self.chunk_ms / 1000)
        self.device = audio_cfg.get("device")

        # Pre-load sound effects
        sounds_cfg = config.get("sounds", {})
        self._chime_data = None
        self._error_data = None
        # One pool per tier: the long personality lines on the first wake of a
        # run, the short ones every wake after. See services/activation_phrases.
        self._activation_pools = {tier: [] for tier in TIERS}
        self._activation_blocking = bool(sounds_cfg.get("activation_blocking", False))
        self._load_sounds(sounds_cfg)

        # Per-turn speaker session. See open_speaker.
        self._speaker = None
        self._speaker_sr = 0
        self._speaker_dead = False
        self._speaker_tail_s = float(sounds_cfg.get("speaker_tail_ms", 500)) / 1000.0
        self._abort = threading.Event()
        self._speaker_lock = threading.Lock()

    def _load_sounds(self, sounds_cfg: dict):
        """Load or generate activation/error sounds."""
        chime_path = sounds_cfg.get("activation", "sounds/chime.wav")
        error_path = sounds_cfg.get("error", "sounds/error.wav")
        activation_dir = sounds_cfg.get("activation_dir", "sounds/california_activations")
        generate = sounds_cfg.get("generate_if_missing", True)

        # Load randomized activation sounds. Preferred layout is one subdirectory
        # per tier (cold/ and warm/); a flat directory is the pre-tier layout and
        # is loaded into both pools so an older generated set keeps working.
        if os.path.isdir(activation_dir):
            manifest = self._load_manifest(activation_dir)
            for tier in TIERS:
                tier_dir = os.path.join(activation_dir, tier)
                if os.path.isdir(tier_dir):
                    self._activation_pools[tier] = self._load_pool(
                        tier_dir, manifest.get(tier, {})
                    )
            if not any(self._activation_pools.values()):
                flat = self._load_pool(activation_dir, manifest.get(WARM, {}))
                for tier in TIERS:
                    self._activation_pools[tier] = flat
                if flat:
                    logger.info(
                        "Activation sounds in '%s' are untiered — using all %d for both "
                        "tiers. Re-run generate_activation_phrases.py for cold/warm split.",
                        activation_dir, len(flat),
                    )
            else:
                logger.info(
                    "Loaded activation sounds: %d cold, %d warm from '%s'",
                    len(self._activation_pools[COLD]),
                    len(self._activation_pools[WARM]),
                    activation_dir,
                )

        # Fallback: single chime file
        if not any(self._activation_pools.values()):
            if not os.path.exists(chime_path) and generate:
                self._generate_chime(chime_path)
            if os.path.exists(chime_path):
                self._chime_data, self._chime_sr = sf.read(chime_path, dtype="float32")

        if not os.path.exists(error_path) and generate:
            self._generate_error_sound(error_path)
        if os.path.exists(error_path):
            self._error_data, self._error_sr = sf.read(error_path, dtype="float32")

    @staticmethod
    def _load_manifest(activation_dir: str) -> dict:
        """
        Read the phrase text written by generate_activation_phrases.py.

        The text is what `strip_activation_echo` matches against when a line
        bleeds into the recording, so a missing manifest costs echo cleanup but
        nothing else.
        """
        path = os.path.join(activation_dir, "manifest.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("Could not read activation manifest %s: %s", path, exc)
            return {}

    @staticmethod
    def _load_pool(directory: str, texts: dict) -> list:
        """Load every WAV in `directory` into (name, text, data, sample_rate)."""
        pool = []
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".wav"):
                continue
            name = filename[: -len(".wav")]
            try:
                data, sr = sf.read(os.path.join(directory, filename), dtype="float32")
            except Exception as exc:
                logger.warning("Skipping unreadable activation sound %s: %s", filename, exc)
                continue
            pool.append((name, texts.get(name, ""), data, sr))
        return pool

    def _generate_chime(self, path: str):
        """Generate a pleasant two-tone chime."""
        sr = 22050
        duration = 0.3
        t = np.linspace(0, duration, int(sr * duration), False)

        # Two ascending tones (C5 + E5)
        tone1 = 0.4 * np.sin(2 * math.pi * 523.25 * t) * np.exp(-4 * t)
        tone2 = 0.4 * np.sin(2 * math.pi * 659.25 * t) * np.exp(-3 * t)

        # Offset the second tone slightly
        chime = np.zeros(int(sr * 0.5))
        chime[: len(tone1)] += tone1
        offset = int(sr * 0.12)
        chime[offset : offset + len(tone2)] += tone2

        # Fade out
        fade_len = int(sr * 0.05)
        chime[-fade_len:] *= np.linspace(1, 0, fade_len)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        sf.write(path, chime.astype(np.float32), sr)

    def _generate_error_sound(self, path: str):
        """Generate a low error buzz."""
        sr = 22050
        duration = 0.4
        t = np.linspace(0, duration, int(sr * duration), False)
        tone = 0.3 * np.sin(2 * math.pi * 220 * t) * np.exp(-3 * t)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        sf.write(path, tone.astype(np.float32), sr)

    def create_mic_stream(self):
        """
        Create a raw input stream from the microphone.
        Returns a sounddevice.RawInputStream that yields int16 chunks.
        """
        return sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self.chunk_samples,
            dtype="int16",
            channels=self.channels,
            device=self.device,
        )

    def drain_mic_stream(self, mic_stream, max_seconds: float = 30.0) -> int:
        """
        Throw away everything sitting in the mic's buffer. Returns frames dropped.

        The stream is opened once at boot and never stopped, so it keeps
        capturing whenever nobody is reading it — through the bootup sound,
        through a blocking activation line, and through the whole LLM and TTS
        response. `RawInputStream.read()` hands back the OLDEST buffered frames,
        so without this the next read replays California's own voice out of the
        buffer instead of listening to the room. That is not a theoretical
        concern: it is why a blocking activation line was still being recorded
        and transcribed, and why her own spoken reply was being fed straight
        back into the wake-word detector.

        Call this at every point where the orchestrator has been away from the
        microphone and is about to start listening again.
        """
        limit = int(max_seconds * self.sample_rate)
        dropped = 0
        # New audio keeps arriving while draining, but reads drain far faster
        # than real time, so this converges. `limit` is a safety net, not a
        # working part.
        while dropped < limit:
            available = mic_stream.read_available
            if available <= 0:
                break
            frames = min(available, limit - dropped)
            mic_stream.read(frames)
            dropped += frames
        return dropped

    def bytes_to_numpy(self, audio_bytes: bytes) -> np.ndarray:
        """Convert raw int16 bytes to numpy array."""
        return np.frombuffer(audio_bytes, dtype=np.int16)

    def numpy_to_wav_bytes(self, audio: np.ndarray) -> bytes:
        """Convert numpy int16 array to WAV file bytes (for sending to STT APIs)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()

    def play_activation_sound(
        self,
        tier: str = WARM,
        blocking: bool | None = None,
    ) -> ActivationPlayback | None:
        """
        Start a random activation sound from `tier`'s pool, or the chime.

        Non-blocking by default, which is the point: the caller starts recording
        immediately instead of waiting out the line, so Master Miguel can talk
        straight over it. The returned duration tells the caller how long the
        microphone will be hearing this through the speaker.

        `blocking=True` waits for the line to finish, for callers that record on
        their own without an EchoGate to drop the bleed — the manual test modes
        in main.py do this. `sounds.activation_blocking: true` sets it globally.
        """
        # Fall back to whichever pool has content: a half-generated set should
        # still say something rather than drop to the bare chime.
        pool = self._activation_pools.get(tier) or next(
            (candidate for candidate in self._activation_pools.values() if candidate), None
        )
        if pool:
            name, text, data, sr = random.choice(pool)
        elif self._chime_data is not None:
            name, text, data, sr = "chime", "", self._chime_data, self._chime_sr
        else:
            return None

        blocking = self._activation_blocking if blocking is None else blocking
        if blocking:
            # Through the turn's speaker session when one is open, so the
            # clip's 20ms lead-in is not the first thing a freshly opened
            # device hears.
            self.play_audio(data, sr, blocking=True)
        else:
            sd.play(data, sr, blocking=False)
        # A blocking call has already finished by the time we return, so it
        # leaves no overlap for the caller to gate against.
        duration = 0.0 if blocking else len(data) / float(sr)
        return ActivationPlayback(name=name, text=text, duration=duration, blocking=blocking)

    def play_error_sound(self):
        """Play error indication sound."""
        if self._error_data is not None:
            self.play_audio(self._error_data, self._error_sr, blocking=True)

    # ─── Speaker session ─────────────────────────────────────────────

    # One write per this many milliseconds. It bounds how long a stop takes
    # to land (one block, plus whatever PortAudio already holds).
    SPEAKER_BLOCK_MS = 40

    def open_speaker(self, sample_rate: int = 24000) -> None:
        """
        Open the speaker for a turn and hold it open.

        `sd.play` opens and closes a fresh PortAudio stream per clip. On this
        laptop (MME, `latency='high'`) that is ~180ms of primed silence plus
        the device open before every sentence — a hole between chunks, and a
        device opening right in front of an activation clip trimmed to a 20ms
        lead-in, which is how the first syllable of "Sup." went missing. One
        stream per turn, opened at the wake and closed after the last word,
        removes both. It is deliberately not process-lifetime: the device is
        released whenever she is idle.

        Measured on the real device: the very first open of the process is
        ~2s (driver init; `run()` pays it during the bootup sound), every open
        after that ~200ms. That is the same cost `sd.play` paid in front of
        the acknowledgement clip, so the ack is no later than before and the
        reply no longer pays it per sentence.

        Idempotent at the same rate. A different rate reopens (the 22050 Hz
        chime/error fallbacks; every pre-rendered clip is 24 kHz mono), and so
        does a stream a stop has aborted (see stop_playback). A failure to
        open is logged and leaves `play_audio` on its `sd.play` fallback, so a
        speaker problem never costs a turn.
        """
        with self._speaker_lock:
            if (
                self._speaker is not None
                and self._speaker_sr == sample_rate
                and not self._speaker_dead
            ):
                return
            self._close_speaker_locked(tail_s=0.0)
            try:
                stream = sd.OutputStream(
                    samplerate=sample_rate, channels=1, dtype="float32"
                )
                stream.start()
            except Exception:
                logger.exception("Could not open the speaker at %d Hz", sample_rate)
                return
            self._speaker = stream
            self._speaker_sr = sample_rate
            self._speaker_dead = False
            logger.debug("Speaker open at %d Hz", sample_rate)

    def close_speaker(self, tail_s: float | None = None) -> None:
        """
        Close the turn's speaker session.

        Writes `tail_s` of silence first (default `sounds.speaker_tail_ms`) so
        the last real samples are out of the device before it closes and the
        amp has nothing to gate mid-word. No-op when nothing is open. Never
        raises.
        """
        with self._speaker_lock:
            self._close_speaker_locked(
                self._speaker_tail_s if tail_s is None else tail_s
            )

    def _close_speaker_locked(self, tail_s: float) -> None:
        stream = self._speaker
        if stream is None:
            return
        self._speaker = None
        sr = self._speaker_sr
        self._speaker_sr = 0
        dead = self._speaker_dead
        self._speaker_dead = False
        if not dead:
            # An aborted stream cannot be written to or restarted on MME
            # ("cannot perform this operation while media data is still
            # playing"), and there is nothing left in it to flush anyway.
            try:
                if tail_s > 0:
                    stream.write(np.zeros(int(sr * tail_s), dtype=np.float32))
                stream.stop()
            except Exception:
                logger.debug("Speaker tail/stop failed on close", exc_info=True)
        try:
            stream.close()
        except Exception:
            logger.debug("Speaker close failed", exc_info=True)
        logger.debug("Speaker closed")

    @property
    def speaker_open(self) -> bool:
        return self._speaker is not None

    def play_audio(self, audio_data: np.ndarray, sample_rate: int, blocking: bool = True):
        """
        Play arbitrary audio data through speakers.

        With a speaker session open (and `blocking`), the clip is written into
        it in `SPEAKER_BLOCK_MS` blocks, checking for `stop_playback` between
        blocks. Otherwise it is one `sd.play`, as before — the manual test
        modes in main.py never open a session.
        """
        if not blocking or self._speaker is None:
            sd.play(audio_data, sample_rate, blocking=blocking)
            return

        # The abort flag is NOT cleared here. Once a stop lands it stays in
        # force until the next turn calls reset_playback(), so a chunk the
        # player thread had already dequeued when the stop came in is dropped
        # rather than played in full. Clearing per clip lost that race.
        if self._abort.is_set():
            return

        if sample_rate != self._speaker_sr or self._speaker_dead:
            # A stop aborted the last stream; MME will not restart it. A fresh
            # one is ~200ms, paid once per interrupt.
            self.open_speaker(sample_rate)
            if self._speaker is None:
                sd.play(audio_data, sample_rate, blocking=True)
                return

        stream = self._speaker
        data = self._as_mono_float32(audio_data)
        if data.size == 0:
            return

        try:
            block = max(1, sample_rate * self.SPEAKER_BLOCK_MS // 1000)
            for start in range(0, data.size, block):
                if self._abort.is_set():
                    logger.debug("Playback stopped %.2fs in", start / sample_rate)
                    return
                stream.write(data[start : start + block])
        except Exception:
            if self._abort.is_set():
                # The stop aborted the stream mid-write. Expected.
                return
            logger.exception("Speaker write failed; falling back to sd.play")
            sd.play(audio_data, sample_rate, blocking=True)

    @staticmethod
    def _as_mono_float32(audio_data: np.ndarray) -> np.ndarray:
        data = np.asarray(audio_data)
        if data.dtype == np.int16:
            data = data.astype(np.float32) / 32768.0
        elif data.dtype != np.float32:
            data = data.astype(np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1, dtype=np.float32)
        return np.ascontiguousarray(data)

    def reset_playback(self):
        """Arm the speaker for a new turn: clear any stop still in force."""
        self._abort.clear()

    def stop_playback(self):
        """
        Stop whatever is playing, now, and keep it stopped.

        Sets the abort flag the `play_audio` write loop checks, and aborts the
        open stream so the ~180ms PortAudio already holds is discarded rather
        than played out. Safe from any thread. The aborted stream is marked
        dead: on MME it can neither be written to nor restarted afterwards
        (measured: `start()` fails with "media data is still playing"), so the
        next play opens a fresh one. Playback stays refused until
        `reset_playback` (the start of the next turn) — see `play_audio`.
        """
        self._abort.set()
        stream = self._speaker
        if stream is not None:
            self._speaker_dead = True
            try:
                stream.abort()
            except Exception:
                logger.debug("Speaker abort failed", exc_info=True)
        sd.stop()
