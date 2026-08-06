"""Speech-to-text engines behind one interface."""

from .base import Transcriber, Transcript, TranscriptionError
from .fallback import FallbackTranscriber
from .registry import build_primary_transcriber, build_transcribers

__all__ = [
    "Transcriber",
    "Transcript",
    "TranscriptionError",
    "FallbackTranscriber",
    "build_transcribers",
    "build_primary_transcriber",
]
