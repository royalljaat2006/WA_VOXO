"""GuardedHandler tests — the gate in front of every handler.

The load-bearing property: a denied message must not reach the inner handler at
all. No media download, no transcription, no model call, no cost.
"""

import pytest

from whatsapp_voice.auth import AccessGuard
from whatsapp_voice.config import AuthSettings
from whatsapp_voice.whatsapp import GuardedHandler, MessageHandler

from .fakes import FakeClient, audio_message, text_message

ALLOWED = "919876543210"
DENIED = "910000000000"


class RecordingHandler(MessageHandler):
    def __init__(self):
        self.handled = []
        self.warmed = False

    def warmup(self):
        self.warmed = True

    def handle(self, message):
        self.handled.append(message)


def make(spec=f"{ALLOWED}:admin", **overrides):
    client = FakeClient()
    inner = RecordingHandler()
    guard = AccessGuard.from_settings(AuthSettings(allowed_numbers=spec, **overrides))
    return GuardedHandler(inner, guard, client), inner, client


class TestAllowed:
    def test_passes_the_message_through(self):
        handler, inner, _ = make()
        message = text_message(from_number=ALLOWED)

        handler.handle(message)

        assert inner.handled == [message]

    def test_guard_sends_nothing_itself(self):
        handler, _, client = make()
        handler.handle(text_message(from_number=ALLOWED))
        assert client.sent == [], "the inner handler owns the reply"

    def test_voice_notes_pass_through_too(self):
        handler, inner, _ = make()
        handler.handle(audio_message(from_number=ALLOWED))
        assert len(inner.handled) == 1


class TestDenied:
    def test_inner_handler_is_never_reached(self):
        """The point of the phase: no download, no transcription, no model call."""
        handler, inner, _ = make()
        handler.handle(audio_message(from_number=DENIED))
        assert inner.handled == []

    def test_user_is_still_told_something(self):
        handler, _, client = make()
        handler.handle(text_message(from_number=DENIED))
        assert len(client.sent) == 1

    def test_denial_goes_to_the_sender(self):
        handler, _, client = make()
        handler.handle(text_message(from_number=DENIED))
        assert client.sent[0][0] == DENIED

    def test_rate_limited_sender_is_blocked_from_the_inner_handler(self):
        handler, inner, client = make(rate_limit=1)

        handler.handle(text_message(from_number=ALLOWED))
        handler.handle(text_message(from_number=ALLOWED))

        assert len(inner.handled) == 1
        assert "try again" in client.sent[-1][1].lower()

    def test_send_failure_on_denial_does_not_raise(self):
        from whatsapp_voice.whatsapp import WhatsAppError

        client = FakeClient(send_error=WhatsAppError("token expired"))
        inner = RecordingHandler()
        guard = AccessGuard.from_settings(AuthSettings(allowed_numbers=f"{ALLOWED}:admin"))

        GuardedHandler(inner, guard, client).handle(text_message(from_number=DENIED))
        # must not raise — a background task crash would be worse than a lost notice


class TestEmptyAllowlist:
    def test_denies_everyone(self):
        handler, inner, _ = make(spec="")
        handler.handle(text_message(from_number=ALLOWED))
        assert inner.handled == []


class TestDisabledAuth:
    def test_everything_passes_through(self):
        handler, inner, _ = make(spec="", enabled=False)
        handler.handle(text_message(from_number=DENIED))
        assert len(inner.handled) == 1


class TestWarmup:
    def test_delegates_to_the_inner_handler(self):
        handler, inner, _ = make()
        handler.warmup()
        assert inner.warmed is True


class TestBuildHandlerWiring:
    def test_every_handler_is_wrapped_in_the_guard(self, monkeypatch):
        """Auth must be impossible to forget when adding a handler."""
        from whatsapp_voice.config import Settings
        from whatsapp_voice.whatsapp.app import build_handler

        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", f"{ALLOWED}:admin")

        for handler_name in ("echo",):
            monkeypatch.setenv("HANDLER", handler_name)
            assert isinstance(build_handler(Settings.load()), GuardedHandler)

    @pytest.mark.parametrize("handler_name", ["transcribe", "agent"])
    def test_heavier_handlers_are_wrapped_too(self, monkeypatch, handler_name):
        from whatsapp_voice.config import Settings
        from whatsapp_voice.whatsapp.app import build_handler

        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("WHISPER_MODEL", "tiny")
        monkeypatch.setenv("HANDLER", handler_name)

        assert isinstance(build_handler(Settings.load()), GuardedHandler)
