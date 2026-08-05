"""Turning spoken Indian numbers into money, and spoken digits into a PIN.

This is the highest-risk code in the payment path. A transcript read as 2 instead
of 2000, or 200 instead of 2000, sends the wrong amount to a real person and
cannot be undone.

So the rule throughout: **when uncertain, return None.** The caller then asks the
user rather than guessing. An extra question costs a second; a wrong amount costs
money.

Amounts are returned in **paise as int**, never rupees as float — floats lose
precision on money.

### The "bhej do" trap

Hindi `do` means both the number 2 and the imperative "give". "Ramesh ko 2000
bhej do" ends in `do`, and a naive parser reads a second amount of ₹2 there,
making the whole sentence ambiguous.

The fix: **a spoken number word only counts when a multiplier follows it.**
"do hazaar" is an amount; a bare "do" is not. Nobody dictates ₹2 by voice, and
if they do they can say "2". Digits are always accepted, since `2000` is
unambiguous.
"""

from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------- numerals

#: Devanagari digits map onto ASCII positionally.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

#: Spoken number words, Hindi and English. Only ever used when followed by a
#: multiplier — see the module docstring.
_UNITS: dict[str, int] = {
    "ek": 1, "one": 1,
    "do": 2, "two": 2,
    "teen": 3, "tin": 3, "three": 3,
    "char": 4, "chaar": 4, "four": 4,
    "panch": 5, "paanch": 5, "five": 5,
    "chhe": 6, "che": 6, "chah": 6, "six": 6,
    "saat": 7, "seven": 7,
    "aath": 8, "ath": 8, "eight": 8,
    "nau": 9, "nine": 9,
    "das": 10, "dus": 10, "ten": 10,
    "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19,
    "bees": 20, "bis": 20, "twenty": 20,
    "pachees": 25, "pachchees": 25, "twentyfive": 25,
    "tees": 30, "thirty": 30,
    "chalees": 40, "chalis": 40, "forty": 40,
    "pachas": 50, "pachaas": 50, "fifty": 50,
    "saath": 60, "sixty": 60,
    "sattar": 70, "seventy": 70,
    "assi": 80, "eighty": 80,
    "nabbe": 90, "ninety": 90,
    # Devanagari — what Sarvam actually returns for Hindi speech. Without these,
    # a Hindi speaker's amount is silently unparseable.
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5,
    "छह": 6, "छे": 6, "सात": 7, "आठ": 8, "नौ": 9,
    "दस": 10, "ग्यारह": 11, "बारह": 12, "पंद्रह": 15,
    "बीस": 20, "पच्चीस": 25, "तीस": 30, "चालीस": 40, "पचास": 50,
    "साठ": 60, "सत्तर": 70, "अस्सी": 80, "नब्बे": 90,
}

#: Multipliers. `sau` doubles as one ("paanch sau" = 500).
_MULTIPLIERS: dict[str, int] = {
    "sau": 100, "hundred": 100,
    "hazaar": 1000, "hazar": 1000, "hajar": 1000, "hajaar": 1000,
    "thousand": 1000,
    "lakh": 100_000, "lac": 100_000, "lakhs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000,
    # Devanagari. Both spellings of hazaar are listed because the nukta (़) is a
    # combining mark that survives normalisation, so the two are distinct keys.
    "सौ": 100, "हज़ार": 1000, "हजार": 1000, "लाख": 100_000, "करोड़": 10_000_000,
}

#: Halves and quarters Indian speech uses constantly *before* a multiplier.
#: "dhaai hazaar" = 2500.
_FRACTIONAL: dict[str, float] = {
    "adha": 0.5, "aadha": 0.5, "half": 0.5,
    "dedh": 1.5, "derh": 1.5,
    "dhaai": 2.5, "dhai": 2.5, "adhai": 2.5,
    "आधा": 0.5, "डेढ़": 1.5, "ढाई": 2.5,
}

#: Currency words stripped before parsing, so they cannot end a number early.
_CURRENCY_NOISE = re.compile(
    r"\b(rupaye|rupaya|rupees|rupee|rs|inr|paise)\b|[₹]", re.IGNORECASE
)

def _strip_punctuation(text: str) -> str:
    """Remove punctuation and symbols by Unicode category, keeping marks.

    A `[^\\w\\s]` regex would strip Devanagari combining marks, because `\\w`
    excludes category Mn — "हज़ार" would lose its nukta and "पाँच" its chandrabindu,
    silently breaking every Hindi amount. Same trap already documented in
    `metrics.py`.
    """
    return "".join(
        " " if unicodedata.category(char).startswith(("P", "S")) else char
        for char in text
    )

