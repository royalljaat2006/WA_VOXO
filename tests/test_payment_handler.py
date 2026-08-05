"""The WhatsApp end of the payment flow.

The flow's own logic is covered in test_payment_flow.py. What these tests defend
is the wiring: that the PIN never reaches the agent, never reaches the log, and
never gets echoed back into the chat.
"""

import logging

import pytest

from whatsapp_voice.agent.payments import (
    ContactBook,
    PendingIntents,
    StubPaymentGateway,
    current_payer,
)
from whatsapp_voice.config import PaymentSettings
from whatsapp_voice.whatsapp.payloads import ContactMessage, SharedContact
from whatsapp_voice.whatsapp.payment_handler import PaymentHandler

from .fakes import FakeAgent, FakeClient, FakeTranscriber, audio_message, text_message

PAYER = "919876543210"


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


@pytest.fixture
def pending():
    return PendingIntents()


@pytest.fixture
def contacts():
    return ContactBook()


def build(client, gateway, pending, transcript="Ramesh ko 2000 bhej do", **kwargs):
    return PaymentHandler(
        client,
        FakeTranscriber(transcript),
        FakeAgent("₹2,000 Ramesh ko. PIN boliye."),
        gateway,
        pending,
        PaymentSettings(enabled=True, stub_pin="1234"),
        **kwargs,
    )


def contact_message(*shared: SharedContact) -> ContactMessage:
    return ContactMessage(
        message_id="wamid.C", from_number=PAYER, timestamp="1", contacts=shared,
    )


def open_intent(gateway, pending, amount_paise=200_000):
    intent = gateway.prepare_transfer(PAYER, gateway.resolve_payee("Ramesh"), amount_paise)
    pending.put(intent)
    return intent


class TestOrdinaryMessages:
    def test_a_payment_instruction_goes_to_the_agent(self, gateway, pending):
        client = FakeClient()
        agent = FakeAgent("₹2,000 Ramesh ko. PIN boliye.")
        handler = PaymentHandler(
            client, FakeTranscriber(), agent, gateway, pending,
            PaymentSettings(enabled=True),
        )
        handler.handle(text_message("Ramesh ko 2000 bhej do"))

        assert agent.asked == [(PAYER, "Ramesh ko 2000 bhej do")]
        assert "PIN boliye" in client.last_reply

    def test_the_payer_is_the_sender(self, gateway, pending):
        handler = build(FakeClient(), gateway, pending)
        handler.handle(text_message("kuch bhi"))
        assert current_payer.get() == PAYER


class TestPinNeverReachesTheAgent:
    def test_typed_pin_is_intercepted(self, gateway, pending):
        client, agent = FakeClient(), FakeAgent()
        handler = PaymentHandler(
            client, FakeTranscriber(), agent, gateway, pending,
            PaymentSettings(enabled=True, stub_pin="1234"),
        )
        open_intent(gateway, pending)
        handler.handle(text_message("1234"))

        assert agent.asked == [], "the PIN was forwarded to the model"
        assert len(gateway.executed) == 1

    def test_spoken_pin_is_intercepted(self, gateway, pending, tmp_path):
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        agent = FakeAgent()
        handler = PaymentHandler(
            client, FakeTranscriber("ek do teen char"), agent, gateway, pending,
            PaymentSettings(enabled=True, stub_pin="1234"),
        )
        open_intent(gateway, pending)
        handler.handle(audio_message())

        assert agent.asked == []
        assert len(gateway.executed) == 1

    def test_receipt_is_sent(self, gateway, pending):
        client = FakeClient()
        handler = build(client, gateway, pending)
        open_intent(gateway, pending)
        handler.handle(text_message("1234"))

        assert "2,000.00" in client.last_reply
        assert "Ramesh Kumar" in client.last_reply

    def test_stub_receipt_is_labelled_as_a_test(self, gateway, pending):
        """Nobody should be able to mistake a rehearsal for a real transfer."""
        client = FakeClient()
        handler = build(client, gateway, pending)
        open_intent(gateway, pending)
        handler.handle(text_message("1234"))
        assert "TEST MODE" in client.last_reply


