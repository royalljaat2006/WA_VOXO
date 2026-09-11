"""Pushing a PINTWIN key to the payer's phone.

The property that matters most here is a negative one: this is a side
channel, and no failure inside it may cost a payer their transfer. Every
test below is either about that, or about not aiming a real notification at
the wrong phone.
"""

import pytest

from whatsapp_voice.agent.payment_tools import PrepareTransferTool
from whatsapp_voice.agent.payments import PendingIntents, StubPaymentGateway, current_payer
from whatsapp_voice.agent.pintwin_push import (
    HttpKeyPusher,
    NullKeyPusher,
    build_key_pusher,
)
from whatsapp_voice.agent.tools import ToolError
from whatsapp_voice.config import PaymentSettings, PinTwinSettings

PAYER = "919000000009"
RAMESH_VPA = "9876543210@upi"
LIMITS = PaymentSettings(enabled=True)
CONFIGURED = PinTwinSettings(enabled=True, push_url="https://eko.example/keys/push")


class FakeSession:
    """Records posts. Optionally explodes, to prove failures are contained."""

    def __init__(self, status_code=200, error=None):
        self._status_code = status_code
        self._error = error
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if self._error:
            raise self._error
        return type("R", (), {"status_code": self._status_code})()


class RecordingPusher:
    def __init__(self, result=True):
        self.calls = []
        self._result = result

    def push(self, payer, intent_id):
        self.calls.append((payer, intent_id))
        return self._result


class ExplodingPusher:
    """A pusher that ignores its own contract and raises anyway."""

    def push(self, payer, intent_id):
        raise RuntimeError("push service on fire")


@pytest.fixture
def pending():
    return PendingIntents()


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


def prepare_tool(gateway, pending, pusher=None):
    current_payer.set(PAYER)
    return PrepareTransferTool(gateway, pending, LIMITS, None, pusher)


class TestWhichPusherGetsBuilt:
    def test_nothing_configured_means_nothing_is_pushed(self):
        assert isinstance(build_key_pusher(None), NullKeyPusher)

    def test_off_by_default(self):
        """A deployment that has not opted in must gain no network call in the
        payment path."""
        assert isinstance(build_key_pusher(PinTwinSettings()), NullKeyPusher)

    def test_enabled_without_a_url_falls_back_rather_than_crashing(self):
        settings = PinTwinSettings(enabled=True, push_url=None)
        assert isinstance(build_key_pusher(settings), NullKeyPusher)

    def test_fully_configured_gets_the_http_pusher(self):
        assert isinstance(build_key_pusher(CONFIGURED), HttpKeyPusher)


class TestTheRequest:
    def test_it_is_keyed_by_the_payers_own_mobile(self):
        """The same identity the transfer travels under, and the same number
        the app is logged in with — one person, one phone."""
        session = FakeSession()
        HttpKeyPusher(CONFIGURED, session=session).push(PAYER, "intent-1")

        assert session.posts[0]["json"]["mobile"] == "9000000009"

    def test_it_carries_the_intent_for_correlation(self):
        session = FakeSession()
        HttpKeyPusher(CONFIGURED, session=session).push(PAYER, "intent-1")
        assert session.posts[0]["json"]["reference"] == "intent-1"

    def test_the_api_key_rides_as_a_bearer_token(self):
        session = FakeSession()
        settings = PinTwinSettings(
            enabled=True, push_url="https://eko.example/keys/push", api_key="k-1"
        )
        HttpKeyPusher(settings, session=session).push(PAYER, "intent-1")

        assert session.posts[0]["headers"]["Authorization"] == "Bearer k-1"

    def test_a_payer_who_is_not_a_phone_number_is_skipped(self):
        """The demo path's `demo:<session>` reaches here. Inventing a
        mobile-shaped string would aim a real notification at a stranger."""
        session = FakeSession()
        sent = HttpKeyPusher(CONFIGURED, session=session).push("demo:abc", "intent-1")

        assert sent is False
        assert session.posts == [], "pushed to a fabricated number"


class TestFailuresAreContained:
    """A side channel may never raise into the payment path."""

    def test_a_network_error_is_swallowed(self):
        session = FakeSession(error=ConnectionResetError("socket died"))
        assert HttpKeyPusher(CONFIGURED, session=session).push(PAYER, "i-1") is False

    def test_a_timeout_is_swallowed(self):
        session = FakeSession(error=TimeoutError("too slow"))
        assert HttpKeyPusher(CONFIGURED, session=session).push(PAYER, "i-1") is False

    def test_a_rejection_is_reported_not_raised(self):
        session = FakeSession(status_code=500)
        assert HttpKeyPusher(CONFIGURED, session=session).push(PAYER, "i-1") is False

    def test_success_says_so(self):
        assert HttpKeyPusher(CONFIGURED, session=FakeSession(200)).push(PAYER, "i-1") is True


class TestItRunsAtTheRightMomentInAPayment:
    def test_a_prepared_transfer_asks_for_a_key(self, gateway, pending):
        """The bot's next sentence asks for the PINTWIN, so this is exactly
        when the payer needs one."""
        pusher = RecordingPusher()
        prepare_tool(gateway, pending, pusher).run(
            payee=RAMESH_VPA, amount="2000", recipient_name="Ramesh Kumar"
        )

        assert pusher.calls == [(PAYER, pending.get(PAYER).intent_id)]

    def test_a_refused_transfer_pushes_nothing(self, gateway, pending):
        """No intent, no PINTWIN to ask for — a notification here would invite
        the payer to authorise something that does not exist."""
        pusher = RecordingPusher()
        with pytest.raises(ToolError):
            prepare_tool(gateway, pending, pusher).run(
                payee=RAMESH_VPA, amount="50000", recipient_name="Ramesh Kumar"
            )

        assert pusher.calls == []

    def test_the_transfer_survives_a_pusher_that_raises(self, gateway, pending):
        """`NullKeyPusher` and `HttpKeyPusher` both honour the contract, but a
        future one might not. The payment must not be what discovers that."""
        tool = prepare_tool(gateway, pending, ExplodingPusher())
        result = tool.run(
            payee=RAMESH_VPA, amount="2000", recipient_name="Ramesh Kumar"
        )

        assert "status=prepared" in result
        assert pending.get(PAYER) is not None

    def test_no_pusher_configured_still_prepares_normally(self, gateway, pending):
        result = prepare_tool(gateway, pending).run(
            payee=RAMESH_VPA, amount="2000", recipient_name="Ramesh Kumar"
        )
        assert "status=prepared" in result