#: Words a user may wrap around a PIN without changing it.
_PIN_FILLER = frozenset({
    "pin", "mera", "meri", "hai", "he", "is", "my", "the", "code",
    "password", "number", "yeh", "ye", "ok", "okay", "haan", "han",
})

#: Spoken digits for a PIN. Separate from _UNITS: here a bare "do" IS a 2,
#: because the whole utterance is expected to be nothing but digits.
_PIN_WORDS: dict[str, str] = {
    "zero": "0", "shunya": "0", "sifar": "0", "o": "0",
    "ek": "1", "one": "1",
    "do": "2", "two": "2",
    "teen": "3", "tin": "3", "three": "3",
    "char": "4", "chaar": "4", "four": "4",
    "panch": "5", "paanch": "5", "five": "5",
    "chhe": "6", "che": "6", "chah": "6", "six": "6",
    "saat": "7", "seven": "7",
    "aath": "8", "ath": "8", "eight": "8",
    "nau": "9", "nine": "9",
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4",
    "पांच": "5", "पाँच": "5", "छह": "6", "छे": "6",
    "सात": "7", "आठ": "8", "नौ": "9",
}


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DEVANAGARI_DIGITS)
    text = _CURRENCY_NOISE.sub(" ", text)
    # Digit grouping only: "2,000" -> "2000". Between digits, so it can never
    # merge two separate numbers.
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = _strip_punctuation(text)
    return " ".join(text.lower().split())


# ----------------------------------------------------------------- amount

def parse_amount_paise(text: str) -> int | None:
    """Extract a rupee amount from speech, in paise.

    Returns None when the text holds no amount, or more than one — the caller
    must ask rather than assume.

    Handles: `2000` · `2,000` · `₹2000` · `2000 rupaye` · `do hazaar` ·
    `बीस हज़ार` · `2 lakh` · `dhaai hazaar` · `paanch sau` ·
    `ek lakh bees hazaar`.
    """
    if not text:
        return None

    words = _normalise(text).split()
    if not words:
        return None

    amounts = [a for a in _collect_amounts(words) if a > 0]

    # Two different amounts in one sentence is ambiguous — ask, don't pick.
    if len(set(amounts)) != 1:
        return None

    return int(round(amounts[0] * 100))


def _collect_amounts(words: list[str]) -> list[float]:
    """Every complete number in the token stream.

    A value is taken from a digit group, or from a word/fraction **only when a
    multiplier follows it**. Compound amounts accumulate ("ek lakh bees hazaar"
    = 120000) until an unrelated word closes them.
    """
    amounts: list[float] = []
    current = 0.0
    open_number = False
    index = 0

    def close() -> None:
        nonlocal current, open_number
        if open_number and current > 0:
            amounts.append(current)
        current, open_number = 0.0, False

    while index < len(words):
        word = words[index]
        following = words[index + 1] if index + 1 < len(words) else None
        has_multiplier = following in _MULTIPLIERS

        if word.isdigit():
            value: float | None = float(word)
        elif word in _FRACTIONAL and has_multiplier:
            value = _FRACTIONAL[word]
        elif word in _UNITS and has_multiplier:
            value = float(_UNITS[word])
        else:
            # Bare number words, stray multipliers and unknown words all end the
            # current number. This is what defuses "bhej do".
            close()
            index += 1
            continue

        if has_multiplier:
            current += value * _MULTIPLIERS[following]  # type: ignore[index]
            index += 2
        else:
            current += value
            index += 1
        open_number = True

    close()
    return amounts


def format_amount(paise: int) -> str:
    """`200000` -> `2,000.00`, with Indian digit grouping."""
    rupees, remainder = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        digits = f"{head},{tail}"
    return f"{digits}.{remainder:02d}"


# -------------------------------------------------------------------- PIN

def extract_pin(text: str, lengths: tuple[int, ...] = (4, 6)) -> str | None:
    """Pull a PIN out of speech. None if it is not unambiguously one.

    Accepts `1234` · `1 2 3 4` · `१२३४` · `one two three four` ·
    `ek do teen char` · `mera pin 1234 hai`.

    Deliberately strict: once filler words are removed, the utterance must be
    **nothing but digits**, and the count must match exactly. "Ramesh ko 2000
    bhej do" can therefore never be mistaken for a PIN.

    ⚠️ The return value is a secret. Never log it, store it, or pass it to a
    model — see `PaymentIntentHandler`.
    """
    if not text:
        return None

    normalised = _normalise(text)
    if not normalised:
        return None

    digits: list[str] = []
    for word in normalised.split():
        if word.isdigit():
            digits.extend(word)
        elif word in _PIN_WORDS:
            digits.append(_PIN_WORDS[word])
        elif word in _PIN_FILLER:
            continue
        else:
            return None  # a real sentence, not a PIN

    return "".join(digits) if len(digits) in lengths else None
