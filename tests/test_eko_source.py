"""eko's reporting API, behind the read-only data-source boundary.

The specifics of eko's reporting contract are not known yet, so what these tests
pin is what does not depend on them: the envelope, the read-only shape, and — most
importantly — that a missing number becomes an error rather than a zero. Reporting
"0 campaigns" on a day that had twelve is the one failure mode worth engineering
against, because nobody questions a confident number.
"""

import json
from datetime import date

import pytest

from whatsapp_voice.agent.datasource import CampaignDataSource, DataSourceError
from whatsapp_voice.agent.eko_source import EkoCampaignSource
from whatsapp_voice.config import EkoSettings

ON = date(2026, 8, 4)


def settings(**overrides) -> EkoSettings:
    base = dict(
        base_url="https://api.eko.in",
        developer_key="dev-key",
        auth_key="auth-key",
        user_code="10472151",
        initiator_id="9406921732",
        report_count_code="90",
        report_delivery_code="91",
        report_list_code="92",
    )
    base.update(overrides)
    return EkoSettings(**base)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "body": json, "headers": headers})
        if self._error:
            raise self._error
        return self._response

    @property
    def last_body(self):
        return self.posts[-1]["body"]


def source(payload=None, error=None, **overrides):
    response = FakeResponse(payload) if payload is not None else None
    return EkoCampaignSource(settings(**overrides), session=FakeSession(response, error))


class TestConfiguration:
    def test_it_satisfies_the_protocol(self):
        """So it is substitutable for the stub without anything above noticing."""
        assert isinstance(source({"status": "0"}), CampaignDataSource)

    def test_the_report_codes_are_required(self):
        """Nobody can guess which code selects which report."""
        missing = settings(report_count_code="", report_delivery_code="").missing_report_fields()
        assert "EKO_REPORT_COUNT_CODE" in missing
        assert "EKO_REPORT_DELIVERY_CODE" in missing

    def test_latlong_is_not_required_for_a_report(self):
        """It is a transfer requirement — a report moves no money."""
        assert "EKO_LATLONG" not in settings(latlong="").missing_report_fields()

    def test_it_refuses_to_construct_unconfigured(self):
        with pytest.raises(DataSourceError, match="EKO_REPORT_COUNT_CODE"):
            EkoCampaignSource(settings(report_count_code="", report_delivery_code=""))

    def test_there_is_no_write_method(self):
        """The protocol has no mutating method, so neither does this. An interface
        with no write side cannot be talked into one by a prompt."""
        methods = {name for name in dir(EkoCampaignSource) if not name.startswith("_")}
        assert methods == {"campaign_count", "delivery_report", "list_campaigns"}
        for forbidden in ("send", "create", "update", "delete", "send_campaign"):
            assert not any(forbidden in name for name in methods)


