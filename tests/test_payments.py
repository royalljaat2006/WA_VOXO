"""The payment gateway boundary.

The property these tests defend is structural: the money-moving method is not on
any path a model can reach. If someone later adds `execute_transfer` to the
gateway and wires it to a tool, `test_no_execute_tool_exists` fails.
"""

import time

import pytest

from whatsapp_voice.agent.payments import (
    IntentStatus,
    Payee,
    PaymentError,
    PendingIntents,
    StubPaymentGateway,
    spell_digits,
)
from whatsapp_voice.store import MemoryStore


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


class TestResolvePayee:
    def test_known_number_resolves_to_a_name(self, gateway):
        payee = gateway.resolve_payee("919876543210")
        assert payee.resolved_name == "Ramesh Kumar"
        assert payee.masked_account == "•••4321"
        assert payee.is_new is False

    def test_known_name_resolves_to_a_number(self, gateway):
        assert gateway.resolve_payee("Ramesh").phone == "919876543210"

    def test_devanagari_name_resolves(self, gateway):
        """Sarvam returns Hindi speech in Devanagari, so this is the normal path
        for a Hindi speaker, not an edge case."""
        assert gateway.resolve_payee("रमेश").phone == "919876543210"

    def test_name_lookup_is_case_insensitive(self, gateway):
        assert gateway.resolve_payee("RAMESH KUMAR").phone == "919876543210"

    def test_unknown_number_resolves_but_stays_unnamed(self, gateway):
        """Arbitrary numbers are allowed — the user has no payee list. What is
        not allowed is pretending we know who it is."""
        payee = gateway.resolve_payee("9123456789012"[:12])
        assert payee.resolved_name is None
        assert payee.is_new is True

    def test_unknown_name_is_an_error_not_a_guess(self, gateway):
        """A near match would send money to the wrong human being."""
        with pytest.raises(PaymentError):
            gateway.resolve_payee("Rameshwar")

    def test_close_but_wrong_name_is_not_fuzzy_matched(self, gateway):
        with pytest.raises(PaymentError):
            gateway.resolve_payee("Ramash")

    def test_number_with_formatting_still_resolves(self, gateway):
        assert gateway.resolve_payee("+91 98765 43210").phone == "919876543210"


class TestPrepareTransfer:
    def test_prepared_intent_is_not_executed(self, gateway):
        payee = gateway.resolve_payee("Ramesh")
        intent = gateway.prepare_transfer("919000000009", payee, 200_000)
        assert intent.status is IntentStatus.AWAITING_PIN
        assert gateway.executed == []

    def test_zero_amount_is_refused(self, gateway):
        payee = gateway.resolve_payee("Ramesh")
        with pytest.raises(PaymentError):
            gateway.prepare_transfer("919000000009", payee, 0)

    def test_negative_amount_is_refused(self, gateway):
        payee = gateway.resolve_payee("Ramesh")
        with pytest.raises(PaymentError):
            gateway.prepare_transfer("919000000009", payee, -100)

    def test_each_intent_gets_its_own_id(self, gateway):
        payee = gateway.resolve_payee("Ramesh")
        a = gateway.prepare_transfer("919000000009", payee, 200_000)
        b = gateway.prepare_transfer("919000000009", payee, 200_000)
        assert a.intent_id != b.intent_id


class TestConfirmAndExecute:
    def test_correct_pin_executes(self, gateway):
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        result = gateway.confirm_and_execute(intent, "1234")
        assert result.ok
        assert result.reference.startswith("TXN")
        assert gateway.executed == [intent]

    def test_wrong_pin_does_not_execute(self, gateway):
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        result = gateway.confirm_and_execute(intent, "9999")
        assert not result.ok
        assert gateway.executed == []

    def test_failure_message_does_not_leak_the_correct_pin(self, gateway):
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        result = gateway.confirm_and_execute(intent, "9999")
        assert "1234" not in (result.message or "")


class TestNoExecutePath:
    def test_no_execute_tool_exists(self):
        """The structural guarantee: nothing a model can call moves money.

        `confirm_and_execute` takes a PIN, and only the handler ever has one.
        """
        from whatsapp_voice.agent.payment_tools import payment_tools
        from whatsapp_voice.config import PaymentSettings

        gateway = StubPaymentGateway()
        tools = payment_tools(gateway, PendingIntents(), PaymentSettings(enabled=True))
        names = {tool.name for tool in tools}

        assert names == {"resolve_payee", "prepare_transfer"}
        for forbidden in ("execute_transfer", "send_money", "confirm_transfer", "transfer"):
            assert forbidden not in names

    def test_gateway_execute_requires_a_pin(self):
        """No default — a caller cannot execute by omitting the argument."""
        import inspect

        signature = inspect.signature(StubPaymentGateway.confirm_and_execute)
        assert signature.parameters["pin"].default is inspect.Parameter.empty


