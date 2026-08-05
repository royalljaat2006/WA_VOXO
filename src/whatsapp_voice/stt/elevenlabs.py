"""ElevenLabs Scribe."""

from __future__ import annotations

from pathlib import Path

import requests

from .base import Transcriber, TranscriptionError

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class ElevenLabsTranscriber(Transcriber):
    def __init__(self, api_key: str, model: str = "scribe_v1",
                 timeout_s: int = 180) -> None:
        self.name = f"elevenlabs/{model}"
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def _run(self, audio: Path) -> tuple[str, str | None]:
        with audio.open("rb") as handle:
            response = requests.post(
                API_URL,
                headers={"xi-api-key": self._api_key},
                files={"file": (audio.name, handle, "audio/wav")},
                data={"model_id": self._model},
                timeout=self._timeout_s,
            )
        if not response.ok:
            raise TranscriptionError(
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        return payload.get("text", ""), payload.get("language_code")
