"""Authorising in the payer's own app, and the callback that reports back.

The spoken PIN is the POC mechanism: a knowledge factor, neither dynamic nor
transaction-unique, so it does not satisfy the RBI directions effective 1 April
2026. This is the production path, where the payer's own app collects the
credential and we never see it.

The callback tests are the important ones, because that endpoint is public and it
asserts that money moved. Unsigned, anyone who finds the URL can mint receipts;
non-idempotent, a retry pays the user's receipt twice and shows the transfer twice
in reconciliation.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsapp_voice.agent.handoff import (
    HandoffKind,
    build_handoff,
)
from whatsapp_voice.agent.payments import (
    IntentStatus,
    PendingIntents,
    StubPaymentGateway,
)
from whatsapp_voice.audit import AuditTrail
from whatsapp_voice.config import PaymentSettings, Settings
from whatsapp_voice.store import MemoryStore
from whatsapp_voice.whatsapp.callback import (
    CALLBACK_SIGNATURE_HEADER,
    compute_signature,
    mount_payment_callback,
)

SECRET = "callback-secret"
PAYER = "919406921732"


@pytest.fixture
def gateway():
    return StubPaymentGateway(pin="1234")


@pytest.fixture
def pending():
    return PendingIntents()


@pytest.fixture
def intent(gateway, pending):
    made = gateway.prepare_transfer(PAYER, gateway.resolve_payee("Ramesh"), 200_000)
    pending.put(made)
    return made


class RecordingClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to, body):
        self.sent.append((to, body))
        return "wamid.X"


# ---------------------------------------------------------------- handoff

class TestSpokenPinIsTheDefault:
    def test_nothing_changes_until_a_mechanism_is_chosen(self, intent):
        instruction = build_handoff(intent)
        assert instruction.kind is HandoffKind.SPOKEN_PIN
        assert "PIN boliye" in instruction.display_text

    def test_a_spoken_pin_deployment_needs_no_callback(self):
        assert PaymentSettings(enabled=True).needs_callback is False

    def test_any_other_kind_does(self):
        assert PaymentSettings(enabled=True, handoff_kind="upi_link").needs_callback is True


class TestUpiLink:
    def test_it_carries_the_amount_payee_and_our_reference(self, intent):
        link = build_handoff(
            intent, HandoffKind.UPI_LINK, payee_vpa="ramesh@ptyes"
        ).payload
        query = parse_qs(urlparse(link).query)

        assert urlparse(link).scheme == "upi"
        assert query["pa"] == ["ramesh@ptyes"]
        assert query["am"] == ["2000.00"]
        assert query["cu"] == ["INR"]
        # `tr` is our intent_id, so the callback matches without trusting the rest.
        assert query["tr"] == [intent.intent_id]

    @pytest.mark.parametrize("paise,rendered", [
        (200_000, "2000.00"),
        (100, "1.00"),
        (50, "0.50"),
        (199_999, "1999.99"),
        (10_000_000, "100000.00"),
    ])
    def test_the_amount_is_exact(self, gateway, pending, paise, rendered):
        """`am` is what gets debited. Decimal, not float — a float that renders as
        1999.99 instead of 2000.00 is a different transaction."""
        made = gateway.prepare_transfer(PAYER, gateway.resolve_payee("Ramesh"), paise)
        link = build_handoff(made, HandoffKind.UPI_LINK, payee_vpa="r@p").payload
        assert parse_qs(urlparse(link).query)["am"] == [rendered]

    def test_it_refuses_without_a_vpa(self, intent):
        """There would be nothing to address the payment to."""
        with pytest.raises(ValueError, match="VPA"):
            build_handoff(intent, HandoffKind.UPI_LINK)

    def test_the_link_is_in_the_message_body_too(self, intent):
        """Groups never receive interactive messages, so a button-only link would
        be invisible there."""
        instruction = build_handoff(intent, HandoffKind.UPI_LINK, payee_vpa="r@p")
        assert instruction.payload in instruction.display_text

    def test_it_says_the_pin_does_not_come_to_us(self, intent):
        """Which is both true and the reassurance a payer needs."""
        text = build_handoff(intent, HandoffKind.UPI_LINK, payee_vpa="r@p").display_text
        assert "hamare paas nahi" in text


class TestWebLink:
    def test_it_needs_a_base_url(self, intent):
        with pytest.raises(ValueError, match="PAYMENT_HANDOFF_URL"):
            build_handoff(intent, HandoffKind.WEB_LINK)

    def test_it_addresses_the_intent(self, intent):
        instruction = build_handoff(
            intent, HandoffKind.WEB_LINK, web_base_url="https://pay.eko.in"
        )
        assert instruction.payload.endswith(f"/authorise/{intent.intent_id}")


# --------------------------------------------------------------- callback

def build_app(gateway, pending, client, audit=None, store=None):
    app = FastAPI()
    mount_payment_callback(
        app,
        Settings(payments=PaymentSettings(
            enabled=True, handoff_kind="upi_link", callback_secret=SECRET,
        )),
        gateway=gateway, pending=pending, client=client,
        audit=audit or AuditTrail(None),
        store=store or MemoryStore(),
    )
    return TestClient(app)


def post(client: TestClient, body: dict, secret: str = SECRET):
    raw = json.dumps(body).encode()
    return client.post(
        "/payments/callback",
        content=raw,
        headers={CALLBACK_SIGNATURE_HEADER: compute_signature(raw, secret)},
    )


class TestSignature:
    def test_a_valid_signature_is_accepted(self, gateway, pending, intent):
        client = build_app(gateway, pending, RecordingClient())
        assert post(client, {"intent_id": intent.intent_id, "status": "success"}).status_code == 200

    def test_a_wrong_signature_is_403(self, gateway, pending, intent):
        """The endpoint is public and it says money moved."""
        client = build_app(gateway, pending, RecordingClient())
        assert post(client, {"intent_id": intent.intent_id}, secret="wrong").status_code == 403

    def test_a_missing_signature_is_403(self, gateway, pending, intent):
        client = build_app(gateway, pending, RecordingClient())
        response = client.post("/payments/callback", json={"intent_id": intent.intent_id})
        assert response.status_code == 403

    def test_nothing_happens_on_a_rejected_callback(self, gateway, pending, intent):
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"intent_id": intent.intent_id, "status": "success"}, secret="wrong")

        assert chat.sent == []
        assert pending.find_by_id(intent.intent_id) is not None

    def test_mounting_without_a_secret_is_refused(self, gateway, pending):
        """An unsigned endpoint that says money moved can be called by anyone."""
        with pytest.raises(ValueError, match="PAYMENT_CALLBACK_SECRET"):
            mount_payment_callback(
                FastAPI(),
                Settings(payments=PaymentSettings(enabled=True, handoff_kind="upi_link")),
                gateway=gateway, pending=pending, client=RecordingClient(),
                audit=AuditTrail(None), store=MemoryStore(),
            )


class TestIdempotency:
    def test_two_callbacks_produce_one_receipt(self, gateway, pending, intent):
        """Rails retry. Twice would mean the user is told twice and reconciliation
        shows the payment twice."""
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        body = {"intent_id": intent.intent_id, "status": "success", "reference": "T1"}

        assert post(client, body).status_code == 200
        assert post(client, body).status_code == 200

        assert len(chat.sent) == 1

    def test_a_duplicate_still_returns_200(self, gateway, pending, intent):
        """A non-200 makes the rail retry harder at something already done."""
        client = build_app(gateway, pending, RecordingClient())
        body = {"intent_id": intent.intent_id, "status": "success"}
        post(client, body)
        assert post(client, body).status_code == 200

    def test_the_claim_is_shared_across_workers(self, gateway, pending, intent):
        shared = MemoryStore()
        chat = RecordingClient()
        worker_a = build_app(gateway, pending, chat, store=shared)
        worker_b = build_app(gateway, pending, chat, store=shared)
        body = {"intent_id": intent.intent_id, "status": "success"}

        post(worker_a, body)
        post(worker_b, body)

        assert len(chat.sent) == 1


class TestOutcome:
    def test_success_sends_a_receipt_with_the_reference(self, gateway, pending, intent):
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"intent_id": intent.intent_id, "status": "success", "reference": "TXN9"})

        to, body = chat.sent[-1]
        assert to == PAYER
        assert "2,000.00" in body
        assert "TXN9" in body
        assert intent.status is IntentStatus.COMPLETED

    def test_failure_says_so_and_clears_the_intent(self, gateway, pending, intent):
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"intent_id": intent.intent_id, "status": "failed", "message": "declined"})

        assert "nahi ho paya" in chat.sent[-1][1]
        assert intent.status is IntentStatus.FAILED
        assert pending.find_by_id(intent.intent_id) is None

    def test_an_unrecognised_status_is_treated_as_failure(self, gateway, pending, intent):
        """Telling a user their money arrived when we do not know is the worse
        mistake, and the one they act on."""
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"intent_id": intent.intent_id, "status": "who knows"})

        assert "nahi ho paya" in chat.sent[-1][1]

    def test_a_missing_status_is_treated_as_failure(self, gateway, pending, intent):
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"intent_id": intent.intent_id})
        assert "nahi ho paya" in chat.sent[-1][1]

    def test_the_upi_tr_field_is_accepted_as_the_id(self, gateway, pending, intent):
        """A UPI app returns our reference in `tr`, not `intent_id`."""
        chat = RecordingClient()
        client = build_app(gateway, pending, chat)
        post(client, {"tr": intent.intent_id, "status": "success"})
        assert chat.sent


class TestMalformedCallbacks:
    def test_no_intent_id_is_400(self, gateway, pending):
        client = build_app(gateway, pending, RecordingClient())
        assert post(client, {"status": "success"}).status_code == 400

    def test_a_non_json_body_is_400(self, gateway, pending):
        client = build_app(gateway, pending, RecordingClient())
        raw = b"not json"
        response = client.post(
            "/payments/callback", content=raw,
            headers={CALLBACK_SIGNATURE_HEADER: compute_signature(raw, SECRET)},
        )
        assert response.status_code == 400

    def test_an_unknown_intent_is_recorded_not_silently_dropped(
        self, gateway, pending, tmp_path
    ):
        """A callback for a transfer we have no record of is either a replay after
        expiry or something forged. Both are worth knowing about."""
        trail = AuditTrail(tmp_path / "audit.jsonl")
        chat = RecordingClient()
        client = build_app(gateway, pending, chat, audit=trail)

        assert post(client, {"intent_id": "nope", "status": "success"}).status_code == 200
        assert chat.sent == []
        assert "callback_unknown_intent" in trail._path.read_text()


class TestAuditTrail:
    def test_success_is_recorded_with_the_reference(self, gateway, pending, intent, tmp_path):
        trail = AuditTrail(tmp_path / "audit.jsonl")
        client = build_app(gateway, pending, RecordingClient(), audit=trail)
        post(client, {"intent_id": intent.intent_id, "status": "success", "reference": "TXN9"})

        written = trail._path.read_text()
        assert "transfer_executed" in written
        assert "TXN9" in written

    def test_failure_is_recorded_with_the_reason(self, gateway, pending, intent, tmp_path):
        trail = AuditTrail(tmp_path / "audit.jsonl")
        client = build_app(gateway, pending, RecordingClient(), audit=trail)
        post(client, {"intent_id": intent.intent_id, "status": "failed", "message": "declined"})

        written = trail._path.read_text()
        assert "transfer_failed" in written
        assert "declined" in written
