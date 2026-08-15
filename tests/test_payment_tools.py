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
from whatsapp_voice.agent.payments import PendingIntents, StubPaymentGateway, current_payer
from whatsapp_voice.agent.tools import ToolError, ToolRegistry
from whatsapp_voice.config import PaymentSettings

PAYER = "919000000009"

#: A VPA whose local part happens to be `_STUB_DIRECTORY`'s known
#: "Ramesh Kumar" number — one of several shapes a real VPA takes.
RAMESH_VPA = "9876543210@upi"
#: A VPA with a valid mobile-number local part that is not in the stub
#: directory — an arbitrary, unnamed payee.
UNKNOWN_VPA = "9111222333@upi"

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
def prepare_tool(gateway, pending):
    current_payer.set(PAYER)
    return PrepareTransferTool(gateway, pending, LIMITS)


class TestResolvePayeeTool:
    def test_names_a_known_vpa(self, gateway):
        result = ResolvePayeeTool(gateway).run(payee=RAMESH_VPA)
        assert "name=Ramesh Kumar" in result
        assert "is_new=false" in result
        assert f"vpa={RAMESH_VPA}" in result

    def test_unknown_vpa_is_still_resolved_but_unnamed(self, gateway):
        result = ResolvePayeeTool(gateway).run(payee=UNKNOWN_VPA)
        assert "name=UNKNOWN" in result
        assert "is_new=true" in result

    def test_spoken_at_is_understood(self, gateway):
        """ASR renders '@' as the word 'at', never the symbol."""
        result = ResolvePayeeTool(gateway).run(payee="9876543210 at upi")
        assert f"vpa={RAMESH_VPA}" in result

    def test_not_a_vpa_is_a_tool_error(self, gateway):
        with pytest.raises(ToolError):
            ResolvePayeeTool(gateway).run(payee="Rameshwar")

    def test_a_name_based_vpa_resolves_on_its_own(self, gateway):
        """`customer_vpa` identifies the payee to eko by itself, so a UPI ID
        with no mobile number in it needs nothing added."""
        result = ResolvePayeeTool(gateway).run(payee="ramesh@okaxis")
        assert "vpa=ramesh@okaxis" in result


class TestPrepareTransferTool:
    def test_prepares_an_intent(self, prepare_tool, pending):
        result = prepare_tool.run(payee=RAMESH_VPA, amount="2000")
        assert "status=prepared" in result
        assert "amount_rupees=2,000.00" in result
        assert f"payee_vpa={RAMESH_VPA}" in result
        assert pending.get(PAYER) is not None

    def test_the_tool_result_carries_the_payer_initiator_id(self, prepare_tool):
        """Per an explicit instruction from the project owner: the
        confirmation the user sees must be `Payer = <initiator_id>` / `Payee
        = ...` / `Amount = ...`, and the model is told to fill in
        `payer_initiator_id` rather than derive it — the same 10-digit,
        no-country-code shape eko_gateway.py sends eko as `initiator_id`
        (PAYER's own digits, minus the "91")."""
        result = prepare_tool.run(payee=RAMESH_VPA, amount="2000")
        assert "payer_initiator_id=9000000009" in result
        assert "Payer = <payer_initiator_id>" in result
        assert "Payee = <the UPI ID>" in result
        assert "Amount = <amount_rupees>" in result

    def test_the_intent_carries_both_the_vpa_and_the_mobile_number(self, prepare_tool, pending):
        prepare_tool.run(payee=RAMESH_VPA, amount="2000")
        payee = pending.get(PAYER).payee
        assert payee.vpa == RAMESH_VPA
        assert payee.phone == "9876543210"
        assert payee.resolved_name == "Ramesh Kumar"

    def test_spoken_amount_is_parsed_by_us_not_the_model(self, prepare_tool, pending):
        prepare_tool.run(payee=RAMESH_VPA, amount="do hazaar")
        assert pending.get(PAYER).amount_paise == 200_000

    def test_devanagari_amount(self, prepare_tool, pending):
        prepare_tool.run(payee=RAMESH_VPA, amount="दो हज़ार")
        assert pending.get(PAYER).amount_paise == 200_000

    def test_unparseable_amount_asks_rather_than_guesses(self, prepare_tool, pending):
        with pytest.raises(ToolError):
            prepare_tool.run(payee=RAMESH_VPA, amount="kuch paise")
        assert pending.get(PAYER) is None

    def test_ambiguous_amount_is_refused(self, prepare_tool):
        with pytest.raises(ToolError):
            prepare_tool.run(payee=RAMESH_VPA, amount="2000 ya 3000")

    def test_preparing_moves_no_money(self, prepare_tool, gateway):
        prepare_tool.run(payee=RAMESH_VPA, amount="2000")
        assert gateway.executed == []

    def test_not_a_vpa_is_a_tool_error(self, prepare_tool, pending):
        with pytest.raises(ToolError):
            prepare_tool.run(payee="Ramesh", amount="2000")
        assert pending.get(PAYER) is None

    def test_a_name_based_vpa_prepares_on_its_own(self, prepare_tool, pending):
        prepare_tool.run(payee="ramesh@okaxis", amount="400")
        assert pending.get(PAYER).payee.vpa == "ramesh@okaxis"


