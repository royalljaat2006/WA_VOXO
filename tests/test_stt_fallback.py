from pathlib import Path

import pytest

from whatsapp_voice.stt.base import Transcriber, TranscriptionError
from whatsapp_voice.stt.fallback import FallbackTranscriber


class Fake(Transcriber):
    def __init__(self, name: str, text: str = "ok", error: Exception | None = None) -> None:
        self.name = name
        self._text = text
        self._error = error
        self.warmed_up = False
        self.calls = 0

    def warmup(self) -> None:
        self.warmed_up = True

    def _run(self, audio: Path) -> tuple[str, str | None]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._text, None


AUDIO = Path("unused.wav")


class TestFallbackTranscriber:
    def test_uses_primary_when_it_succeeds(self):
        primary = Fake("primary", text="from primary")
        fallback = Fake("fallback", text="from fallback")
        transcript = FallbackTranscriber(primary, fallback).transcribe(AUDIO)

        assert transcript.text == "from primary"
        assert transcript.engine == "primary"
        assert fallback.calls == 0

    def test_falls_back_when_primary_raises(self):
        primary = Fake("primary", error=TranscriptionError("HTTP 500"))
        fallback = Fake("fallback", text="from fallback")
        transcript = FallbackTranscriber(primary, fallback).transcribe(AUDIO)

        assert transcript.text == "from fallback"
        assert transcript.engine == "fallback"

    def test_fallback_error_propagates_when_both_fail(self):
        primary = Fake("primary", error=TranscriptionError("primary down"))
        fallback = Fake("fallback", error=TranscriptionError("fallback down"))

        with pytest.raises(TranscriptionError, match="fallback down"):
            FallbackTranscriber(primary, fallback).transcribe(AUDIO)

    def test_warmup_warms_up_both(self):
        primary = Fake("primary")
        fallback = Fake("fallback")
        FallbackTranscriber(primary, fallback).warmup()

        assert primary.warmed_up
        assert fallback.warmed_up

    def test_name_reflects_both_engines(self):
        transcriber = FallbackTranscriber(Fake("local-whisper/tiny"), Fake("sarvam/saarika:v2.5"))
        assert transcriber.name == "local-whisper/tiny+fallback:sarvam/saarika:v2.5"
