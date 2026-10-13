"""
Loudness on one 0..1 scale, for the live UI's halo.

Her voice never plays in the browser -- it goes out through `sounddevice` on
the laptop -- so the page cannot measure it. This module turns a clip into an
envelope (one value per 1/hz s) that travels with the clip's `clip_start`
event, and a mic chunk into one level for the `mic` event.

dBFS, not raw RMS: speech RMS spans about 30 dB, and a linear scale leaves
every quiet syllable near zero so the ring only moves on the loudest words.
Measured in this project: room tone ~-76 dBFS (0), her voice through the mic
~-37 dBFS (~0.46), his ~-32 dBFS (~0.56); TTS output is near full scale.

Pure numpy, no I/O, no logging, never raises: this runs on the hot path.
"""

import numpy as np

# The scale. Calibrate both against real Chirp clips on the laptop (plan step 9)
# rather than guessing: a ceiling too low pins her TTS at 1 and the ring stops
# moving; a floor too high hides quiet syllables.
DB_FLOOR = -60.0     # dBFS at or below which the level is 0
DB_CEIL = -10.0      # dBFS at or above which the level is 1


def _to_float(audio) -> np.ndarray:
    """int16 -> float32 in [-1, 1]; float passes through; stereo -> mono mean."""
    data = np.asarray(audio)
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype != np.float32:
        data = data.astype(np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1, dtype=np.float32)
    return data.reshape(-1)


def _db_to_level(rms: np.ndarray) -> np.ndarray:
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    return np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0, 1.0)


def envelope(audio, sample_rate: int, hz: int = 25) -> list[float]:
    """Loudness per 1/hz s window, 0..1, rounded to 2 decimals.

    `ceil(len / sample_rate * hz)` values; the last window is zero-padded.
    Never raises: anything unusable returns [].
    """
    try:
        if audio is None or not sample_rate or not hz:
            return []
        data = _to_float(audio)
        n = data.size
        if n == 0:
            return []
        win = max(1, int(round(sample_rate / float(hz))))
        # Integer ceiling: n * hz / sample_rate, rounded up, with no float error.
        count = -(-n * int(hz) // int(sample_rate))
        padded = np.zeros(count * win, dtype=np.float32)
        take = min(n, padded.size)
        padded[:take] = data[:take]
        frames = padded.reshape(count, win)
        rms = np.sqrt(np.mean(np.square(frames), axis=1))
        return np.round(_db_to_level(rms.astype(np.float64)), 2).tolist()
    except Exception:
        return []


def level(chunk) -> float:
    """One mic chunk's loudness on the same 0..1 scale. Never raises."""
    try:
        if chunk is None:
            return 0.0
        data = _to_float(chunk)
        if data.size == 0:
            return 0.0
        rms = np.sqrt(np.mean(np.square(data, dtype=np.float64)))
        return round(float(_db_to_level(np.asarray(rms))), 2)
    except Exception:
        return 0.0
