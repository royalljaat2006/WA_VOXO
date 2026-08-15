import time
from pathlib import Path

import pytest

from whatsapp_voice.config import SttSettings
from whatsapp_voice.stt import TranscriptionError, build_primary_transcriber, build_transcribers
from whatsapp_voice.stt.base import Transcriber


class TestBuildTranscribers:
    def test_local_whisper_only_by_default(self):
        engines = build_transcribers(SttSettings(whisper_model="tiny"))
        assert [e.name for e in engines] == ["local-whisper/tiny"]

    def test_no_engines_when_everything_disabled(self):
        assert build_transcribers(SttSettings(run_local_whisper=False)) == []

    def test_api_keys_add_engines(self):
        settings = SttSettings(
            run_local_whisper=False,
            openai_api_key="sk-test",
            openai_models=("whisper-1",),
            sarvam_api_key="sarvam-test",
            elevenlabs_api_key="el-test",
            google_credentials_path="/tmp/creds.json",
        )
        assert [e.name for e in build_transcribers(settings)] == [
            "openai/whisper-1",
            "sarvam/saarika:v2.5",
            "elevenlabs/scribe_v1",
            "google/latest_long",
        ]

    def test_google_credentials_path_alone_adds_only_google(self):
        """No key file means the engine is skipped, same as every other
        engine's missing-key behaviour — construction never touches the
        network, so this needs no mocking."""
        settings = SttSettings(
            run_local_whisper=False,
            google_credentials_path="/tmp/creds.json",
            google_model="latest_short",
        )
        assert [e.name for e in build_transcribers(settings)] == ["google/latest_short"]

    def test_primary_is_first_configured_engine(self):
        settings = SttSettings(run_local_whisper=False, openai_api_key="sk-test")
        assert build_primary_transcriber(settings).name == "openai/whisper-1"

    def test_primary_raises_when_none_configured(self):
        with pytest.raises(RuntimeError, match="No STT engine configured"):
            build_primary_transcriber(SttSettings(run_local_whisper=False))


class TestExplicitEngineSelection:
    """STT_ENGINE — the explicit choice the docstring always promised, made
    once Phase 0 had real benchmark evidence (Sarvam: ~15x faster, more
    accurate on Hindi/Hinglish than local Whisper)."""

    def test_picks_the_named_engine_over_declaration_order(self):
        settings = SttSettings(
            whisper_model="tiny",  # would be engines[0] without STT_ENGINE
            sarvam_api_key="sarvam-test",
            engine="sarvam",
        )
        assert build_primary_transcriber(settings).name == "sarvam/saarika:v2.5"

    def test_matches_by_name_prefix_not_full_equality(self):
        """"sarvam" must match "sarvam/saarika:v2.5" without pinning the model
        version in two places."""
        settings = SttSettings(
            run_local_whisper=False, sarvam_api_key="sarvam-test",
            sarvam_model="future-model:v9", engine="sarvam",
        )
        assert build_primary_transcriber(settings).name == "sarvam/future-model:v9"

    def test_unset_falls_back_to_first_configured(self):
        """A checkout with no STT_ENGINE must still run on local Whisper alone
        — the pre-Phase-0 behaviour, preserved on purpose."""
        settings = SttSettings(whisper_model="tiny", engine=None)
        assert build_primary_transcriber(settings).name == "local-whisper/tiny"

    def test_requested_engine_not_configured_raises_clearly(self):
        """Asking for an engine whose key is missing must fail loudly, not
        silently fall back to whatever happens to be engines[0]."""
        settings = SttSettings(whisper_model="tiny", engine="sarvam")
        with pytest.raises(RuntimeError, match="STT_ENGINE='sarvam'.*no configured"):
            build_primary_transcriber(settings)

    def test_error_lists_what_is_actually_available(self):
        settings = SttSettings(whisper_model="tiny", engine="nonexistent")
        with pytest.raises(RuntimeError, match=r"local-whisper/tiny"):
            build_primary_transcriber(settings)


class TestFallbackEngine:
    """STT_ENGINE_FALLBACK — resilience against the primary's key/network
    failing, independent of the Sarvam-vs-Whisper quality benchmark above."""

    def test_wraps_primary_and_fallback(self):
        settings = SttSettings(
            whisper_model="tiny", sarvam_api_key="sarvam-test",
            engine="local-whisper", fallback_engine="sarvam",
        )
        transcriber = build_primary_transcriber(settings)
        assert transcriber.name == "local-whisper/tiny+fallback:sarvam/saarika:v2.5"

    def test_no_fallback_configured_returns_primary_unwrapped(self):
        settings = SttSettings(whisper_model="tiny", engine="local-whisper")
        assert build_primary_transcriber(settings).name == "local-whisper/tiny"

    def test_fallback_engine_not_configured_raises_clearly(self):
        settings = SttSettings(
            whisper_model="tiny", engine="local-whisper", fallback_engine="sarvam",
        )
        with pytest.raises(RuntimeError, match="STT_ENGINE_FALLBACK='sarvam'.*no configured"):
            build_primary_transcriber(settings)

    def test_fallback_same_as_primary_raises(self):
        settings = SttSettings(
            whisper_model="tiny", engine="local-whisper", fallback_engine="local-whisper",
        )
        with pytest.raises(RuntimeError, match="resolves to the same engine"):
            build_primary_transcriber(settings)


class TestTranscriberContract:
    """The base class must time successes and wrap arbitrary engine failures."""

    def test_times_and_labels_the_transcript(self):
        class Fake(Transcriber):
            name = "fake"

            def _run(self, audio: Path) -> tuple[str, str | None]:
                return "  delivery report  ", "hi"

        transcript = Fake().transcribe(Path("unused.wav"))
        assert transcript.text == "delivery report"
        assert transcript.engine == "fake"
        assert transcript.language == "hi"
        assert transcript.latency_s >= 0

    def test_wraps_unexpected_errors(self):
        class Exploding(Transcriber):
            name = "exploding"

            def _run(self, audio: Path) -> tuple[str, str | None]:
                raise ConnectionResetError("socket died")

        with pytest.raises(TranscriptionError, match="ConnectionResetError: socket died"):
            Exploding().transcribe(Path("unused.wav"))

    def test_warmup_is_a_no_op_by_default(self):
        class Fake(Transcriber):
            name = "fake"

            def _run(self, audio: Path) -> tuple[str, str | None]:
                return "", None

        Fake().warmup()  # must not raise for engines with no setup cost

    def test_warmup_keeps_model_load_out_of_measured_latency(self):
        class Slow(Transcriber):
            name = "slow"

            def __init__(self) -> None:
                self.loaded = False

            def warmup(self) -> None:
                time.sleep(0.05)  # stands in for loading a model
                self.loaded = True

            def _run(self, audio: Path) -> tuple[str, str | None]:
                assert self.loaded, "warmup should have run first"
                return "ok", None

        engine = Slow()
        engine.warmup()
        assert engine.transcribe(Path("unused.wav")).latency_s < 0.05

    def test_passes_through_transcription_errors_unchanged(self):
        class Rejecting(Transcriber):
            name = "rejecting"

            def _run(self, audio: Path) -> tuple[str, str | None]:
                raise TranscriptionError("HTTP 401: bad key")

        with pytest.raises(TranscriptionError, match="^HTTP 401: bad key$"):
            Rejecting().transcribe(Path("unused.wav"))