class TestPendingIntents:
    def test_put_and_get(self, gateway):
        """Store-backed: `get()` deserialises its own copy, so this checks
        equality, not identity — see `PendingIntents`'s own docstring."""
        pending = PendingIntents()
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        pending.put(intent)
        assert pending.get("919000000009") == intent

    def test_get_for_another_payer_is_none(self, gateway):
        """A PIN from one person must never confirm another person's transfer."""
        pending = PendingIntents()
        pending.put(
            gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        )
        assert pending.get("919000000008") is None

    def test_second_intent_replaces_the_first(self, gateway):
        """One pending intent per payer, so a stray PIN cannot be applied to the
        wrong transfer."""
        pending = PendingIntents()
        payee = gateway.resolve_payee("Ramesh")
        first = gateway.prepare_transfer("919000000009", payee, 200_000)
        second = gateway.prepare_transfer("919000000009", payee, 500_000)
        pending.put(first)
        pending.put(second)
        assert pending.get("919000000009") == second
        assert len(pending) == 1

    def test_expired_intent_is_not_returned(self):
        """A PIN spoken long after the instruction confirms nothing."""
        gateway = StubPaymentGateway(pin="1234", intent_ttl_s=-1)
        pending = PendingIntents()
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        pending.put(intent)
        assert pending.get("919000000009") is None

    def test_clear_removes_it(self, gateway):
        pending = PendingIntents()
        pending.put(
            gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        )
        pending.clear("919000000009")
        assert pending.get("919000000009") is None

    def test_intent_is_open_within_ttl(self, gateway):
        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        assert intent.is_open()
        assert not intent.is_expired(time.time())


class TestPendingIntentsAcrossWorkers:
    """The reason `PendingIntents` is store-backed at all: `store.py`'s own
    ranking calls this the worst of the shared-state gaps this project has —
    "worker A prepares the transfer, worker B gets the PIN and finds nothing
    pending." These simulate two workers as two separate `PendingIntents`
    instances sharing one `MemoryStore`, the same relationship `RedisStore`
    has to real separate processes."""

    def test_a_different_instance_sees_what_was_prepared(self, gateway):
        store = MemoryStore()
        worker_a = PendingIntents(store)
        worker_b = PendingIntents(store)

        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        worker_a.put(intent)

        assert worker_b.get("919000000009") == intent

    def test_a_clear_on_one_instance_is_visible_on_another(self, gateway):
        store = MemoryStore()
        worker_a = PendingIntents(store)
        worker_b = PendingIntents(store)

        worker_a.put(
            gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        )
        worker_b.clear("919000000009")

        assert worker_a.get("919000000009") is None

    def test_find_by_id_works_across_instances(self, gateway):
        """The payment callback path (`whatsapp/callback.py`) knows the
        transfer, not who is waiting for it — this lookup direction has to
        work from whichever worker the callback happens to land on."""
        store = MemoryStore()
        worker_a = PendingIntents(store)
        worker_b = PendingIntents(store)

        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        worker_a.put(intent)

        assert worker_b.find_by_id(intent.intent_id) == intent

    def test_an_attempt_increment_is_visible_across_instances_once_saved(self, gateway):
        """The wrong-PIN counter (`agent/payment_flow.py::_confirm`) mutates a
        fetched intent and must call `put()` again to save it — this is the
        regression that would silently reopen the "unlimited PIN guesses"
        hole if that explicit re-save were ever removed."""
        from dataclasses import replace

        store = MemoryStore()
        worker_a = PendingIntents(store)
        worker_b = PendingIntents(store)

        intent = gateway.prepare_transfer("919000000009", gateway.resolve_payee("Ramesh"), 200_000)
        worker_a.put(intent)

        fetched = worker_b.get("919000000009")
        worker_b.put(replace(fetched, attempts=fetched.attempts + 1))

        assert worker_a.get("919000000009").attempts == 1


class TestPayeeDisplay:
    def test_named_payee_shows_name_and_account(self):
        payee = Payee(phone="919876543210", resolved_name="Ramesh Kumar",
                      masked_account="•••4321")
        assert payee.display == "Ramesh Kumar (•••4321)"

    def test_unnamed_payee_shows_grouped_digits(self):
        assert Payee(phone="919876543210").display == "91 98765 43210"

    def test_spell_digits_is_readable_aloud(self):
        assert spell_digits("919876543210") == "9 1 9 8 7 6 5 4 3 2 1 0"
