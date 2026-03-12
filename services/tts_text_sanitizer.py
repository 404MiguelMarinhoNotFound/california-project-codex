"""
TTS Text Sanitizer — Cleans text chunks before they hit Kokoro TTS.

Kokoro (StyleTTS2-based) treats punctuation as literal prosody cues.
Em dashes, ellipsis, trailing periods, and stacked punctuation all cause
multi-second pauses in the generated audio.  This module replaces or
strips those patterns so the TTS output sounds natural.
"""

import re

# Markdown-style formatting that should never reach TTS
_MD_BOLD_ITALIC = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_MD_INLINE_CODE = re.compile(r"`(.+?)`")

# Stacked / heavy punctuation
_ELLIPSIS = re.compile(r"\.{2,}")          # ".." or "..." or more
_MULTI_PUNCT = re.compile(r"[,;:]{2,}")    # ",," or ";;" etc.
_MULTI_SPACE = re.compile(r"\s{2,}")


def sanitize_for_tts(text: str) -> str:
    """
    Clean a single sentence chunk for TTS consumption.

    Call this on every chunk *before* passing to the TTS engine.
    The goal is to remove or soften any punctuation that Kokoro
    interprets as a long prosodic break.
    """
    if not text:
        return ""

    # --- Strip markdown artifacts ---
    text = _MD_BOLD_ITALIC.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)

    # --- Replace em dashes / en dashes with comma ---
    text = text.replace("—", ",")   # em dash
    text = text.replace("–", ",")   # en dash
    # Spaced hyphen used as dash: " - " -> ", "
    text = re.sub(r"\s+-\s+", ", ", text)

    # --- Collapse ellipsis to comma ---
    text = _ELLIPSIS.sub(",", text)

    # --- Soften semicolons and colons mid-sentence to commas ---
    text = text.replace(";", ",")
    text = text.replace(":", ",")

    # --- Collapse stacked commas ---
    text = _MULTI_PUNCT.sub(",", text)
    text = re.sub(r"(,\s*)+", ", ", text)

    # --- Strip trailing sentence-ending punctuation ---
    # The chunker already determined the boundary; trailing . ! ? just
    # makes Kokoro generate silence at the tail.
    text = text.rstrip(" .!?;:,")

    # --- Clean up whitespace ---
    text = _MULTI_SPACE.sub(" ", text).strip()

    return text
