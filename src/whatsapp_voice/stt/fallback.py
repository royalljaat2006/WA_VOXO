"""Wraps two engines so the primary's failure does not become the reply.

Not a leaf engine — a composite over two others, so it delegates to each
child's own `transcribe()` rather than implementing `_run`. That way the
`Transcript` returned carries whichever child actually produced it (its real
`engine` name, latency, and detected language), not this wrapper's identity.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import Transcriber, Transcript, TranscriptionError

logger = logging.getLogger(__name__)


class FallbackTranscriber(Transcriber):
    def __init__(self, primary: Transcriber, fallback: Transcriber) -> None:
        self.name = f"{primary.name}+fallback:{fallback.name}"
        self._primary = primary
        self._fallback = fallback

    def warmup(self) -> None:
        # Both must be ready before the first real request — discovering the
        # fallback is misconfigured only during an outage defeats the point.
        self._primary.warmup()
        self._fallback.warmup()

    def transcribe(self, audio: Path) -> Transcript:
        try:
            return self._primary.transcribe(audio)
        except TranscriptionError as exc:
            logger.warning(
                "%s failed (%s) — falling back to %s",
                self._primary.name, exc, self._fallback.name,
            )
            return self._fallback.transcribe(audio)

    def _run(self, audio: Path) -> tuple[str, str | None]:
        # Unreachable: transcribe() is overridden above and never calls this.
        raise NotImplementedError("FallbackTranscriber delegates via transcribe(), not _run")
