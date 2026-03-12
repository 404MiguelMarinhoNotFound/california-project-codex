"""
Audio Pipeline — Microphone capture and speaker playback.

Handles:
- Continuous mic streaming (16kHz mono int16)
- Recording to WAV buffer
- Audio playback via sounddevice
- Chime/sound effect playback
"""

import io
import wave
import struct
import math
import os
import random
import logging
import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)


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
        self._activation_cache = []
        self._load_sounds(sounds_cfg)

    def _load_sounds(self, sounds_cfg: dict):
        """Load or generate activation/error sounds."""
        chime_path = sounds_cfg.get("activation", "sounds/chime.wav")
        error_path = sounds_cfg.get("error", "sounds/error.wav")
        activation_dir = sounds_cfg.get("activation_dir", "sounds/california_activations")
        generate = sounds_cfg.get("generate_if_missing", True)

        # Load randomized activation sounds from directory if it exists
        if os.path.isdir(activation_dir):
            for f in sorted(os.listdir(activation_dir)):
                if f.endswith(".wav"):
                    path = os.path.join(activation_dir, f)
                    data, sr = sf.read(path, dtype="float32")
                    self._activation_cache.append((data, sr))
            logger.info(f"Loaded {len(self._activation_cache)} activation sound(s) from '{activation_dir}'")

        # Fallback: single chime file
        if not self._activation_cache:
            if not os.path.exists(chime_path) and generate:
                self._generate_chime(chime_path)
            if os.path.exists(chime_path):
                self._chime_data, self._chime_sr = sf.read(chime_path, dtype="float32")

        if not os.path.exists(error_path) and generate:
            self._generate_error_sound(error_path)
        if os.path.exists(error_path):
            self._error_data, self._error_sr = sf.read(error_path, dtype="float32")

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

    def play_activation_sound(self):
        """Play a random activation sound from the cache, or fall back to the chime."""
        if self._activation_cache:
            data, sr = random.choice(self._activation_cache)
            sd.play(data, sr, blocking=True)
        elif self._chime_data is not None:
            sd.play(self._chime_data, self._chime_sr, blocking=True)

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
