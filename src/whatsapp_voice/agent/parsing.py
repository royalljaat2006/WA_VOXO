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

#: Words a user may wrap around a PIN without changing it. "ha" is the
#: colloquial Hinglish spelling of "hai" — extremely common in typed
#: WhatsApp messages — and its absence here silently rejected every real
#: "mera pin 1234 ha" a user actually typed, dropping the pending transfer.
#:
#: The Devanagari entries matter just as much: `_PIN_WORDS` below already
#: accepts Devanagari digit-words, but a Hindi voice PIN is never *just*
#: digits — "मेरा पिन ... है" wraps them the same way "mera pin ... hai"
#: does, and Whisper/Sarvam transcribe Hindi speech in Devanagari script, not
#: Latin. Without these, every Hindi-spoken PIN confirmation was silently
#: dropped as "not a PIN" — observed in a real run, voice only, language 'hi'.
_PIN_FILLER = frozenset({
    "pin", "mera", "meri", "hai", "ha", "he", "is", "my", "the", "code",
    "password", "number", "yeh", "ye", "ok", "okay", "haan", "han",
    "use", "kro", "karo",
    "पिन", "मेरा", "मेरी", "है", "हैं", "हे", "यह", "ये", "हाँ", "हां",
    "कोड", "नंबर", "पासवर्ड",
    # eko calls this field "pintwin" — user-facing prompts now say that word
    # instead of "PIN" (agent/handoff.py, PAYMENT_PROMPT), so a payer reading
    # a prompt back ("mera pintwin 1234 hai") says it too. "twin" alone is
    # included as well, in case a transcript splits "pintwin" into two words.
    "pintwin", "twin", "पिनट्विन", "ट्विन",
    # Politeness particles very common in spoken Hindi/Hinglish PIN confirmations
    # ("sir mera pin ... hai", "... hai ji") — a real transfer was wrongly dropped
    # as "user changed subject" because "ji" wasn't recognised and the strict
    # nothing-but-digits check rejected the whole utterance on that one word.
    "sir", "ji", "जी", "सर",
})

#: Spoken digits for a PIN. Separate from _UNITS: here a bare "do" IS a 2,
#: because the whole utterance is expected to be nothing but digits.
#:
#: Includes Devanagari-phonetic spellings of the *English* digit words
#: ("वन", "टू", "थ्री", "फोर"...) alongside native Hindi ones — Hindi-language
#: STT (Sarvam/Whisper) transliterates English speech into Devanagari script
#: rather than switching to Latin, the same trap `_SPOKEN_DIGITS` (mobile/VPA
#: parsing) already accounts for. A payer digit-by-digit PIN spoken in English
#: but transcribed in Hindi mode ("वन टू थ्री फोर") was silently rejected as
#: "not a PIN" before this — observed in a real transfer, cancelled instead of
#: confirmed.
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
    "ज़ीरो": "0", "जीरो": "0",
    "वन": "1", "टू": "2", "थ्री": "3", "फोर": "4", "फ़ोर": "4",
    "फाइव": "5", "सिक्स": "6", "सेवन": "7", "सेवेन": "7", "नाइन": "9",
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

#: A UPI VPA: `local-part@handle`, e.g. `ramesh@okaxis`. The handle is the PSP
#: code (bank/wallet), always alphanumeric starting with a letter; the local
#: part is looser, matching what NPCI actually allows.
_VPA_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,63}$")

#: Single digits, however Sarvam happens to render them for a Hindi speaker
#: reading a VPA or mobile number *digit by digit* — sometimes the English
#: word transliterated into Devanagari ("सेवेन"), sometimes literal English,
#: sometimes the native Hindi word ("सात"). A VPA's digits are always read
#: this way, never as a number word like an amount is ("panch sau") — so this
#: table is separate from `_UNITS` above and only ever single digits.
#: "एट"/"at" is deliberately absent — it is ambiguous with "eight" and
#: resolved positionally in `_reconstruct_spoken_vpa`, not here.
_SPOKEN_DIGITS: dict[str, str] = {
    "zero": "0", "ज़ीरो": "0", "जीरो": "0", "शून्य": "0",
    "one": "1", "वन": "1", "एक": "1",
    "two": "2", "टू": "2", "दो": "2",
    "three": "3", "थ्री": "3", "तीन": "3",
    "four": "4", "फोर": "4", "फ़ोर": "4", "चार": "4",
    "five": "5", "फाइव": "5", "पांच": "5", "पाँच": "5",
    "six": "6", "सिक्स": "6", "छह": "6", "छे": "6",
    "seven": "7", "सेवन": "7", "सेवेन": "7", "सात": "7",
    "eight": "8", "आठ": "8",
    "nine": "9", "नाइन": "9", "नौ": "9",
}

