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
        sd.play(data, sr, blocking=blocking)
        # A blocking call has already finished by the time we return, so it
        # leaves no overlap for the caller to gate against.
        duration = 0.0 if blocking else len(data) / float(sr)
        return ActivationPlayback(name=name, text=text, duration=duration, blocking=blocking)

    def play_error_sound(self):
        """Play error indication sound."""
        if self._error_data is not None:
            sd.play(self._error_data, self._error_sr, blocking=True)

    def play_audio(self, audio_data: np.ndarray, sample_rate: int, blocking: bool = True):
        """Play arbitrary audio data through speakers."""
        sd.play(audio_data, sample_rate, blocking=blocking)

    def stop_playback(self):
        """Stop any currently playing audio (for barge-in)."""
        sd.stop()
