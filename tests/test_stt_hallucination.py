"""
Whisper hallucinates filler out of non-speech, and that used to cost a full turn.

A false wake recorded 5.2s of room tone, Whisper returned "Hmm.", and the only
guard between that and the LLM was `transcript.strip() == ""`. Claude answered it
and California spoke. STTService now asks for verbose_json and throws the turn
away before it costs anything.

These tests never touch the network: STTService is built with __new__ and handed
a fake client, the same shape as tests/test_govee_service.py.
"""

import unittest

from services.stt import HALLUCINATION_FILLERS, STTService, _is_filler


class _FakeSegment:
    def __init__(self, no_speech_prob=0.0, avg_logprob=0.0):
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class _FakeTranscription:
    """What the Groq client returns for response_format="verbose_json"."""

    def __init__(self, text, segments=None):
        self.text = text
        if segments is not None:
            self.segments = segments


class _FakeTranscriptions:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.result


class _FakeClient:
    def __init__(self, result):
        self.audio = type("audio", (), {})()
        self.audio.transcriptions = _FakeTranscriptions(result)


def _service(result, max_no_speech=0.6, min_logprob=-1.0):
    svc = STTService.__new__(STTService)
    svc.provider = "groq"
    svc.model = "whisper-large-v3-turbo"
    svc.language = "en"
    svc.max_no_speech_prob = max_no_speech
    svc.min_avg_logprob = min_logprob
    svc.client = _FakeClient(result)
    return svc


WAV = b"RIFF....WAVE"


class FillerTests(unittest.TestCase):
    def test_the_transcript_that_started_this(self):
        self.assertTrue(_is_filler("Hmm."))

    def test_matching_ignores_case_punctuation_and_whitespace(self):
        for text in ("  you  ", "You.", "THANK YOU!", "thanks for watching..."):
            self.assertTrue(_is_filler(text), text)

    def test_real_commands_are_not_filler(self):
        for text in ("go home", "stop", "go", "play Shrinking", "turn the lights on"):
            self.assertFalse(_is_filler(text), text)

    def test_live_control_words_are_not_in_the_blocklist(self):
        """"go" and "stop" are real control_tv actions — never block them."""
        self.assertNotIn("go", HALLUCINATION_FILLERS)
        self.assertNotIn("stop", HALLUCINATION_FILLERS)


class RejectionTests(unittest.TestCase):
    def test_a_filler_transcript_is_dropped(self):
        svc = _service(_FakeTranscription("Hmm.", [_FakeSegment()]))
        self.assertEqual(svc._transcribe_groq(WAV), "")

    def test_a_high_no_speech_prob_is_dropped(self):
        svc = _service(
            _FakeTranscription("Sounds about right.", [_FakeSegment(no_speech_prob=0.9)])
        )
        self.assertEqual(svc._transcribe_groq(WAV), "")

    def test_a_low_avg_logprob_is_dropped(self):
        svc = _service(
            _FakeTranscription("Sounds about right.", [_FakeSegment(avg_logprob=-2.5)])
        )
        self.assertEqual(svc._transcribe_groq(WAV), "")

    def test_a_real_command_survives_untouched(self):
        svc = _service(
            _FakeTranscription(
                "  play Shrinking  ",
                [_FakeSegment(no_speech_prob=0.02, avg_logprob=-0.2)],
            )
        )
        self.assertEqual(svc._transcribe_groq(WAV), "play Shrinking")

    def test_the_worst_segment_decides_not_the_first(self):
        svc = _service(
            _FakeTranscription(
                "play Shrinking",
                [_FakeSegment(no_speech_prob=0.01), _FakeSegment(no_speech_prob=0.95)],
            )
        )
        self.assertEqual(svc._transcribe_groq(WAV), "")

    def test_it_asks_for_verbose_json(self):
        """Without this the probability fields never come back at all."""
        svc = _service(_FakeTranscription("play Shrinking", [_FakeSegment()]))
        svc._transcribe_groq(WAV)
        self.assertEqual(
            svc.client.audio.transcriptions.kwargs["response_format"], "verbose_json"
        )


class FailOpenTests(unittest.TestCase):
    """
    A diagnostic field must never be the thing that loses a command.
    """

    def test_a_response_with_no_segments_still_returns_its_text(self):
        svc = _service(_FakeTranscription("play Shrinking"))
        self.assertEqual(svc._transcribe_groq(WAV), "play Shrinking")

    def test_a_bare_string_response_still_works(self):
        svc = _service("play Shrinking")
        self.assertEqual(svc._transcribe_groq(WAV), "play Shrinking")

    def test_a_dict_response_still_works(self):
        svc = _service({"text": "play Shrinking", "segments": [{"no_speech_prob": 0.01}]})
        self.assertEqual(svc._transcribe_groq(WAV), "play Shrinking")

    def test_a_dict_response_is_still_filtered(self):
        svc = _service({"text": "Hmm", "segments": [{"no_speech_prob": 0.99}]})
        self.assertEqual(svc._transcribe_groq(WAV), "")

    def test_garbage_segments_do_not_raise(self):
        svc = _service(_FakeTranscription("play Shrinking", segments=["nonsense", 42]))
        self.assertEqual(svc._transcribe_groq(WAV), "play Shrinking")

    def test_an_empty_transcript_is_empty(self):
        svc = _service(_FakeTranscription("   ", [_FakeSegment()]))
        self.assertEqual(svc._transcribe_groq(WAV), "")


if __name__ == "__main__":
    unittest.main()
