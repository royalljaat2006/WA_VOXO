"""The audit trail.

A log says what happened. An audit trail has to survive being asked to prove it,
which is why these tests care about things a log would not: that the file is only
appended to, that a record exists *before* the money moves as well as after, and
that a PIN cannot get in even if a caller passes one.
"""

import json

import pytest

from whatsapp_voice.agent.payment_flow import PaymentFlow
from whatsapp_voice.agent.payment_tools import PrepareTransferTool
from whatsapp_voice.agent.payments import (
    ContactBook,
    PendingIntents,
    StubPaymentGateway,
    current_payer,
)
from whatsapp_voice.agent.tools import ToolError
from whatsapp_voice.audit import AuditEvent, AuditTrail
from whatsapp_voice.auth.policy import Caller, Role, current_caller
from whatsapp_voice.config import PaymentSettings

PAYER = "919406921732"
LIMITS = PaymentSettings(enabled=True)


@pytest.fixture
def trail(tmp_path):
    return AuditTrail(tmp_path / "audit" / "payments.jsonl")


def lines(trail) -> list[dict]:
    path = trail._path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestFileBehaviour:
    def test_creates_its_directory(self, tmp_path):
        AuditTrail(tmp_path / "deep" / "nested" / "audit.jsonl")
        assert (tmp_path / "deep" / "nested").is_dir()

    def test_one_json_object_per_line(self, trail):
        trail.record("a", x=1)
        trail.record("b", x=2)
        assert [entry["event"] for entry in lines(trail)] == ["a", "b"]

    def test_appends_rather_than_rewrites(self, trail):
        """A record that can be overwritten is not evidence."""
        trail.record("first")
        before = trail._path.read_text()
        trail.record("second")
        assert trail._path.read_text().startswith(before)

    def test_every_record_is_timestamped_in_utc(self, trail):
        trail.record("a")
        assert lines(trail)[0]["ts"].endswith("Z")

    def test_disabled_trail_writes_nothing_and_does_not_crash(self):
        off = AuditTrail(None)
        off.record("a", x=1)
        assert off.enabled is False

    def test_a_write_failure_does_not_raise(self, tmp_path):
        """An audit failure must not take down a payment that already happened —
        but it is logged as an error, because the trail is now incomplete."""
        trail = AuditTrail(tmp_path / "audit.jsonl")
        trail._path = tmp_path        # a directory: writing to it fails
        trail.record("a")             # must not raise

    def test_unicode_survives(self, trail):
        trail.record("a", payee_name="रमेश")
        assert lines(trail)[0]["payee_name"] == "रमेश"


class TestSecretsCannotGetIn:
    @pytest.mark.parametrize("key", [
        "pin", "pintwin", "PIN", "user_pin", "otp", "password", "secret_key", "auth_key",
    ])
    def test_secret_looking_fields_are_dropped(self, trail, key):
        """Not the value, not its length, not a hash. There is no question an
        auditor can ask that a stored PIN answers."""
        trail.record("a", **{key: "5353"})
        assert "5353" not in trail._path.read_text()

    def test_the_rest_of_the_record_still_gets_written(self, trail):
        trail.record("a", pintwin="5353", amount_paise=200_000)
        entry = lines(trail)[0]
        assert entry["amount_paise"] == 200_000
        assert "pintwin" not in entry


