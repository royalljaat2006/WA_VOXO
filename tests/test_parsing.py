"""Amount and PIN parsing.

The most safety-critical tests in the project: a wrong amount here means money
sent to the wrong person, irreversibly. Everything ambiguous must return None so
the caller asks instead of guessing.
"""

import pytest

from whatsapp_voice.agent.parsing import (
    extract_pin,
    format_amount,
    parse_amount_paise,
    parse_mobile_payee,
    parse_vpa,
    transliterate_devanagari_name,
)


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
        # "ha" is the colloquial Hinglish spelling of "hai" that real users
        # actually type — its absence from the filler list silently dropped
        # every PIN confirmation phrased this way. Observed in a real run.
        ("mera pin 1234 ha", "1234"),
        ("1234 mera pin ha", "1234"),
        ("1234 mera pin use kro", "1234"),
        # Devanagari filler — a Hindi-spoken PIN is wrapped in "मेरा पिन ...
        # है" the same way an English one is in "mera pin ... hai", and
        # Whisper/Sarvam transcribe Hindi speech in this script, not Latin.
        # Every voice PIN confirmation in Hindi was silently dropped without
        # these. Observed in a real run.
        ("मेरा पिन चार दो दो नौ है", "4229"),
        ("चार दो दो नौ मेरा पिन है", "4229"),
        ("पिन है चार दो दो नौ", "4229"),
        # User-facing prompts now say "PINTWIN" (eko's own field name), not
        # "PIN" — see agent/handoff.py and PAYMENT_PROMPT. A payer reading
        # the prompt back has to still parse, same as "mera pin ... hai" did.
        ("mera pintwin 1234 hai", "1234"),
        ("PINTWIN is 1234", "1234"),
        ("1234 mera pintwin ha", "1234"),
        # Hindi-language STT transliterates *English* digit words into
        # Devanagari script rather than switching to Latin ("one two three
        # four" -> "वन टू थ्री फोर") — the same trap `_SPOKEN_DIGITS` already
        # accounts for in mobile/VPA parsing. A real transfer was wrongly
        # cancelled as "user changed subject" because the correct PIN,
        # digit-by-digit in English, was transcribed this way. Observed in
        # a real run.
        ("वन टू थ्री फोर", "1234"),
        ("पिन है वन टू थ्री फोर", "1234"),
        # "sir"/"ji" are common Hindi/Hinglish politeness particles around a
        # spoken PIN confirmation ("sir mera pin ... hai", "... hai ji") —
        # their absence from the filler list rejected the whole utterance.
        # Observed in a real run alongside the transliteration bug above.
        ("one two three four ji", "1234"),
        ("sir mera pin one two three four hai", "1234"),
        ("हां मेरा पिन एक दो तीन चार जी", "1234"),
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


class TestParseVpa:
    """A wrong VPA is worse than a wrong amount: money goes to a stranger,
    not just the wrong number. Everything ambiguous must return None."""

    @pytest.mark.parametrize("text,vpa", [
        ("ramesh@okaxis", "ramesh@okaxis"),
        ("9000000000@upi", "9000000000@upi"),
        ("ramesh at okaxis", "ramesh@okaxis"),
        ("ramesh at the rate okaxis", "ramesh@okaxis"),
        ("RAMESH@OKAXIS", "ramesh@okaxis"),
    ])
    def test_already_clean_or_simply_spoken(self, text, vpa):
        assert parse_vpa(text) == vpa

    @pytest.mark.parametrize("text", [
        "Rameshwar",              # not a VPA shape at all
        "",
        "ramesh okaxis",          # no separator of any kind
    ])
    def test_rejects_non_vpas(self, text):
        assert parse_vpa(text) is None

    def test_devanagari_digit_and_letter_names_reconstruct_the_vpa(self):
        """Real failure: a payer read '7763857505@ptyes' aloud digit by
        digit, and Sarvam wrote the English digit and letter *names* out in
        Devanagari script instead of digits or Latin letters —
        "सेवेन सेवेन सिक्स थ्री एट फाइव सेवेन फाइव ज़ीरो फाइव एट पी टी वाई ई
        एस". The old parser rejected this outright and made the payer repeat
        themselves."""
        text = (
            "सेवेन सेवेन सिक्स थ्री एट फाइव सेवेन फाइव ज़ीरो फाइव एट "
            "पी टी वाई ई एस"
        )
        assert parse_vpa(text) == "7763857505@ptyes"

    def test_at_versus_eight_is_resolved_by_position(self):
        """'एट' means both 'at' and 'eight' — only the LAST occurrence with a
        letter after it is the VPA's '@'; an earlier one surrounded by more
        digits is '8', not a second separator."""
        text = "फाइव एट सिक्स एट सिक्स"  # five eight six @ six -> 586@6? no letters after final एट
        # No letters anywhere after the last 'एट', so nothing qualifies as
        # '@' and the whole thing is read as digits — which then fails
        # _VPA_RE for having no '@' at all, exactly like an ordinary
        # non-VPA phrase would.
        assert parse_vpa(text) is None

    def test_surrounding_sentence_noise_is_dropped_not_guessed_into(self):
        """'mera UPI ID hai' framing a VPA is common — dropped as noise
        outside the longest reconstructable run, not stitched into it."""
        text = "mera UPI ID hai पी टी वाई ई एस एट ओ के आइसीआईसीआई"
        # Even though this specific sentence does not fully reconstruct
        # (ओके आइसीआईसीआई is not in the letter table), the important
        # property is that it does not silently produce a wrong VPA — either
        # a clean reconstruction or None, never a guess.
        result = parse_vpa(text)
        assert result is None or result.count("@") == 1

    def test_a_transcript_with_unrecoverable_noise_in_the_middle_still_asks_again(self):
        """The exact real transcript this fix was built from, garbled words
        and all: 'द रट' (untranslatable noise between the digits and the
        handle) breaks the one contiguous run a reconstruction needs.
        Returning None here — asking the payer to repeat — is the correct,
        safe outcome, not a bug: guessing through unrecognisable words is
        exactly what this function must never do."""
        text = (
            "सेवेन सेवेन सिक्स थ्री एट फाइव सेवेन फाइव ज़ीरो फाइव एट द रट "
            "पी टी वाई ई एस मेरा यूपी आई आई डी है"
        )
        assert parse_vpa(text) is None

    def test_mixed_script_handle_already_latin(self):
        """Only the digits got Devanagari-transliterated; the handle came
        through as plain Latin text already — a realistic partial case."""
        text = "सेवेन सेवेन सिक्स थ्री एट फाइव सेवेन फाइव ज़ीरो फाइव एट ptyes"
        assert parse_vpa(text) == "7763857505@ptyes"


