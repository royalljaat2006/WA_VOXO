"""The four limits, and the gap the cumulative one closes.

Per-transaction and first-time caps bound how bad a *single* mistake can be.
Neither bounds the total: ₹500 to fifty different wrong numbers passes both.
That is what the daily cap is for.
"""

import pytest

from whatsapp_voice.agent.payment_tools import PrepareTransferTool
from whatsapp_voice.agent.payments import PendingIntents, StubPaymentGateway, current_payer
from whatsapp_voice.agent.tools import ToolError
from whatsapp_voice.config import PaymentSettings

PAYER = "919000000009"

#: `_STUB_DIRECTORY`'s known "Ramesh Kumar" number, as a mobilenumber@handle VPA.
RAMESH_VPA = "9876543210@upi"


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


def tool_with(gateway, **overrides):
    current_payer.set(PAYER)
    settings = PaymentSettings(enabled=True, **overrides)
    return PrepareTransferTool(gateway, PendingIntents(), settings)


def send(gateway, amount_paise, payee="9876543210"):
    """Prepare and execute, so the gateway's ledger records a real spend."""
    intent = gateway.prepare_transfer(PAYER, gateway.resolve_payee(payee), amount_paise)
    assert gateway.confirm_and_execute(intent, "1234").ok


class TestDailyCap:
    def test_nothing_spent_yet(self, gateway):
        assert gateway.spent_today_paise(PAYER) == 0

    def test_executing_records_the_spend(self, gateway):
        send(gateway, 200_000)
        assert gateway.spent_today_paise(PAYER) == 200_000

    def test_spends_accumulate(self, gateway):
        send(gateway, 200_000)
        send(gateway, 300_000)
        assert gateway.spent_today_paise(PAYER) == 500_000

    def test_preparing_alone_does_not_count(self, gateway):
        """An intent nobody confirmed spent nothing, so it must not eat into the
        day's allowance."""
        tool = tool_with(gateway)
        tool.run(payee=RAMESH_VPA, amount="2000")
        assert gateway.spent_today_paise(PAYER) == 0

    def test_transfer_over_the_remaining_allowance_is_refused(self, gateway):
        tool = tool_with(gateway, daily_cap_paise=500_000)
        send(gateway, 400_000)
        with pytest.raises(ToolError, match="limit"):
            tool.run(payee=RAMESH_VPA, amount="2000")

    def test_transfer_within_the_remaining_allowance_is_allowed(self, gateway):
        tool = tool_with(gateway, daily_cap_paise=500_000)
        send(gateway, 400_000)
        assert "status=prepared" in tool.run(payee=RAMESH_VPA, amount="500")

    def test_the_message_says_what_is_left(self, gateway):
        """The user has to be able to act on the refusal, not just receive it."""
        tool = tool_with(gateway, daily_cap_paise=500_000)
        send(gateway, 400_000)
        with pytest.raises(ToolError) as caught:
            tool.run(payee=RAMESH_VPA, amount="2000")
        assert "1,000.00" in str(caught.value)   # ₹1,000 left of ₹5,000

    def test_many_small_transfers_hit_the_cap(self, gateway):
        """The gap the per-transaction caps leave open."""
        tool = tool_with(gateway, daily_cap_paise=200_000)
        send(gateway, 50_000)
        send(gateway, 50_000)
        send(gateway, 50_000)
        send(gateway, 50_000)
        with pytest.raises(ToolError):
            tool.run(payee=RAMESH_VPA, amount="100")

    def test_the_cap_is_per_payer(self, gateway):
        send(gateway, 400_000)
        assert gateway.spent_today_paise("919000000001") == 0

    def test_nothing_is_left_when_the_cap_is_exhausted(self, gateway):
        tool = tool_with(gateway, daily_cap_paise=200_000)
        send(gateway, 200_000)
        with pytest.raises(ToolError, match="0.00 is left"):
            tool.run(payee=RAMESH_VPA, amount="100")


class TestCapsCompose:
    def test_per_transaction_cap_is_checked_first(self, gateway):
        """A ₹50,000 request should be told about the ₹10,000 transaction limit,
        not about the day's remaining balance — the transaction limit is the one
        it can never satisfy."""
        tool = tool_with(gateway)
        with pytest.raises(ToolError, match="per-transaction"):
            tool.run(payee=RAMESH_VPA, amount="50000")

    def test_a_refused_transfer_leaves_no_pending_intent(self, gateway):
        tool = tool_with(gateway, daily_cap_paise=100)
        with pytest.raises(ToolError):
            tool.run(payee=RAMESH_VPA, amount="2000")
        assert len(tool._pending) == 0

    def test_defaults_are_the_documented_ones(self):
        settings = PaymentSettings()
        assert settings.max_amount_paise == 1_000_000       # ₹10,000
        assert settings.first_time_cap_paise == 50_000      # ₹500
        assert settings.daily_cap_paise == 2_500_000        # ₹25,000
        assert settings.payee_input_mode == "contact_preferred"

    def test_payments_are_off_by_default(self):
        """Money should require opting in, not failing to opt out."""
        assert PaymentSettings().enabled is False
        assert PaymentSettings().moves_real_money is False
