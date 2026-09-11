"""
The speech path between the LLM stream and the speaker, which is where long
replies came to sound like they were about to stop and then carried on.

Three things were wrong, and they stacked:

  * The sanitizer stripped every chunk's terminal . ! ? before Kokoro saw it,
    on the theory that they only bought silence at the tail. Kokoro is
    StyleTTS2-based and that punctuation is the cue that closes a clause, so
    every chunk rendered on a rising, unresolved contour -- "I am not done"
    over text that was.
  * The chunker split on semicolons, colons and dashes from eight characters
    up, so one paragraph became eight synthesis calls. Kokoro pads ~400ms of
    silence onto the front of every clip, and only the tail was being trimmed,
    so each of those calls landed as dead air in the middle of the answer.
    Measured live: ~4.7s injected into a twelve-second reply.
  * Splitting on " - " happened before the sanitizer ran, so fragments arrived
    ending in a bare "-" that the sanitizer's spaced-dash rule could not see.

The contract these tests pin: a chunk ends in . ! ? when it is a finished
sentence and in a single comma when it is not; the last chunk of a response
always resolves; short sentences travel together; the first chunk still ships
as soon as one sentence exists; and both ends of the audio are trimmed.
"""

import unittest

import numpy as np

from services.sentence_chunker import chunk_sentences
from services.tts import TTSService
from services.tts_text_sanitizer import sanitize_for_tts
from tests.config_fixture import real_config

SAMPLE_RATE = 24000


def _stream(text: str):
    """Word-at-a-time token stream, the shape the LLM produces."""
    for word in text.split(" "):
        yield word + " "


# The reply that measured 8 chunks / ~4.7s of silence before the fix.
REALISTIC_REPLY = (
    "Alright Master Miguel, here's the deal: the box was asleep, so I woke the "
    "television first - that's the only route that works - and then toggled the "
    "input. Stremio's up now. You were on Shrinking, season two, episode four; "
    "I can pick that up, or we can start something else. Your call."
)


class SanitizerKeepsTheProsody(unittest.TestCase):

    def test_terminal_period_survives(self):
        self.assertEqual(sanitize_for_tts("I turned the lights on."), "I turned the lights on.")

    def test_question_and_exclamation_survive(self):
        self.assertEqual(sanitize_for_tts("Want me to keep going?"), "Want me to keep going?")
        self.assertEqual(sanitize_for_tts("Done!"), "Done!")

    def test_a_fragment_gets_a_continuation_comma(self):
        # A soft split mid-sentence arrives bare. Bare trails off; a comma
        # tells the model more is coming.
        self.assertEqual(sanitize_for_tts("the box was asleep"), "the box was asleep,")

    def test_a_dangling_hyphen_is_removed(self):
        self.assertEqual(
            sanitize_for_tts("so I woke the television first -"),
            "so I woke the television first,",
        )

    def test_a_trailing_separator_collapses_to_one_comma(self):
        self.assertEqual(sanitize_for_tts("here's the deal:"), "here's the deal,")
        self.assertEqual(sanitize_for_tts("episode four;"), "episode four,")
        self.assertEqual(sanitize_for_tts("and then, , "), "and then,")

    def test_mid_sentence_heavy_punctuation_is_still_softened(self):
        self.assertEqual(
            sanitize_for_tts("Two things — first: the box; second, the lights."),
            "Two things, first, the box, second, the lights.",
        )

    def test_empty_stays_empty(self):
        self.assertEqual(sanitize_for_tts(""), "")
        self.assertEqual(sanitize_for_tts("   "), "")
        self.assertEqual(sanitize_for_tts(" - "), "")


