"""
TTS Text Sanitizer — Cleans text chunks before they hit Kokoro TTS.

Kokoro (StyleTTS2-based) treats punctuation as a literal prosody cue, and that
cuts both ways. Em dashes, ellipsis and stacked punctuation buy long pauses
nobody asked for, so this module softens them.

Terminal . ! ? are the opposite case and MUST survive. In StyleTTS2 the
duration predictor learns phrase-final behaviour from them -- they are what
drops the pitch and closes the clause. This module used to strip them on the
theory that they were "just silence at the tail", which left every chunk on a
rising, unresolved contour: prosodically "I am not finished" over text that
was. That is what made long replies sound like they were about to stop and
then carried on.
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
    text = re.sub(r"\s+,", ",", text)       # "things , first" from a spaced dash
    text = re.sub(r"(,\s*)+", ", ", text)

    # --- Clean up whitespace ---
    text = _MULTI_SPACE.sub(" ", text).strip()

    # --- Resolve the chunk's final punctuation ---
    # A terminal . ! ? is prosody and is kept exactly as it arrived.
    #
    # Anything else trailing is a split artefact rather than intent: a chunk cut
    # at a separator arrives with the separator still attached, and a soft split
    # mid-sentence arrives bare. Both become a single comma, which Kokoro reads
    # as "more is coming" instead of trailing off.
    #
    # The dash case is the one that bit: the chunker split on " - " before this
    # module ran, so the fragment ended with an unmatched "-" that the
    # `\s+-\s+` rule above could not see (no trailing space to match).
    if text and text[-1] not in ".!?":
        text = text.rstrip(" ,;:—–-")
        if text:
            text += ","

    return text
