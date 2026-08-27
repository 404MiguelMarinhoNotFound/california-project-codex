"""
Activation phrase tiers and echo cleanup.

Two concerns live here, both deliberately free of audio dependencies so the
tests can exercise them without sounddevice or a working PortAudio:

1. **Tiers.** The wake acknowledgement is split into two pools. The first wake
   of a run plays a `cold` line — the long, personality-heavy ones that make
   California feel like she booted up. Every wake after that plays a `warm`
   line, which is one or two words. The long lines are only charming on first
   contact; on the fifth command in a row they are a toll booth.

2. **Echo cleanup.** Playback no longer blocks the microphone (see
   `AudioPipeline.play_activation_sound`), so the recording starts while
   California is still talking and her voice bleeds into it through the
   speaker. `core.orchestrator._record_speech` drops the bleed in the audio
   domain, which handles almost all of it. `strip_activation_echo` is the
   safety net for whatever survives the trim and reaches Whisper as a prefix,
   e.g. "call lights on" after "Was wondering when you'd call."

   It is deliberately conservative: a match of fewer than `min_tokens` words is
   ignored, because plenty of real commands open with the same single word as a
   short line ("Go." vs "go home", which is a real control_tv action).
"""

import string

COLD = "cold"
WARM = "warm"
TIERS = (COLD, WARM)

# Below this many matched words, a leading phrase match is treated as the user
# talking rather than as speaker bleed. Single-word overlaps collide with real
# commands too often to strip safely.
MIN_ECHO_TOKENS = 2

_PUNCTUATION = str.maketrans("", "", string.punctuation + "‘’“”–—")


def resolve_tier(wake_count: int) -> str:
    """
    Pick the pool for this activation.

    `wake_count` is how many wakes have already happened this run, so the very
    first one (0) is cold and everything after is warm.
    """
    return COLD if wake_count <= 0 else WARM


def _normalize(word: str) -> str:
    return word.translate(_PUNCTUATION).casefold().strip()


def strip_activation_echo(
    transcript: str,
    phrase: str,
    min_tokens: int = MIN_ECHO_TOKENS,
) -> str:
    """
    Remove a leading echo of `phrase` from `transcript`.

    Matches the longest *suffix* of the phrase that prefixes the transcript,
    because the audio trim usually clips the start of the line and Whisper only
    hears its tail. Returns `transcript` unchanged when nothing matches, and ""
    when the transcript was nothing but echo (the caller already treats an empty
    transcription as "go back to idle").
    """
    if not transcript or not phrase:
        return transcript

    raw = transcript.split()
    normalized = [_normalize(word) for word in raw]
    # Keep the mapping back to `raw` so the returned text preserves the original
    # casing and punctuation of whatever we did not strip.
    positions = [i for i, word in enumerate(normalized) if word]
    said = [normalized[i] for i in positions]
    spoken = [word for word in (_normalize(w) for w in phrase.split()) if word]

    if not said or not spoken:
        return transcript

    # Longest window first: start=0 is the whole phrase, and each step drops one
    # leading word to allow for a line that got clipped at the front.
    for start in range(len(spoken)):
        window = spoken[start:]
        if len(window) < min_tokens or len(window) > len(said):
            continue
        if said[: len(window)] == window:
            if len(window) == len(said):
                return ""
            return " ".join(raw[positions[len(window)] :])

    return transcript


class EchoGate:
    """
    Decides when the microphone has started hearing Master Miguel rather than
    California's own activation line coming back through the speaker.

    Recording now begins the instant the wake word fires, so the front of every
    recording overlaps playback. Until the gate arms, those chunks are captured
    but excluded from both the VAD clock and the audio sent to Whisper.

    It arms one of two ways:

    - the line finished playing, so everything after it is the user; or
    - the mic went loud enough during playback that it can only be the user
      talking over her, which also cuts the line short.

    `barge_in_rms` sits well above `vad.energy_threshold` on purpose: it has to
    clear the speaker bleed, and the user is far closer to the mic than the
    speaker is. If it is set too high the gate simply never barges in and the
    line plays out, which is the old behaviour minus the blocked microphone —
    a bad value costs responsiveness, never correctness.
    """

    def __init__(
        self,
        window_s: float,
        barge_in_rms: float,
        onset_guard_s: float = 0.15,
    ):
        self.window_s = max(0.0, window_s)
        self.barge_in_rms = barge_in_rms
        # Ignore the first moments of playback: nobody reacts that fast, and the
        # onset of the line itself is its loudest part.
        self.onset_guard_s = onset_guard_s
        self.armed = self.window_s <= 0
        self.barged_in = False

    def update(self, elapsed: float, rms: float) -> bool:
        """
        Feed one chunk's elapsed time and RMS. Returns True once recording is
        live, and stays True from then on.
        """
        if self.armed:
            return True
        if elapsed >= self.window_s:
            self.armed = True
        elif elapsed >= self.onset_guard_s and rms >= self.barge_in_rms:
            self.armed = True
            self.barged_in = True
        return self.armed