class TestLimits:
    def test_above_the_per_transaction_cap_is_refused(self, prepare_tool):
        with pytest.raises(ToolError, match="limit"):
            prepare_tool.run(payee=RAMESH_VPA, amount="50000")

    def test_first_transfer_to_an_unknown_vpa_is_capped_low(self, prepare_tool):
        """No payee list means every VPA is arbitrary. This cap is what turns a
        misheard digit into a small mistake instead of a large one."""
        with pytest.raises(ToolError, match="first transfer"):
            prepare_tool.run(payee=UNKNOWN_VPA, amount="2000")

    def test_small_first_transfer_to_an_unknown_vpa_is_allowed(self, prepare_tool, pending):
        prepare_tool.run(payee=UNKNOWN_VPA, amount="400")
        assert pending.get(PAYER).amount_paise == 40_000

    def test_known_payee_is_not_subject_to_the_first_time_cap(self, prepare_tool, pending):
        prepare_tool.run(payee=RAMESH_VPA, amount="2000")
        assert pending.get(PAYER) is not None

    def test_a_refused_transfer_leaves_nothing_pending(self, prepare_tool, pending):
        with pytest.raises(ToolError):
            prepare_tool.run(payee=RAMESH_VPA, amount="50000")
        assert pending.get(PAYER) is None


class TestPayerIdentity:
    def test_no_payer_in_context_refuses(self, gateway, pending):
        """Reached only if something calls the agent without establishing who is
        speaking. There is nobody to charge, so it must not proceed."""
        current_payer.set("")
        tool = PrepareTransferTool(gateway, pending, LIMITS)
        with pytest.raises(ToolError, match="payer"):
            tool.run(payee=RAMESH_VPA, amount="2000")

    def test_intent_is_owned_by_the_payer_in_context(self, gateway, pending):
        current_payer.set("919000000007")
        tool = PrepareTransferTool(gateway, pending, LIMITS)
        tool.run(payee=RAMESH_VPA, amount="2000")

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

    def test_does_not_claim_a_vpa_must_contain_a_mobile_number(self):
        """Most VPAs do not. Telling the model otherwise made it reject
        ordinary UPI IDs like 'firstname.lastname@okicici'."""
        assert "mobilenumber@handle" not in PAYMENT_PROMPT
        assert "okicici" in PAYMENT_PROMPT, "show the model a name-based VPA"


