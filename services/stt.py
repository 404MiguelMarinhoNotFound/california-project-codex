"""
Speech-to-Text — Groq Whisper API (primary) with local fallback.

Converts recorded audio (WAV bytes) to text transcription.
"""

import os
import time
import logging
import tempfile

logger = logging.getLogger(__name__)


class STTService:
    def __init__(self, config: dict):
        stt_cfg = config["stt"]
        self.provider = stt_cfg["provider"]

        if self.provider == "groq":
            from groq import Groq
            api_key = os.environ.get("GROQ_API_KEY")
            if not api_key:
                raise ValueError("GROQ_API_KEY not set in environment")
            self.client = Groq(api_key=api_key)
            self.model = stt_cfg["groq"]["model"]
            self.language = stt_cfg["groq"]["language"]
            logger.info(f"STT initialized: Groq ({self.model})")

        elif self.provider == "local":
            # Placeholder for whisper.cpp or faster-whisper
            raise NotImplementedError("Local STT not yet implemented. Use 'groq' provider.")

    def transcribe(self, wav_bytes: bytes) -> str:
        """
        Transcribe audio to text.
        wav_bytes: Complete WAV file as bytes (with header).
        Returns: Transcribed text string.
        """
        start = time.time()

        if self.provider == "groq":
            result = self._transcribe_groq(wav_bytes)
        else:
            result = self._transcribe_local(wav_bytes)

        elapsed = time.time() - start
        logger.info(f"STT completed in {elapsed:.2f}s: '{result[:80]}...' " if len(result) > 80
                     else f"STT completed in {elapsed:.2f}s: '{result}'")
        return result

    def _transcribe_groq(self, wav_bytes: bytes) -> str:
        """Transcribe using Groq's Whisper API."""
        # Groq expects a file-like object with a name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            tmp.write(wav_bytes)
            tmp.flush()
            tmp.seek(0)

            transcription = self.client.audio.transcriptions.create(
                file=(tmp.name, tmp.read()),
                model=self.model,
                language=self.language,
                response_format="text",
            )

        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        return text

    def _transcribe_local(self, wav_bytes: bytes) -> str:
        """Transcribe using local Whisper model."""
        raise NotImplementedError("Local STT not yet implemented")
