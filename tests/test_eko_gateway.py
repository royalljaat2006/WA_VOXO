"""eko's real payment API.

No network here — the session is a fake that records what would have been sent.
What matters is the request body, because every field in it is something eko acts
on, and one of them is the user's PIN.
"""

import copy
import json
import logging

import pytest

from whatsapp_voice.agent.eko_gateway import (
    EkoPaymentGateway,
    _client_ref_id,
    _is_pin_failure,
    _rupees,
)
from whatsapp_voice.agent.payments import Payee, PayeeSource, PaymentError, current_payer
from whatsapp_voice.config import EkoSettings

PAYER = "919406921732"


def settings(**overrides) -> EkoSettings:
    base = dict(
        base_url="https://api.eko.in",
        developer_key="dev-key",
        auth_key="auth-key",
        user_code="10472151",
        initiator_id="9406921732",
        # Fixed merchant-side value, not a payee — see EkoSettings.customer_id.
        customer_id="9000000001",
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


class NotJsonResponse:
    """What a gateway or a load balancer returns when eko never saw the request:
    an HTML page, with a `json()` that raises like `requests`' own does."""

    def __init__(self, content: bytes, status_code: int = 502):
        self.content = content
        self.status_code = status_code

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


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


def gateway(session=None, curl_get=None, **overrides):
    return EkoPaymentGateway(
        settings(**overrides), session=session or FakeSession(), curl_get=curl_get
    )


class TestConfiguration:
    def test_missing_credentials_name_themselves(self):
        """"Set EKO_BASE_URL" is fixable; "not configured" is a guessing game."""
        missing = EkoSettings(latlong="x").missing_fields()
        assert "EKO_BASE_URL" in missing

    def test_latlong_is_required(self):
        """eko rejects a transfer with no location, and WhatsApp gives us none."""
        assert "EKO_LATLONG" in settings(latlong="").missing_fields()

    def test_developer_key_and_auth_key_are_not_required(self):
        """eko's platform team confirmed these headers may be sent empty —
        they must be present, but do not have to hold a real value yet."""
        assert settings(developer_key=None, auth_key=None, static_secret_key=None).is_configured

    def test_user_code_is_not_required(self):
        """Per an explicit instruction from the project owner (2026-08-15):
        user_code varies with initiator_id and there is currently no source
        to derive the right value per payer, so an empty value is sent
        deliberately, rather than refusing to even try."""
        assert settings(user_code=None).is_configured

    def test_refuses_to_construct_when_unconfigured(self):
        with pytest.raises(PaymentError, match="EKO_BASE_URL"):
            EkoPaymentGateway(EkoSettings(latlong="x"))

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


class TestTheRequestAndResponseAreLogged:
    """Both directions, in full — so a failure can be diffed against a
    known-good curl instead of guessed at."""

    def test_the_request_body_is_logged(self, payee, caplog):
        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession())
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        [line] = [r.getMessage() for r in caplog.records if "transfer request" in r.getMessage()]
        assert '"customer_id": "9000000001"' in line
        assert '"amount": "1000"' in line

    def test_the_logged_request_carries_no_pin(self, payee, caplog):
        """The one thing that must never appear in a log line, in the one place
        that now prints the whole request body."""
        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession())
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "5353" not in caplog.text

    def test_the_response_body_is_logged(self, payee, caplog):
        response = FakeResponse({"status": "1123", "message": "some eko detail"})
        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession(response))
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "some eko detail" in caplog.text
        assert "1123" in caplog.text

    def test_a_successful_response_is_logged_too(self, payee, caplog):
        """"What exactly came back" is the question after a real transfer, not
        only after a failed one."""
        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession())
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert any("transfer response" in r.getMessage() for r in caplog.records)

    def test_an_unparseable_response_is_still_logged(self, payee, caplog):
        """The case most worth seeing verbatim: eko returning an HTML error page
        rather than JSON. Parsing raises, but the log line must come first — so
        the one response you most need to read is not the one that vanishes."""
        broken = NotJsonResponse(b"<html><body>502 Bad Gateway</body></html>", 502)

        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession(broken))
            with pytest.raises(PaymentError):
                gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "502 Bad Gateway" in caplog.text

    def test_a_huge_response_is_truncated(self, payee, caplog):
        """A runaway error page must not flood the log or the /ops ring buffer."""
        flood = NotJsonResponse(b"x" * 50_000, 500)

        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession(flood))
            with pytest.raises(PaymentError):
                gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "[truncated]" in caplog.text
        assert len(caplog.text) < 10_000

    def test_an_empty_response_says_so_rather_than_logging_nothing(self, payee, caplog):
        empty = FakeResponse()
        empty.content = b""

        with caplog.at_level(logging.INFO, logger="whatsapp_voice.agent.eko_gateway"):
            gw = gateway(FakeSession(empty))
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "<empty>" in caplog.text

    def test_amount_is_sent_in_rupees(self, payee):
        """The codebase carries paise; eko takes rupees. One conversion, here."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["amount"] == "1000"

    def test_the_merchant_identity_fields_are_all_from_config(self, payee):
        """`customer_id` reads like a payee field and is not one — it is issued
        with the merchant credentials, like `user_code`. `initiator_id` is
        deliberately NOT tested here — see
        `test_initiator_id_is_the_payer_not_a_config_value` below; it is no
        longer a merchant-identity field at all."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        body = session.last_body

        assert body["customer_id"] == "9000000001"      # fixed, from settings
        assert body["user_code"] == "10472151"
        assert body["recipient_name"] == "RAM"

    def test_initiator_id_is_the_payer_not_a_config_value(self, payee):
        """Per an explicit instruction from the project owner: `initiator_id`
        now identifies the WhatsApp payer actually sending this transfer, not
        a fixed value issued with the credentials — the opposite of
        `customer_id`/`user_code` above. Deliberately not using module-level
        `PAYER` here — its digits happen to equal `settings()`'s configured
        `initiator_id` ("9406921732") by coincidence, which would let this
        test pass even if the code silently fell back to that config value."""
        different_payer = "918800011122"
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(
            gw.prepare_transfer(different_payer, payee, 100_000), "5353"
        )

        assert session.last_body["initiator_id"] == "8800011122"
        assert session.last_body["initiator_id"] != "9406921732"  # not settings()'s value

    def test_intent_id_is_the_idempotency_key(self, payee):
        """So a retried request cannot pay twice.

        Reformatted into the 20-digit numeric shape eko's own working request
        used, but still a pure function of the intent id — that is what makes it
        idempotent."""
        session = FakeSession()
        gw = gateway(session)
        intent = gw.prepare_transfer(PAYER, payee, 100_000)
        gw.confirm_and_execute(intent, "5353")
        ref = session.last_body["client_ref_id"]

        assert ref == _client_ref_id(intent.intent_id)
        assert len(ref) == 20
        assert ref.isdigit()

    def test_a_different_intent_gets_a_different_ref(self, payee):
        session = FakeSession()
        gw = gateway(session)
        first = gw.prepare_transfer(PAYER, payee, 100_000)
        second = gw.prepare_transfer(PAYER, payee, 100_000)

        assert _client_ref_id(first.intent_id) != _client_ref_id(second.intent_id)


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

        assert session.posts[-1]["url"] == "https://api.eko.in/ekoicici/v1/request"

    def test_configured_recipient_name_is_used_when_the_payee_has_none(self):
        """eko never returns a payee name, so without this fallback every real
        transfer would send an empty `recipient_name` — a field eko's own
        working request carries a value in."""
        session = FakeSession()
        gw = EkoPaymentGateway(settings(recipient_name="TEST PAYEE"), session)
        unnamed = Payee(phone="9002331157", source=PayeeSource.DICTATED)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, unnamed, 100_000), "5353")

        assert session.last_body["recipient_name"] == "TEST PAYEE"

    def test_a_resolved_name_wins_over_the_configured_one(self, payee):
        session = FakeSession()
        gw = EkoPaymentGateway(settings(recipient_name="TEST PAYEE"), session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["recipient_name"] == "RAM"

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

    def test_headers_present_but_empty_without_credentials(self, payee):
        """eko's platform team: these three headers must always be present,
        even before real credentials are issued — an empty secret_key, not a
        meaningless HMAC of an empty key."""
        session = FakeSession()
        gw = gateway(session, developer_key=None, auth_key=None, static_secret_key=None)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        headers = session.posts[-1]["headers"]

        assert headers["developer_key"] == ""
        assert headers["secret_key"] == ""
        assert headers["secret_key_timestamp"].isdigit()


class TestRecipientIdAndVpa:
    """`customer_vpa` identifies the receiving party; `customer_id` does not.

    `customer_id` is a fixed merchant-side value. It was read as the payee's
    mobile because one confirmed curl carried a `<mobile>@upi` VPA, so the two
    held the same digits — which made this flow demand a phone number for
    every payee and refuse ordinary UPI IDs like `name@okicici`.
    """

    def test_recipient_id_type_is_always_mobile_number(self, payee):
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["recipient_id_type"] == "mobile_number"

    def test_no_vpa_means_no_customer_vpa_field(self, payee):
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert "customer_vpa" not in session.last_body

    def test_a_stated_vpa_is_sent_unmasked(self):
        """The display form is for humans; eko cannot route on •••@handle."""
        payee = Payee(
            phone="", vpa="firstname.lastname@okicici", source=PayeeSource.DICTATED,
        )
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
        body = session.last_body

        assert body["customer_vpa"] == "firstname.lastname@okicici"
        assert "•" not in body["customer_vpa"]
        # Unchanged by the payee, because it never came from the payee.
        assert body["customer_id"] == "9000000001"


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


#: A real eko response, captured verbatim from production on 2026-08-13. Kept
#: byte-for-byte (only the tid shortened) because the bug this pins was caused
#: entirely by a *type*: every other test in this file sends `"status": "0"` as
#: a string, and eko sends the number `0`. Hand-written fixtures agreed with
#: each other and disagreed with reality, so a passing suite meant nothing.
REAL_EKO_ACCEPTED = {
    "response_status_id": 2,
    "data": {
        "client_ref_id": "90000000000000000001", "tx_status": "2",
        "amount": "10.0", "tds": "", "balance": "1000.00", "fee": "0.0",
        "customer_vpa": "9000000000@upi", "recipient_mobile": "",
        "commission": "", "recipient_name": "TEST PAYEE", "tid": "TXN00001",
        "timestamp": "Thu Jan 01 00:00:00 IST 2026",
    },
    "response_type_id": 1982,
    "message": "Transaction Initiated",
    "status": 0,
}

#: The matching real failure, same source. `64` is truthy, which is why the
#: failure path worked while the success path did not.
REAL_EKO_WRONG_PIN = {
    "response_status_id": 1,
    "invalid_params": {"pintwin": "Use correct PinTwin"},
    "data": {"last_used_okekey": "125"},
    "response_type_id": -1,
    "message": "Incorrect PIN",
    "status": 64,
}


class TestRealEkoResponses:
    """The captured production bodies, exactly as eko sent them.

    A `status` of `0` is eko's success code, and `0` is falsy in Python — so
    `str(field or "")` turned every success into `""`, which is not in
    `success_statuses`. Real transfers were reported to the sender as a wrong
    PIN while the money moved.
    """

    def test_numeric_zero_status_is_not_read_as_a_pin_failure(self, payee):
        """The bug's user-visible symptom: correct PIN, told 'PIN galat hai'."""
        session = FakeSession(FakeResponse(REAL_EKO_ACCEPTED))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert not result.pin_rejected
        assert result.message != "PIN galat hai."
        assert result.reference == "TXN00001"

    def test_the_captured_response_is_pending_not_settled(self, payee):
        """`status: 0` means eko accepted the request; `tx_status: "2"` means
        the money had not moved yet. This exact response was receipted as done
        and reversed hours later."""
        session = FakeSession(FakeResponse(REAL_EKO_ACCEPTED))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert result.pending
        assert not result.ok, "a pending transfer must never produce a receipt"

    def test_settled_tx_status_is_a_real_success(self, payee):
        session = FakeSession(FakeResponse(
            {**REAL_EKO_ACCEPTED, "data": {**REAL_EKO_ACCEPTED["data"], "tx_status": "0"}}
        ))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert result.ok
        assert not result.pending
        assert result.reference == "TXN00001"

    def test_failed_tx_status_is_a_failure_not_a_pending(self, payee):
        session = FakeSession(FakeResponse(
            {**REAL_EKO_ACCEPTED, "data": {**REAL_EKO_ACCEPTED["data"], "tx_status": "1"}}
        ))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert not result.ok
        assert not result.pending
        assert not result.pin_rejected

    def test_a_missing_tx_status_is_pending_not_success(self, payee):
        """Absent is 'unknown', and unknown money is not a receipt. eko's own
        rule for anything that is not 0 or 1 is to inquire, not to assume."""
        session = FakeSession(FakeResponse({"status": 0, "data": {"tid": "TXN99"}}))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert result.pending
        assert not result.ok

    def test_real_wrong_pin_still_reads_as_a_pin_rejection(self, payee):
        session = FakeSession(FakeResponse(REAL_EKO_WRONG_PIN))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert not result.ok
        assert result.pin_rejected
        assert result.message == "PIN galat hai."

    def test_a_numeric_status_the_list_does_not_know_is_still_a_failure(self, payee):
        """Guards the fix from overcorrecting: not every number is success."""
        session = FakeSession(FakeResponse({"status": 1123, "message": "nope"}))
        gw = gateway(session)
        result = gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 1000), "5353")

        assert not result.ok
        assert not result.pin_rejected


