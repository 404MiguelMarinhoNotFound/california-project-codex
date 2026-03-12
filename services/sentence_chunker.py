"""
Sentence Chunker — Splits streaming LLM tokens into speakable sentences.

This is the glue between the LLM stream and TTS. The LLM yields tokens one at a time,
and we need to accumulate them into complete sentences before sending to TTS.

Why not just wait for the full response?
  → Because streaming sentence-by-sentence cuts perceived latency by 50-70%.
  → User hears the first sentence while the LLM is still generating the rest.
"""

import re
import logging
from typing import Generator

from services.tts_text_sanitizer import sanitize_for_tts

logger = logging.getLogger(__name__)

# Abbreviations that end with a period but are NOT sentence boundaries
ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "ave", "blvd",
    "etc", "vs", "e.g", "i.e", "u.s", "u.k", "a.m", "p.m",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}

# Minimum characters before we consider splitting (avoids splitting on "Dr. ")
MIN_SENTENCE_LENGTH = 8


def chunk_sentences(
    token_stream: Generator[str, None, None],
    *,
    max_chars: int = 240,
    prefer_period_breaks: bool = True,
) -> Generator[str, None, None]:
    """
    Accumulate streaming tokens into complete sentences.

    Yields sentences as soon as a boundary is detected.
    The final fragment (without a sentence-ending punctuation) is yielded at the end.

    Args:
        token_stream: Generator yielding text chunks from LLM
        max_chars: If the buffer gets this big without a hard boundary, force a "soft" split
        prefer_period_breaks: Kept for compatibility / future tweaks (currently unused)

    Yields:
        Complete sentences (or final fragment)
    """
    buffer = ""

    for token in token_stream:
        buffer += token

        # Try to extract complete sentences from buffer
        while True:
            sentence, remaining = _try_split(buffer)
            if sentence is None:
                # If buffer is huge, force a soft split for low-latency TTS
                if len(buffer) >= max_chars:
                    forced, rest = _force_soft_split(buffer)
                    if forced:
                        clean = sanitize_for_tts(forced.strip())
                        if clean:
                            yield clean
                        buffer = rest
                        continue
                break

            clean = sanitize_for_tts(sentence.strip())
            if clean:
                logger.debug(f"Sentence chunk: '{clean}'")
                yield clean
            buffer = remaining

    # Yield any remaining text
    if buffer.strip():
        clean = sanitize_for_tts(buffer.strip())
        if clean:
            logger.debug(f"Final chunk: '{clean}'")
            yield clean


def _try_split(text: str) -> tuple[str | None, str]:
    """
    Try to split off a complete sentence from the beginning of text.
    Returns (sentence, remaining) or (None, original_text) if no split found.
    """
    if len(text) < MIN_SENTENCE_LENGTH:
        return None, text

    # Look for sentence boundaries: . ! ? followed by space or end
    # Also split on semicolons, colons, and em-dashes for natural speech breaks
    for i, char in enumerate(text):
        # Handle ellipsis "..." as a boundary once complete
        # Do this before the general ".!?" handling so it doesn't get swallowed.
        if char == "." and i + 2 < len(text) and text[i: i + 3] == "...":
            if i >= MIN_SENTENCE_LENGTH - 1:
                # If followed by space/newline, split at the ellipsis.
                if i + 3 < len(text) and text[i + 3] in " \n":
                    return text[: i + 3], text[i + 4 :]
                # If ellipsis is at end of current buffer, don't split yet (wait for more tokens).
            continue

        if char in ".!?":
            # Check it's not an abbreviation
            if char == "." and _is_abbreviation(text, i):
                continue

            # Check it's not a decimal number (e.g., "3.5")
            if char == "." and _is_decimal(text, i):
                continue

            # Check there's something after the punctuation (or it's end of text)
            if i + 1 < len(text):
                next_char = text[i + 1]
                # Sentence boundary: punctuation followed by space/newline (and enough text before)
                if next_char in " \n" and i >= MIN_SENTENCE_LENGTH - 1:
                    return text[: i + 1], text[i + 2 :]

            elif i == len(text) - 1 and i >= MIN_SENTENCE_LENGTH - 1:
                # End of buffer with sentence-ending punctuation — might be complete
                # But we wait for more tokens to be sure (next token might be more text)
                # Only yield if this is the final flush (handled by caller)
                pass

        # Split on semicolons if the chunk is long enough
        elif char == ";" and i >= MIN_SENTENCE_LENGTH - 1:
            if i + 1 < len(text) and text[i + 1] == " ":
                return text[: i + 1], text[i + 2 :]

        # NEW: Split on colons (great for "California: ..." style prefixes)
        elif char == ":" and i >= MIN_SENTENCE_LENGTH - 1:
            if i + 1 < len(text) and text[i + 1] == " ":
                return text[: i + 1], text[i + 2 :]

        # Split on em-dash / dash for natural speech breaks (only when followed by space)
        elif char in "—-" and i >= MIN_SENTENCE_LENGTH - 1:
            if i + 1 < len(text) and text[i + 1] == " ":
                return text[: i + 1], text[i + 2 :]

    return None, text


def _force_soft_split(text: str) -> tuple[str | None, str]:
    """
    Force a split when the buffer is too long (for low-latency TTS).
    Prefer splitting at a comma/colon/semicolon/em-dash near the end of the chunk.
    """
    if not text:
        return None, ""

    # Prefer breaking on these punctuation marks (natural speech pauses)
    break_chars = [",", ":", ";", "—", "-"]

    # Search a window near the end so we don't cut too early
    start = max(0, int(len(text) * 0.55))
    end = min(len(text), int(len(text) * 0.90))

    best_idx = -1

    # 1) Try punctuation breaks (prefer ones followed by whitespace)
    for i in range(end, start, -1):
        ch = text[i - 1]
        if ch in break_chars:
            if i < len(text) and text[i] in " \n":
                best_idx = i
                break

    # 2) Fallback: split at last space in the window
    if best_idx == -1:
        for i in range(end, start, -1):
            if text[i - 1] == " ":
                best_idx = i
                break

    # 3) Absolute fallback: cut to a safe-ish length
    if best_idx == -1:
        best_idx = min(len(text), 200)

    left = text[:best_idx].strip()
    right = text[best_idx:].lstrip()
    return (left if left else None), right


def _is_abbreviation(text: str, period_index: int) -> bool:
    """Check if a period is part of an abbreviation."""
    # Look backwards to find the word before the period
    start = period_index - 1
    while start >= 0 and text[start].isalpha():
        start -= 1
    start += 1

    word = text[start:period_index].lower()
    if word in ABBREVIATIONS:
        return True

    # Check for patterns like "U.S." or "e.g."
    if period_index >= 2 and text[period_index - 2] == ".":
        return True

    # Single letter followed by period (likely initial: "J. K. Rowling")
    if len(word) == 1 and word.isalpha():
        return True

    return False


def _is_decimal(text: str, period_index: int) -> bool:
    """Check if a period is a decimal point (e.g., '3.5')."""
    if period_index > 0 and period_index < len(text) - 1:
        return text[period_index - 1].isdigit() and text[period_index + 1].isdigit()
    return False