#: "at"/"एट" — a VPA's '@' read aloud. The same word (both spellings) also
#: means "eight", which `_reconstruct_spoken_vpa` resolves by position, not
#: this table.
_AT_WORDS = {"at", "एट"}

#: Spoken letter names, Devanagari transliteration, for a UPI handle read out
#: letter by letter ("पी टी वाई ई एस" → "ptyes"). A bare single Latin letter
#: ("p", "t"...) already passes through unchanged — this table exists only
#: for the script Sarvam actually returns for a Hindi speaker.
_SPOKEN_LETTERS: dict[str, str] = {
    "ए": "a", "बी": "b", "सी": "c", "डी": "d", "ई": "e", "एफ": "f",
    "जी": "g", "एच": "h", "आई": "i", "जे": "j", "के": "k", "एल": "l",
    "एम": "m", "एन": "n", "ओ": "o", "पी": "p", "क्यू": "q", "आर": "r",
    "एस": "s", "टी": "t", "यू": "u", "वी": "v", "डब्ल्यू": "w",
    "एक्स": "x", "वाई": "y", "ज़ेड": "z", "ज़ी": "z",
}

#: One already-clean token: a bare digit string, a bare Latin letter, or a
#: short run of VPA-legal characters ("ptyes", "9000000000") — the shape a
#: partially-transcribed VPA arrives in when only *some* of it got rendered
#: in Devanagari.
_CLEAN_VPA_CHUNK = re.compile(r"^[a-zA-Z0-9@._-]+$")


#: Full-word spoken forms of common, unambiguous bank-issued UPI handles — a
#: payer who says "ओके एचडीएफसी बैंक" or "okay HDFC bank" instead of spelling
#: "o-k-h-d-f-c-b-a-n-k" letter by letter should not have to repeat
#: themselves. Real failure this fixes: a payer's actual VPA was
#: "khatrisurender94@okhdfcbank" and the transcript carried the handle as
#: three spoken words ("ओके एचडीएफसी बैंक"), which nothing here recognised.
#:
#: Deliberately limited to bank-**issued** handles, which map to exactly one
#: string. "PhonePe"/"Google Pay" are excluded on purpose: their actual handle
#: varies by which bank is linked (@ybl, @okaxis, @oksbi...), and guessing one
#: is exactly the "confidently wrong" failure this module exists to avoid.
#: `oksbi` and `ptyes` are not a guess — both were the real recipient in a
#: production transcript this session; `okicici`/`okaxis` are the same
#: bank-issued family and already appear as this project's own canonical VPA
#: examples (`eko_gateway.py`, test fixtures).
_OK_WORDS = ("ok", "okay", "ओके")
_BANK_SUFFIX_WORDS = ("bank", "बैंक")

#: bank code -> the one handle it actually issues.
_OK_HANDLES: dict[str, str] = {
    "hdfc": "okhdfcbank",
    "sbi": "oksbi",
    "icici": "okicici",
    "axis": "okaxis",
}
#: bank code -> every way a payer says the bank's name, Hindi and English.
_OK_BANK_WORDS: dict[str, tuple[str, ...]] = {
    "hdfc": ("hdfc", "एचडीएफसी"),
    "sbi": ("sbi", "एसबीआई"),
    "icici": ("icici", "आईसीआईसीआई", "आइसीआईसीआई"),
    "axis": ("axis", "एक्सिस"),
}

