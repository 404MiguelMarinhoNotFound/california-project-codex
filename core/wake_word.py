"""
Wake Word Detection — openWakeWord + Porcupine (.ppn) support.

Continuously processes audio chunks and detects the configured wake word.

Backends:
- .ppn  → Picovoice Porcupine (high accuracy, needs PICOVOICE_ACCESS_KEY in .env)
- .onnx → openWakeWord custom model
- name  → openWakeWord pre-built model (hey_jarvis, alexa, etc.)
"""

import os
import time
import logging
import numpy as np

logger = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, config: dict):
        ww_cfg = config["wake_word"]
        self.threshold = ww_cfg["threshold"]
        self.consecutive_required = ww_cfg["consecutive_frames"]
        self.debounce_seconds = ww_cfg["debounce_seconds"]

        model_spec = ww_cfg["model"]
        self.model_name = model_spec

        # State tracking (shared by both backends)
        self._consecutive_count = 0
        self._last_activation_time = 0.0
        self._enabled = True

        # ── Porcupine (.ppn) ────────────────────────────────────────
        if model_spec.endswith(".ppn"):
            self._backend = "porcupine"
            self._init_porcupine(model_spec, ww_cfg)

        # ── openWakeWord (.onnx or built-in name) ───────────────────
        else:
            self._backend = "oww"
            self._init_oww(model_spec)

    # ─── Init helpers ────────────────────────────────────────────────

    def _init_porcupine(self, model_path: str, ww_cfg: dict):
        """Load a Porcupine .ppn wake word model."""
        try:
            import pvporcupine
        except ImportError:
            raise RuntimeError(
                "pvporcupine not installed. Run: pip install pvporcupine"
            )

        access_key = os.environ.get("PICOVOICE_ACCESS_KEY", "")
        if not access_key:
            raise RuntimeError(
                "PICOVOICE_ACCESS_KEY is not set. "
                "Get a free key at https://console.picovoice.ai/ and add it to your .env"
            )

        sensitivity = ww_cfg.get("sensitivity", self.threshold)

        logger.info(f"Loading Porcupine wake word model: {model_path}")
        self._porcupine = pvporcupine.create(
            access_key=access_key,
            keyword_paths=[model_path],
            sensitivities=[sensitivity],
        )
        # Porcupine requires exactly frame_length samples of int16 per call
        self._ppn_frame_length = self._porcupine.frame_length
        self._ppn_buffer = np.array([], dtype=np.int16)
        logger.info(
            f"Porcupine ready — frame_length={self._ppn_frame_length}, "
            f"sample_rate={self._porcupine.sample_rate}"
        )

    def _init_oww(self, model_spec: str):
        """Load an openWakeWord model (built-in name or .onnx path)."""
        from openwakeword.model import Model as OWWModel

        if model_spec.endswith(".onnx"):
            logger.info(f"Loading custom openWakeWord model: {model_spec}")
        else:
            logger.info(f"Loading pre-built openWakeWord model: {model_spec}")

        self._oww_model = OWWModel(wakeword_models=[model_spec])

        model_keys = list(self._oww_model.models.keys())
        if not model_keys:
            raise ValueError(f"No wake word models loaded. Check model name: {model_spec}")
        self.primary_key = model_keys[0]
        logger.info(f"Wake word model key: {self.primary_key}")

    # ─── Audio processing ────────────────────────────────────────────

    def process_audio(self, audio_chunk: np.ndarray) -> bool:
        """
        Feed an audio chunk (int16 numpy array) to the detector.
        Returns True if the wake word was detected (with debouncing).
        """
        if not self._enabled:
            return False

        if self._backend == "porcupine":
            return self._process_porcupine(audio_chunk)
        else:
            return self._process_oww(audio_chunk)

    def _process_porcupine(self, audio_chunk: np.ndarray) -> bool:
        """
        Porcupine needs exactly frame_length int16 samples per call.
        Buffer incoming chunks and process as many full frames as available.
        """
        self._ppn_buffer = np.concatenate([self._ppn_buffer, audio_chunk.astype(np.int16)])

        detected = False
        while len(self._ppn_buffer) >= self._ppn_frame_length:
            frame = self._ppn_buffer[:self._ppn_frame_length]
            self._ppn_buffer = self._ppn_buffer[self._ppn_frame_length:]

            result = self._porcupine.process(frame)
            if result >= 0:  # keyword index ≥ 0 means detected
                detected = True

        if detected:
            return self._check_debounce(score=1.0)
        return False

    def _process_oww(self, audio_chunk: np.ndarray) -> bool:
        """openWakeWord scoring with consecutive-frame logic."""
        prediction = self._oww_model.predict(audio_chunk)
        score = prediction.get(self.primary_key, 0.0)

        if score >= self.threshold:
            self._consecutive_count += 1
        else:
            self._consecutive_count = 0

        if self._consecutive_count >= self.consecutive_required:
            return self._check_debounce(score=score)
        return False

    def _check_debounce(self, score: float) -> bool:
        """Shared debounce + logging logic. Returns True if activation is valid."""
        now = time.time()
        if now - self._last_activation_time < self.debounce_seconds:
            self._consecutive_count = 0
            return False

        logger.info(f"Wake word detected! Score: {score:.3f}, backend: {self._backend}")
        self._last_activation_time = now
        self._consecutive_count = 0

        if self._backend == "oww":
            self._oww_model.reset()
        return True

    # ─── Control ─────────────────────────────────────────────────────

    def enable(self):
        """Enable wake word detection."""
        self._enabled = True

    def disable(self):
        """Temporarily disable wake word detection (e.g., during TTS playback)."""
        self._enabled = False

    def reset(self):
        """Reset detector state."""
        self._consecutive_count = 0
        if self._backend == "oww":
            self._oww_model.reset()
        elif self._backend == "porcupine":
            self._ppn_buffer = np.array([], dtype=np.int16)

    def __del__(self):
        """Clean up Porcupine resources."""
        if getattr(self, "_backend", None) == "porcupine":
            try:
                self._porcupine.delete()
            except Exception:
                pass