class ChunkerHandsOverWholeSentences(unittest.TestCase):

    def test_the_realistic_reply_is_a_few_calls_not_eight(self):
        chunks = list(chunk_sentences(_stream(REALISTIC_REPLY)))
        self.assertLessEqual(len(chunks), 4, chunks)
        for chunk in chunks:
            self.assertIn(chunk[-1], ".!?,", chunk)
            self.assertNotIn(" -", chunk)
            self.assertNotIn(":", chunk)
        # Only the first chunk may be a fragment; everything after it is
        # whole sentences, and the last one resolves.
        for chunk in chunks[1:]:
            self.assertIn(chunk[-1], ".!?", chunk)

    def test_short_sentences_travel_together(self):
        text = "Alright, Stremio is up and running for you. Yep. It's on. Your call."
        chunks = list(chunk_sentences(_stream(text)))
        self.assertEqual(chunks, [
            "Alright, Stremio is up and running for you.",
            "Yep. It's on. Your call.",
        ])

    def test_colon_and_dash_are_not_boundaries(self):
        text = "Here's the plan: wake the box - then open Stremio."
        chunks = list(chunk_sentences(_stream(text)))
        self.assertEqual(chunks, ["Here's the plan, wake the box, then open Stremio."])

    def test_the_first_chunk_ships_on_the_first_sentence(self):
        # Latency lives in the first chunk. It must go out before the rest of
        # the response has even arrived, however short it is.
        fed = []

        def tokens():
            for word in "Sure thing Master Miguel. Now for the long part".split(" "):
                fed.append(word)
                yield word + " "

        gen = chunk_sentences(tokens())
        first = next(gen)
        self.assertEqual(first, "Sure thing Master Miguel.")
        self.assertLess(len(fed), 8, "first chunk waited for tokens it did not need")

    def test_a_long_opening_sentence_does_not_delay_first_audio(self):
        # Kokoro is ~real time here, so a 150-char first sentence is ~9s before
        # anything is heard. The first chunk is soft-split well before that.
        fed = []

        def tokens():
            for word in REALISTIC_REPLY.split(" "):
                fed.append(word)
                yield word + " "

        gen = chunk_sentences(tokens())
        first = next(gen)
        self.assertLessEqual(len(first), 70, first)
        self.assertTrue(first.endswith(","), first)   # a continuation, not a stop
        self.assertLess(len(fed), 16, "first chunk waited on the whole sentence")
        # And the rest of that sentence still arrives, resolved.
        rest = list(gen)
        self.assertTrue(any(c.endswith(".") for c in rest))

    def test_the_first_chunk_ceiling_does_not_apply_once_speaking(self):
        # After the first chunk, a long sentence runs to max_chars as before.
        text = "The opener clears the first bar. " + " ".join(["word"] * 40) + "."
        chunks = list(chunk_sentences(_stream(text)))
        self.assertEqual(chunks[0], "The opener clears the first bar.")
        self.assertGreater(len(chunks[1]), 70)

    def test_later_chunks_are_held_until_they_are_worth_a_call(self):
        # Once something is playing there is no hurry: sentences accumulate to
        # MIN_CHUNK_CHARS before costing a synthesis call, then go together.
        first = "Alright, here is the full status."
        mids = [
            "The box is awake.",
            "Stremio is open.",
            "Volume is at twelve.",
            "The lights are on.",
            "The strip is warm white.",
        ]
        tail = "Anything else?"
        chunks = list(chunk_sentences(_stream(" ".join([first, *mids, tail]))))
        self.assertEqual(chunks, [first, " ".join(mids), tail])

    def test_a_tiny_first_sentence_is_not_shipped_alone(self):
        # "Yep." would cost a full Kokoro call (~1.5s of synthesis, ~400ms of
        # padding) for four characters. The first-chunk bar holds it until
        # there is enough to be worth saying.
        chunks = list(chunk_sentences(_stream("Yep. It's on. Volume's at twelve. Anything else?")))
        self.assertEqual(chunks[0], "Yep. It's on. Volume's at twelve.")

    def test_the_last_chunk_always_resolves(self):
        # A stream cut mid-sentence still ends on a full stop, never a comma.
        chunks = list(chunk_sentences(_stream("The box is awake and the lights are")))
        self.assertEqual(chunks, ["The box is awake and the lights are."])

    def test_a_long_run_without_a_full_stop_is_soft_split_with_a_comma(self):
        text = "one two three four five six seven eight nine ten, " * 8
        chunks = list(chunk_sentences(_stream(text.strip()), max_chars=120))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(chunk.endswith(","), chunk)
        self.assertTrue(chunks[-1].endswith("."), chunks[-1])

    def test_abbreviations_still_do_not_split(self):
        chunks = list(chunk_sentences(_stream("Dr. Smith is here. Say hi.")))
        self.assertEqual(chunks, ["Dr. Smith is here. Say hi."])


