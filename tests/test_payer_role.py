"""Who may move money.

Reading a report and sending a rupee are different powers on different axes.
Before this existed, every number on the allowlist could do both — a `viewer` who
could see delivery rates could also transfer ₹10,000.

The number still does not authorise the transfer; a PIN does. This decides who is
allowed to *try*.
"""

import pytest

from whatsapp_voice.agent.payment_tools import PrepareTransferTool
from whatsapp_voice.agent.payments import (
    ContactBook,
    PendingIntents,
    StubPaymentGateway,
    current_payer,
)
from whatsapp_voice.agent.tools import ToolError
from whatsapp_voice.auth.guard import AccessGuard
from whatsapp_voice.auth.policy import AccessPolicy, Caller, Role, current_caller
from whatsapp_voice.config import AuthSettings, PaymentSettings

PAYER = "919876543210"
LIMITS = PaymentSettings(enabled=True)


class TestParsingThePayFlag:
    def test_plain_role_cannot_pay(self):
        """The default has to be "no". A role is about what you may see."""
        policy = AccessPolicy.parse(f"{PAYER}:admin")
        assert policy.lookup(PAYER).can_pay is False

    def test_pay_suffix_grants_it(self):
        policy = AccessPolicy.parse(f"{PAYER}:admin+pay")
        caller = policy.lookup(PAYER)
        assert caller.can_pay is True
        assert caller.role is Role.ADMIN

    def test_pay_works_on_any_role(self):
        """Payment is orthogonal to read access, not a rank above admin."""
        policy = AccessPolicy.parse(f"{PAYER}:viewer+pay")
        caller = policy.lookup(PAYER)
        assert caller.role is Role.VIEWER
        assert caller.can_pay is True

    def test_bare_number_with_pay_gets_the_default_role(self):
        policy = AccessPolicy.parse(f"{PAYER}:+pay", default_role=Role.VIEWER)
        caller = policy.lookup(PAYER)
        assert caller.role is Role.VIEWER
        assert caller.can_pay is True

    def test_only_the_listed_number_gets_it(self):
        policy = AccessPolicy.parse(f"{PAYER}:admin+pay,919000000001:admin")
        assert policy.lookup(PAYER).can_pay is True
        assert policy.lookup("919000000001").can_pay is False

    def test_a_mistyped_role_still_fails_loudly(self):
        """A typo must not silently downgrade — or silently grant payment."""
        with pytest.raises(ValueError, match="Unknown role"):
            AccessPolicy.parse(f"{PAYER}:analsyt+pay")

    def test_bare_number_cannot_pay(self):
        assert AccessPolicy.parse(PAYER).lookup(PAYER).can_pay is False


class TestGuardPublishesTheCaller:
    def test_allowed_caller_is_published(self):
        guard = AccessGuard.from_settings(
            AuthSettings(enabled=True, allowed_numbers=f"{PAYER}:admin+pay")
        )
        result = guard.check(PAYER)
        assert result.allowed
        assert result.caller.can_pay is True

    def test_auth_disabled_permits_payment(self):
        """Otherwise local testing could never exercise the payment path. The
        constructor warns loudly about exactly this."""
        guard = AccessGuard.from_settings(AuthSettings(enabled=False))
        assert guard.check(PAYER).caller.can_pay is True

    def test_unknown_number_has_no_caller(self):
        guard = AccessGuard.from_settings(
            AuthSettings(enabled=True, allowed_numbers=f"{PAYER}:admin+pay")
        )
        result = guard.check("919000000002")
        assert not result.allowed
        assert result.caller is None


class TestToolRefusesWithoutTheFlag:
    @pytest.fixture
    def tool(self):
        current_payer.set(PAYER)
        return PrepareTransferTool(
            StubPaymentGateway(), PendingIntents(), LIMITS, ContactBook()
        )

    def test_a_reader_cannot_transfer(self, tool):
        current_caller.set(Caller(number=PAYER, role=Role.ADMIN, can_pay=False))
        with pytest.raises(ToolError, match="not authorised to send money"):
            tool.run(payee="Ramesh", amount="2000")

    def test_a_payer_can(self, tool):
        current_caller.set(Caller(number=PAYER, role=Role.VIEWER, can_pay=True))
        assert "status=prepared" in tool.run(payee="Ramesh", amount="2000")

    def test_no_caller_at_all_is_refused(self, tool):
        """Fail closed: an unset caller means nobody verified this request."""
        current_caller.set(None)
        with pytest.raises(ToolError, match="not authorised"):
            tool.run(payee="Ramesh", amount="2000")

    def test_a_refused_caller_leaves_nothing_pending(self, tool):
        current_caller.set(Caller(number=PAYER, role=Role.ADMIN, can_pay=False))
        with pytest.raises(ToolError):
            tool.run(payee="Ramesh", amount="2000")
        assert len(tool._pending) == 0

    def test_refusal_does_not_move_money(self, tool):
        current_caller.set(Caller(number=PAYER, role=Role.ADMIN, can_pay=False))
        with pytest.raises(ToolError):
            tool.run(payee="Ramesh", amount="2000")
        assert tool._gateway.executed == []


class TestReadAccessIsUnaffected:
    def test_roles_still_rank_as_before(self):
        assert Role.ADMIN.allows(Role.VIEWER)
        assert not Role.VIEWER.allows(Role.ADMIN)

    def test_a_payer_does_not_automatically_see_more(self):
        """+pay must not smuggle in read access a viewer should not have."""
        caller = AccessPolicy.parse(f"{PAYER}:viewer+pay").lookup(PAYER)
        assert not caller.may(Role.ANALYST)