#: Paytm-issued handles, "pt" + bank code — limited to the two with real
#: evidence (see above), not extended to other banks on a guess.
_PAYTM_WORDS = ("paytm", "पेटीएम")
_PAYTM_HANDLES: dict[str, str] = {"sbi": "ptsbi", "yes": "ptyes"}
_PAYTM_BANK_WORDS: dict[str, tuple[str, ...]] = {
    "sbi": ("sbi", "एसबीआई"),
    "yes": ("yes", "यस"),
}

#: Generic, bank-independent handles spoken as a single whole word rather
#: than spelled out — "9000000000@upi" and "9876543210@paytm" are both
#: already-canonical shapes elsewhere in this project.
_STANDALONE_HANDLES: dict[str, tuple[str, ...]] = {
    "upi": ("upi", "यूपीआई"),
    "paytm": ("paytm", "पेटीएम"),
}


def _build_handle_phrase_patterns() -> list[tuple[re.Pattern[str], str]]:
    """Compiled (pattern, replacement) pairs, most specific first.

    Order matters: "paytm sbi" must be replaced by "ptsbi" before the bare
    "paytm" pattern ever gets a chance to fire on what would otherwise be a
    leftover word — one pass of `re.sub` per pattern, in this order, means
    the combo patterns consume their words before the standalone ones run.
    """

    def alt(words: tuple[str, ...]) -> str:
        return "(?:" + "|".join(re.escape(w) for w in words) + ")"

    patterns: list[tuple[re.Pattern[str], str]] = []
    for code, handle in _OK_HANDLES.items():
        body = rf"{alt(_OK_WORDS)}\s+{alt(_OK_BANK_WORDS[code])}(?:\s+{alt(_BANK_SUFFIX_WORDS)})?"
        patterns.append((re.compile(body, re.IGNORECASE), handle))
    for code, handle in _PAYTM_HANDLES.items():
        bank_words = alt(_PAYTM_BANK_WORDS[code])
        body = rf"{alt(_PAYTM_WORDS)}\s+{bank_words}(?:\s+{alt(_BANK_SUFFIX_WORDS)})?"
        patterns.append((re.compile(body, re.IGNORECASE), handle))
    for handle, words in _STANDALONE_HANDLES.items():
        patterns.append((re.compile(alt(words), re.IGNORECASE), handle))
    return patterns


_HANDLE_PHRASE_PATTERNS = _build_handle_phrase_patterns()


def _expand_spoken_handles(text: str) -> str:
    """Replace a spoken bank-handle phrase with its compact ASCII form.

    Runs on the raw transcript, before any tokenising — "94 एट ओके एचडीएफसी
    बैंक" becomes "94 एट okhdfcbank": three unclassifiable Devanagari words
    collapse into one clean token `_reconstruct_spoken_vpa` can recognise
    whole, the same way an already-Latin handle is recognised today.
    """
    for pattern, handle in _HANDLE_PHRASE_PATTERNS:
        text = pattern.sub(handle, text)
    return text


#: What a payer's sentence may contain *around* a dictated VPA without that
#: being part of it — "mera UPI ID hai <vpa>". Deliberately small and
#: word-for-word, the same reasoning as `_PIN_FILLER`: anything NOT in this
#: set that `_reconstruct_spoken_vpa` would otherwise drop is treated as
#: unresolvable rather than discarded, because it might be real content
#: (a name) rather than framing — see that function's safety check.
_VPA_FRAME_FILLER = frozenset({
    "mera", "meri", "hai", "ye", "yeh", "is", "the", "my", "upi", "id",
    "apna", "apni", "please", "kripya",
    "मेरा", "मेरी", "है", "ये", "यह", "अपना", "अपनी", "यूपीआई", "आईडी", "कृपया",
})


#: Words that are ONLY ever grammar around a dictated VPA, never legitimate
#: VPA content themselves — unlike "upi"/"paytm" (in `_VPA_FRAME_FILLER` too,
#: but also real standalone handles), nothing here is ever a real handle or
#: local part on its own, so these must never be swept into a reconstructed
#: VPA just because they happen to be pure ASCII letters.
#:
#: Without this, "mera UPI ID hai ptyes एट ओके आइसीआईसीआई" reconstructed to
#: "meraupiidhaiptyes@okicici" — a wrong-but-valid-shaped VPA, not a caught
#: error — because `_CLEAN_VPA_CHUNK` cannot tell an ordinary Latin-script
#: sentence word from a fragment of a dictated VPA.
_VPA_NEVER_CONTENT = frozenset({
    "mera", "meri", "hai", "ye", "yeh", "is", "the", "my", "id",
    "apna", "apni", "please", "kripya",
})


