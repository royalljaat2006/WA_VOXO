"""Google Cloud Speech-to-Text.

Authenticates with an explicit service-account JSON key file rather than
relying on ambient Application Default Credentials, matching the project's
"every credential is a typed setting" rule (see config.py).
"""

from __future__ import annotations

from pathlib import Path

from .base import Transcriber


class GoogleSpeechTranscriber(Transcriber):
    def __init__(
        self,
        credentials_path: str,
        model: str = "latest_long",
        language: str = "hi-IN",
        alternative_languages: tuple[str, ...] = ("en-IN",),
        timeout_s: int = 180,
    ) -> None:
        self.name = f"google/{model}"
        self._credentials_path = credentials_path
        self._model = model
        self._language = language
        self._alternative_languages = list(alternative_languages)
        self._timeout_s = timeout_s
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import speech

            self._client = speech.SpeechClient.from_service_account_file(
                self._credentials_path
            )
        return self._client

    def warmup(self) -> None:
        _ = self.client  # construct now, not on the first user's request

    def _run(self, audio: Path) -> tuple[str, str | None]:
        from google.cloud import speech

        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16_000,  # audio.py always converts to this
            language_code=self._language,
            alternative_language_codes=self._alternative_languages,
            model=self._model,
            enable_automatic_punctuation=True,
        )
        response = self.client.recognize(
            config=config,
            audio=speech.RecognitionAudio(content=audio.read_bytes()),
            timeout=self._timeout_s,
        )
        text = " ".join(
            result.alternatives[0].transcript
            for result in response.results
            if result.alternatives
        )
        language = response.results[0].language_code if response.results else None
        return text, language or None
