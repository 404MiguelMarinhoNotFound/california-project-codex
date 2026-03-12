    """
    Text-to-Speech — Multiple provider support.

    Providers:
    - "edge":       Microsoft Edge TTS (free, good quality, needs internet)
    - "piper":      Piper TTS (free, local, fast on Linux/Pi)
    - "elevenlabs": ElevenLabs (premium quality, paid)
    - "kokoro":     Kokoro TTS (free, local, high quality)

    All providers implement the same interface:
    synthesize(text) -> (audio_data: np.ndarray, sample_rate: int)
    """

    import os
    import io
    import time
    import asyncio
    import logging
    import tempfile
    import numpy as np
    import soundfile as sf

    logger = logging.getLogger(__name__)


    class TTSService:
        def __init__(self, config: dict):
            tts_cfg = config["tts"]
            self.provider = tts_cfg["provider"]

            if self.provider == "edge":
                self.voice = tts_cfg["edge"]["voice"]
                self.rate = tts_cfg["edge"]["rate"]
                logger.info(f"TTS initialized: Edge ({self.voice})")

            elif self.provider == "piper":
                self.piper_model = tts_cfg["piper"]["model"]
                self.piper_speed = tts_cfg["piper"].get("speed", 1.0)
                self._check_piper()
                logger.info(f"TTS initialized: Piper ({self.piper_model})")

            elif self.provider == "elevenlabs":
                self.voice_id = tts_cfg["elevenlabs"]["voice_id"]
                self.el_model = tts_cfg["elevenlabs"]["model"]
                self._init_elevenlabs()
                logger.info(f"TTS initialized: ElevenLabs ({self.voice_id})")

            elif self.provider == "kokoro":
                kokoro_cfg = tts_cfg["kokoro"]
                self.kokoro_voice = kokoro_cfg.get("voice", "bm_george")
                self.kokoro_speed = kokoro_cfg.get("speed", 1.0)
                self.kokoro_lang_code = kokoro_cfg.get("lang_code", "b")
                self._init_kokoro()
                logger.info(f"TTS initialized: Kokoro ({self.kokoro_voice})")

            else:
                raise ValueError(f"Unknown TTS provider: {self.provider}")

        def synthesize(self, text: str) -> tuple[np.ndarray, int]:
            """
            Convert text to speech audio.
            Returns (audio_data as float32 numpy array, sample_rate).
            """
            if not text.strip():
                return np.array([], dtype=np.float32), 22050

            start = time.time()

            if self.provider == "edge":
                audio, sr = self._synthesize_edge(text)
            elif self.provider == "piper":
                audio, sr = self._synthesize_piper(text)
            elif self.provider == "elevenlabs":
                audio, sr = self._synthesize_elevenlabs(text)
            elif self.provider == "kokoro":
                audio, sr = self._synthesize_kokoro(text)

            elapsed = time.time() - start
            duration = len(audio) / sr if sr > 0 and len(audio) > 0 else 0
            logger.debug(f"TTS: {elapsed:.2f}s to generate {duration:.1f}s of audio "
                        f"(RTF: {elapsed/duration:.2f}x)" if duration > 0 else
                        f"TTS: {elapsed:.2f}s (empty audio)")

            return audio, sr

        # ─── Edge TTS ───────────────────────────────────────────────────

        def _synthesize_edge(self, text: str) -> tuple[np.ndarray, int]:
            """Synthesize using Microsoft Edge TTS (free, cloud-based)."""
            import edge_tts

            # edge_tts is async — always create a fresh event loop so this works
            # correctly on background threads (Python 3.10+ has no default loop on threads)
            loop = asyncio.new_event_loop()
            try:
                audio_bytes = loop.run_until_complete(self._edge_generate(text))
            finally:
                loop.close()

            if not audio_bytes:
                return np.array([], dtype=np.float32), 24000

            # Edge TTS returns MP3 — decode to numpy
            audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
            return audio, sr

        async def _edge_generate(self, text: str) -> bytes:
            """Async edge-tts generation."""
            import edge_tts

            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)

            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])

            return b"".join(audio_chunks)

        # ─── Piper TTS ──────────────────────────────────────────────────

        def _check_piper(self):
            """Load the piper-tts Python API and model directly (no CLI needed)."""
            try:
                from piper import PiperVoice
            except ImportError:
                raise RuntimeError(
                    "piper-tts Python package not found. "
                    "Install it with: pip install piper-tts"
                )

            logger.info(f"Loading Piper model: {self.piper_model}")
            self._piper_voice = PiperVoice.load(self.piper_model)
            logger.info(f"TTS initialized: Piper ({self.piper_model})")

        def _synthesize_piper(self, text: str) -> tuple[np.ndarray, int]:
            """Synthesize using Piper TTS Python API (no subprocess, no PATH issues)."""
            import wave
            from piper.voice import SynthesisConfig

            syn_config = SynthesisConfig(
                length_scale=1.0 / self.piper_speed if self.piper_speed != 1.0 else 1.0,
            )

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav_file:
                self._piper_voice.synthesize_wav(text, wav_file, syn_config=syn_config)

            buf.seek(0)
            audio, sr = sf.read(buf, dtype="float32")
            return audio, sr

        # ─── Kokoro TTS ─────────────────────────────────────────────────

        def _init_kokoro(self):
            """Load the Kokoro pipeline."""
            try:
                from kokoro import KPipeline
            except ImportError:
                raise RuntimeError(
                    "kokoro package not found. Install it with: pip install kokoro"
                )
            logger.info(f"Loading Kokoro pipeline (lang_code='{self.kokoro_lang_code}', voice='{self.kokoro_voice}')")
            self._kokoro_pipeline = KPipeline(lang_code=self.kokoro_lang_code)

        def _synthesize_kokoro(self, text: str) -> tuple[np.ndarray, int]:
            """Synthesize using Kokoro TTS (local, high quality)."""
            generator = self._kokoro_pipeline(
                text,
                voice=self.kokoro_voice,
                speed=self.kokoro_speed,
                split_pattern=r'\n+',
            )
            audio_chunks = [audio for _, _, audio in generator]
            if not audio_chunks:
                return np.array([], dtype=np.float32), 24000
            audio = np.concatenate(audio_chunks).astype(np.float32)
            return audio, 24000  # Kokoro outputs at 24kHz

        # ─── ElevenLabs ─────────────────────────────────────────────────

        def _init_elevenlabs(self):
            """Initialize ElevenLabs client."""
            api_key = os.environ.get("ELEVENLABS_API_KEY")
            if not api_key:
                logger.warning("ELEVENLABS_API_KEY not set — falling back to edge TTS")
                self.provider = "edge"
                self.voice = "en-US-AriaNeural"
                self.rate = "+0%"
                return

            try:
                from elevenlabs.client import ElevenLabs
                self.el_client = ElevenLabs(api_key=api_key)
            except ImportError:
                logger.warning("elevenlabs package not installed — falling back to edge TTS")
                self.provider = "edge"
                self.voice = "en-US-AriaNeural"
                self.rate = "+0%"

        def _synthesize_elevenlabs(self, text: str) -> tuple[np.ndarray, int]:
            """Synthesize using ElevenLabs API."""
            audio_generator = self.el_client.generate(
                text=text,
                voice=self.voice_id,
                model=self.el_model,
            )

            # Collect audio bytes
            audio_bytes = b"".join(audio_generator)

            if not audio_bytes:
                return np.array([], dtype=np.float32), 24000

            audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
            return audio, sr
