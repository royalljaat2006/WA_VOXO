"""OpenAI transcription (whisper-1 and the gpt-4o-transcribe family)."""

from __future__ import annotations

from pathlib import Path

from .base import Transcriber


class OpenAITranscriber(Transcriber):
    def __init__(self, api_key: str, model: str, timeout_s: int = 180) -> None:
        self.name = f"openai/{model}"
        self._model = model
        self._timeout_s = timeout_s
        self._api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, timeout=self._timeout_s)
        return self._client

    def _run(self, audio: Path) -> tuple[str, str | None]:
        with audio.open("rb") as handle:
            response = self.client.audio.transcriptions.create(
                model=self._model, file=handle
            )
        return response.text, getattr(response, "language", None)
