"""Transcription accuracy metrics.

Caveat worth remembering: for Hinglish, WER punishes script choice as harshly
as it punishes genuine errors — "aaj" and "आज" score as a total miss. Treat WER
as a rough signal and read the transcripts.
"""

from __future__ import annotations

import unicodedata


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, normalise Unicode.

    Punctuation is removed by Unicode category rather than by `[^\\w\\s]`, which
    would also strip Devanagari combining marks — `\\w` does not match category
    Mn, so "के" would collapse to "क" and every Hindi transcript would score as
    garbage. Categories P (punctuation) and S (symbol) go; letters, marks and
    digits stay.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    stripped = "".join(
        " " if unicodedata.category(char).startswith(("P", "S")) else char
        for char in text
    )
    return " ".join(stripped.split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over words, divided by reference length.

    0.0 is perfect. Values above 1.0 are possible when the hypothesis inserts
    more words than the reference contains.
    """
    ref = normalise(reference).split()
    hyp = normalise(hypothesis).split()

    if not ref:
        raise ValueError("reference must contain at least one word")
    if not hyp:
        return 1.0

    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            current.append(
                min(
                    previous[j] + 1,                          # deletion
                    current[j - 1] + 1,                       # insertion
                    previous[j - 1] + (ref_word != hyp_word),  # substitution
                )
            )
        previous = current

    return previous[-1] / len(ref)
