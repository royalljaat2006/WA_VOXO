"""Tests for the `/ops` activity page.

Like the demo API, gating gets the most attention — this route is the one
place real payer numbers and real payment attempts are ever returned over
HTTP, so refusing correctly matters more than the page rendering nicely.
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsapp_voice import logging_setup
from whatsapp_voice.audit import AuditTrail
from whatsapp_voice.config import OpsSettings, PaymentSettings, Settings
from whatsapp_voice.ops_page import create_ops_router


def build(ops=None, payments=None):
    settings = Settings(
        ops=ops or OpsSettings(enabled=True, token="s3cret"),
        payments=payments or PaymentSettings(),
    )
    app = FastAPI()
    app.include_router(create_ops_router(settings))
    return TestClient(app), settings


class TestGating:
    def test_disabled_returns_404_not_401(self):
        """404 rather than 401: a disabled ops page should not advertise it exists."""
        client, _ = build(ops=OpsSettings(enabled=False))
        assert client.get("/ops").status_code == 404
        assert client.get("/ops/activity").status_code == 404

    def test_no_token_configured_refuses_with_503(self):
        """Real data, no equivalent of demo's 'fine, it's fake anyway' fallback."""
        client, _ = build(ops=OpsSettings(enabled=True, token=None))
        response = client.get("/ops/activity")
        assert response.status_code == 503

    def test_missing_header_is_401(self):
        client, _ = build()
        assert client.get("/ops/activity").status_code == 401

    def test_wrong_token_is_401(self):
        client, _ = build()
        response = client.get("/ops/activity", headers={"X-Ops-Token": "guess"})
        assert response.status_code == 401

    def test_correct_token_is_accepted(self):
        client, _ = build()
        response = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"})
        assert response.status_code == 200

    def test_disabled_by_default(self):
        assert OpsSettings().enabled is False

    def test_page_requires_enabled(self):
        client, _ = build(ops=OpsSettings(enabled=True, token="s3cret"))
        assert client.get("/ops").status_code == 200

    def test_page_html_mentions_token_header(self):
        client, _ = build()
        assert "text/html" in client.get("/ops").headers["content-type"]


class TestActivityMerging:
    def test_app_log_events_are_included(self):
        logging_setup.configure()
        logging.getLogger("test.ops.merge").warning("something happened")

        client, _ = build()
        events = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"}).json()["events"]

        assert any(e["source"] == "app" and "something happened" in e["detail"] for e in events)

    def test_payment_audit_events_are_included(self, tmp_path):
        trail = AuditTrail(tmp_path / "payments.jsonl")
        trail.executed(
            type("Intent", (), {
                "intent_id": "abc123", "payer": "919876543210",
                "amount_paise": 200_000, "status": "completed",
                "payee": type("P", (), {
                    "phone": "919000000001", "resolved_name": "Ramesh",
                    "source": None, "is_new": False,
                })(),
            })(),
            gateway="StubPaymentGateway", reference="TXN1",
        )

        client, _ = build(payments=PaymentSettings(audit_path=str(tmp_path / "payments.jsonl")))
        events = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"}).json()["events"]

        payment_events = [e for e in events if e["source"] == "payment"]
        assert len(payment_events) == 1
        assert payment_events[0]["actor"] == "919876543210"
        assert "919000000001" in payment_events[0]["detail"]
        assert "TXN1" in payment_events[0]["detail"]

    def test_no_audit_path_means_no_payment_events_but_no_error(self):
        client, _ = build(payments=PaymentSettings(audit_path=""))
        response = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"})
        assert response.status_code == 200
        assert all(e["source"] != "payment" for e in response.json()["events"])

    def test_events_are_sorted_newest_first(self, tmp_path):
        path = tmp_path / "payments.jsonl"
        path.write_text(
            '{"ts": "2026-01-01T00:00:00.000Z", "event": "a", "payer": "1"}\n'
            '{"ts": "2026-06-01T00:00:00.000Z", "event": "b", "payer": "2"}\n',
            encoding="utf-8",
        )
        client, _ = build(payments=PaymentSettings(audit_path=str(path)))
        events = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"}).json()["events"]
        payment_events = [e for e in events if e["source"] == "payment"]
        assert payment_events[0]["kind"] == "b"
        assert payment_events[1]["kind"] == "a"

    def test_a_torn_final_line_does_not_crash_the_page(self, tmp_path):
        path = tmp_path / "payments.jsonl"
        path.write_text(
            '{"ts": "2026-01-01T00:00:00.000Z", "event": "a", "payer": "1"}\n'
            '{"ts": "2026-01-01T00:00:01.000Z", "event": "b", "pay',
            encoding="utf-8",
        )
        client, _ = build(payments=PaymentSettings(audit_path=str(path)))
        response = client.get("/ops/activity", headers={"X-Ops-Token": "s3cret"})
        assert response.status_code == 200
        assert len([e for e in response.json()["events"] if e["source"] == "payment"]) == 1
