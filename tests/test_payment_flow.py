"""The PIN gate.

Everything here is about one question: does a secret ever escape? The gate sits
in front of the model, so a bug in it puts a PIN into an LLM's context, a
conversation history and a log line, all in one go.

The second question is: can money move without one? `TestNothingMovesWithoutAPin`
covers that.
"""

import pytest

from whatsapp_voice.agent.payment_flow import PaymentFlow, receipt
from whatsapp_voice.agent.payments import PendingIntents, StubPaymentGateway
from whatsapp_voice.config import PaymentSettings

PAYER = "919000000009"


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


@pytest.fixture
def pending():
    return PendingIntents()


@pytest.fixture
def flow(gateway, pending):
    return PaymentFlow(gateway, pending, PaymentSettings(enabled=True, stub_pin="1234"))


def prepare(gateway, pending, amount_paise=200_000, payee="Ramesh"):
    intent = gateway.prepare_transfer(PAYER, gateway.resolve_payee(payee), amount_paise)
    pending.put(intent)
    return intent


class TestNoPendingIntent:
    def test_everything_is_forwarded(self, flow):
        outcome = flow.handle(PAYER, "Ramesh ko 2000 bhej do")
        assert outcome.forward
        assert outcome.reply is None

    def test_a_bare_pin_is_never_forwarded_even_with_nothing_pending(self, flow, gateway):
        """The rule is unconditional, not conditional on our state being right.

        A model that says "PIN bolo" without having called prepare_transfer
        leaves nothing pending. If a bare PIN were forwarded in that state, the
        user's secret would land in the model's context — which is exactly what
        happened in a real run before this check existed.
        """
        outcome = flow.handle(PAYER, "1234")

        assert not outcome.forward, "a PIN was forwarded to the model"
        assert "pending nahi" in outcome.reply
        assert gateway.executed == []

    def test_spoken_pin_with_nothing_pending_is_also_dropped(self, flow):
        assert not flow.handle(PAYER, "ek do teen char").forward

    def test_an_ordinary_question_is_still_forwarded(self, flow):
        """The guard must not swallow normal conversation."""
        assert flow.handle(PAYER, "aaj kitne campaign gaye").forward
        assert flow.handle(PAYER, "India ki capital kya hai").forward

    def test_awaiting_pin_is_false(self, flow):
        assert flow.awaiting_pin(PAYER) is False


class TestPinConfirms:
    def test_correct_pin_executes_and_returns_a_receipt(self, flow, gateway, pending):
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "1234")

        assert outcome.reply is not None
        assert not outcome.forward           # the model never sees it
        assert len(gateway.executed) == 1
        assert "2,000.00" in outcome.reply

    def test_spoken_pin_works(self, flow, gateway, pending):
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "ek do teen char")
        assert len(gateway.executed) == 1
        assert outcome.reply is not None

    def test_devanagari_spoken_pin_works(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "एक दो तीन चार")
        assert len(gateway.executed) == 1

    def test_pin_with_filler_words_works(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "mera pin 1234 hai")
        assert len(gateway.executed) == 1

    def test_intent_is_cleared_after_success(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "1234")
        assert pending.get(PAYER) is None

    def test_replaying_the_pin_does_not_pay_twice(self, flow, gateway, pending):
        """The intent is consumed, so a duplicate webhook delivery of the same
        PIN cannot double-spend."""
        prepare(gateway, pending)
        flow.handle(PAYER, "1234")
        second = flow.handle(PAYER, "1234")

        assert len(gateway.executed) == 1
        # The intent is gone, so the replayed PIN is dropped rather than
        # forwarded — a secret is a secret either way.
        assert not second.forward


class TestWrongPin:
    def test_wrong_pin_does_not_execute(self, flow, gateway, pending):
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "9999")
        assert gateway.executed == []
        assert not outcome.forward       # still never reaches the model
        assert "galat" in outcome.reply

    def test_the_reply_says_pintwin_not_pin(self, flow, gateway, pending):
        """eko calls this field "pintwin" — user-facing replies say that
        word, matching what the confirmation prompt already asked for."""
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "9999")
        assert "PINTWIN" in outcome.reply

    def test_intent_survives_a_wrong_pin(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "9999")
        assert pending.get(PAYER) is not None

    def test_third_wrong_pin_cancels_the_transfer(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "9999")
        flow.handle(PAYER, "8888")
        outcome = flow.handle(PAYER, "7777")

        assert pending.get(PAYER) is None
        assert gateway.executed == []
        assert "cancel" in outcome.reply.lower()

    def test_correct_pin_after_cancellation_does_nothing(self, flow, gateway, pending):
        prepare(gateway, pending)
        for wrong in ("9999", "8888", "7777"):
            flow.handle(PAYER, wrong)
        outcome = flow.handle(PAYER, "1234")

        assert gateway.executed == []
        assert not outcome.forward

    def test_attempt_limit_is_configurable(self, gateway, pending):
        flow = PaymentFlow(
            gateway, pending,
            PaymentSettings(enabled=True, stub_pin="1234", max_pin_attempts=1),
        )
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "9999")
        assert pending.get(PAYER) is None
        assert "cancel" in outcome.reply.lower()


