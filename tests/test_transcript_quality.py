"""Refusing to act on a transcript we cannot trust.

Sarvam reports no confidence score. `saarika:v2.5` returns exactly three fields —
`request_id`, `transcript`, `language_code` — verified against the live API on
clear speech, noise and silence alike. So the signal here is the one that does
exist: how much text a recording produced for its length.

Thresholds come from real measurements against Sarvam:

    hi_1.wav   2.5s   35 chars   14.1 c/s
    en_1.wav   3.1s   53 chars   17.2 c/s
    hx_2.wav   3.8s   56 chars   14.7 c/s
    noise      3.0s    0 chars    0.0 c/s
    silence    4.0s    0 chars    0.0 c/s

Genuine speech clears 14 c/s in both scripts; failure returns nothing. The cutoff
sits at 3, roughly a 5× margin.
"""

import pytest

from whatsapp_voice.stt.base import (
    MIN_CHARS_PER_SECOND,
    MIN_DURATION_FOR_YIELD_CHECK_S,
    is_low_yield,
)


class TestRealSpeechPasses:
    @pytest.mark.parametrize("text,duration", [
        ("आज दिवाली कैंपेन के कितने मैसेज गए?", 2.5),
        ("जुलाई वाले कैंपेन की डिलीवरी रिपोर्ट भेजो।", 2.7),
        ("How many messages did the Diwali campaign send?", 3.1),
        ("रमेश को दो हज़ार भेज दो", 2.6),
        ("Ramesh ko 2000 bhej do", 2.5),
    ])
    def test_measured_utterances_are_not_flagged(self, text, duration):
        assert is_low_yield(text, duration) is False

    def test_devanagari_is_denser_and_still_passes(self):
        """Devanagari packs more sound into fewer characters than Latin, so it
        sits closer to the floor. It must still clear it comfortably."""
        assert is_low_yield("आज दिवाली कैंपेन के कितने मैसेज गए?", 2.5) is False


class TestFailedRecognitionIsCaught:
    def test_silence_is_flagged(self):
        assert is_low_yield("", 4.0) is True

    def test_whitespace_only_is_flagged(self):
        assert is_low_yield("   \n  ", 4.0) is True

    def test_a_fragment_from_a_long_recording_is_flagged(self):
        """The case the empty-transcript check misses: six seconds of speech
        coming back as one word, which then looks like a confident short
        instruction to everything downstream."""
        assert is_low_yield("हाँ", 6.0) is True

    def test_two_words_from_ten_seconds_is_flagged(self):
        assert is_low_yield("bhej do", 10.0) is True


class TestShortUtterancesAreExempt:
    """"Haan", "1234" and "cancel" are legitimately tiny. Below the minimum
    duration the ratio means nothing."""

    @pytest.mark.parametrize("text,duration", [
        ("1234", 1.2),
        ("haan", 0.8),
        ("ok", 0.6),
        ("", 1.0),
        ("nahi", 1.5),
    ])
    def test_not_flagged(self, text, duration):
        assert is_low_yield(text, duration) is False

    def test_the_exemption_ends_at_the_threshold(self):
        just_under = MIN_DURATION_FOR_YIELD_CHECK_S - 0.1
        assert is_low_yield("", just_under) is False
        assert is_low_yield("", MIN_DURATION_FOR_YIELD_CHECK_S) is True


class TestUnknownDuration:
    def test_zero_duration_never_flags(self):
        """`duration_seconds` failing must not cost the user a good transcript."""
        assert is_low_yield("Ramesh ko 2000 bhej do", 0.0) is False


class TestThreshold:
    def test_sits_well_below_measured_speech(self):
        """If someone lowers real speech onto the cutoff, this fails loudly
        rather than the guard quietly rejecting valid recordings."""
        slowest_measured = 14.1
        assert slowest_measured / 4 > MIN_CHARS_PER_SECOND

    def test_boundary(self):
        # 3.0s at exactly the cutoff → not flagged; one character less → flagged.
        exact = "x" * int(MIN_CHARS_PER_SECOND * 3.0)
        assert is_low_yield(exact, 3.0) is False
        assert is_low_yield(exact[:-1], 3.0) is True
