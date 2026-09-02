"""
Voice Activity Detection — Determines when the user has stopped speaking.

Two engines:
- "energy": Simple RMS-based detection (lightweight, no extra deps)
- "silero": Neural VAD via Silero (requires torch, more accurate in noise)

Recording has two phases, and they are not symmetric:

  1. Pre-speech. From start_recording() until the first real speech, the
     silence timer does not run at all. It only expires against
     `speech_timeout`, which stops with reason "no_speech". This is what
     lets Master Miguel call California and then take a second to think:
     his pause is not an answer, it is a pause.
  2. Post-speech. Once speech has been seen, the old behaviour applies —
     stop after `silence_duration` of quiet, with `min_recording` measured
     from the first speech rather than from the top of the recording.

Before this split, `min_recording` was wall-clock elapsed since
start_recording(), so silence counted toward it and pure room tone stopped
the recording at ~0.9s and went to Whisper as if it were a command.
"""

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)

# Silero VAD accepts exactly this many samples per call at 16kHz (256 at 8kHz),
# and raises ValueError on anything else. The pipeline's chunk is 640 samples
# (40ms), so chunks are re-framed to this size rather than passed through.
SILERO_WINDOW_16K = 512
SILERO_WINDOW_8K = 256


class VAD:
    def __init__(self, config: dict):
        vad_cfg = config["vad"]
        self.engine = vad_cfg["engine"]
        self.energy_threshold = vad_cfg["energy_threshold"]
        self.silence_duration = vad_cfg["silence_duration"]
        self.max_recording = vad_cfg["max_recording"]
        self.min_recording = vad_cfg["min_recording"]
        self.sample_rate = config["audio"]["sample_rate"]

        # Read with defaults: an older config.yaml (or a test dict) that predates
        # these keys still constructs and keeps working.
        self.speech_timeout = float(vad_cfg.get("speech_timeout", 4.0))
        self.speech_start_frames = int(vad_cfg.get("speech_start_frames", 2))
        self.silero_threshold = float(vad_cfg.get("silero_threshold", 0.5))

        # State
        self._silence_start = None
        self._recording_start = None
        self._saw_speech = False
        self._speech_start = None
        self._speech_run = 0
        self._silero_buffer = np.array([], dtype=np.float32)

        # Load Silero if requested
        self._silero_model = None
        if self.engine == "silero":
            self._load_silero()

        logger.info(f"VAD initialized with engine: {self.engine}")

    @property
    def saw_speech(self) -> bool:
        """True once this recording has heard speech, not just room tone."""
        return self._saw_speech

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
        except Exception as exc:
            # Not necessarily torch itself: the silero-vad hub module also needs
            # torchaudio, which only the `silero` extra installs (the `kokoro`
            # extra pulls torch alone). A torch.hub download can also fail on a
            # network blip, which is not an ImportError — catch broadly, because
            # no VAD backend is worth refusing to boot over.
            logger.warning(
                f"Silero VAD unavailable ({exc}) — falling back to energy-based VAD. "
                "Install the full stack with: uv sync --extra silero"
            )
            self.engine = "energy"
            self._silero_model = None

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

    @property
    def _silero_window(self) -> int:
        return SILERO_WINDOW_8K if self.sample_rate == 8000 else SILERO_WINDOW_16K

    def _silero_detect(self, audio_chunk: np.ndarray) -> bool:
        """
        Neural speech detection using Silero VAD.

        Silero rejects any window that is not exactly 512 samples at 16kHz, and
        the pipeline feeds 640, so buffer across calls and run whole windows.
        A leftover partial window is carried into the next chunk.
        """
        import torch

        # Silero expects float32 in [-1, 1] range
        audio_float = audio_chunk.astype(np.float32) / 32768.0
        self._silero_buffer = np.concatenate([self._silero_buffer, audio_float])

        window = self._silero_window
        detected = False
        try:
            while len(self._silero_buffer) >= window:
                frame = self._silero_buffer[:window]
                self._silero_buffer = self._silero_buffer[window:]
                confidence = self._silero_model(
                    torch.from_numpy(frame), self.sample_rate
                ).item()
                if confidence > self.silero_threshold:
                    detected = True
        except Exception:
            # A VAD failure must never take down a turn. Log once, drop to the
            # energy detector for the rest of the run, and answer this chunk
            # with it so the caller sees a normal result.
            logger.exception("Silero VAD failed — falling back to energy VAD for this run")
            self.engine = "energy"
            self._silero_model = None
            self._silero_buffer = np.array([], dtype=np.float32)
            return self._energy_detect(audio_chunk)

        return detected

    def start_recording(self):
        """Call when recording begins."""
        self._recording_start = time.time()
        self._silence_start = None
        self._saw_speech = False
        self._speech_start = None
        self._speech_run = 0
        self._silero_buffer = np.array([], dtype=np.float32)

    def should_stop_recording(self, audio_chunk: np.ndarray) -> tuple[bool, str]:
        """
        Process a chunk during recording. Returns (should_stop, reason).
        Reasons: "silence", "no_speech", "max_duration", "continue"
        """
        now = time.time()
        elapsed = now - (self._recording_start or now)

        # Hard cap on recording duration
        if elapsed >= self.max_recording:
            return True, "max_duration"

        speech_detected = self.is_speech(audio_chunk)

        # ── Pre-speech: waiting for him to start ────────────────────────
        if not self._saw_speech:
            if speech_detected:
                self._speech_run += 1
                # A single loud chunk is a door or a keyboard, not a sentence.
                if self._speech_run >= self.speech_start_frames:
                    self._saw_speech = True
                    self._speech_start = now
                    self._silence_start = None
            else:
                self._speech_run = 0

            if not self._saw_speech:
                if elapsed >= self.speech_timeout:
                    # Nothing was ever said. This is a false wake or an
                    # abandoned one, and the caller drops it without an STT call.
                    return True, "no_speech"
                # Deliberately no silence bookkeeping here: waiting is not an answer.
                return False, "continue"

            return False, "continue"

        # ── Post-speech: normal endpointing ─────────────────────────────
        if speech_detected:
            # Reset silence timer
            self._silence_start = None
            return False, "continue"
        else:
            # Track silence duration
            if self._silence_start is None:
                self._silence_start = now

            silence_elapsed = now - self._silence_start
            speech_elapsed = now - (self._speech_start or now)

            # Only stop if he has spoken enough AND silence is long enough
            if speech_elapsed >= self.min_recording and silence_elapsed >= self.silence_duration:
                return True, "silence"

            return False, "continue"

    def reset(self):
        """Reset VAD state."""
        self._silence_start = None
        self._recording_start = None
        self._saw_speech = False
        self._speech_start = None
        self._speech_run = 0
        self._silero_buffer = np.array([], dtype=np.float32)
        if self._silero_model is not None:
            self._silero_model.reset_states()