class TestCancel:
    @pytest.mark.parametrize("word", ["cancel", "nahi", "रहने दो", "rehne do", "stop", "रुको"])
    def test_cancel_words_drop_the_intent(self, flow, gateway, pending, word):
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, word)
        assert pending.get(PAYER) is None
        assert gateway.executed == []
        assert not outcome.forward

    def test_cancel_inside_a_longer_instruction_is_not_a_cancel(self, flow, gateway, pending):
        """"cancel karke 500 Ramesh ko bhejo" is a new instruction, not a stop."""
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "cancel karke 500 Ramesh ko bhejo")
        assert outcome.forward


class TestChangingTheSubject:
    def test_unrelated_message_drops_the_intent(self, flow, gateway, pending):
        """Otherwise a PIN spoken minutes later would confirm a transfer the user
        has stopped thinking about."""
        prepare(gateway, pending)
        outcome = flow.handle(PAYER, "aaj kitne campaign gaye")

        assert pending.get(PAYER) is None
        assert outcome.forward
        assert "cancel" in outcome.prefix.lower()

    def test_pin_after_changing_the_subject_pays_nothing(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "aaj kitne campaign gaye")
        flow.handle(PAYER, "1234")
        assert gateway.executed == []


class TestNothingMovesWithoutAPin:
    def test_a_payment_instruction_never_executes_by_itself(self, flow, gateway, pending):
        """The sentence that starts everything must not also finish it."""
        prepare(gateway, pending)
        flow.handle(PAYER, "Ramesh ko 2000 bhej do")
        assert gateway.executed == []

    def test_yes_is_not_a_confirmation(self, flow, gateway, pending):
        """Agreement is not authorisation — only a PIN is."""
        for agreeable in ("haan", "yes", "ok", "theek hai", "haan bhej do"):
            prepare(gateway, pending)
            flow.handle(PAYER, agreeable)
            assert gateway.executed == [], f"{agreeable!r} authorised a payment"


class TestIsolationBetweenPayers:
    def test_one_payers_pin_cannot_confirm_anothers_transfer(self, flow, gateway, pending):
        prepare(gateway, pending)
        outcome = flow.handle("919000000001", "1234")

        assert gateway.executed == []
        assert not outcome.forward       # dropped, not handed to the model
        assert pending.get(PAYER) is not None


class TestAwaitingPinWindow:
    def test_true_while_an_intent_is_open(self, flow, gateway, pending):
        prepare(gateway, pending)
        assert flow.awaiting_pin(PAYER) is True

    def test_false_after_confirmation(self, flow, gateway, pending):
        prepare(gateway, pending)
        flow.handle(PAYER, "1234")
        assert flow.awaiting_pin(PAYER) is False

    def test_false_for_a_different_payer(self, flow, gateway, pending):
        prepare(gateway, pending)
        assert flow.awaiting_pin("919000000001") is False


class TestReceipt:
    def test_carries_the_exact_amount_and_payee(self, gateway, pending):
        intent = prepare(gateway, pending, 250_000)
        gateway.confirm_and_execute(intent, "1234")
        intent.reference = "TXNABCD1234"

        text = receipt(intent, gateway)
        assert "2,500.00" in text
        assert "Ramesh Kumar" in text
        assert "TXNABCD1234" in text

    def test_stub_receipt_says_no_real_money_moved(self, gateway, pending):
        intent = prepare(gateway, pending)
        intent.reference = "TXNABCD1234"
        assert "TEST MODE" in receipt(intent, gateway)

    def test_indian_grouping_on_a_large_amount(self, gateway, pending):
        intent = prepare(gateway, pending, 10_000_000)
        intent.reference = "TXNABCD1234"
        assert "1,00,000.00" in receipt(intent, gateway)