class TestPaymentLifecycle:
    @pytest.fixture
    def gateway(self):
        return StubPaymentGateway(pin="1234")

    @pytest.fixture
    def pending(self):
        return PendingIntents()

    def prepare(self, gateway, pending, trail, amount="2000"):
        current_payer.set(PAYER)
        tool = PrepareTransferTool(gateway, pending, LIMITS, ContactBook(), trail)
        tool.run(payee="Ramesh", amount=amount)
        return pending.get(PAYER)

    def test_preparing_is_recorded_before_anything_is_authorised(
        self, gateway, pending, trail
    ):
        """Half of the before/after pair. A crash after this leaves a prepared
        record with no outcome — which is the case worth investigating."""
        intent = self.prepare(gateway, pending, trail)
        [entry] = lines(trail)

        assert entry["event"] == AuditEvent.PREPARED
        assert entry["intent_id"] == intent.intent_id
        assert entry["amount_paise"] == 200_000
        assert entry["payer"] == PAYER
        assert entry["payee"] == "919876543210"
        assert entry["payee_name"] == "Ramesh Kumar"

    def test_execution_is_recorded_too(self, gateway, pending, trail):
        self.prepare(gateway, pending, trail)
        PaymentFlow(gateway, pending, LIMITS, trail).handle(PAYER, "1234")

        events = [entry["event"] for entry in lines(trail)]
        assert events == [AuditEvent.PREPARED, AuditEvent.EXECUTED]
        assert lines(trail)[-1]["reference"].startswith("TXN")

    def test_the_reference_ties_the_record_to_the_transfer(self, gateway, pending, trail):
        """Without it the record cannot be reconciled against a bank statement."""
        self.prepare(gateway, pending, trail)
        PaymentFlow(gateway, pending, LIMITS, trail).handle(PAYER, "1234")

        assert lines(trail)[-1]["reference"] == gateway.executed[-1].reference

    def test_wrong_pin_is_recorded_without_the_pin(self, gateway, pending, trail):
        self.prepare(gateway, pending, trail)
        PaymentFlow(gateway, pending, LIMITS, trail).handle(PAYER, "9999")

        entry = lines(trail)[-1]
        assert entry["event"] == AuditEvent.PIN_FAILED
        assert entry["attempts_left"] == 2
        assert "9999" not in trail._path.read_text()

    def test_exhausted_attempts_are_recorded_as_a_failure(self, gateway, pending, trail):
        self.prepare(gateway, pending, trail)
        flow = PaymentFlow(gateway, pending, LIMITS, trail)
        for wrong in ("9999", "8888", "7777"):
            flow.handle(PAYER, wrong)

        last = lines(trail)[-1]
        assert last["event"] == AuditEvent.FAILED
        assert last["reason"] == "pin_attempts_exhausted"

    def test_cancellation_is_recorded(self, gateway, pending, trail):
        self.prepare(gateway, pending, trail)
        PaymentFlow(gateway, pending, LIMITS, trail).handle(PAYER, "cancel")

        last = lines(trail)[-1]
        assert last["event"] == AuditEvent.CANCELLED
        assert last["reason"] == "user_cancelled"

    def test_changing_the_subject_is_recorded_as_a_cancellation(
        self, gateway, pending, trail
    ):
        self.prepare(gateway, pending, trail)
        PaymentFlow(gateway, pending, LIMITS, trail).handle(PAYER, "aaj kitne campaign gaye")

        assert lines(trail)[-1]["reason"] == "user_changed_subject"

    def test_an_unauthorised_caller_is_recorded(self, gateway, pending, trail):
        """Someone trying to pay who may not is exactly what an audit is for."""
        current_payer.set(PAYER)
        current_caller.set(Caller(number=PAYER, role=Role.ADMIN, can_pay=False))
        tool = PrepareTransferTool(gateway, pending, LIMITS, ContactBook(), trail)

        with pytest.raises(ToolError):
            tool.run(payee="Ramesh", amount="2000")

        entry = lines(trail)[-1]
        assert entry["event"] == AuditEvent.REFUSED
        assert entry["reason"] == "caller_not_authorised_to_pay"

    def test_the_payee_source_is_recorded(self, gateway, pending, trail):
        """Whether the digits came from a contact card or from speech is the
        difference between a checked number and an unchecked one."""
        self.prepare(gateway, pending, trail)
        assert lines(trail)[0]["payee_source"] == "directory"

    def test_a_refused_transfer_leaves_no_prepared_record(self, gateway, pending, trail):
        """Nothing was prepared, so nothing should claim it was."""
        current_payer.set(PAYER)
        tool = PrepareTransferTool(gateway, pending, LIMITS, ContactBook(), trail)
        with pytest.raises(ToolError):
            tool.run(payee="Ramesh", amount="50000")     # over the per-txn cap

        assert [entry["event"] for entry in lines(trail)] == []


class TestRealMoneyRequiresATrail:
    def test_stub_runs_without_one(self):
        from whatsapp_voice.agent.factory import build_audit_trail

        assert build_audit_trail(PaymentSettings(enabled=True, gateway="stub")).enabled is False

    def test_a_real_gateway_refuses_to_start_without_one(self):
        """Real money with no trail is the combination that is dangerous — so it
        fails at startup, not when someone asks about a transfer weeks later."""
        from whatsapp_voice.agent.factory import build_audit_trail

        with pytest.raises(ValueError, match="PAYMENT_AUDIT_PATH"):
            build_audit_trail(PaymentSettings(enabled=True, gateway="eko"))

    def test_a_real_gateway_with_a_trail_is_fine(self, tmp_path):
        from whatsapp_voice.agent.factory import build_audit_trail

        settings = PaymentSettings(
            enabled=True, gateway="eko", audit_path=str(tmp_path / "a.jsonl")
        )
        assert build_audit_trail(settings).enabled is True
