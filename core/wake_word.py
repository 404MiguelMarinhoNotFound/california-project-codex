"""
Wake Word Detection — openWakeWord + Porcupine (.ppn) support.

Continuously processes audio chunks and detects the configured wake word.

Backends:
- .ppn  → Picovoice Porcupine (high accuracy, needs PICOVOICE_ACCESS_KEY in .env)
- .onnx → openWakeWord custom model. A livekit-wakeword export also loads here:
          it has the same `embeddings (batch, 16, 96)` -> `score (batch, 1)`
          contract, and openWakeWord reads the input name off the model rather
          than hardcoding it, so no separate backend is needed. See
          training/README.md
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
            self._init_oww(model_spec, ww_cfg)

    # ─── Init helpers ────────────────────────────────────────────────

    def _init_porcupine(self, model_path: str, ww_cfg: dict):
        """Load a Porcupine .ppn wake word model."""
        try:
            import pvporcupine
        except ImportError:
            raise RuntimeError(
                "pvporcupine not installed. Run: uv sync --extra porcupine"
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

    @staticmethod
    def _ensure_oww_models(model_spec: str, framework: str):
        """
        Make sure openWakeWord's model files are on disk before loading them.

        openWakeWord ships no models in its wheel: they are downloaded at runtime
        into site-packages/openwakeword/resources/models/. Nothing in the package
        RECORD covers them, so any `uv sync` that reinstalls openwakeword deletes
        them again and startup dies with onnxruntime NO_SUCHFILE. Re-fetch them
        whenever they are missing so the environment is self-healing.
        """
        import os

        import openwakeword
        from openwakeword.utils import download_models

        ext = "onnx" if framework == "onnx" else "tflite"
        res_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")

        # Feature extractors are always required, plus the wake word model itself
        # unless the user pointed at their own .onnx elsewhere on disk.
        required = [f"melspectrogram.{ext}", f"embedding_model.{ext}"]
        if not model_spec.endswith((".onnx", ".tflite")):
            required.append(f"{model_spec}.{ext}")

        missing = [f for f in required if not os.path.exists(os.path.join(res_dir, f))]
        if not missing:
            return

        logger.info(
            f"openWakeWord model files missing ({', '.join(missing)}) - downloading. "
            "This happens on first run and after any uv sync that reinstalls openwakeword."
        )
        os.makedirs(res_dir, exist_ok=True)
        download_models()
        logger.info("openWakeWord models downloaded")

    def _init_oww(self, model_spec: str, ww_cfg: dict):
        """Load an openWakeWord model (built-in name or .onnx path)."""
        from openwakeword.model import Model as OWWModel

        if model_spec.endswith(".onnx"):
            logger.info(f"Loading custom openWakeWord model: {model_spec}")
        else:
            logger.info(f"Loading pre-built openWakeWord model: {model_spec}")

        # openWakeWord defaults to the tflite runtime, but `tflite-runtime` has no
        # Windows wheels. onnxruntime is already pulled in by openwakeword itself and
        # works on Windows, Linux, and the Pi, so it is the portable default here.
        framework = ww_cfg.get("inference_framework", "onnx")

        self._ensure_oww_models(model_spec, framework)

        try:
            self._oww_model = OWWModel(
                wakeword_models=[model_spec], inference_framework=framework
            )
        except ValueError as exc:
            # Most often: the model files were never downloaded post-install.
            raise RuntimeError(
                f"openWakeWord failed to load '{model_spec}' with the "
                f"'{framework}' runtime ({exc}). If the model files are missing, run: "
                "uv run python -c \"import openwakeword.utils as u; u.download_models()\""
            ) from exc

        model_keys = list(self._oww_model.models.keys())
        if not model_keys:
            raise ValueError(f"No wake word models loaded. Check model name: {model_spec}")
        self.primary_key = model_keys[0]

        # openWakeWord only computes a new embedding every 1280 samples: see
        # openwakeword/utils.py::AudioFeatures._streaming_features, which gates on
        # `accumulated_samples >= 1280 and accumulated_samples % 1280 == 0`. Feed it
        # anything smaller and Model.predict takes its `n_prepared_samples < 1280`
        # branch and returns the PREVIOUS prediction verbatim. With this project's
        # 40ms/640-sample chunks that made the score stream 0, S1, S1, S2, S2, ...
        # so every real detection produced two identical frames above threshold and
        # `consecutive_frames: 2` was silently equivalent to 1 — the false-positive
        # defence was off. Buffer to the native frame so a "frame" is one inference.
        self._oww_frame_length = 1280
        self._oww_buffer = np.array([], dtype=np.int16)

        # Raise the noise floor before scoring. See _apply_dither: without this,
        # the quietest moments in the room are the ones that score highest.
        self._dither_rms = float(ww_cfg.get("dither_rms", 0.0))
        self._dither_rng = np.random.default_rng()
        if self._dither_rms:
            logger.info(f"Wake-word dither floor: RMS {self._dither_rms:g}")

        logger.info(f"Wake word model key: {self.primary_key}")

    def _apply_dither(self, frame: np.ndarray) -> np.ndarray:
        """
        Add an inaudible noise floor to one native frame before scoring it.

        Master Miguel's Realtek mic array gates near-silence down to RMS ~5 while
        leaving its spectral structure intact, and openWakeWord's log-mel front end
        turns that structured near-nothing into features the model has never seen:
        every training clip has real background mixed in at an audible level. The
        model does not fail quietly on it, it fails confidently. Measured over 150s
        of the real room, the highest-scoring frames were the QUIETEST ones (RMS 3-9
        across their whole 1.3s context) while the loudest events in the same
        recording (RMS ~500) scored low. Worst ambient score 0.8005 against a 0.81
        threshold — 0.0095 of headroom, which is what the false wakes were.

        Adding white noise at RMS 10 takes that worst ambient score to 0.032 and
        drops frames over 0.5 from 14 to 0. It is not a threshold workaround: it
        removes the artefact, which is why it works at every threshold rather than
        just pushing the same overlap around. Flat digital silence and flat white
        noise both score ~0.002, so the trigger is specifically gated near-silence,
        not low energy as such.

        Only the wake path is dithered. _record_speech reads the mic separately, so
        Whisper still gets clean audio, and the capture ring stays raw so recorded
        negatives can be re-scored honestly.

        RMS 20 and 40 were also measured and start costing real recall. 0 disables
        this exactly, for a one-line revert.
        """
        if not self._dither_rms:
            return frame
        noisy = frame.astype(np.float32) + self._dither_rng.normal(
            0.0, self._dither_rms, len(frame)
        )
        # A loud frame plus noise overflows int16 without this.
        return np.clip(noisy, -32768, 32767).astype(np.int16)

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
        """
        openWakeWord scoring with consecutive-frame logic.

        Buffers to openWakeWord's native 1280-sample step (see _init_oww) so each
        counted frame is a distinct inference. `consecutive_frames: 2` therefore
        means ~160ms of sustained detection, which is what it always claimed to mean.
        """
        self._oww_buffer = np.concatenate([self._oww_buffer, audio_chunk.astype(np.int16)])

        while len(self._oww_buffer) >= self._oww_frame_length:
            frame = self._oww_buffer[:self._oww_frame_length]
            self._oww_buffer = self._oww_buffer[self._oww_frame_length:]

            # Dither the frame handed to the model, never the buffer: framing and
            # the remainder carried to the next call must stay byte-identical.
            prediction = self._oww_model.predict(self._apply_dither(frame))
            score = prediction.get(self.primary_key, 0.0)

            if score >= self.threshold:
                self._consecutive_count += 1
            else:
                self._consecutive_count = 0

            if self._consecutive_count >= self.consecutive_required:
                # Whatever is left in the buffer is the start of his command,
                # not wake word, and the recorder owns the mic from here.
                self._oww_buffer = np.array([], dtype=np.int16)
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
            self._oww_buffer = np.array([], dtype=np.int16)
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
            self._oww_buffer = np.array([], dtype=np.int16)
        elif self._backend == "porcupine":
            self._ppn_buffer = np.array([], dtype=np.int16)

    def __del__(self):
        """Clean up Porcupine resources."""
        if getattr(self, "_backend", None) == "porcupine":
            try:
                self._porcupine.delete()
            except Exception:
                pass
