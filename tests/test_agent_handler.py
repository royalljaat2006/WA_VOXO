"""AgentHandler tests — the last link: transcript → agent → WhatsApp reply.

Reuses the fakes from test_transcribe_handler, since AgentHandler extends
TranscribeHandler and the front half of the pipeline is identical.
"""

import pytest

from whatsapp_voice.agent.base import AgentError
from whatsapp_voice.whatsapp import AgentHandler, UnsupportedMessage

from .fakes import FakeAgent, FakeClient, FakeTranscriber, audio_message, text_message


class TestVoiceNote:
    def test_transcript_reaches_the_agent(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        agent = FakeAgent()
        handler = AgentHandler(client, FakeTranscriber("aaj kitne campaign gaye"), agent)

        handler.handle(audio_message())

        assert agent.asked == [("919876543210", "aaj kitne campaign gaye")]

    def test_reply_contains_the_answer(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = AgentHandler(client, FakeTranscriber(), FakeAgent("Aaj 12 gaye."))

        handler.handle(audio_message())

        assert "Aaj 12 gaye." in client.last_reply

    def test_transcript_is_echoed_so_asr_errors_are_visible(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = AgentHandler(client, FakeTranscriber("delivery force bhej de"), FakeAgent())

        handler.handle(audio_message())

        assert "delivery force bhej de" in client.last_reply

    def test_echo_can_be_switched_off(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = AgentHandler(client, FakeTranscriber("some question"),
                               FakeAgent("Answer."), echo_transcript=False)

        handler.handle(audio_message())

        assert client.last_reply == "Answer."

    def test_one_reply_not_two(self, opus_bytes):
        """The transcript and the answer go out together — not as separate messages."""
        client = FakeClient(media=opus_bytes)
        handler = AgentHandler(client, FakeTranscriber(), FakeAgent())

        handler.handle(audio_message())

        assert len(client.sent) == 1


class TestTextMessage:
    def test_text_is_answered_too(self):
        client = FakeClient()
        agent = FakeAgent("Kal 9 gaye.")
        handler = AgentHandler(client, FakeTranscriber(), agent)

        handler.handle(text_message("aur kal?"))

        assert agent.asked == [("919876543210", "aur kal?")]
        assert client.last_reply == "Kal 9 gaye."

    def test_text_reply_has_no_transcript_prefix(self):
        client = FakeClient()
        handler = AgentHandler(client, FakeTranscriber(), FakeAgent("Answer."))
        handler.handle(text_message())
        assert "🎤" not in client.last_reply


class TestFailurePaths:
    def test_agent_error_still_replies(self, opus_bytes):
        client = FakeClient(media=opus_bytes)
        handler = AgentHandler(client, FakeTranscriber(),
                               FakeAgent(error=AgentError("api down")))

        handler.handle(audio_message())

        assert client.sent, "a failed agent must not leave the user in silence"
        assert "try again" in client.last_reply.lower()

    def test_agent_error_on_text_replies(self):
        client = FakeClient()
        handler = AgentHandler(client, FakeTranscriber(),
                               FakeAgent(error=AgentError("api down")))
        handler.handle(text_message())
        assert client.sent

    def test_agent_is_not_called_when_transcription_fails(self, opus_bytes):
        from whatsapp_voice.stt.base import TranscriptionError

        client = FakeClient(media=opus_bytes)
        agent = FakeAgent()
        handler = AgentHandler(
            client, FakeTranscriber(error=TranscriptionError("no speech")), agent
        )

        handler.handle(audio_message())

        assert agent.asked == []
        assert client.sent  # the user is still told something

    def test_unsupported_kind_is_guided(self):
        client = FakeClient()
        handler = AgentHandler(client, FakeTranscriber(), FakeAgent())

        handler.handle(UnsupportedMessage(
            message_id="wamid.X", from_number="919876543210",
            timestamp="1", kind="sticker",
        ))

        assert "sticker" in client.last_reply


class TestWarmup:
    def test_warms_both_the_engine_and_the_agent(self):
        transcriber = FakeTranscriber()
        agent = FakeAgent()
        AgentHandler(FakeClient(), transcriber, agent).warmup()
        assert transcriber.warmed is True
        assert agent.warmed is True


class TestBuildHandler:
    def test_agent_handler_is_selected_by_config(self, monkeypatch):
        from whatsapp_voice.config import Settings
        from whatsapp_voice.whatsapp.app import _innermost, build_handler

        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HANDLER", "agent")
        monkeypatch.setenv("RUN_LOCAL_WHISPER", "1")
        monkeypatch.setenv("WHISPER_MODEL", "tiny")
        # `Settings.load()` reads the developer's own .env for anything not set
        # here, so the wrappers have to be pinned off or this asserts against
        # whatever that file happens to say today.
        monkeypatch.setenv("WHATSAPP_SUPERUSER_NUMBERS", "")
        monkeypatch.setenv("GROUPS_ENABLED", "false")

        handler = build_handler(Settings.load())
        # Innermost, not `.inner`: the question is which behaviour HANDLER chose,
        # and any number of wrappers may sit above it.
        assert isinstance(_innermost(handler), AgentHandler)

    def test_unknown_handler_names_the_valid_options(self, monkeypatch):
        from whatsapp_voice.config import Settings
        from whatsapp_voice.whatsapp.app import build_handler

        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("HANDLER", "nonsense")

        with pytest.raises(ValueError, match="agent"):
            build_handler(Settings.load())
