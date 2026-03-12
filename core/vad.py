"""
Voice Activity Detection — Determines when the user has stopped speaking.

Two engines:
- "energy": Simple RMS-based detection (lightweight, no extra deps)
- "silero": Neural VAD via Silero (requires torch, more accurate in noise)
"""

import logging
import numpy as np

logger = logging.getLogger(__name__)


class VAD:
    def __init__(self, config: dict):
        vad_cfg = config["vad"]
        self.engine = vad_cfg["engine"]
        self.energy_threshold = vad_cfg["energy_threshold"]
        self.silence_duration = vad_cfg["silence_duration"]
        self.max_recording = vad_cfg["max_recording"]
        self.min_recording = vad_cfg["min_recording"]
        self.sample_rate = config["audio"]["sample_rate"]

        # State
        self._silence_start = None
        self._recording_start = None

        # Load Silero if requested
        self._silero_model = None
        if self.engine == "silero":
            self._load_silero()

        logger.info(f"VAD initialized with engine: {self.engine}")

    def _load_silero(self):
        """Load Silero VAD model."""
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            self._silero_model = model
            self._silero_get_speech = utils[0]
            logger.info("Silero VAD loaded successfully")
        except ImportError:
            logger.warning("torch not installed — falling back to energy-based VAD")
            self.engine = "energy"

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """
        Determine if an audio chunk contains speech.
        audio_chunk: int16 numpy array
        """
        if self.engine == "silero" and self._silero_model is not None:
            return self._silero_detect(audio_chunk)
        else:
            return self._energy_detect(audio_chunk)

    def _energy_detect(self, audio_chunk: np.ndarray) -> bool:
        """Simple RMS energy-based speech detection."""
        if len(audio_chunk) == 0:
            return False
        rms = np.sqrt(np.mean(audio_chunk.astype(np.float32) ** 2))
        return rms > self.energy_threshold

    def _silero_detect(self, audio_chunk: np.ndarray) -> bool:
        """Neural speech detection using Silero VAD."""
        import torch
        # Silero expects float32 in [-1, 1] range
        audio_float = audio_chunk.astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio_float)
        confidence = self._silero_model(tensor, self.sample_rate).item()
        return confidence > 0.5

    def start_recording(self):
        """Call when recording begins."""
        import time
        self._recording_start = time.time()
        self._silence_start = None

    def should_stop_recording(self, audio_chunk: np.ndarray) -> tuple[bool, str]:
        """
        Process a chunk during recording. Returns (should_stop, reason).
        Reasons: "silence", "max_duration", "continue"
        """
        import time
        now = time.time()
        elapsed = now - (self._recording_start or now)

        # Hard cap on recording duration
        if elapsed >= self.max_recording:
            return True, "max_duration"

        speech_detected = self.is_speech(audio_chunk)

        if speech_detected:
            # Reset silence timer
            self._silence_start = None
            return False, "continue"
        else:
            # Track silence duration
            if self._silence_start is None:
                self._silence_start = now

            silence_elapsed = now - self._silence_start

            # Only stop if we've recorded enough AND silence is long enough
            if elapsed >= self.min_recording and silence_elapsed >= self.silence_duration:
                return True, "silence"

            return False, "continue"

    def reset(self):
        """Reset VAD state."""
        self._silence_start = None
        self._recording_start = None
        if self._silero_model is not None:
            self._silero_model.reset_states()