class TestPinIsNotEchoed:
    def test_transcript_of_a_pin_is_not_quoted_back(self, gateway, pending, tmp_path):
        """AgentHandler normally echoes what it heard so ASR errors are visible.
        During the PIN window that would print the secret into the chat."""
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        handler = PaymentHandler(
            client, FakeTranscriber("ek do teen char"), FakeAgent(), gateway, pending,
            PaymentSettings(enabled=True, stub_pin="1234"),
        )
        open_intent(gateway, pending)
        handler.handle(audio_message())

        assert "ek do teen char" not in client.last_reply
        assert "1234" not in client.last_reply

    def test_wrong_pin_reply_does_not_contain_the_attempt(self, gateway, pending):
        client = FakeClient()
        handler = build(client, gateway, pending)
        open_intent(gateway, pending)
        handler.handle(text_message("9999"))

        assert "9999" not in client.last_reply

    def test_ordinary_transcripts_are_still_echoed(self, gateway, pending, tmp_path):
        """The echo is useful — it must only be suppressed inside the PIN
        window, not switched off altogether."""
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        handler = PaymentHandler(
            client, FakeTranscriber("Ramesh ko 2000 bhej do"), FakeAgent("Ok."),
            gateway, pending, PaymentSettings(enabled=True),
        )
        handler.handle(audio_message())

        assert "Ramesh ko 2000 bhej do" in client.last_reply


class TestPinIsNotLogged:
    def test_transcript_is_withheld_while_awaiting_a_pin(
        self, gateway, pending, tmp_path, caplog
    ):
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        handler = PaymentHandler(
            client, FakeTranscriber("ek do teen char"), FakeAgent(), gateway, pending,
            PaymentSettings(enabled=True, stub_pin="1234"),
        )
        open_intent(gateway, pending)

        with caplog.at_level(logging.INFO):
            handler.handle(audio_message())

        logged = caplog.text
        assert "ek do teen char" not in logged
        assert "withheld" in logged

    def test_ordinary_transcript_is_logged(self, gateway, pending, tmp_path, caplog):
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        handler = PaymentHandler(
            client, FakeTranscriber("Ramesh ko 2000 bhej do"), FakeAgent(),
            gateway, pending, PaymentSettings(enabled=True),
        )
        with caplog.at_level(logging.INFO):
            handler.handle(audio_message())

        assert "Ramesh ko 2000 bhej do" in caplog.text


class TestLanguageReachesTheAgent:
    def test_detected_language_is_passed_through(self, gateway, pending, tmp_path):
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path))
        agent = FakeAgent("Ok.")
        handler = PaymentHandler(
            client, FakeTranscriber("Ramesh ko 2000 bhej do", language="ta-IN"),
            agent, gateway, pending, PaymentSettings(enabled=True),
        )
        handler.handle(audio_message())

        assert agent.languages == ["ta-IN"]

    def test_typed_messages_carry_no_language(self, gateway, pending):
        agent = FakeAgent("Ok.")
        handler = PaymentHandler(
            FakeClient(), FakeTranscriber(), agent, gateway, pending,
            PaymentSettings(enabled=True),
        )
        handler.handle(text_message("aur kal ka?"))
        assert agent.languages == [None]


