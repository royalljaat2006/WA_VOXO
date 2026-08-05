"""eko's real payment API.

No network here — the session is a fake that records what would have been sent.
What matters is the request body, because every field in it is something eko acts
on, and one of them is the user's PIN.
"""

import copy
import json

import pytest

from whatsapp_voice.agent.eko_gateway import EkoPaymentGateway, _is_pin_failure, _rupees
from whatsapp_voice.agent.payments import Payee, PayeeSource, PaymentError
from whatsapp_voice.config import EkoSettings

PAYER = "919406921732"


def settings(**overrides) -> EkoSettings:
    base = dict(
        base_url="https://api.eko.in:25002",
        developer_key="dev-key",
        auth_key="auth-key",
        user_code="10472151",
        initiator_id="9406921732",
        latlong="28.494226,77.082654,15",
    )
    base.update(overrides)
    return EkoSettings(**base)


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {"status": "0", "data": {"tid": "T1"}}
        self.status_code = status_code
        self.content = json.dumps(self._payload).encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    """Records posts instead of making them.

    The body is snapshotted, not referenced. A real client serialises it during
    the call, so the gateway is free to scrub the PIN out of the dict afterwards
    — holding a live reference would show the scrubbed value and hide what was
    actually sent.
    """

    def __init__(self, response=None, error=None):
        self._response = response or FakeResponse()
        self._error = error
        self.posts = []
        self.live_bodies = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "body": copy.deepcopy(json), "headers": headers})
        self.live_bodies.append(json)
        if self._error:
            raise self._error
        return self._response

    @property
    def last_body(self):
        return self.posts[-1]["body"]


@pytest.fixture
def payee():
    return Payee(phone="9002331157", resolved_name="RAM", source=PayeeSource.DIRECTORY)


def gateway(session=None, **overrides):
    return EkoPaymentGateway(settings(**overrides), session=session or FakeSession())


class TestConfiguration:
    def test_missing_credentials_name_themselves(self):
        """"Set EKO_USER_CODE" is fixable; "not configured" is a guessing game."""
        missing = EkoSettings(base_url="x").missing_fields()
        assert "EKO_DEVELOPER_KEY" in missing
        assert "EKO_USER_CODE" in missing

    def test_latlong_is_required(self):
        """eko rejects a transfer with no location, and WhatsApp gives us none."""
        assert "EKO_LATLONG" in settings(latlong="").missing_fields()

    def test_either_auth_key_or_static_secret_satisfies_auth(self):
        assert settings(auth_key=None, static_secret_key="s").is_configured
        assert settings(auth_key="a", static_secret_key=None).is_configured
        assert not settings(auth_key=None, static_secret_key=None).is_configured

    def test_refuses_to_construct_when_unconfigured(self):
        with pytest.raises(PaymentError, match="EKO_DEVELOPER_KEY"):
            EkoPaymentGateway(EkoSettings(base_url="x"))

    def test_is_not_a_stub(self):
        """`is_stub` drives the TEST MODE banner on every receipt."""
        assert gateway().is_stub is False


class TestTransferRequest:
    def test_pin_goes_into_pintwin(self, payee):
        """eko's name for the PIN. This mapping is the reason the whole gate
        upstream exists."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["pintwin"] == "5353"

    def test_pin_is_scrubbed_from_the_request_dict_afterwards(self, payee):
        """Defence in depth: once the request is away, the PIN should not still
        be sitting in a dict some later traceback could render."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.live_bodies[-1]["pintwin"] == ""

    def test_pin_never_reaches_the_intent(self, payee):
        """The intent outlives the request and is logged by id elsewhere."""
        session = FakeSession()
        gw = gateway(session)
        intent = gw.prepare_transfer(PAYER, payee, 100_000)
        gw.confirm_and_execute(intent, "5353")

        assert "5353" not in repr(intent)

    def test_amount_is_sent_in_rupees(self, payee):
        """The codebase carries paise; eko takes rupees. One conversion, here."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["amount"] == "1000"

    def test_recipient_and_initiator_are_right_way_round(self, payee):
        """Reversing these sends the money backwards."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        body = session.last_body

        assert body["customer_id"] == "9002331157"      # who receives
        assert body["initiator_id"] == "9406921732"     # who sends
        assert body["recipient_name"] == "RAM"

    def test_intent_id_is_the_idempotency_key(self, payee):
        """So a retried request cannot pay twice."""
        session = FakeSession()
        gw = gateway(session)
        intent = gw.prepare_transfer(PAYER, payee, 100_000)
        gw.confirm_and_execute(intent, "5353")

        assert session.last_body["client_ref_id"] == intent.intent_id

    def test_merchant_identity_is_sent(self, payee):
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        body = session.last_body

        assert body["user_code"] == "10472151"
        assert body["service_code"] == "86"
        assert body["interaction_type_id"] == "840"
        assert body["latlong"] == "28.494226,77.082654,15"

    def test_hits_the_documented_endpoint(self, payee):
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.posts[-1]["url"] == "https://api.eko.in:25002/ekoicici/v1/request"

    def test_sends_ekos_three_auth_headers(self, payee):
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        headers = session.posts[-1]["headers"]

        assert headers["developer_key"] == "dev-key"
        assert headers["secret_key"]
        assert headers["secret_key_timestamp"].isdigit()

    def test_static_secret_is_used_verbatim_when_set(self, payee):
        session = FakeSession()
        gw = gateway(session, static_secret_key="fixed-secret")
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.posts[-1]["headers"]["secret_key"] == "fixed-secret"


