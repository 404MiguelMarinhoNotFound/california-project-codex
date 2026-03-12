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
        logger.debug("TTSService.__init__ start")
        tts_cfg = config["tts"]
        self.provider = tts_cfg["provider"]
        logger.debug(f"TTSService.__init__ provider={self.provider}")

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

        logger.debug("TTSService.__init__ done")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """
        Convert text to speech audio.
        Returns (audio_data as float32 numpy array, sample_rate).
        """
        logger.debug(f"synthesize start provider={self.provider} text_len={len(text) if text is not None else 'None'}")

        if not (text or "").strip():
            logger.debug("synthesize empty text -> empty audio, sr=22050")
            return np.array([], dtype=np.float32), 22050

        start = time.time()

        try:
            if self.provider == "edge":
                logger.debug("synthesize dispatch -> _synthesize_edge")
                audio, sr = self._synthesize_edge(text)

            elif self.provider == "piper":
                logger.debug("synthesize dispatch -> _synthesize_piper")
                audio, sr = self._synthesize_piper(text)

            elif self.provider == "elevenlabs":
                logger.debug("synthesize dispatch -> _synthesize_elevenlabs")
                audio, sr = self._synthesize_elevenlabs(text)

            elif self.provider == "kokoro":
                logger.debug("synthesize dispatch -> _synthesize_kokoro")
                audio, sr = self._synthesize_kokoro(text)

            else:
                raise ValueError(f"Unknown TTS provider: {self.provider}")

        except Exception as e:
            logger.exception(f"synthesize exception provider={self.provider}: {e}")
            raise

        dt = time.time() - start
        logger.debug(
            f"synthesize done provider={self.provider} seconds={dt:.3f} "
            f"audio_type={type(audio).__name__} sr={sr} audio_len={getattr(audio, 'shape', None)}"
        )

        # Guard rails so you never return None accidentally
        if audio is None or sr is None:
            logger.error("synthesize produced None audio or sr; returning empty audio as fallback")
            return np.array([], dtype=np.float32), 22050

        return audio, sr

    # ─── Edge TTS ───────────────────────────────────────────────────

    def _synthesize_edge(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize using Microsoft Edge TTS (free, cloud-based)."""
        logger.debug(f"_synthesize_edge text_len={len(text)}")
        import edge_tts

        loop = asyncio.new_event_loop()
        try:
            audio_bytes = loop.run_until_complete(self._edge_generate(text))
        finally:
            loop.close()

        logger.debug(f"_synthesize_edge audio_bytes_len={len(audio_bytes) if audio_bytes else 0}")
        if not audio_bytes:
            return np.array([], dtype=np.float32), 24000

        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        logger.debug(f"_synthesize_edge decoded sr={sr} audio_shape={audio.shape}")
        return audio, sr

    async def _edge_generate(self, text: str) -> bytes:
        """Async edge-tts generation."""
        logger.debug(f"_edge_generate text_len={len(text)}")
        import edge_tts

        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)

        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        out = b"".join(audio_chunks)
        logger.debug(f"_edge_generate chunks={len(audio_chunks)} bytes={len(out)}")
        return out

    # ─── Piper TTS ──────────────────────────────────────────────────

    def _check_piper(self):
        """Load the piper-tts Python API and model directly (no CLI needed)."""
        logger.debug("_check_piper start")
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
        logger.debug("_check_piper done")

    def _synthesize_piper(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize using Piper TTS Python API (no subprocess, no PATH issues)."""
        logger.debug(f"_synthesize_piper text_len={len(text)} piper_speed={getattr(self, 'piper_speed', None)}")
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
        logger.debug(f"_synthesize_piper decoded sr={sr} audio_shape={audio.shape}")
        return audio, sr

    # ─── Kokoro TTS ─────────────────────────────────────────────────

    def _init_kokoro(self):
        """Load the Kokoro pipeline."""
        logger.debug("_init_kokoro start")
        try:
            from kokoro import KPipeline
        except ImportError:
            raise RuntimeError(
                "kokoro package not found. Install it with: pip install kokoro"
            )
        logger.info(
            f"Loading Kokoro pipeline (lang_code='{self.kokoro_lang_code}', voice='{self.kokoro_voice}')"
        )
        self._kokoro_pipeline = KPipeline(lang_code=self.kokoro_lang_code)
        logger.debug("_init_kokoro done")

    def _synthesize_kokoro(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize using Kokoro TTS (local, high quality)."""
        logger.debug(f"_synthesize_kokoro text_len={len(text)} voice={self.kokoro_voice} speed={self.kokoro_speed}")

        # Text arrives pre-sanitized from the sentence chunker.
        # Feed it directly to the pipeline — no re-splitting needed.
        generator = self._kokoro_pipeline(
            text,
            voice=self.kokoro_voice,
            speed=self.kokoro_speed,
        )

        parts: list[np.ndarray] = []
        try:
            for item in generator:
                logger.debug(f"_synthesize_kokoro generator item type={type(item).__name__}")
                try:
                    a = item[2]
                except Exception:
                    logger.exception(f"_synthesize_kokoro unexpected generator item: {item!r}")
                    raise
                parts.append(a)
        except Exception as e:
            logger.exception(f"_synthesize_kokoro generator failed: {e}")
            raise

        if not parts:
            logger.debug("_synthesize_kokoro no parts -> empty")
            return np.array([], dtype=np.float32), 24000

        audio = np.concatenate(parts).astype(np.float32)

        # Trim trailing silence — Kokoro bakes in long pauses after punctuation
        audio = self._trim_trailing_silence(audio, sample_rate=24000)

        logger.debug(f"_synthesize_kokoro final audio_shape={audio.shape}")
        return audio, 24000

    def _trim_trailing_silence(
        self,
        audio: np.ndarray,
        sample_rate: int = 24000,
        threshold: float = 0.01,
        max_tail_ms: int = 200,
    ) -> np.ndarray:
        """
        Trim excessive trailing silence from TTS audio.

        Keeps at most `max_tail_ms` of silence at the end.
        Silence = samples whose absolute amplitude < `threshold`.
        """
        if audio.size == 0:
            return audio

        abs_audio = np.abs(audio)

        # Find last sample above threshold
        above = np.where(abs_audio > threshold)[0]
        if above.size == 0:
            # Entire clip is silence — return a tiny silent buffer
            keep = int(sample_rate * (max_tail_ms / 1000.0))
            return audio[:min(keep, audio.size)]

        last_voice = above[-1]

        # Allow max_tail_ms of silence after the last voiced sample
        tail_samples = int(sample_rate * (max_tail_ms / 1000.0))
        cut_at = min(last_voice + tail_samples, audio.size)

        if cut_at < audio.size:
            trimmed = audio.size - cut_at
            logger.debug(
                f"_trim_trailing_silence trimmed {trimmed} samples "
                f"({trimmed / sample_rate * 1000:.0f}ms)"
            )

        return audio[:cut_at]

    def _silence(self, sr: int, ms: int) -> np.ndarray:
        logger.debug(f"_silence sr={sr} ms={ms}")
        n = int(sr * (ms / 1000.0))
        return np.zeros(n, dtype=np.float32)

    def _concat_with_gaps(self, chunks: list[np.ndarray], sr: int, gap_ms: int) -> np.ndarray:
        logger.debug(f"_concat_with_gaps chunks={len(chunks)} sr={sr} gap_ms={gap_ms}")
        if not chunks:
            return np.array([], dtype=np.float32)
        out: list[np.ndarray] = []
        for i, ch in enumerate(chunks):
            if ch is None:
                logger.error(f"_concat_with_gaps got None chunk at index {i}")
                continue
            out.append(ch.astype(np.float32))
            if i < len(chunks) - 1 and gap_ms > 0:
                out.append(self._silence(sr=sr, ms=gap_ms))
        if not out:
            return np.array([], dtype=np.float32)
        merged = np.concatenate(out).astype(np.float32)
        logger.debug(f"_concat_with_gaps merged_shape={merged.shape}")
        return merged

    # ─── ElevenLabs ─────────────────────────────────────────────────

    def _init_elevenlabs(self):
        """Initialize ElevenLabs client."""
        logger.debug("_init_elevenlabs start")
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
            logger.debug("_init_elevenlabs client ok")
        except ImportError:
            logger.warning("elevenlabs package not installed — falling back to edge TTS")
            self.provider = "edge"
            self.voice = "en-US-AriaNeural"
            self.rate = "+0%"

        logger.debug("_init_elevenlabs done")

    def _synthesize_elevenlabs(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize using ElevenLabs API."""
        logger.debug(f"_synthesize_elevenlabs text_len={len(text)} model={getattr(self, 'el_model', None)}")
        audio_generator = self.el_client.generate(
            text=text,
            voice=self.voice_id,
            model=self.el_model,
        )

        audio_bytes = b"".join(audio_generator)
        logger.debug(f"_synthesize_elevenlabs audio_bytes_len={len(audio_bytes) if audio_bytes else 0}")

        if not audio_bytes:
            return np.array([], dtype=np.float32), 24000

        audio, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        logger.debug(f"_synthesize_elevenlabs decoded sr={sr} audio_shape={audio.shape}")
        return audio, sr