class TestResponses:
    def test_success_returns_the_reference(self, payee):
        session = FakeSession(FakeResponse(
            {"status": "0", "data": {"tid": "TXN99", "tx_status": "0"}}
        ))
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
        current_payer.set(PAYER)
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
        current_payer.set(PAYER)
        session = FakeSession(FakeResponse({"status": "0", "data": {"recipient_name": "RAM"}}))
        gw = gateway(session, verify_path="/verify")
        assert gw.resolve_payee("9002331157").is_new is True

    def test_verify_request_uses_the_current_payer_as_initiator_id(self):
        """Same per-payer rule as the transfer request — see
        test_initiator_id_is_the_payer_not_a_config_value. A payer whose
        digits do not match settings()'s configured initiator_id, so this
        cannot pass by falling back to that config value."""
        current_payer.set("918800011122")
        session = FakeSession(FakeResponse({"status": "0", "data": {"recipient_name": "RAM"}}))
        gw = gateway(session, verify_path="/verify")
        gw.resolve_payee("9002331157")

        assert session.last_body["initiator_id"] == "8800011122"


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


class FakeCurlGet:
    """Stands in for `_curl_get` — records calls instead of shelling out to
    curl. `inquire` calls this directly (not through `self._session`); see
    its docstring for why the real inquiry call cannot go through `requests`."""

    def __init__(self, response=None, error=None):
        self._response = response or FakeResponse()
        self._error = error
        self.calls = []

    def __call__(self, url, params, headers, timeout_s):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if self._error:
            raise self._error
        return self._response