class TestAmountConversion:
    @pytest.mark.parametrize("paise,rupees", [
        (100_000, "1000"),
        (200_000, "2000"),
        (100, "1"),
        (10_000_000, "100000"),
    ])
    def test_whole_rupees(self, paise, rupees):
        assert _rupees(paise) == rupees

    def test_fractional_rupees_are_refused_not_rounded(self):
        """Silently dropping paise off a transfer is only noticed at
        reconciliation."""
        with pytest.raises(PaymentError, match="whole rupees"):
            _rupees(150_050)

    def test_prepare_fails_early_on_a_fractional_amount(self, payee):
        """Better to fail before the user has spoken their PIN than after."""
        gw = gateway()
        with pytest.raises(PaymentError):
            gw.prepare_transfer(PAYER, payee, 150_050)

    def test_zero_is_refused(self, payee):
        with pytest.raises(PaymentError):
            gateway().prepare_transfer(PAYER, payee, 0)


class TestResponses:
    def test_success_returns_the_reference(self, payee):
        session = FakeSession(FakeResponse({"status": "0", "data": {"tid": "TXN99"}}))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert result.ok
        assert result.reference == "TXN99"

    def test_success_without_a_reference_is_refused(self, payee):
        """A success we cannot trace is a success we cannot support."""
        session = FakeSession(FakeResponse({"status": "0", "data": {}}))
        gw = gateway(session)
        with pytest.raises(PaymentError, match="cannot be traced"):
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

    def test_wrong_pin_says_so_and_nothing_more(self, payee):
        session = FakeSession(FakeResponse({"status": "1", "message": "Invalid pintwin"}))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert not result.ok
        assert result.message == "PIN galat hai."

    def test_401_is_a_pin_failure(self, payee):
        session = FakeSession(FakeResponse({}, status_code=401))
        gw = gateway(session)
        assert gw.confirm_and_execute(
            gw.prepare_transfer(PAYER, payee, 100_000), "5353"
        ).message == "PIN galat hai."

    def test_other_failures_pass_ekos_message_through(self, payee):
        session = FakeSession(FakeResponse({"status": "1", "message": "Limit exceeded"}))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert not result.ok
        assert "Limit exceeded" in result.message

    def test_network_failure_does_not_claim_the_transfer_failed(self, payee):
        """It may well have gone through. Claiming failure when money moved is
        the worse of the two lies."""
        import requests

        session = FakeSession(error=requests.ConnectionError("boom"))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert not result.ok
        assert "confirm nahi kar pa raha" in result.message

    def test_non_json_response_is_an_error_not_a_crash(self, payee):
        class Junk(FakeResponse):
            def json(self):
                raise ValueError("not json")

        session = FakeSession(Junk())
        gw = gateway(session)
        with pytest.raises(PaymentError, match="not JSON"):
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")


class TestPinFailureDetection:
    @pytest.mark.parametrize("status,message", [
        ("1", "Invalid pintwin"),
        ("1", "incorrect PIN entered"),
        ("PINTWIN_MISMATCH", ""),
        ("1", "wrong pin"),
    ])
    def test_recognised(self, status, message):
        assert _is_pin_failure(200, status, message)

    def test_ordinary_failure_is_not_a_pin_failure(self):
        assert not _is_pin_failure(200, "1", "Daily limit exceeded")
        assert not _is_pin_failure(500, "1", "Internal server error")


class TestResolvePayee:
    def test_without_a_verify_endpoint_the_payee_is_unnamed(self):
        """No name means the digits must be read aloud — the honest fallback."""
        resolved = gateway().resolve_payee("9002331157")

        assert resolved.resolved_name is None
        assert resolved.needs_digit_readback is True

    def test_country_code_is_stripped(self):
        """eko wants the 10-digit mobile; WhatsApp gives us 12 with the 91."""
        assert gateway().resolve_payee("919002331157").phone == "9002331157"

    def test_short_number_is_refused(self):
        with pytest.raises(PaymentError):
            gateway().resolve_payee("12345")

    def test_verify_endpoint_supplies_a_name(self):
        session = FakeSession(FakeResponse({
            "status": "0",
            "data": {"recipient_name": "RAM", "customer_vpa": "satyabhan-39@ptyes"},
        }))
        gw = gateway(session, verify_path="/ekoicici/v1/verify")
        resolved = gw.resolve_payee("9002331157")

        assert resolved.resolved_name == "RAM"
        assert resolved.needs_digit_readback is False
        assert resolved.masked_account == "•••@ptyes"    # never the full VPA

    def test_unverified_payee_is_treated_as_new(self):
        """Absent a definite answer, apply the tighter cap."""
        session = FakeSession(FakeResponse({"status": "0", "data": {"recipient_name": "RAM"}}))
        gw = gateway(session, verify_path="/verify")
        assert gw.resolve_payee("9002331157").is_new is True


class TestDailyLimit:
    def test_without_an_endpoint_it_reports_nothing_spent(self):
        """And warns, because the daily cap is then only counting this bot."""
        assert gateway().spent_today_paise(PAYER) == 0

    def test_reads_ekos_used_limit_when_available(self):
        session = FakeSession(FakeResponse({"status": "0", "data": {"used_limit": 500_000}}))
        gw = gateway(session, spent_today_path="/limits")
        assert gw.spent_today_paise(PAYER) == 500_000


class TestPreparingSendsNothing:
    def test_prepare_makes_no_request(self, payee):
        """No reason to tell eko about a transfer the user has not confirmed."""
        session = FakeSession()
        gateway(session).prepare_transfer(PAYER, payee, 100_000)
        assert session.posts == []
