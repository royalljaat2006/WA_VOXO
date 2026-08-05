
import pytest

from whatsapp_voice.audio import AudioError
from whatsapp_voice.stt.base import TranscriptionError
from whatsapp_voice.whatsapp import (
    TextMessage,
    TranscribeHandler,
    UnsupportedMessage,
    WhatsAppError,
)

from .fakes import FakeClient, FakeTranscriber, audio_message


class TestVoiceNoteSuccess:
    def test_replies_with_the_transcript(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = TranscribeHandler(client, FakeTranscriber("aaj kitne campaign gaye"))

        handler.handle(audio_message())

        to, body = client.sent[0]
        assert to == "919876543210"
        assert "aaj kitne campaign gaye" in body

    def test_audio_file_is_handled_like_a_voice_note(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = TranscribeHandler(client, FakeTranscriber("hello there"))

        handler.handle(audio_message(is_voice_note=False))

        assert "hello there" in client.last_reply

    def test_leaves_no_audio_on_disk(self, opus_bytes, tmp_path, monkeypatch):
        # Voice notes are customer data — the working directory must be cleaned
        # up even on the success path.
        monkeypatch.setattr("tempfile.mkdtemp",
                            lambda **kw: str(tmp_path / "workdir"))
        (tmp_path / "workdir").mkdir()
        client = FakeClient(media=opus_bytes)

        TranscribeHandler(client, FakeTranscriber()).handle(audio_message())

        assert not (tmp_path / "workdir").exists()


class TestFailurePaths:
    """Every failure must still say something — silence looks like a dead bot."""

    def test_download_failure_replies(self):
        client = FakeClient(fetch_error=WhatsAppError("HTTP 404", status_code=404))
        handler = TranscribeHandler(client, FakeTranscriber())

        handler.handle(audio_message())

        assert "couldn't download" in client.last_reply.lower()

    def test_transcription_failure_replies(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = TranscribeHandler(
            client, FakeTranscriber(error=TranscriptionError("engine exploded"))
        )

        handler.handle(audio_message())

        assert "couldn't make out" in client.last_reply.lower()

    def test_unconvertible_audio_replies(self):
        client = FakeClient(media=b"this is not audio")
        handler = TranscribeHandler(client, FakeTranscriber())

        handler.handle(audio_message())

        assert "format" in client.last_reply.lower()

    def test_empty_transcript_replies(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = TranscribeHandler(client, FakeTranscriber(text="   "))

        handler.handle(audio_message())

        assert "didn't hear anything" in client.last_reply.lower()

    def test_cleans_up_even_when_transcription_fails(self, opus_bytes, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.mkdtemp", lambda **kw: str(tmp_path / "wd"))
        (tmp_path / "wd").mkdir()
        client = FakeClient(media=opus_bytes)
        handler = TranscribeHandler(
            client, FakeTranscriber(error=TranscriptionError("boom"))
        )

        handler.handle(audio_message())

        assert not (tmp_path / "wd").exists()

    def test_send_failure_does_not_raise(self, opus_bytes):
        # The reply itself failing is the one error we cannot report to the user;
        # it must not crash the background task.
        client = FakeClient(media=opus_bytes, send_error=WhatsAppError("token expired"))
        handler = TranscribeHandler(client, FakeTranscriber())

        handler.handle(audio_message())  # must not raise


class TestOtherMessageKinds:
    def test_text_gets_guidance_not_a_transcript(self):
        client = FakeClient()
        handler = TranscribeHandler(client, FakeTranscriber())

        handler.handle(TextMessage(message_id="1", from_number="91999", timestamp="1",
                                   text="hello"))

        assert "voice note" in client.last_reply.lower()

    def test_unsupported_kind_is_told_what_to_send(self):
        client = FakeClient()
        handler = TranscribeHandler(client, FakeTranscriber())

        handler.handle(UnsupportedMessage(message_id="1", from_number="91999",
                                          timestamp="1", kind="image"))

        assert "image" in client.last_reply.lower()


class TestWarmup:
    def test_warmup_delegates_to_the_engine(self):
        transcriber = FakeTranscriber()
        TranscribeHandler(FakeClient(), transcriber).warmup()
        assert transcriber.warmed is True

    def test_audio_error_is_raised_for_junk(self, tmp_path):
        # Guard on the underlying utility the handler relies on.
        from whatsapp_voice import audio

        junk = tmp_path / "junk.opus"
        junk.write_bytes(b"not audio at all")
        with pytest.raises(AudioError):
            audio.to_wav(junk, tmp_path / "cache")
