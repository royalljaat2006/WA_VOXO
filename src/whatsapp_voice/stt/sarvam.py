"""Sarvam AI — Indic-first ASR, the most promising candidate for Hinglish.

Model names move fast, so the model id is configuration rather than a constant.
"""

from __future__ import annotations

from pathlib import Path

import requests

from .base import Transcriber, TranscriptionError

API_URL = "https://api.sarvam.ai/speech-to-text"


class SarvamTranscriber(Transcriber):
    name = "sarvam"

    def __init__(self, api_key: str, model: str, language: str,
                 timeout_s: int = 180) -> None:
        self.name = f"sarvam/{model}"
        self._api_key = api_key
        self._model = model
        self._language = language
        self._timeout_s = timeout_s

    def _run(self, audio: Path) -> tuple[str, str | None]:
        with audio.open("rb") as handle:
            response = requests.post(
                API_URL,
                headers={"api-subscription-key": self._api_key},
                files={"file": (audio.name, handle, "audio/wav")},
                data={"model": self._model, "language_code": self._language},
                timeout=self._timeout_s,
            )
        if not response.ok:
            raise TranscriptionError(
                f"HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        return payload.get("transcript", ""), payload.get("language_code")