class TestTheVpaIsTheWholeIdentification:
    """`customer_vpa` identifies the receiving party to eko on its own.

    An earlier version required a 10-digit mobile for every payee, on the
    strength of one confirmed curl that carried both fields — but that curl's
    VPA was itself `<mobile>@upi`, so the two held the same digits and the
    mobile looked mandatory. It is not. Requiring it meant an ordinary UPI ID
    like `name@okicici` could not be paid without interrogating the sender for
    a number eko never needed.
    """

    @pytest.mark.parametrize("vpa", [
        "firstname.lastname@okicici",
        "nickname99@ybl",
        "first_last@paytm",
        "name-with-dash@okaxis",
        "9000000000@upi",
        "919000000000@upi",
    ])
    def test_every_shape_of_real_upi_id_resolves(self, gateway, vpa):
        assert f"vpa={vpa}" in ResolvePayeeTool(gateway).run(payee=vpa)

    @pytest.mark.parametrize("vpa", [
        "firstname.lastname@okicici",
        "nickname99@ybl",
        "9000000000@upi",
    ])
    def test_every_shape_of_real_upi_id_prepares(self, prepare_tool, pending, vpa):
        prepare_tool.run(payee=vpa, amount="400")
        assert pending.get(PAYER).payee.vpa == vpa

    def test_a_mobile_shaped_vpa_still_fills_in_the_phone(self, gateway):
        """Reproduces the confirmed curl: both `customer_vpa` and `customer_id`."""
        result = ResolvePayeeTool(gateway).run(payee=RAMESH_VPA)
        assert "phone=9876543210" in result
        assert "name=Ramesh Kumar" in result

    def test_a_name_based_vpa_carries_no_phone_rather_than_a_made_up_one(self, gateway):
        """Empty is the honest answer. A fabricated `customer_id` pays a
        stranger, and eko does not need one."""
        result = ResolvePayeeTool(gateway).run(payee="firstname.lastname@okicici")
        assert "phone=\n" in result + "\n"

    def test_digits_inside_a_name_are_not_mistaken_for_a_phone_number(self, gateway):
        """`nickname9876543210@ybl` is a handle. Reading a number out of it
        would attach someone else's mobile to this payee."""
        result = ResolvePayeeTool(gateway).run(payee="nickname9876543210@ybl")
        assert "phone=\n" in result + "\n"

    def test_a_name_based_payee_is_still_new_and_capped(self, prepare_tool):
        """No directory hit means the first-time cap applies — the protection
        that matters most when the payee cannot be named."""
        with pytest.raises(ToolError, match="first transfer"):
            prepare_tool.run(payee="firstname.lastname@okicici", amount="2000")


class TestPromptDoesNotAskForExtraDetails:
    def test_it_forbids_asking_for_a_phone_number(self):
        assert "Never ask for a phone" in PAYMENT_PROMPT

    def test_it_names_the_whole_flow(self):
        assert "Amount, UPI ID, PIN" in PAYMENT_PROMPT


class TestTheLookupToolIsOnlyOfferedWhenItCanAnswer:
    """Two tools mean two chances for the model to stop after the first.

    The observed failure: the model called `resolve_payee`, replied asking for
    the PIN, and never called `prepare_transfer`. Nothing was pending, so the
    PIN the user then spoke was dropped by the gate and they were told no
    transfer existed. Against a gateway that cannot name a payee — eko's
    transfer API never returns a name — the lookup buys nothing to pay for that
    risk, so it is not offered at all.
    """

    class Nameless(StubPaymentGateway):
        resolves_names = False

    def test_a_gateway_that_cannot_name_a_payee_gets_one_tool(self, pending):
        registry = ToolRegistry(payment_tools(self.Nameless(), pending, LIMITS))

        assert "prepare_transfer" in registry
        assert "resolve_payee" not in registry
        assert len(registry) == 1

    def test_a_gateway_that_can_name_a_payee_still_gets_both(self, gateway, pending):
        registry = ToolRegistry(payment_tools(gateway, pending, LIMITS))
        assert "resolve_payee" in registry

    def test_preparing_still_works_with_no_lookup_step(self, pending):
        """The one remaining tool has to be able to do the whole job — it
        resolves the payee itself."""
        current_payer.set(PAYER)
        [prepare] = payment_tools(self.Nameless(), pending, LIMITS)
        result = prepare.run(payee="firstname.lastname@okicici", amount="400")

        assert "status=prepared" in result
        assert pending.get(PAYER).payee.vpa == "firstname.lastname@okicici"

    def test_the_prompt_tells_the_model_to_prepare_as_soon_as_it_can(self):
        assert "As soon as you have BOTH" in PAYMENT_PROMPT
        assert "call the tool, then speak" in PAYMENT_PROMPT
