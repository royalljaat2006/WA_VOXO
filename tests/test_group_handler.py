"""Group etiquette: dormant by default, awake when summoned, asleep again after.

Three things are being defended.

That 1:1 behaviour is untouched — this is a wrapper, and adding it must change
nothing about the chat that already worked.

That a dormant group's messages are not acted on, stored, or even downloaded.
Everything a Cloud API business shares a group with reaches its webhook,
including messages nobody addressed to it; acting on those costs money and
processes other people's speech for no reason.

That a payment cannot be confirmed in a group. A PIN spoken as a voice note there
is audible to every participant and stays in the chat to be replayed. Restricting
*who* may pay does not help — it is the payer's own PIN being exposed.
"""

import pytest

from whatsapp_voice.whatsapp.group_handler import GroupHandler, GroupRegistry
from whatsapp_voice.whatsapp.handlers import MessageHandler
from whatsapp_voice.whatsapp.payloads import (
    AudioMessage,
    IncomingMessage,
    TextMessage,
)

GROUP = "120363000000000001@g.us"
SENDER = "919876543210"


class RecordingHandler(MessageHandler):
    def __init__(self):
        self.handled: list[IncomingMessage] = []
        self.warmed = False

    def warmup(self) -> None:
        self.warmed = True

    def handle(self, message: IncomingMessage) -> None:
        self.handled.append(message)


class RecordingClient:
    def __init__(self):
        self.group_sends: list[tuple[str, str]] = []
        self.direct_sends: list[tuple[str, str]] = []

    def send_group_text(self, group_id: str, body: str) -> str:
        self.group_sends.append((group_id, body))
        return "wamid.G"

    def send_text(self, to: str, body: str) -> str:
        self.direct_sends.append((to, body))
        return "wamid.D"


@pytest.fixture
def inner():
    return RecordingHandler()


@pytest.fixture
def client():
    return RecordingClient()


@pytest.fixture
def handler(inner, client):
    return GroupHandler(inner, client, registry=GroupRegistry(awake_for_s=300))


def group_text(text: str, group_id: str = GROUP) -> TextMessage:
    return TextMessage(
        message_id="wamid.T", from_number=SENDER, timestamp="1",
        group_id=group_id, text=text,
    )


def group_audio(group_id: str = GROUP) -> AudioMessage:
    return AudioMessage(
        message_id="wamid.A", from_number=SENDER, timestamp="1", group_id=group_id,
        media_id="MEDIA1", mime_type="audio/ogg", is_voice_note=True,
    )


class TestDirectMessagesAreUntouched:
    def test_text_passes_straight_through(self, handler, inner):
        message = TextMessage(
            message_id="wamid.T", from_number=SENDER, timestamp="1",
            text="Ramesh ko 2000 bhej do",
        )
        handler.handle(message)
        assert inner.handled == [message]

    def test_a_payment_in_a_1to1_is_not_redirected(self, handler, inner, client):
        """The redirect exists because groups are public. A private chat is not."""
        handler.handle(TextMessage(
            message_id="wamid.T", from_number=SENDER, timestamp="1",
            text="Ramesh ko 2000 bhej do",
        ))
        assert len(inner.handled) == 1
        assert client.group_sends == []

    def test_audio_passes_straight_through(self, handler, inner):
        message = AudioMessage(
            message_id="wamid.A", from_number=SENDER, timestamp="1",
            media_id="M", mime_type="audio/ogg", is_voice_note=True,
        )
        handler.handle(message)
        assert inner.handled == [message]

    def test_warmup_reaches_the_inner_handler(self, handler, inner):
        handler.warmup()
        assert inner.warmed is True


class TestDormantGroup:
    def test_ordinary_chatter_is_ignored(self, handler, inner, client):
        handler.handle(group_text("kal milte hain phir"))
        assert inner.handled == []
        assert client.group_sends == []

    def test_audio_is_not_even_downloaded(self, handler, inner):
        """Deciding after transcription would mean paying to transcribe speech
        nobody addressed to us — and processing it."""
        handler.handle(group_audio())
        assert inner.handled == []

    def test_nothing_is_replied_to(self, handler, client):
        handler.handle(group_text("random message"))
        handler.handle(group_audio())
        assert client.group_sends == []
        assert client.direct_sends == []