class TestCampaignCount:
    def test_reads_the_total(self):
        s = source({"status": "0", "data": {"total_campaigns": 12}})
        assert s.campaign_count(ON).total == 12

    def test_reads_the_per_campaign_split_when_present(self):
        s = source({"status": "0", "data": {
            "total_campaigns": 3, "by_campaign": {"Diwali": 2, "Onam": 1},
        }})
        assert s.campaign_count(ON).by_campaign == {"Diwali": 2, "Onam": 1}

    def test_no_split_is_none_not_empty(self):
        """None means "not provided"; {} would mean "provided, and empty"."""
        count = source({"status": "0", "data": {"total_campaigns": 3}}).campaign_count(ON)
        assert count.by_campaign is None

    def test_a_missing_total_is_an_error_not_a_zero(self):
        """Reporting zero campaigns on a day that had some is the failure worth
        engineering against — nobody questions a confident number."""
        with pytest.raises(DataSourceError, match="EKO_FIELD_CAMPAIGN_TOTAL"):
            source({"status": "0", "data": {}}).campaign_count(ON)

    def test_the_date_is_sent(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        assert s._session.last_body["report_date"] == "2026-08-04"

    def test_the_count_code_selects_the_operation(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        assert s._session.last_body["service_code"] == "90"


class TestDeliveryReport:
    @pytest.fixture
    def payload(self):
        return {"status": "0", "data": {
            "campaign_name": "Diwali Cashback",
            "sent": 2841, "delivered": 2824, "failed": 12, "pending": 5,
        }}

    def test_reads_every_figure(self, payload):
        report = source(payload).delivery_report("Diwali Cashback", ON)
        assert (report.sent, report.delivered, report.failed, report.pending) == (2841, 2824, 12, 5)

    def test_delivery_rate_is_derived(self, payload):
        report = source(payload).delivery_report("Diwali Cashback", ON)
        assert round(report.delivery_rate, 4) == 0.994

    def test_figures_are_not_derived_from_each_other(self):
        """Deriving pending = sent - delivered - failed would silently absorb a
        disagreement in eko's own numbers, and a reconciliation gap is exactly the
        thing worth seeing."""
        s = source({"status": "0", "data": {
            "sent": 100, "delivered": 90, "failed": 5, "pending": 2,   # sums to 97
        }})
        report = s.delivery_report("X", ON)
        assert report.pending == 2, "pending was recomputed instead of read"

    def test_an_unknown_campaign_is_an_error(self):
        """Matched exactly, never fuzzily — ASR mangles names, and a confidently
        wrong report is worse than an error."""
        with pytest.raises(DataSourceError, match="matched exactly"):
            source({"status": "0", "data": {}}).delivery_report("Diwalii", ON)

    def test_the_campaign_name_is_sent(self, payload):
        s = source(payload)
        s.delivery_report("Diwali Cashback", ON)
        assert s._session.last_body["campaign_name"] == "Diwali Cashback"

    def test_a_missing_optional_figure_reads_as_zero(self):
        """Only `sent` is load-bearing — without it there is no report at all."""
        s = source({"status": "0", "data": {"sent": 10}})
        assert s.delivery_report("X", ON).delivered == 0


class TestListCampaigns:
    def test_a_list_of_strings(self):
        s = source({"status": "0", "data": {"campaigns": ["Diwali", "Onam"]}})
        assert s.list_campaigns(ON) == ["Diwali", "Onam"]

    def test_a_list_of_objects(self):
        s = source({"status": "0", "data": {"campaigns": [{"name": "Diwali", "id": 1}]}})
        assert s.list_campaigns(ON) == ["Diwali"]

    def test_a_dict_keyed_by_name(self):
        s = source({"status": "0", "data": {"campaigns": {"Onam": 1, "Diwali": 2}}})
        assert s.list_campaigns(ON) == ["Diwali", "Onam"]

    def test_nothing_is_an_empty_list_not_an_error(self):
        """A day with no campaigns is a real answer."""
        assert source({"status": "0", "data": {}}).list_campaigns(ON) == []


class TestEnvelope:
    def test_it_sends_ekos_three_auth_headers(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        headers = s._session.posts[-1]["headers"]

        assert headers["developer_key"] == "dev-key"
        assert headers["secret_key"]
        assert headers["secret_key_timestamp"].isdigit()

    def test_it_sends_the_merchant_identity(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        body = s._session.last_body

        assert body["user_code"] == "10472151"
        assert body["initiator_id"] == "9406921732"

    def test_each_request_gets_its_own_reference(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        s.campaign_count(ON)
        first, second = (post["body"]["client_ref_id"] for post in s._session.posts)
        assert first != second

    def test_it_defaults_to_the_transfer_endpoint(self):
        """eko dispatches on one path and selects with codes."""
        s = source({"status": "0", "data": {"total_campaigns": 1}})
        s.campaign_count(ON)
        assert s._session.posts[-1]["url"].endswith("/ekoicici/v1/request")

    def test_a_different_path_can_be_configured(self):
        s = source({"status": "0", "data": {"total_campaigns": 1}},
                   report_path="/reports/v1/query")
        s.campaign_count(ON)
        assert s._session.posts[-1]["url"].endswith("/reports/v1/query")


class TestFailures:
    def test_a_refusal_carries_ekos_message(self):
        s = source({"status": "1", "message": "Invalid service code"})
        with pytest.raises(DataSourceError, match="Invalid service code"):
            s.campaign_count(ON)

    def test_an_http_error_is_a_data_source_error(self):
        s = EkoCampaignSource(settings(), session=FakeSession(FakeResponse({}, status_code=500)))
        with pytest.raises(DataSourceError, match="HTTP 500"):
            s.campaign_count(ON)

    def test_a_network_failure_is_a_data_source_error(self):
        import requests

        s = source(error=requests.ConnectionError("boom"))
        with pytest.raises(DataSourceError, match="unreachable"):
            s.campaign_count(ON)

    def test_a_non_json_response_does_not_crash(self):
        class Junk(FakeResponse):
            def json(self):
                raise ValueError("not json")

        from whatsapp_voice.agent.payments import PaymentError

        s = EkoCampaignSource(settings(), session=FakeSession(Junk({})))
        # Raised by the shared `_json` helper, which both eko modules use.
        with pytest.raises((DataSourceError, PaymentError), match="not JSON"):
            s.campaign_count(ON)


class TestFactory:
    def test_stub_is_still_the_default(self):
        from whatsapp_voice.agent.datasource import StubCampaignSource
        from whatsapp_voice.agent.factory import build_data_source
        from whatsapp_voice.config import AgentSettings

        assert isinstance(build_data_source(AgentSettings()), StubCampaignSource)

    def test_eko_is_selectable(self):
        from whatsapp_voice.agent.factory import build_data_source
        from whatsapp_voice.config import AgentSettings

        built = build_data_source(AgentSettings(data_source="eko"), settings())
        assert isinstance(built, EkoCampaignSource)

    def test_an_unknown_source_names_the_valid_ones(self):
        from whatsapp_voice.agent.factory import build_data_source
        from whatsapp_voice.config import AgentSettings

        with pytest.raises(ValueError, match="'stub', 'eko'"):
            build_data_source(AgentSettings(data_source="postgres"))