class TestInquiry:
    """eko has no webhook, so this is the only way to learn a transfer's fate."""

    def test_it_gets_the_documented_url_with_the_initiator(self):
        """The live endpoint — same host as the transfer request
        (`api.eko.in`), different path (`ekoicici/v3`) — confirmed directly
        by eko with a real curl. Getting here took three earlier
        guesses/hosts: `eps.eko.in` turned out to be eko's documentation
        website, not a gateway; `api.eko.in` alone (no path) never got past
        its auth-before-routing 402; `staging.eko.in/ekoapi/v3` confirmed the
        request shape but turned out to be sandbox-only. See
        EkoSettings.inquiry_base_url."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gateway(curl_get=curl).inquire("TXN00001", PAYER)

        [call] = curl.calls
        # `tid` is a query parameter, not a path segment — appending it to
        # the path (matching eko's example curl literally) 404ed against the
        # live gateway for every tid tried. See EkoSettings.inquiry_path.
        assert call["url"] == "https://api.eko.in/ekoicici/v3/tools/reference/transaction"
        assert call["params"]["tid"] == "TXN00001"
        # The payer's own number, 10 digits, no country code — not a fixed
        # settings value. See EkoSettings.inquiry_initiator_id (now unused)
        # and eko_gateway.py::_initiator_id_for.
        assert call["params"]["initiator_id"] == "9406921732"

    def test_a_fresh_client_ref_id_goes_with_every_inquiry(self):
        """eko's example shows one on every call. It identifies this question,
        not the transaction — a fresh value each time, never the transfer's own
        client_ref_id (that identification is already `tid`)."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gw = gateway(curl_get=curl)
        gw.inquire("TXN00001", PAYER)
        gw.inquire("TXN00001", PAYER)

        first, second = (c["params"]["client_ref_id"] for c in curl.calls)
        assert first and second and first != second
        assert len(first) <= 20

    def test_a_real_not_found_response_is_pending_not_failed(self):
        """eko reuses tx_status "1" ("Fail") for "no such tid" as well as a
        genuinely failed transfer — distinguished only by status 69 /
        "failed!inquired.tx.not.found". Confirmed against eko's live gateway:
        querying a real production tid that this staging host doesn't have
        returns exactly this shape. Telling a payer their money is lost
        because eko can't find the tid yet would be a real mistake with real
        money, so this must come back pending, never failed."""
        curl = FakeCurlGet(FakeResponse({
            "response_status_id": 1,
            "invalid_params": {
                "tid": "Please provide a valid TID to know the status of the transaction."
            },
            "data": {"tx_status": "1", "txstatus_desc": "Failed"},
            "response_type_id": -1,
            "message": "failed!inquired.tx.not.found",
            "status": 69,
        }))
        result = gateway(curl_get=curl).inquire("3573074249", PAYER)

        assert result.pending
        assert not result.ok

    def test_it_does_not_use_the_docs_website_host(self):
        """`eps.eko.in` returns an HTML docs page for every path — not an API."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert "eps.eko.in" not in curl.calls[0]["url"]

    def test_settled_tx_status_is_ok(self):
        curl = FakeCurlGet(FakeResponse(
            {"data": {"tx_status": "0", "txstatus_desc": "Success"}}
        ))
        result = gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert result.ok
        assert not result.pending
        assert result.message == "Success"

    def test_failed_tx_status_is_not_pending(self):
        curl = FakeCurlGet(FakeResponse(
            {"data": {"tx_status": "1", "txstatus_desc": "Failed"}}
        ))
        result = gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert not result.ok
        assert not result.pending

    def test_refunded_counts_as_failed(self):
        """eko's tx_status 4. The money went out and came back, so from the
        payer's side this transfer did not happen."""
        curl = FakeCurlGet(FakeResponse(
            {"data": {"tx_status": "4", "txstatus_desc": "Refunded"}}
        ))
        result = gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert not result.ok
        assert not result.pending

    @pytest.mark.parametrize("tx_status", ["2", "3", "5"])
    def test_the_documented_in_between_states_stay_pending(self, tx_status):
        """2 = initiated, 3 = refund pending, 5 = hold. eko's rule: only 0 and 1
        (and 4, by extension) are verdicts — everything else means ask again."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": tx_status}}))
        result = gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert result.pending
        assert not result.ok

    def test_an_http_error_raises_rather_than_inventing_a_verdict(self):
        curl = FakeCurlGet(FakeResponse({}, status_code=500))
        with pytest.raises(PaymentError):
            gateway(curl_get=curl).inquire("TXN00001", PAYER)

    def test_an_unreachable_eko_raises(self):
        curl = FakeCurlGet(error=OSError("no route"))
        with pytest.raises(PaymentError, match="unreachable"):
            gateway(curl_get=curl).inquire("TXN00001", PAYER)

    def test_both_header_spellings_are_sent(self):
        """eko documents the inquiry with hyphens and the transfer with
        underscores. An unread extra header is harmless; a missing one is a 401."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gateway(curl_get=curl).inquire("TXN00001", PAYER)

        headers = curl.calls[0]["headers"]
        assert "secret_key" in headers
        assert "secret-key" in headers

    def test_no_inquiry_path_refuses_rather_than_guessing_a_url(self):
        with pytest.raises(PaymentError, match="EKO_INQUIRY_PATH"):
            gateway(curl_get=FakeCurlGet(), inquiry_path="").inquire("TXN00001", PAYER)


