"""Local Whisper via faster-whisper — no API key, no per-minute cost.

Useful as a permanent baseline: if a paid engine cannot beat this, it is not
worth its bill.
"""

from __future__ import annotations

from pathlib import Path

from .base import Transcriber


class LocalWhisperTranscriber(Transcriber):
    def __init__(self, model_size: str, language: str | None = None,
                 device: str = "cpu", compute_type: str = "int8") -> None:
        self.name = f"local-whisper/{model_size}"
        self._model_size = model_size
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._model = None  # loaded lazily; first load may download ~1.5GB

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )
        return self._model

    def warmup(self) -> None:
        _ = self.model

    def _run(self, audio: Path) -> tuple[str, str | None]:
        segments, info = self.model.transcribe(
            str(audio), language=self._language, beam_size=5
        )
        # `segments` is a generator — consuming it is what actually does the work.
        text = " ".join(segment.text.strip() for segment in segments)
        return text, getattr(info, "language", None)
