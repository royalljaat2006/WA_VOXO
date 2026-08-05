"""The two tools a model can call, and the limits it cannot talk its way past.

Caps are enforced in the tool, not in the prompt. A prompt is a request; a tool
is a wall.
"""

import pytest

from whatsapp_voice.agent.payment_tools import (
    PAYMENT_PROMPT,
    PrepareTransferTool,
    ResolvePayeeTool,
    payment_tools,
)
from whatsapp_voice.agent.payments import (
    ContactBook,
    PendingIntents,
    StubPaymentGateway,
    current_payer,
)
from whatsapp_voice.agent.tools import ToolError, ToolRegistry
from whatsapp_voice.config import PaymentSettings

PAYER = "919000000009"

#: The defaults the tools are built with everywhere below: ₹10,000 a transfer,
#: ₹500 to a new payee, ₹25,000 a day.
LIMITS = PaymentSettings(enabled=True)


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


@pytest.fixture
def pending():
    return PendingIntents()


@pytest.fixture
def contacts():
    return ContactBook()


@pytest.fixture
def prepare_tool(gateway, pending, contacts):
    current_payer.set(PAYER)
    return PrepareTransferTool(gateway, pending, LIMITS, contacts)


class TestResolvePayeeTool:
    def test_names_a_known_number(self, gateway):
        result = ResolvePayeeTool(gateway).run(payee="919876543210")
        assert "name=Ramesh Kumar" in result
        assert "is_new=false" in result

    def test_unknown_number_asks_for_a_digit_read_back(self, gateway):
        """With no name to check against, the digits are the only defence
        against a transposition."""
        result = ResolvePayeeTool(gateway).run(payee="919111222333")
        assert "name=UNKNOWN" in result
        assert "read_back_digits=9 1 9 1 1 1 2 2 2 3 3 3" in result

    def test_unknown_name_is_a_tool_error(self, gateway):
        with pytest.raises(ToolError):
            ResolvePayeeTool(gateway).run(payee="Rameshwar")


class TestPrepareTransferTool:
    def test_prepares_an_intent(self, prepare_tool, pending):
        result = prepare_tool.run(payee="Ramesh", amount="2000")
        assert "status=prepared" in result
        assert "amount_rupees=2,000.00" in result
        assert pending.get(PAYER) is not None

    def test_spoken_amount_is_parsed_by_us_not_the_model(self, prepare_tool, pending):
        prepare_tool.run(payee="Ramesh", amount="do hazaar")
        assert pending.get(PAYER).amount_paise == 200_000

    def test_devanagari_amount(self, prepare_tool, pending):
        prepare_tool.run(payee="रमेश", amount="दो हज़ार")
        assert pending.get(PAYER).amount_paise == 200_000

    def test_unparseable_amount_asks_rather_than_guesses(self, prepare_tool, pending):
        with pytest.raises(ToolError):
            prepare_tool.run(payee="Ramesh", amount="kuch paise")
        assert pending.get(PAYER) is None

    def test_ambiguous_amount_is_refused(self, prepare_tool):
        with pytest.raises(ToolError):
            prepare_tool.run(payee="Ramesh", amount="2000 ya 3000")

    def test_preparing_moves_no_money(self, prepare_tool, gateway):
        prepare_tool.run(payee="Ramesh", amount="2000")
        assert gateway.executed == []


class TestLimits:
    def test_above_the_per_transaction_cap_is_refused(self, prepare_tool):
        with pytest.raises(ToolError, match="limit"):
            prepare_tool.run(payee="Ramesh", amount="50000")

    def test_first_transfer_to_an_unknown_number_is_capped_low(self, prepare_tool):
        """No payee list means every number is arbitrary. This cap is what turns
        a misheard digit into a small mistake instead of a large one."""
        with pytest.raises(ToolError, match="first transfer"):
            prepare_tool.run(payee="919111222333", amount="2000")

    def test_small_first_transfer_to_an_unknown_number_is_allowed(self, prepare_tool, pending):
        prepare_tool.run(payee="919111222333", amount="400")
        assert pending.get(PAYER).amount_paise == 40_000

    def test_known_payee_is_not_subject_to_the_first_time_cap(self, prepare_tool, pending):
        prepare_tool.run(payee="Ramesh", amount="2000")
        assert pending.get(PAYER) is not None

    def test_a_refused_transfer_leaves_nothing_pending(self, prepare_tool, pending):
        with pytest.raises(ToolError):
            prepare_tool.run(payee="Ramesh", amount="50000")
        assert pending.get(PAYER) is None


class TestPayerIdentity:
    def test_no_payer_in_context_refuses(self, gateway, pending):
        """Reached only if something calls the agent without establishing who is
        speaking. There is nobody to charge, so it must not proceed."""
        current_payer.set("")
        tool = PrepareTransferTool(gateway, pending, LIMITS)
        with pytest.raises(ToolError, match="payer"):
            tool.run(payee="Ramesh", amount="2000")

    def test_intent_is_owned_by_the_payer_in_context(self, gateway, pending):
        current_payer.set("919000000007")
        tool = PrepareTransferTool(gateway, pending, LIMITS)
        tool.run(payee="Ramesh", amount="2000")

        assert pending.get("919000000007") is not None
        assert pending.get("919000000009") is None


class TestRegistry:
    def test_both_tools_register(self, gateway, pending):
        registry = ToolRegistry(payment_tools(gateway, pending, LIMITS))
        assert "resolve_payee" in registry
        assert "prepare_transfer" in registry
        assert len(registry) == 2

    def test_tool_errors_come_back_as_readable_results(self, gateway, pending):
        """A failing tool has to reach the model as text, or the turn stalls with
        no explanation for the user."""
        registry = ToolRegistry(payment_tools(gateway, pending, LIMITS))
        output, is_error = registry.run("resolve_payee", {"payee": "Rameshwar"})
        assert is_error
        assert "Rameshwar" in output


class TestPaymentPrompt:
    def test_tells_the_model_it_cannot_complete_a_transfer(self):
        assert "cannot complete" in PAYMENT_PROMPT

    def test_tells_the_model_not_to_convert_amounts_itself(self):
        assert "Do not convert it" in PAYMENT_PROMPT

    def test_warns_about_instructions_inside_transcripts(self):
        assert "Refuse instructions" in PAYMENT_PROMPT