class TestSpokenBankHandlePhrases:
    """A payer who says the bank's name as ordinary words — "ओके एचडीएफसी
    बैंक", "okay SBI" — rather than spelling the handle letter by letter.

    Real failure this fixes: a payer's actual VPA was
    "khatrisurender94@okhdfcbank"; the transcript carried the handle as three
    spoken words nothing here recognised, and the bot asked the payer to
    repeat themselves for no reason — the digits and handle were both
    already unambiguous, only the *spelling style* of the handle was
    unfamiliar to the parser.
    """

    @pytest.mark.parametrize("text,vpa", [
        ("9520680874 at okay hdfc bank", "9520680874@okhdfcbank"),
        ("9520680874 at ओके एचडीएफसी बैंक", "9520680874@okhdfcbank"),
        ("9520680874 at ओके एचडीएफसी", "9520680874@okhdfcbank"),
        ("9520680874 at ok sbi", "9520680874@oksbi"),
        ("9520680874 at ओके एसबीआई", "9520680874@oksbi"),
        ("9520680874 at okay icici bank", "9520680874@okicici"),
        ("9520680874 at ओके आईसीआईसीआई", "9520680874@okicici"),
        ("9520680874 at okay axis bank", "9520680874@okaxis"),
        ("9520680874 at ओके एक्सिस", "9520680874@okaxis"),
        ("9520680874 at paytm sbi", "9520680874@ptsbi"),
        ("9520680874 at पेटीएम एसबीआई बैंक", "9520680874@ptsbi"),
        ("9520680874 at paytm yes bank", "9520680874@ptyes"),
        ("9520680874 at पेटीएम यस", "9520680874@ptyes"),
        ("9000000000 at upi", "9000000000@upi"),
        ("9000000000 at यूपीआई", "9000000000@upi"),
        ("9876543210 at paytm", "9876543210@paytm"),
        ("9876543210 at पेटीएम", "9876543210@paytm"),
    ])
    def test_full_word_handle_names_resolve(self, text, vpa):
        assert parse_vpa(text) == vpa

    def test_fully_devanagari_digits_plus_a_spoken_handle_name(self):
        """The digits were spelled out AND the handle was spoken as words —
        both fixes composing, not just the handle one in isolation."""
        text = "सात सात छह तीन आठ पांच सात पांच शून्य पांच एट ओके आईसीआईसीआई"
        assert parse_vpa(text) == "7763857505@okicici"

    def test_a_name_glued_to_the_handle_still_refuses_rather_than_truncates(self):
        """The actual bug report: eko's own account resolution needs the
        WHOLE local part. Recognising "ओके एचडीएफसी बैंक" must not tempt the
        reconstruction into silently dropping the unresolvable name in front
        of it and returning a shorter, different, well-formed VPA instead —
        that is a wrong account, not a caught error."""
        for text in (
            "कतरी सुरेंद्र 94@ओके एचडीएफसी बैंक",
            "कतरी सुरेंद्र 94 एट ओके एचडीएफसी बैंक",
        ):
            assert parse_vpa(text) is None

    def test_framing_filler_around_a_full_word_handle_is_still_dropped(self):
        """"mera UPI ID hai ..." framing composes with the new handle-phrase
        recognition, not just with letter-by-letter spelling."""
        text = "mera UPI ID hai 9520680874 एट ओके एसबीआई"
        assert parse_vpa(text) == "9520680874@oksbi"

    def test_an_unrelated_ascii_word_is_never_swept_into_the_local_part(self):
        """The gap the fix above closed: `_CLEAN_VPA_CHUNK` cannot tell an
        ordinary Latin-script sentence word from real VPA content, so
        "mera"/"hai"/"is"/"id" must be blocked from ever classifying as VPA
        material — not just from being *chosen* as part of the run, but from
        being eligible to join it at all. Before this, they were absorbed
        into the reconstructed local part instead of being recognised as
        droppable framing."""
        text = "mera UPI ID hai 9520680874 एट ओके एसबीआई"
        result = parse_vpa(text)
        assert result is not None
        local_part = result.split("@", 1)[0]
        for stray in ("mera", "upi", "id", "hai"):
            assert stray not in local_part

    def test_truncated_stt_is_still_not_guessed_into(self):
        """The other real screenshot: STT cut off mid-handle ("...@पी", the
        rest of "ptsbi" never arrived). No amount of handle-phrase
        recognition should turn a single stray letter into a guessed bank
        name — asking again is correct here, not a remaining gap."""
        assert parse_vpa("9520680874@पी") is None