class TestCustomerIdIsMerchantIdentity:
    """`customer_id` is issued with the credentials, like `user_code`.

    Its name is the trap. Reading it as the payee's mobile is what made this
    flow demand a 10-digit number for every transfer and refuse ordinary UPI
    IDs — `name@okicici` carries no phone number at all.
    """

    def test_it_comes_from_settings_not_the_payee(self):
        session = FakeSession()
        gw = gateway(session)
        payee = Payee(phone="9002331157", vpa="9002331157@upi")
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["customer_id"] == "9000000001"

    def test_it_is_identical_for_two_different_payees(self):
        """The one assertion that would have caught the original misreading."""
        bodies = []
        for payee in (
            Payee(phone="9002331157", vpa="9002331157@upi"),
            Payee(phone="", vpa="firstname.lastname@okicici"),
        ):
            session = FakeSession()
            gw = gateway(session)
            gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")
            bodies.append(session.last_body)

        assert bodies[0]["customer_id"] == bodies[1]["customer_id"]
        assert bodies[0]["customer_vpa"] != bodies[1]["customer_vpa"]

    def test_a_payee_with_no_phone_still_sends_customer_id(self):
        """Nothing about the payee can remove a merchant-identity field."""
        session = FakeSession()
        gw = gateway(session)
        payee = Payee(phone="", vpa="firstname.lastname@okicici")
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        body = session.last_body
        assert body["customer_id"] == "9000000001"
        assert body["recipient_id_type"] == "mobile_number"

    def test_a_missing_customer_id_is_sent_as_an_empty_string(self):
        """Not required to construct, per an explicit instruction from the
        project owner (2026-08-15): like `user_code`, there is currently no
        source to derive the right per-payer value, so an empty string is
        sent deliberately rather than refusing to start — see
        `EkoSettings.missing_fields`."""
        session = FakeSession()
        gw = gateway(session, customer_id=None)
        payee = Payee(phone="9002331157", vpa="9002331157@upi")
        gw.confirm_and_execute(gw.prepare_transfer(PAYER, payee, 100_000), "5353")

        assert session.last_body["customer_id"] == ""