def _classify_vpa_token(token: str) -> str | None:
    """One token → the character(s) it contributes to a VPA, or None if it
    is not part of one (filler, noise, an unrecognised word)."""
    low = token.lower()
    if low in _VPA_NEVER_CONTENT:
        return None
    if low in _SPOKEN_DIGITS:
        return _SPOKEN_DIGITS[low]
    if low in _AT_WORDS:
        return "@"  # tentative; `_reconstruct_spoken_vpa` may rewrite to "8"
    if token in _SPOKEN_LETTERS:
        return _SPOKEN_LETTERS[token]
    if len(token) == 1 and token.isalpha() and token.isascii():
        return token.lower()
    if _CLEAN_VPA_CHUNK.match(token):
        return token.lower()
    return None


def _looks_like_handle_piece(token: str) -> bool:
    """Whether this token is evidence a UPI handle has started — used only to
    resolve "एट"/"at" against its "eight" reading. Deliberately broader than
    "a single spelled-out letter": a handle can also arrive already-clean
    ("ptyes") when only the digits before it got Devanagari-transliterated."""
    if token in _SPOKEN_LETTERS:
        return True
    if len(token) == 1 and token.isalpha() and token.isascii():
        return True
    low = token.lower()
    if low in _SPOKEN_DIGITS or low in _AT_WORDS:
        return False
    return bool(_CLEAN_VPA_CHUNK.match(token)) and any(c.isalpha() for c in token)


def _reconstruct_spoken_vpa(text: str) -> str | None:
    """Rebuild a VPA from a transcript where Sarvam wrote spoken digits and
    letters out as words instead of characters — `parse_vpa`'s fallback for
    exactly this, once the plain "already mostly Latin/digits" path fails.

    Real example that motivated this: a payer read "7763857505@ptyes" aloud
    digit by digit, and a Hindi-language transcript came back as
    "सेवेन सेवेन सिक्स थ्री एट फाइव सेवेन फाइव ज़ीरो फाइव एट पी टी वाई ई एस" —
    the English digit and letter *names*, transliterated into Devanagari, not
    digits or Latin letters at all. `_VPA_RE` correctly rejects that outright;
    this recovers it instead of making the payer repeat themselves.

    Finds the **longest contiguous run** of classifiable tokens and treats
    everything outside it as surrounding sentence noise ("mera UPI ID hai",
    "at the rate") — deliberately not stripping filler word by word, which
    would mean silently guessing which words are safe to drop. A genuinely
    unrecognisable or noisy transcript breaks the run and this returns None,
    same as `parse_vpa` already does — asking again is the safe failure, not
    a best-effort reconstruction through words that make no sense.
    """
    tokens = text.strip().split()
    if not tokens:
        return None

    classified = [_classify_vpa_token(t) for t in tokens]

    best_start = best_len = -1
    run_start = None
    for i, c in enumerate(classified + [None]):
        if c is not None and run_start is None:
            run_start = i
        elif c is None and run_start is not None:
            if i - run_start > best_len:
                best_start, best_len = run_start, i - run_start
            run_start = None

    if best_start == -1:
        return None

    # Everything outside the chosen run must be recognised framing filler —
    # "mera UPI ID hai" — or this refuses rather than reconstructs. Without
    # this check, an unresolvable name glued directly onto the real local
    # part (no separator, e.g. "khatrisurender94") could have its name
    # silently dropped, leaving a shorter, different, well-formed VPA that
    # still passes _VPA_RE — a wrong-but-plausible account, not a caught
    # error. Observed as a live risk once handle-phrase expansion (below)
    # started turning previously-unclassifiable trailing words into a clean
    # run on its own.
    excluded = tokens[:best_start] + tokens[best_start + best_len:]
    if any(t.lower() not in _VPA_FRAME_FILLER for t in excluded):
        return None

    run_tokens = tokens[best_start:best_start + best_len]
    run_chars = classified[best_start:best_start + best_len]

    # Resolve "at"/"एट" within the chosen run only: the LAST occurrence that
    # still has something handle-shaped after it is the VPA's one "@"; every
    # earlier occurrence is "8" — a VPA has exactly one separator, and the
    # digits always come before the handle. "Handle-shaped" is deliberately
    # broader than "a single spelled-out letter": a handle can also arrive
    # already-clean ("ptyes") when only the digits got Devanagari-
    # transliterated — see test_mixed_script_handle_already_latin.
    at_positions = [i for i, t in enumerate(run_tokens) if t.lower() in _AT_WORDS]
    chosen_at = None
    for i in at_positions:
        if any(_looks_like_handle_piece(run_tokens[j]) for j in range(i + 1, len(run_tokens))):
            chosen_at = i
    for i in at_positions:
        run_chars[i] = "@" if i == chosen_at else "8"

    return "".join(run_chars) or None


