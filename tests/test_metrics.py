import pytest

from whatsapp_voice.metrics import normalise, word_error_rate


class TestNormalise:
    def test_lowercases_and_strips_punctuation(self):
        assert normalise("Aaj Kitne Campaign Gaye?!") == "aaj kitne campaign gaye"

    def test_collapses_whitespace(self):
        assert normalise("  delivery   report \n bhej  do ") == "delivery report bhej do"

    def test_preserves_devanagari(self):
        assert normalise("कल के कैंपेन का!") == "कल के कैंपेन का"

    def test_preserves_combining_marks(self):
        # Regression: `[^\w\s]` stripped matras because \w excludes category Mn,
        # turning "के" into "क" and corrupting WER for every Hindi transcript.
        assert normalise("के") == "के"
        assert len(normalise("कैंपेन")) == len("कैंपेन")

    def test_keeps_digits(self):
        assert normalise("12 campaign gaye") == "12 campaign gaye"


class TestWordErrorRate:
    def test_identical_is_zero(self):
        assert word_error_rate("aaj kitne campaign gaye", "aaj kitne campaign gaye") == 0.0

    def test_ignores_case_and_punctuation(self):
        assert word_error_rate("delivery report bhej do", "Delivery, report bhej do!") == 0.0

    def test_single_substitution(self):
        assert word_error_rate("aaj kitne campaign gaye", "aaj kitne message gaye") == 0.25

    def test_single_deletion(self):
        assert word_error_rate("aaj kitne campaign gaye", "aaj kitne gaye") == 0.25

    def test_empty_hypothesis_is_total_loss(self):
        assert word_error_rate("aaj kitne campaign gaye", "") == 1.0

    def test_insertions_can_exceed_one(self):
        assert word_error_rate("report", "report report report") == 2.0

    def test_empty_reference_rejected(self):
        with pytest.raises(ValueError):
            word_error_rate("", "kuch bhi")

    def test_script_difference_scores_as_total_miss(self):
        # Documents the Hinglish trap: same meaning, different script, WER 100%.
        assert word_error_rate("aaj", "आज") == 1.0