class TestInquiryHeadersConfirmedAgainstARealCurl:
    """Every field checked against a curl the sender provided, line by line."""

    def test_no_content_type_on_the_inquiry_request(self):
        """Confirmed: inquiry is a GET with no body, so Content-Type does not
        apply — only the transfer POST needs it."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gateway(curl_get=curl).inquire("TXN00001", PAYER)

        headers = curl.calls[0]["headers"]
        assert "Content-Type" not in headers

    def test_the_transfer_request_still_sends_content_type(self):
        """The removal is inquiry-only — the POST still needs it."""
        session = FakeSession()
        gw = gateway(session)
        gw.confirm_and_execute(
            gw.prepare_transfer(PAYER, Payee(phone="9002331157"), 100_000), "5353"
        )

        assert session.posts[0]["headers"]["Content-Type"] == "application/json"

    def test_auth_headers_are_present_but_empty_when_unconfigured(self):
        """Confirmed: sent as empty-valued headers, not omitted."""
        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gw = gateway(
            curl_get=curl, developer_key=None, auth_key=None, static_secret_key=None
        )
        gw.inquire("TXN00001", PAYER)

        headers = curl.calls[0]["headers"]
        assert headers["developer_key"] == ""
        assert headers["secret-key"] == ""
        assert headers["secret-key-timestamp"] != ""  # timestamp is always real

    def test_transfer_and_inquiry_initiator_id_are_the_same_payer(self):
        """Per an explicit instruction from the project owner: `initiator_id`
        is no longer a fixed merchant value on either call — it is now
        whichever WhatsApp number is actually initiating, everywhere. The
        same payer's transfer and the settlement poller's later inquiry about
        it must therefore agree, not differ — a fixed inquiry initiator_id
        (eko's own sandbox sample used `9911572989`) is what this used to be
        before that instruction, not what it is now."""
        transfer_session = FakeSession()
        gw = gateway(transfer_session)
        gw.confirm_and_execute(
            gw.prepare_transfer(PAYER, Payee(phone="9002331157"), 100_000), "5353"
        )

        curl = FakeCurlGet(FakeResponse({"data": {"tx_status": "0"}}))
        gateway(curl_get=curl).inquire("TXN00001", PAYER)

        assert transfer_session.last_body["initiator_id"] == (
            curl.calls[0]["params"]["initiator_id"]
        )
        # The payer's own number, 10 digits, no country code — not PAYER
        # verbatim ("919406921732"), and not either of the old fixed values.
        assert curl.calls[0]["params"]["initiator_id"] == "9406921732"
