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

# How much text to hand the TTS engine in one synthesis call.
#
# Every call is expensive twice over. Kokoro bakes ~400ms of silence onto the
# front of every clip it returns, so N chunks cost N lead-ins of dead air --
# measured at ~4.7s injected into a single paragraph when this module was
# splitting on every semicolon, colon and dash it could find. And a fragment
# gives the model no sentence-level context to shape prosody from, so it sounds
# worse than the same words inside a whole sentence would.
#
# The first chunk is the deliberate exception: it is the one Master Miguel is
# actually waiting on, so it ships as soon as a sentence exists. Everything
# after it plays behind audio that is already queued, so it can afford to be
# longer. Short first fragment for latency, larger ones after, is the same
# trade every streaming-TTS layer lands on.
MIN_CHUNK_CHARS = 100
FIRST_CHUNK_CHARS = 25

# ...and the first chunk must not be allowed to grow long either. Kokoro runs
# at roughly real time on this hardware, so time-to-first-audio IS the first
# chunk's length: a 150-character opening sentence measured 9.4s of synthesis
# before anything was heard. Below this ceiling, an unfinished first sentence
# is soft-split at a comma and shipped as a fragment; the sanitizer marks it as
# a continuation and the rest follows as a normal chunk. max_chars stays the
# ceiling once something is playing.
FIRST_CHUNK_MAX_CHARS = 60


def chunk_sentences(
    token_stream: Generator[str, None, None],
    *,
    max_chars: int = 240,
    min_chunk_chars: int = MIN_CHUNK_CHARS,
    first_chunk_chars: int = FIRST_CHUNK_CHARS,
    first_chunk_max_chars: int = FIRST_CHUNK_MAX_CHARS,
    prefer_period_breaks: bool = True,
) -> Generator[str, None, None]:
    """
    Accumulate streaming tokens into speakable chunks.

    Sentences are detected as they complete, then held until the chunk is worth
    synthesizing -- see MIN_CHUNK_CHARS for why one sentence per call is a bad
    deal. Short sentences therefore travel together, keeping their own terminal
    punctuation, so "Stremio's up now. Your call." is one call rather than two.

    Args:
        token_stream: Generator yielding text chunks from LLM
        max_chars: If the buffer gets this big without a hard boundary, force a "soft" split
        min_chunk_chars: Hold sentences back until a chunk reaches this length
        first_chunk_chars: Lower bar for the first chunk, which sets perceived latency
        first_chunk_max_chars: Soft-split ceiling for the first chunk, for the same reason
        prefer_period_breaks: Kept for compatibility / future tweaks (currently unused)

    Yields:
        Speakable chunks of one or more complete sentences (or a final fragment)
    """
    buffer = ""          # raw tokens not yet resolved into a sentence
    pending: list[str] = []   # finished sentences waiting for the chunk to fill
    pending_len = 0
    spoke = False        # has anything been yielded yet this response

    def _hold(clean: str) -> bool:
        """Add a finished sentence; report whether the chunk is now big enough."""
        nonlocal pending_len
        pending.append(clean)
        pending_len += len(clean) + 1
        return pending_len >= (min_chunk_chars if spoke else first_chunk_chars)

    def _flush() -> str | None:
        nonlocal pending_len, spoke
        if not pending:
            return None
        chunk = " ".join(pending)
        pending.clear()
        pending_len = 0
        spoke = True
        return chunk

    for token in token_stream:
        buffer += token

        # Try to extract complete sentences from buffer
        while True:
            sentence, remaining = _try_split(buffer)
            if sentence is None:
                # If buffer is huge, force a soft split for low-latency TTS.
                # "Huge" is much smaller before anything has been said.
                ceiling = max_chars if spoke else first_chunk_max_chars
                if len(buffer) >= ceiling:
                    forced, rest = _force_soft_split(buffer)
                    if forced:
                        clean = sanitize_for_tts(forced.strip())
                        buffer = rest
                        if clean and _hold(clean):
                            chunk = _flush()
                            if chunk:
                                logger.debug(f"Sentence chunk: '{chunk}'")
                                yield chunk
                        continue
                break

            clean = sanitize_for_tts(sentence.strip())
            buffer = remaining
            if clean and _hold(clean):
                chunk = _flush()
                if chunk:
                    logger.debug(f"Sentence chunk: '{chunk}'")
                    yield chunk

    # Whatever is left is the end of the response: speak it regardless of length
    if buffer.strip():
        clean = sanitize_for_tts(buffer.strip())
        if clean:
            _hold(clean)

    chunk = _flush()
    if chunk:
        # The sanitizer marks an unterminated fragment with a comma, meaning
        # "more is coming". Nothing is coming: this is the end of the response,
        # so it resolves with a full stop instead of hanging on a rising tone.
        if chunk.endswith(","):
            chunk = chunk[:-1] + "."
        logger.debug(f"Final chunk: '{chunk}'")
        yield chunk


def _try_split(text: str) -> tuple[str | None, str]:
    """
    Try to split off a complete sentence from the beginning of text.
    Returns (sentence, remaining) or (None, original_text) if no split found.
    """
    if len(text) < MIN_SENTENCE_LENGTH:
        return None, text

    # Look for sentence boundaries: . ! ? followed by space or end.
    #
    # Semicolons, colons and dashes are deliberately NOT boundaries any more.
    # They used to be, and it cut one paragraph into eight synthesis calls. The
    # sanitizer softens them to commas, which Kokoro handles fine inside a
    # single call; a chunk that runs long without a full stop is bounded by
    # max_chars and _force_soft_split instead.
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