class TestSummons:
    @pytest.mark.parametrize("text", [
        "@eko aaj kitne campaign gaye",
        "@Eko aaj kitne campaign gaye",
        "eko bot report do",
        "एको aaj ka report",
    ])
    def test_wake_words_summon_the_bot(self, handler, inner, text):
        handler.handle(group_text(text))
        assert len(inner.handled) == 1

    def test_the_wake_word_is_stripped_before_the_agent_sees_it(self, handler, inner):
        """The model should get the question, not spend attention on "@eko"."""
        handler.handle(group_text("@eko aaj kitne campaign gaye"))
        assert inner.handled[0].text == "aaj kitne campaign gaye"

    def test_a_bare_summons_still_reaches_the_agent(self, handler, inner):
        """Stripping must not leave an empty message."""
        handler.handle(group_text("@eko"))
        assert inner.handled[0].text.strip() != ""

    def test_the_group_id_survives_stripping(self, handler, inner):
        handler.handle(group_text("@eko report"))
        assert inner.handled[0].group_id == GROUP

    def test_the_sender_survives_stripping(self, handler, inner):
        handler.handle(group_text("@eko report"))
        assert inner.handled[0].from_number == SENDER


class TestStayingAwake:
    def test_a_follow_up_needs_no_second_summons(self, handler, inner):
        handler.handle(group_text("@eko aaj kitne campaign gaye"))
        handler.handle(group_text("aur kal ka?"))
        assert len(inner.handled) == 2

    def test_audio_is_processed_while_awake(self, handler, inner):
        handler.handle(group_text("@eko report chahiye"))
        handler.handle(group_audio())
        assert len(inner.handled) == 2

    def test_the_group_sleeps_again(self, inner, client):
        """A window that never closes is a bot quietly listening to a meeting."""
        handler = GroupHandler(inner, client, registry=GroupRegistry(awake_for_s=-1))
        handler.handle(group_text("@eko report"))
        handler.handle(group_text("aur kal ka?"))

        assert len(inner.handled) == 1     # the summons only

    def test_waking_one_group_does_not_wake_another(self, handler, inner):
        handler.handle(group_text("@eko report", group_id=GROUP))
        handler.handle(group_text("aur kal ka?", group_id="other@g.us"))
        assert len(inner.handled) == 1


class TestPaymentsAreRedirected:
    @pytest.mark.parametrize("text", [
        "@eko Ramesh ko 2000 bhej do",
        "@eko रमेश को 2000 भेज दो",
        "@eko transfer 500 to Suresh",
        "@eko send money to 9111222333",
    ])
    def test_a_payment_request_never_reaches_the_agent(self, handler, inner, client, text):
        handler.handle(group_text(text))

        assert inner.handled == [], "a payment request reached the agent from a group"
        assert client.group_sends, "the user was left with silence"

    def test_the_reply_explains_why(self, handler, client):
        """"Message me directly" without a reason reads as a malfunction."""
        handler.handle(group_text("@eko Ramesh ko 2000 bhej do"))
        _, body = client.group_sends[-1]

        assert "direct message" in body.lower()
        assert "pin" in body.lower()

    def test_the_reply_goes_to_the_group_not_the_sender(self, handler, client):
        handler.handle(group_text("@eko Ramesh ko 2000 bhej do"))
        assert client.group_sends[-1][0] == GROUP
        assert client.direct_sends == []

    def test_reports_still_work_in_groups(self, handler, inner, client):
        """Only money moves out. The rest of the product stays."""
        handler.handle(group_text("@eko aaj kitne campaign gaye"))
        assert len(inner.handled) == 1
        assert client.group_sends == []

    def test_a_payment_request_while_awake_is_also_redirected(self, handler, inner, client):
        handler.handle(group_text("@eko aaj kitne campaign gaye"))
        handler.handle(group_text("ab Ramesh ko 2000 bhej do"))

        assert len(inner.handled) == 1
        assert client.group_sends

    def test_the_override_exists_but_is_off_by_default(self, inner, client):
        """Visible and reversible, rather than buried — but the default is the
        only setting that keeps a spoken PIN private."""
        permissive = GroupHandler(inner, client, allow_payments_in_groups=True)
        permissive.handle(group_text("@eko Ramesh ko 2000 bhej do"))
        assert len(inner.handled) == 1


class TestRegistry:
    def test_wake_then_awake(self):
        registry = GroupRegistry(awake_for_s=300)
        assert registry.is_awake(GROUP) is False
        registry.wake(GROUP)
        assert registry.is_awake(GROUP) is True

    def test_sleep_clears_it(self):
        registry = GroupRegistry(awake_for_s=300)
        registry.wake(GROUP)
        registry.sleep(GROUP)
        assert registry.is_awake(GROUP) is False

    def test_expiry(self):
        registry = GroupRegistry(awake_for_s=-1)
        registry.wake(GROUP)
        assert registry.is_awake(GROUP) is False