def _tts_with_shipped_trim() -> TTSService:
    """A TTSService with the trim values config.yaml ships, and no Kokoro loaded."""
    trim = real_config()["tts"]["kokoro"]["trim"]
    svc = TTSService.__new__(TTSService)
    svc.kokoro_trim_floor = float(trim["silence_floor"])
    svc.kokoro_trim_lead_ms = int(trim["lead_in_ms"])
    svc.kokoro_trim_tail_ms = int(trim["tail_ms"])
    svc.kokoro_trim_fade_ms = int(trim["fade_ms"])
    return svc


def _padded_clip(front_ms=400, voice_ms=300, back_ms=550, amplitude=0.5):
    """What Kokoro returns: silence, then a tone, then more silence."""
    front = np.zeros(SAMPLE_RATE * front_ms // 1000, dtype=np.float32)
    t = np.arange(SAMPLE_RATE * voice_ms // 1000) / SAMPLE_RATE
    voice = (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    back = np.zeros(SAMPLE_RATE * back_ms // 1000, dtype=np.float32)
    return np.concatenate([front, voice, back]), front.size, voice.size


class TrimStripsBothEnds(unittest.TestCase):

    def setUp(self):
        self.tts = _tts_with_shipped_trim()

    def test_leading_silence_is_removed_not_just_trailing(self):
        clip, front, voice = _padded_clip()
        out = self.tts._trim_silence(clip, SAMPLE_RATE)
        lead = SAMPLE_RATE * self.tts.kokoro_trim_lead_ms // 1000
        tail = SAMPLE_RATE * self.tts.kokoro_trim_tail_ms // 1000
        # A sample or two of the sine's zero crossing sits under the floor.
        self.assertAlmostEqual(out.size, lead + voice + tail, delta=SAMPLE_RATE // 1000)
        self.assertLess(out.size, clip.size - front)

    def test_the_kept_lead_in_is_short(self):
        clip, _, _ = _padded_clip()
        out = self.tts._trim_silence(clip, SAMPLE_RATE)
        first_loud = np.flatnonzero(np.abs(out) > 0.5 * self.tts.kokoro_trim_floor)[0]
        self.assertLessEqual(first_loud, SAMPLE_RATE * self.tts.kokoro_trim_lead_ms // 1000)

    def test_the_floor_is_relative_to_the_clip_peak(self):
        # A quiet line is still speech. An absolute floor would eat it.
        clip, _, voice = _padded_clip(amplitude=0.02)
        out = self.tts._trim_silence(clip, SAMPLE_RATE)
        self.assertGreaterEqual(out.size, voice)
        self.assertLess(out.size, clip.size)

    def test_the_cut_edges_are_faded(self):
        clip, _, _ = _padded_clip(front_ms=0, back_ms=0)
        out = self.tts._trim_silence(clip, SAMPLE_RATE)
        self.assertAlmostEqual(float(out[0]), 0.0, places=6)
        self.assertAlmostEqual(float(out[-1]), 0.0, places=6)

    def test_pure_silence_is_returned_untouched(self):
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        out = self.tts._trim_silence(silence, SAMPLE_RATE)
        self.assertEqual(out.size, silence.size)

    def test_empty_is_empty(self):
        out = self.tts._trim_silence(np.array([], dtype=np.float32), SAMPLE_RATE)
        self.assertEqual(out.size, 0)

    def test_the_trim_is_driven_by_config(self):
        # If the config block moves or is renamed, the live path silently falls
        # back to code defaults. This fails instead.
        trim = real_config()["tts"]["kokoro"]["trim"]
        for key in ("silence_floor", "lead_in_ms", "tail_ms", "fade_ms"):
            self.assertIn(key, trim)


if __name__ == "__main__":
    unittest.main()
