"""
Speech-to-Text — Groq Whisper API (primary) with local fallback.

Converts recorded audio (WAV bytes) to text transcription.
"""

import os
import time
import logging
import string
import tempfile

logger = logging.getLogger(__name__)

# What Whisper reaches for when handed non-speech. A false wake produced "Hmm."
# out of 5.2s of room tone, which read as a real command all the way to the LLM.
# The per-segment probability checks in _reject_reason catch most of these; this
# set is the backstop for the ones that come back looking confident.
#
# Everything here is a complete utterance that is worthless as a command. Do NOT
# add single words that could open a real one — "go" and "stop" are both live
# control_tv actions.
HALLUCINATION_FILLERS = frozenset({
    "",
    "hmm",
    "hm",
    "mhm",
    "uh",
    "um",
    "you",
    "thank you",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "bye",
})


def _is_filler(text: str) -> bool:
    """True if the transcript is a known Whisper non-speech filler."""
    stripped = text.strip().lower().strip(string.punctuation + string.whitespace)
    return stripped in HALLUCINATION_FILLERS


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
            # Defaults so an older config.yaml (or a test dict) still constructs.
            self.max_no_speech_prob = float(
                stt_cfg["groq"].get("max_no_speech_prob", 0.6)
            )
            self.min_avg_logprob = float(stt_cfg["groq"].get("min_avg_logprob", -1.0))
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
        """
        Transcribe using Groq's Whisper API.

        Returns "" for anything that looks like a hallucination, which
        Orchestrator._handle_activation already treats as "drop the turn" — no
        LLM call, no spoken reply. Whisper invents filler out of non-speech, and
        a false wake used to cost a full turn because of it.
        """
        # Groq expects a file-like object with a name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            tmp.write(wav_bytes)
            tmp.flush()
            tmp.seek(0)

            transcription = self.client.audio.transcriptions.create(
                file=(tmp.name, tmp.read()),
                model=self.model,
                language=self.language,
                # verbose_json so no_speech_prob and avg_logprob come back. The
                # old "text" format threw away the only signal Whisper gives
                # about whether it was actually listening to speech.
                response_format="verbose_json",
            )

        text = self._extract_text(transcription).strip()
        if not text:
            return ""

        reason = self._reject_reason(text, transcription)
        if reason:
            logger.info("Discarded transcript (%s): %r", reason, text)
            return ""
        return text

    @staticmethod
    def _extract_text(transcription) -> str:
        """
        Pull the transcript out of whatever the client handed back.

        Groq returns an object for verbose_json, a bare string for "text", and a
        dict on some client versions. A transcription must never fail because the
        response shape moved, so every path ends in something string-like.
        """
        if isinstance(transcription, str):
            return transcription
        if isinstance(transcription, dict):
            return str(transcription.get("text", ""))
        return str(getattr(transcription, "text", "") or "")

    def _reject_reason(self, text: str, transcription) -> str | None:
        """
        Why this transcript should be thrown away, or None to keep it.

        Deliberately fails open: if the segments are missing or shaped
        unexpectedly, only the filler check applies and real speech still gets
        through. A diagnostic field is never worth losing a command over.
        """
        if _is_filler(text):
            return "known non-speech filler"

        try:
            segments = transcription.get("segments") if isinstance(transcription, dict) \
                else getattr(transcription, "segments", None)
            if not segments:
                return None

            worst_no_speech = 0.0
            worst_logprob = 0.0
            for seg in segments:
                get = seg.get if isinstance(seg, dict) else lambda k, d=None: getattr(seg, k, d)
                worst_no_speech = max(worst_no_speech, float(get("no_speech_prob", 0.0) or 0.0))
                worst_logprob = min(worst_logprob, float(get("avg_logprob", 0.0) or 0.0))
        except Exception:
            logger.debug("Could not read Whisper segment stats — keeping transcript")
            return None

        if worst_no_speech > self.max_no_speech_prob:
            return f"no_speech_prob {worst_no_speech:.2f} > {self.max_no_speech_prob}"
        if worst_logprob < self.min_avg_logprob:
            return f"avg_logprob {worst_logprob:.2f} < {self.min_avg_logprob}"
        return None

    def _transcribe_local(self, wav_bytes: bytes) -> str:
        """Transcribe using local Whisper model."""
        raise NotImplementedError("Local STT not yet implemented")
