"""Mission control — the status and review/audit screen.

Two properties matter. It is fed from the audit file rather than a parallel
tally, so what the screen shows cannot disagree with the evidence. And it masks
phone numbers, because a dashboard is watched by a room while an audit file is
read by an auditor — same data, different audience, different rules.
"""

import json

import pytest
from fastapi.testclient import TestClient

from whatsapp_voice.audit import AuditTrail
from whatsapp_voice.config import DemoSettings, PaymentSettings, Settings
from whatsapp_voice.demo.api import DemoDeps, create_demo_router


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "payments.jsonl"


@pytest.fixture
def client(audit_path):
    from fastapi import FastAPI

    settings = Settings(
        demo=DemoSettings(enabled=True),
        payments=PaymentSettings(enabled=True, audit_path=str(audit_path)),
    )
    app = FastAPI()
    app.include_router(create_demo_router(DemoDeps(settings=settings)))
    return TestClient(app)


def write(path, **fields):
    trail = AuditTrail(path)
    event = fields.pop("event")
    trail.record(event, **fields)


class TestEmptyState:
    def test_no_trail_yet_is_not_an_error(self, client):
        body = client.get("/demo/audit").json()
        assert body["audit_enabled"] is True
        assert body["total_events"] == 0
        assert body["recent"] == []

    def test_disabled_trail_says_so(self, tmp_path):
        from fastapi import FastAPI

        settings = Settings(
            demo=DemoSettings(enabled=True),
            payments=PaymentSettings(enabled=True, audit_path=""),
        )
        app = FastAPI()
        app.include_router(create_demo_router(DemoDeps(settings=settings)))
        body = TestClient(app).get("/demo/audit").json()

        assert body["audit_enabled"] is False


class TestTotals:
    def test_counts_completed_transfers_and_their_value(self, client, audit_path):
        write(audit_path, event="transfer_executed", amount_paise=200_000, reference="T1")
        write(audit_path, event="transfer_executed", amount_paise=40_000, reference="T2")

        body = client.get("/demo/audit").json()
        assert body["transfers_completed"] == 2
        assert body["total_paise"] == 240_000

    def test_prepared_intents_do_not_count_as_money_moved(self, client, audit_path):
        """An intent nobody confirmed moved nothing, and the screen must not
        imply otherwise."""
        write(audit_path, event="intent_prepared", amount_paise=200_000)

        body = client.get("/demo/audit").json()
        assert body["transfers_completed"] == 0
        assert body["total_paise"] == 0
        assert body["total_events"] == 1

    def test_failures_and_refusals_are_counted_together(self, client, audit_path):
        write(audit_path, event="transfer_failed", reason="pin_attempts_exhausted")
        write(audit_path, event="transfer_refused", reason="caller_not_authorised_to_pay")

        assert client.get("/demo/audit").json()["transfers_failed"] == 2


class TestMasking:
    def test_phone_numbers_are_masked(self, client, audit_path):
        """The file keeps them in full; the screen does not."""
        write(audit_path, event="transfer_executed", payer="919406921732",
              payee="9002331157", amount_paise=100)

        entry = client.get("/demo/audit").json()["recent"][0]
        assert entry["payer"] == "…1732"
        assert entry["payee"] == "…1157"
        assert "919406921732" not in json.dumps(entry)

    def test_names_and_references_are_not_masked(self, client, audit_path):
        """They are what makes a record recognisable at a glance."""
        write(audit_path, event="transfer_executed", payee="9002331157",
              payee_name="RAM", reference="TXN99", amount_paise=100)

        entry = client.get("/demo/audit").json()["recent"][0]
        assert entry["payee_name"] == "RAM"
        assert entry["reference"] == "TXN99"


class TestOrderingAndLimit:
    def test_newest_first(self, client, audit_path):
        write(audit_path, event="intent_prepared", reference="old")
        write(audit_path, event="transfer_executed", reference="new")

        recent = client.get("/demo/audit").json()["recent"]
        assert recent[0]["reference"] == "new"

    def test_limit_trims_the_list_but_not_the_totals(self, client, audit_path):
        for _ in range(5):
            write(audit_path, event="transfer_executed", amount_paise=100)

        body = client.get("/demo/audit?limit=2").json()
        assert len(body["recent"]) == 2
        assert body["total_events"] == 5
        assert body["transfers_completed"] == 5


class TestRobustness:
    def test_a_torn_final_line_is_skipped(self, client, audit_path):
        """Expected on an append-only file that was being written during a
        crash — which is exactly when someone opens this screen."""
        write(audit_path, event="transfer_executed", amount_paise=100)
        with audit_path.open("a") as handle:
            handle.write('{"event": "transfer_exec')

        body = client.get("/demo/audit").json()
        assert body["total_events"] == 1
        assert body["transfers_completed"] == 1

    def test_unknown_fields_do_not_break_the_response(self, client, audit_path):
        """A field added to the trail later must not take the screen down."""
        write(audit_path, event="transfer_executed", amount_paise=100, brand_new_field="x")
        assert client.get("/demo/audit").status_code == 200


class TestGating:
    def test_the_endpoint_is_off_when_the_demo_is(self, tmp_path):
        from fastapi import FastAPI

        settings = Settings(demo=DemoSettings(enabled=False))
        app = FastAPI()
        app.include_router(create_demo_router(DemoDeps(settings=settings)))
        assert TestClient(app).get("/demo/audit").status_code == 404

    def test_a_demo_token_is_enforced(self, tmp_path):
        from fastapi import FastAPI

        settings = Settings(demo=DemoSettings(enabled=True, token="s3cret"))
        app = FastAPI()
        app.include_router(create_demo_router(DemoDeps(settings=settings)))
        client = TestClient(app)

        assert client.get("/demo/audit").status_code == 401
        assert client.get(
            "/demo/audit", headers={"X-Demo-Token": "s3cret"}
        ).status_code == 200
