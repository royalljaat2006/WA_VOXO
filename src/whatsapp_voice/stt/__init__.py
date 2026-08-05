"""Speech-to-text engines behind one interface."""

from .base import Transcriber, Transcript, TranscriptionError
from .registry import build_primary_transcriber, build_transcribers

__all__ = [
    "Transcriber",
    "Transcript",
    "TranscriptionError",
    "build_transcribers",
    "build_primary_transcriber",
]