class TestContactCard:
    def test_shared_contact_is_remembered_and_described_to_the_agent(
        self, gateway, pending, contacts
    ):
        client, agent = FakeClient(), FakeAgent("Kitna bhejna hai?")
        handler = PaymentHandler(
            client, FakeTranscriber(), agent, gateway, pending,
            PaymentSettings(enabled=True), contacts,
        )
        handler.handle(contact_message(
            SharedContact(phone="919111222333", name="Naya Aadmi")
        ))

        assert contacts.lookup(PAYER, "919111222333") == (True, "Naya Aadmi")
        # The model is told in plain words, so the conversation carries on.
        [(_, question)] = agent.asked
        assert "919111222333" in question
        assert "Naya Aadmi" in question
        assert "Kitna bhejna hai?" in client.last_reply

    def test_several_contacts_asks_which_one(self, gateway, pending, contacts):
        """Picking one would be the confident guess this flow exists to avoid."""
        client, agent = FakeClient(), FakeAgent()
        handler = PaymentHandler(
            client, FakeTranscriber(), agent, gateway, pending,
            PaymentSettings(enabled=True), contacts,
        )
        handler.handle(contact_message(
            SharedContact(phone="919111222333", name="Naya Aadmi"),
            SharedContact(phone="919111222444", name="Doosra Aadmi"),
        ))

        assert agent.asked == []
        assert "Kis ko" in client.last_reply
        # Both are still remembered, so naming one next turn works.
        assert contacts.lookup(PAYER, "919111222333")[0] is True
        assert contacts.lookup(PAYER, "919111222444")[0] is True

    def test_empty_card_replies_instead_of_going_silent(self, gateway, pending, contacts):
        client = FakeClient()
        handler = PaymentHandler(
            client, FakeTranscriber(), FakeAgent(), gateway, pending,
            PaymentSettings(enabled=True), contacts,
        )
        handler.handle(contact_message())
        assert client.sent

    def test_sharing_a_contact_does_not_confirm_a_pending_payment(
        self, gateway, pending, contacts
    ):
        """A contact card is not a PIN. An open intent must survive it, not be
        authorised by it."""
        client = FakeClient()
        handler = PaymentHandler(
            client, FakeTranscriber(), FakeAgent("Ok."), gateway, pending,
            PaymentSettings(enabled=True), contacts,
        )
        open_intent(gateway, pending)
        handler.handle(contact_message(SharedContact(phone="919111222333")))

        assert gateway.executed == []


class TestUnclearRecording:
    def test_a_fragment_from_a_long_recording_asks_again(self, gateway, pending, tmp_path):
        """Eight seconds of speech coming back as one word is a failed
        recognition, not a short instruction. Acting on what little was heard is
        the expensive mistake."""
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path, seconds=8.0))
        agent = FakeAgent()
        handler = PaymentHandler(
            client, FakeTranscriber("do"), agent, gateway, pending,
            PaymentSettings(enabled=True),
        )
        handler.handle(audio_message())

        assert agent.asked == [], "acted on a fragment"
        assert "dobara" in client.last_reply.lower()

    def test_a_short_answer_in_a_short_recording_is_fine(self, gateway, pending, tmp_path):
        """"Haan" in one second is a real answer, not a failure."""
        from .fakes import make_opus_bytes

        client = FakeClient(media=make_opus_bytes(tmp_path, seconds=1.0))
        agent = FakeAgent("Ok.")
        handler = PaymentHandler(
            client, FakeTranscriber("haan"), agent, gateway, pending,
            PaymentSettings(enabled=True),
        )
        handler.handle(audio_message())
        assert len(agent.asked) == 1

    def test_a_normal_recording_is_not_flagged(self, gateway, pending, tmp_path):
        from .fakes import make_opus_bytes

        client, agent = FakeClient(media=make_opus_bytes(tmp_path)), FakeAgent("Ok.")
        handler = PaymentHandler(
            client, FakeTranscriber("Ramesh ko 2000 bhej do"), agent, gateway, pending,
            PaymentSettings(enabled=True),
        )
        handler.handle(audio_message())
        assert len(agent.asked) == 1


class TestFailurePaths:
    def test_every_path_replies_with_something(self, gateway, pending):
        """A user cannot tell a broken bot from a slow one."""
        client = FakeClient()
        handler = build(client, gateway, pending)
        open_intent(gateway, pending)

        handler.handle(text_message("9999"))
        assert client.sent, "a wrong PIN produced silence"

    def test_unsupported_message_still_replies(self, gateway, pending):
        from whatsapp_voice.whatsapp.payloads import UnsupportedMessage

        client = FakeClient()
        handler = build(client, gateway, pending)
        handler.handle(UnsupportedMessage(
            message_id="wamid.U", from_number=PAYER, timestamp="1", kind="image",
        ))
        assert "image" in client.last_reply
