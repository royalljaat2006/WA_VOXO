"""The one interface every speech-to-text engine implements.

Phase 0 uses it to run engines side by side; Phase 3 uses it to transcribe a
single incoming voice note. Swapping engines later is a config change, not a
code change — which is the whole point of the seam.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str
    latency_s: float
    #: BCP-47 tag the engine detected, when it reports one. Threaded into the
    #: system prompt so the reply comes back in the language the user spoke.
    language: str | None = None


#: Below this, a transcript is treated as a failed recognition rather than a
#: short answer. Measured against Sarvam on real recordings: genuine speech runs
#: 14–17 characters per second in both Devanagari and Latin script, while noise
#: and silence return nothing at all. Three leaves roughly a 5× margin, which is
#: the right side to err on — a needless "please repeat" costs a second, acting
#: on a fragment can cost money.
MIN_CHARS_PER_SECOND = 3.0

#: Utterances shorter than this are exempt. "Haan", "1234" and "cancel" are
#: legitimately tiny, and the ratio is meaningless over a fraction of a second.
MIN_DURATION_FOR_YIELD_CHECK_S = 2.5


def is_low_yield(text: str, duration_s: float) -> bool:
    """True when a recording produced far less text than its length implies.

    Catches the *partial* failure the empty-transcript check misses: several
    seconds of speech that come back as a two-word fragment, which then looks
    like a confident short instruction to everything downstream.

    Sarvam reports no confidence score — `saarika:v2.5` returns only
    `request_id`, `transcript` and `language_code` — so this ratio is the signal
    actually available, rather than the one we would prefer.
    """
    if duration_s < MIN_DURATION_FOR_YIELD_CHECK_S:
        return False
    stripped = text.strip()
    if not stripped:
        return True
    return len(stripped) / duration_s < MIN_CHARS_PER_SECOND


class TranscriptionError(RuntimeError):
    """An engine could not produce a transcript."""


class Transcriber(ABC):
    """Base class handling timing and error wrapping.

    Subclasses implement `_run` and stay free of boilerplate.
    """

    #: Stable identifier used in reports and logs, e.g. "openai/whisper-1".
    name: str

    def warmup(self) -> None:  # noqa: B027 - intentionally optional, not abstract
        """Do expensive one-time setup now rather than during the first call.

        Without this, model loading (which can take tens of seconds and may
        download gigabytes) is charged to the first transcription's latency,
        making benchmark comparisons meaningless and the first reply to a real
        user needlessly slow. Default is a no-op; must be idempotent.
        """

    def transcribe(self, audio: Path) -> Transcript:
        started = time.perf_counter()
        try:
            text, language = self._run(audio)
        except TranscriptionError:
            raise
        except Exception as exc:  # noqa: BLE001 - engines raise wildly varied types
            raise TranscriptionError(f"{type(exc).__name__}: {exc}") from exc
        return Transcript(
            text=text.strip(),
            engine=self.name,
            latency_s=time.perf_counter() - started,
            language=language,
        )

    @abstractmethod
    def _run(self, audio: Path) -> tuple[str, str | None]:
        """Return (transcript, detected_language). Language may be None."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"
