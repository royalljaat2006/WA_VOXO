"""Amount and PIN parsing.

The most safety-critical tests in the project: a wrong amount here means money
sent to the wrong person, irreversibly. Everything ambiguous must return None so
the caller asks instead of guessing.
"""

import pytest

from whatsapp_voice.agent.parsing import extract_pin, format_amount, parse_amount_paise


class TestPlainDigits:
    @pytest.mark.parametrize("text,paise", [
        ("2000", 200_000),
        ("500", 50_000),
        ("2,000", 200_000),
        ("₹2000", 200_000),
        ("2000 rupaye", 200_000),
        ("Rs 2000", 200_000),
        ("1", 100),
    ])
    def test_digits(self, text, paise):
        assert parse_amount_paise(text) == paise

    def test_devanagari_digits(self):
        assert parse_amount_paise("२०००") == 200_000


class TestSpokenHindiAmounts:
    @pytest.mark.parametrize("text,paise", [
        ("do hazaar", 200_000),
        ("paanch hazaar", 500_000),
        ("bees hazaar", 2_000_000),
        ("das hazaar", 1_000_000),
        ("paanch sau", 50_000),
        ("do sau", 20_000),
        ("ek lakh", 10_000_000),
        ("2 lakh", 20_000_000),
        ("2 hazaar", 200_000),
    ])
    def test_word_plus_multiplier(self, text, paise):
        assert parse_amount_paise(text) == paise

    @pytest.mark.parametrize("text,paise", [
        ("dhaai hazaar", 250_000),
        ("dedh hazaar", 150_000),
        ("adha lakh", 5_000_000),
    ])
    def test_fractional_multipliers(self, text, paise):
        assert parse_amount_paise(text) == paise

    def test_compound_amount(self):
        assert parse_amount_paise("ek lakh bees hazaar") == 12_000_000

    def test_devanagari_words(self):
        assert parse_amount_paise("बीस हज़ार") == 2_000_000


class TestDevanagari:
    """Sarvam returns Devanagari for Hindi speech, so this is the *normal* path
    for a Hindi speaker — not an edge case."""

    @pytest.mark.parametrize("text,paise", [
        ("दो हज़ार", 200_000),
        ("बीस हजार", 2_000_000),      # without nukta
        ("बीस हज़ार", 2_000_000),      # with nukta — must give the same answer
        ("ढाई हज़ार", 250_000),
        ("एक लाख", 10_000_000),
        ("पाँच सौ", 50_000),
        ("२०००", 200_000),
    ])
    def test_amounts(self, text, paise):
        assert parse_amount_paise(text) == paise

    def test_bhej_do_trap_in_devanagari(self):
        assert parse_amount_paise("रमेश को 2000 भेज दो") == 200_000

    def test_combining_marks_survive_normalisation(self):
        """`[^\\w\\s]` would strip the nukta and chandrabindu, silently breaking
        every Hindi amount — the same trap documented in metrics.py."""
        assert parse_amount_paise("पाँच सौ") == 50_000
        assert parse_amount_paise("हज़ार" ) is None  # bare multiplier, no value

    def test_pin_in_devanagari(self):
        assert extract_pin("एक दो तीन चार") == "1234"


class TestTheBhejDoTrap:
    """`do` is both the number 2 and the imperative "give". The most common
    phrasing in the whole product ends with it."""

    def test_bhej_do_does_not_add_a_second_amount(self):
        assert parse_amount_paise("Ramesh ko 2000 bhej do") == 200_000

    def test_bhej_do_with_spoken_amount(self):
        assert parse_amount_paise("Ramesh ko do hazaar bhej do") == 200_000

    def test_kar_do_bhi_safe(self):
        assert parse_amount_paise("2000 transfer kar do") == 200_000

    def test_bare_number_word_is_not_an_amount(self):
        """Nobody dictates ₹2 by voice; a bare "do" is almost always a verb."""
        assert parse_amount_paise("bhej do") is None
        assert parse_amount_paise("kar do") is None


class TestAmbiguityReturnsNone:
    """Every one of these must ask the user rather than guess."""

    def test_no_amount(self):
        assert parse_amount_paise("Ramesh ko paise bhej do") is None

    def test_two_different_amounts(self):
        assert parse_amount_paise("2000 ya 3000 bhej do") is None

    def test_empty(self):
        assert parse_amount_paise("") is None
        assert parse_amount_paise("   ") is None

    def test_zero_is_not_an_amount(self):
        assert parse_amount_paise("0") is None

    def test_same_amount_twice_is_not_ambiguous(self):
        """A read-back repeating the figure should still parse."""
        assert parse_amount_paise("2000, haan 2000 bhej do") == 200_000


class TestPrecision:
    def test_returns_int_paise_never_float(self):
        result = parse_amount_paise("dhaai hazaar")
        assert isinstance(result, int)
        assert result == 250_000

    def test_no_float_drift_on_fractions(self):
        assert parse_amount_paise("dedh hazaar") == 150_000


class TestFormatAmount:
    @pytest.mark.parametrize("paise,text", [
        (200_000, "2,000.00"),
        (50_000, "500.00"),
        (10_000_000, "1,00,000.00"),   # Indian grouping
        (12_000_000, "1,20,000.00"),
        (100, "1.00"),
        (250_000, "2,500.00"),
    ])
    def test_indian_grouping(self, paise, text):
        assert format_amount(paise) == text


class TestExtractPin:
    @pytest.mark.parametrize("text,pin", [
        ("1234", "1234"),
        ("1 2 3 4", "1234"),
        ("123456", "123456"),
        ("१२३४", "1234"),
        ("one two three four", "1234"),
        ("ek do teen char", "1234"),
        ("mera pin 1234 hai", "1234"),
        ("PIN is 1234", "1234"),
    ])
    def test_accepts_pins(self, text, pin):
        assert extract_pin(text) == pin

    @pytest.mark.parametrize("text", [
        "Ramesh ko 2000 bhej do",   # a sentence, not a PIN
        "123",                       # wrong length
        "12345",                     # wrong length
        "",
        "mera pin bhool gaya",       # no digits
        "send money now",
    ])
    def test_rejects_non_pins(self, text):
        assert extract_pin(text) is None

    def test_amount_sentence_is_never_a_pin(self):
        """The critical separation: a payment instruction must not be read as
        a PIN, or the flow would skip confirmation entirely."""
        assert extract_pin("Ramesh ko 2000 bhej do") is None
        assert extract_pin("do hazaar bhej do") is None

    def test_custom_length(self):
        assert extract_pin("12345", lengths=(5,)) == "12345"
        assert extract_pin("1234", lengths=(6,)) is None
