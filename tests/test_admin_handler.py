"""The wrapper that intercepts allowlist commands.

Three restrictions are being defended: only super-users, text only, and never in
a group. Plus the property that everything else passes through untouched — this
is a wrapper, so adding it must change nothing that already worked.
"""

import pytest

from whatsapp_voice.auth import AdminFlow, Roster
from whatsapp_voice.whatsapp.admin_handler import AdminHandler
from whatsapp_voice.whatsapp.handlers import MessageHandler
from whatsapp_voice.whatsapp.payloads import AudioMessage, IncomingMessage, TextMessage

SUPER = "918287151832"
ORDINARY = "919000000001"
GROUP = "120363000000000001@g.us"


class RecordingHandler(MessageHandler):
    def __init__(self):
        self.handled: list[IncomingMessage] = []
        self.warmed = False

    def warmup(self) -> None:
        self.warmed = True

    def handle(self, message: IncomingMessage) -> None:
        self.handled.append(message)


class RecordingClient:
    def __init__(self, error: Exception | None = None):
        self.sent: list[tuple[str, str]] = []
        self._error = error

    def send_text(self, to: str, body: str) -> str:
        if self._error:
            raise self._error
        self.sent.append((to, body))
        return "wamid.D"


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "allowed.txt"
    path.write_text(f"{SUPER}:admin+pay\n{ORDINARY}:viewer\n")
    return Roster(path)


@pytest.fixture
def inner():
    return RecordingHandler()


@pytest.fixture
def client():
    return RecordingClient()


@pytest.fixture
def handler(inner, client, roster):
    return AdminHandler(inner, client, AdminFlow(roster), {SUPER})


def text(body: str, sender: str = SUPER, group_id: str | None = None) -> TextMessage:
    return TextMessage(
        message_id="wamid.T", from_number=sender, timestamp="1",
        group_id=group_id, text=body,
    )


def audio(sender: str = SUPER) -> AudioMessage:
    return AudioMessage(
        message_id="wamid.A", from_number=sender, timestamp="1",
        media_id="media-1", mime_type="audio/ogg", is_voice_note=True,
    )


class TestSuperuserOnly:
    def test_superuser_command_is_handled(self, handler, client, inner, roster):
        handler.handle(text("/adduser 919812345678 viewer"))
        assert "919812345678" in client.sent[-1][1]
        assert inner.handled == []
        assert "919812345678" in roster.entries()

    def test_ordinary_user_command_is_passed_through_silently(
        self, handler, client, inner, roster
    ):
        """Refusing would confirm the capability exists and that they found the
        right service — the same reasoning as UNKNOWN_NUMBER_REPLY."""
        message = text("/adduser 919812345678 admin", sender=ORDINARY)
        handler.handle(message)

        assert client.sent == []
        assert inner.handled == [message]
        assert "919812345678" not in roster.entries()

    def test_reply_reveals_nothing_to_an_ordinary_user(self, handler, client, inner):
        handler.handle(text("/listusers", sender=ORDINARY))
        assert client.sent == []

    def test_number_formatting_still_matches(self, inner, client, roster):
        """A super-user number written with spaces in .env must still match the
        bare digits a webhook delivers."""
        handler = AdminHandler(inner, client, AdminFlow(roster), {"+91 82871-51832"})
        handler.handle(text("/listusers"))
        assert client.sent != []


class TestTextOnly:
    def test_a_voice_note_is_never_a_command(self, handler, inner, client):
        """ASR mangles digits, and there is no read-back step for commands, so
        the input has to be exact by construction."""
        message = audio()
        handler.handle(message)

        assert inner.handled == [message]
        assert client.sent == []


class TestNotInGroups:
    def test_a_command_in_a_group_is_passed_through(self, handler, inner, client, roster):
        """The reply would list who can reach an internal eko system, and a group
        is the wrong audience for that."""
        message = text("/listusers", group_id=GROUP)
        handler.handle(message)

        assert inner.handled == [message]
        assert client.sent == []

    def test_adduser_in_a_group_changes_nothing(self, handler, roster):
        handler.handle(text("/adduser 919812345678 admin", group_id=GROUP))
        assert "919812345678" not in roster.entries()


class TestPassThrough:
    def test_ordinary_text_from_a_superuser_reaches_the_inner_handler(
        self, handler, inner, client
    ):
        message = text("aaj kitne campaign gaye")
        handler.handle(message)

        assert inner.handled == [message]
        assert client.sent == []

    def test_warmup_reaches_the_inner_handler(self, handler, inner):
        handler.warmup()
        assert inner.warmed is True

    def test_inner_is_exposed(self, handler, inner):
        assert handler.inner is inner


class TestReplyFailure:
    def test_a_failed_confirmation_does_not_undo_the_change(self, inner, roster):
        """The change is already applied and audited; a send failure must not
        look like a failed command."""
        from whatsapp_voice.whatsapp.client import WhatsAppError

        client = RecordingClient(error=WhatsAppError("send failed"))
        handler = AdminHandler(inner, client, AdminFlow(roster), {SUPER})

        handler.handle(text("/adduser 919812345678 viewer"))  # must not raise

        assert "919812345678" in roster.entries()