def parse_vpa(text: str) -> str | None:
    """Extract a UPI VPA from speech or text. None if it is not unambiguously one.

    Accepts a literal `ramesh@okaxis` as well as the spoken forms ASR actually
    produces for '@' — `ramesh at okaxis`, `ramesh at the rate okaxis` — and,
    failing that, a transcript where Sarvam wrote spoken digits and letters
    out as Devanagari words instead of characters; see
    `_reconstruct_spoken_vpa`.

    Deliberately strict, same reasoning as `extract_pin`: a string that is not
    clearly `local@handle` returns None rather than a best guess, so the caller
    asks the user again instead of resolving to the wrong account.
    """
    if not text:
        return None

    expanded = _expand_spoken_handles(text)
    normalised = " ".join(expanded.strip().split())
    normalised = re.sub(r"\s+at\s+the\s+rate\s+", "@", normalised, flags=re.IGNORECASE)
    normalised = re.sub(r"\s+at\s+", "@", normalised, flags=re.IGNORECASE)
    normalised = normalised.replace(" ", "").lower()

    if _VPA_RE.match(normalised):
        return normalised

    reconstructed = _reconstruct_spoken_vpa(expanded)
    return reconstructed if reconstructed and _VPA_RE.match(reconstructed) else None


def payer_mobile(payer: str) -> str | None:
    """A WhatsApp payer id reduced to the bare 10-digit mobile, or None.

    `919876543210` → `9876543210`. Returns None rather than padding or
    truncating anything implausible into shape: the demo path's
    `demo:<session_id>` is a payer id that is not a phone number at all, and
    turning it into a mobile-shaped string would aim a real request at a real
    stranger's number.

    Callers that must not proceed without one (`eko_gateway._initiator_id_for`)
    raise on None; callers where it is merely a nice-to-have
    (`pintwin_push`) skip the work instead.
    """
    digits = "".join(c for c in (payer or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def mobile_from_vpa(vpa: str) -> str | None:
    """A mobile number embedded in a VPA's local part, when there is one.

    **Most VPAs do not contain one.** The local part is whatever the payee's
    bank let them choose — a name, a nickname, a mobile, a name and some
    digits, anything: `9000000000@upi`, `firstname.lastname@okicici`,
    `nickname99@ybl`. So this returns a number for the first shape and `None`
    for the rest, and `None` is an ordinary answer here, not a failure.

    It exists because eko's confirmed transfer contract always needs a mobile
    number — `recipient_id_type: mobile_number` / `customer_id: <mobile>` —
    with the VPA riding along separately as `customer_vpa`. When the spoken
    UPI ID happens to carry the mobile, that is one less thing to ask for; when
    it does not, the caller has to ask. What it must never do is *invent* one,
    which is why a local part with too few digits returns `None` rather than
    something padded or truncated into shape.

    Requires the digits to be the whole local part: `firstname9876543210@ybl`
    yields None, because digits sitting inside a name are far more likely to be
    part of the handle than a phone number, and a wrong `customer_id` sends
    money to a stranger.
    """
    local = vpa.split("@", 1)[0]
    digits = "".join(c for c in local if c.isdigit())
    # 10 plain digits, or the same with a country code — and nothing else.
    if digits != local or len(digits) not in (10, 11, 12):
        return None
    return digits[-10:]


#: A real Indian mobile number: 10 digits, first digit 6-9 (no landline,
#: no obviously-wrong shape). eko's transfer contract can identify a payee
#: this way directly — see `EkoPaymentGateway.confirm_and_execute`'s
#: mobile-only branch.
_INDIAN_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")

#: What a payer's sentence may contain *around* a dictated phone number —
#: "mera number hai ...", "uska mobile ... hai" — same reasoning as
#: `_VPA_FRAME_FILLER`: only recognised filler may be dropped, so a name or
#: any other unrecognised word makes the whole utterance ambiguous rather
#: than being silently trimmed off.
_MOBILE_FRAME_FILLER = frozenset({
    "mera", "uska", "iska", "unka", "apna", "number", "no", "mobile", "hai",
    "ye", "yeh", "is", "the", "my", "his", "her", "please",
    "मेरा", "उसका", "इसका", "उनका", "अपना", "नंबर", "मोबाइल", "है", "ये", "यह",
})


def parse_mobile_payee(text: str) -> str | None:
    """Extract a bare 10-digit mobile-number payee from speech — the payee
    identifier when the user dictates a number instead of a UPI ID.

    Handles the same digit-by-digit spoken forms `parse_vpa` does for a
    VPA's digits — plain digits, spoken English/Hindi digit names, and their
    Devanagari-transliterated forms (reusing `_SPOKEN_DIGITS`) — plus a
    leading `+91`/`91`/`0` country/trunk prefix, stripped before validation.

    Deliberately strict, same reasoning as `extract_pin` and `parse_vpa`:
    once recognised filler is removed, the utterance must be **nothing but
    digits**. A number mixed with a name, a UPI handle, or any unrecognised
    word returns None rather than a best guess — the caller asks again
    instead of aiming a transfer at a mistranscribed stranger's number.
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
        elif word in _SPOKEN_DIGITS:
            digits.append(_SPOKEN_DIGITS[word])
        elif word in _MOBILE_FRAME_FILLER:
            continue
        else:
            return None  # a real sentence, not a bare phone number

    number = "".join(digits)
    if len(number) == 12 and number.startswith("91"):
        number = number[2:]
    elif len(number) == 11 and number.startswith("0"):
        number = number[1:]

    return number if _INDIAN_MOBILE_RE.match(number) else None


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


# --------------------------------------------------------------- name transliteration

#: Independent Devanagari vowels (no preceding consonant).
_DEVANAGARI_INDEP_VOWELS: dict[str, str] = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}

#: Dependent vowel signs (matras) attached to a consonant, replacing its
#: inherent "a". Order matches the independent vowels above.
_DEVANAGARI_MATRAS: dict[str, str] = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo",
    "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
}

#: Consonants, Roman equivalent *without* the inherent vowel — the matra table
#: above (or its absence) supplies whatever vowel sound follows.
_DEVANAGARI_CONSONANTS: dict[str, str] = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    # Nukta forms (a dot under the base letter) — distinct sounds in Urdu/Hinglish
    # loanwords, still commonly seen in Hindi names.
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f",
}

#: Two-letter conjuncts common enough in names to spell out rather than derive
#: from their two components + virama (क्ष → "ksh", not "kṣ"-style "kṣh").
_DEVANAGARI_CONJUNCTS: dict[str, str] = {
    "क्ष": "ksh", "ज्ञ": "gy", "त्र": "tr", "श्र": "shr",
}

_DEVANAGARI_VIRAMA = "्"  # Suppresses a consonant's inherent vowel (conjuncts).
_DEVANAGARI_ANUSVARA = "ं"  # Nasalisation — rendered "n", close enough for a label.
_DEVANAGARI_CHANDRABINDU = "ँ"  # Lighter nasalisation — same rendering.
_DEVANAGARI_VISARGA = "ः"  # Rare in names; rendered "h".


def _transliterate_devanagari_word(word: str) -> str:
    """One space-free chunk of Devanagari (plus whatever Latin/digits/punctuation
    it already contains) into a Roman-script approximation.

    Not a linguistically exact transliteration (there is no such thing without a
    name-specific spelling dictionary — "दीपांशु" could honestly be "Deepanshu"
    or "Deepaanshu"): it is a best-effort, deterministic rendering good enough for
    a bank's recipient_name label and for the payer to recognise on read-back.
    Non-Devanagari characters (already-Roman letters, digits, punctuation) pass
    through untouched, so a mixed-script name transliterates only the part that
    needs it.
    """
    out: list[str] = []
    i = 0
    n = len(word)
    while i < n:
        # Longest match first: a listed conjunct beats its parts read separately.
        conjunct = next(
            (c for c in _DEVANAGARI_CONJUNCTS if word.startswith(c, i)), None,
        )
        if conjunct is not None:
            out.append(_DEVANAGARI_CONJUNCTS[conjunct])
            i += len(conjunct)
            # A conjunct already carries its own vowel treatment; a following
            # matra (rare) still applies, an inherent "a" does not.
            if i < n and word[i] in _DEVANAGARI_MATRAS:
                out[-1] += _DEVANAGARI_MATRAS[word[i]]
                i += 1
            elif i < n and word[i] == _DEVANAGARI_VIRAMA:
                i += 1
            continue

        char = word[i]
        if char in _DEVANAGARI_INDEP_VOWELS:
            out.append(_DEVANAGARI_INDEP_VOWELS[char])
            i += 1
            continue
        if char in _DEVANAGARI_CONSONANTS:
            roman = _DEVANAGARI_CONSONANTS[char]
            nxt = word[i + 1] if i + 1 < n else ""
            if nxt in _DEVANAGARI_MATRAS:
                out.append(roman + _DEVANAGARI_MATRAS[nxt])
                i += 2
            elif nxt == _DEVANAGARI_VIRAMA:
                out.append(roman)  # consonant cluster: no vowel here
                i += 2
            elif i == n - 1 or (i + 1 == n - 1 and word[i + 1] in (
                _DEVANAGARI_ANUSVARA, _DEVANAGARI_CHANDRABINDU, _DEVANAGARI_VISARGA,
            )):
                # Word-final bare consonant: Hindi drops the inherent "a" here
                # (schwa deletion) — "राम" is "Raam", not "Raama".
                out.append(roman)
                i += 1
            else:
                out.append(roman + "a")
                i += 1
            continue
        if char in (_DEVANAGARI_ANUSVARA, _DEVANAGARI_CHANDRABINDU):
            # Before ह specifically, anusvara nasalises to "ng" ("सिंह" is "Singh", not
            # "Sinh") — the single common-name case worth special-casing rather than
            # building full velar/palatal/retroflex/dental/labial anusvara disambiguation.
            out.append("ng" if i + 1 < n and word[i + 1] == "ह" else "n")
            i += 1
            continue
        if char == _DEVANAGARI_VISARGA:
            out.append("h")
            i += 1
            continue
        if char == _DEVANAGARI_VIRAMA:
            i += 1  # stray virama with nothing to attach to
            continue
        # Not Devanagari at all — already-Roman letters, digits, punctuation.
        out.append(char)
        i += 1
    return "".join(out)


def transliterate_devanagari_name(text: str) -> str:
    """A name (or any short phrase) with every Devanagari word rendered in Roman
    script, word boundaries and capitalisation preserved as a name would expect.

    Used for `recipient_name` before it reaches eko (`agent/payment_tools.py`):
    eko's bank rail takes English only, and the payer must never be asked to
    repeat a name they already said just because the model passed it through in
    Devanagari — this makes that unnecessary rather than asking for it not to
    happen. A word already in Roman script (or mixed script) is returned as-is
    for the Roman part; only the Devanagari portion is converted.
    """
    words = text.split(" ")
    rendered = []
    for word in words:
        if not word:
            rendered.append(word)
            continue
        converted = _transliterate_devanagari_word(word)
        rendered.append(converted[:1].upper() + converted[1:] if converted else converted)
    return " ".join(rendered)