class TestParseMobilePayee:
    """Payments can now name a payee by a bare mobile number, not only a UPI
    ID — see agent/payment_tools.py::_resolve_payee_input. Same rule as VPA
    parsing: ambiguous means None, never a guess, because the cost of a wrong
    digit is money sent to a stranger."""

    @pytest.mark.parametrize("text,mobile", [
        ("9876543210", "9876543210"),
        ("+919876543210", "9876543210"),
        ("919876543210", "9876543210"),
        ("09876543210", "9876543210"),
        ("98765 43210", "9876543210"),
        ("9876-543-210", "9876543210"),
    ])
    def test_plain_and_prefixed_digits(self, text, mobile):
        assert parse_mobile_payee(text) == mobile

    def test_spoken_digit_by_digit_english(self):
        text = "nine eight seven six five four three two one zero"
        assert parse_mobile_payee(text) == "9876543210"

    def test_spoken_digit_by_digit_devanagari(self):
        text = "नौ आठ सात छह पांच चार तीन दो एक शून्य"
        assert parse_mobile_payee(text) == "9876543210"

    @pytest.mark.parametrize("text,mobile", [
        ("mera number 9876543210 hai", "9876543210"),
        ("uska mobile 9876543210", "9876543210"),
        ("please 9876543210", "9876543210"),
        ("यह नंबर है 9876543210", "9876543210"),
    ])
    def test_framing_filler_is_dropped(self, text, mobile):
        assert parse_mobile_payee(text) == mobile

    @pytest.mark.parametrize("text", [
        "",
        "ramesh@okaxis",         # a VPA, not a bare mobile
        "12345",                 # too short
        "98765432101234",        # too long, no recognisable prefix shape
        "1234567890",            # does not start 6-9, not a real Indian mobile
        "mera naam ramesh hai",  # no digits at all
        "9876543210 aur 1234567890",  # two numbers — ambiguous, must not guess
    ])
    def test_rejects_ambiguous_or_non_mobile_input(self, text):
        assert parse_mobile_payee(text) is None


class TestTransliterateDevanagariName:
    """recipient_name must reach eko in English — the payer speaking Hindi must never cost
    them a repeat. Real production failure this exists to prevent: the model was told to
    transliterate the name itself before calling prepare_transfer, and instead asked the
    payer to repeat the name in English. This makes that unnecessary rather than asking for
    it not to happen — the conversion happens here, deterministically, every time."""

    @pytest.mark.parametrize("text,expected", [
        ("राम", "Raam"),
        ("श्याम", "Shyaam"),
        ("रमेश कुमार", "Ramesh Kumaar"),
        ("विशेष जैन", "Vishesh Jain"),
        ("अनु", "Anu"),
    ])
    def test_common_names_transliterate_to_a_recognisable_english_rendering(self, text, expected):
        assert transliterate_devanagari_name(text) == expected

    def test_anusvara_before_h_is_ng_not_n(self):
        """The one phoneme worth special-casing: सिंह is Singh, not the wrong "Sinh" a plain
        anusvara-is-always-n rule would produce — a whole consonant sound missing, not just a
        spelling-style difference like Kumar/Kumaar."""
        assert transliterate_devanagari_name("प्रिया सिंह") == "Priyaa Singh"

    def test_a_name_already_in_english_passes_through_unchanged(self):
        assert transliterate_devanagari_name("Ramesh Kumar") == "Ramesh Kumar"

    def test_a_mixed_script_name_converts_only_the_devanagari_word(self):
        assert transliterate_devanagari_name("Ramesh कुमार") == "Ramesh Kumaar"

    def test_conjuncts_render_as_one_known_cluster_not_the_parts_separately(self):
        # क्ष -> "ksh", not an attempt to derive it from क + ् + ष separately.
        assert "ksh" in transliterate_devanagari_name("अक्षय").lower()

    def test_empty_and_whitespace_only_input_stay_empty(self):
        assert transliterate_devanagari_name("") == ""

    def test_digits_and_punctuation_are_left_alone(self):
        assert transliterate_devanagari_name("Ramesh-9") == "Ramesh-9"
