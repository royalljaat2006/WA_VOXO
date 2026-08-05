from datetime import date, timedelta

import pytest

from whatsapp_voice.agent.campaign_tools import campaign_tools
from whatsapp_voice.agent.datasource import DataSourceError, StubCampaignSource
from whatsapp_voice.agent.tools import Tool, ToolError, ToolRegistry

TODAY = date(2026, 7, 28)


@pytest.fixture
def source():
    return StubCampaignSource(today=TODAY)


@pytest.fixture
def registry(source):
    return ToolRegistry(campaign_tools(source))


class FakeTool(Tool):
    name = "fake"
    description = "does nothing"
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, behaviour="ok"):
        self._behaviour = behaviour

    def run(self, **kwargs):
        if self._behaviour == "tool_error":
            raise ToolError("declined for a reason")
        if self._behaviour == "boom":
            raise RuntimeError("unexpected")
        return "ok"


class TestToolRegistry:
    def test_definitions_are_sorted_for_cache_stability(self, registry):
        names = [d["name"] for d in registry.definitions()]
        assert names == sorted(names)

    def test_definitions_have_the_wire_shape(self, registry):
        for definition in registry.definitions():
            assert set(definition) == {"name", "description", "input_schema"}
            assert definition["input_schema"]["additionalProperties"] is False

    def test_duplicate_registration_rejected(self):
        registry = ToolRegistry([FakeTool()])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(FakeTool())

    def test_unknown_tool_returns_error_not_raises(self, registry):
        output, is_error = registry.run("no_such_tool", {})
        assert is_error is True
        assert "No such tool" in output
        assert "get_campaign_count" in output  # tells the model what exists

    def test_tool_error_becomes_a_readable_result(self):
        output, is_error = ToolRegistry([FakeTool("tool_error")]).run("fake", {})
        assert (output, is_error) == ("declined for a reason", True)

    def test_unexpected_exception_is_contained(self):
        output, is_error = ToolRegistry([FakeTool("boom")]).run("fake", {})
        assert is_error is True
        assert "unexpectedly" in output

    def test_bad_arguments_are_reported_back(self, registry):
        output, is_error = registry.run("get_campaign_count", {"wrong_arg": 1})
        assert is_error is True
        assert "Invalid arguments" in output

    def test_membership_and_length(self, registry):
        assert len(registry) == 3
        assert "get_campaign_count" in registry


class TestCampaignCountTool:
    def test_returns_total_and_breakdown(self, registry):
        output, is_error = registry.run("get_campaign_count", {"on": "2026-07-28"})
        assert is_error is False
        assert "total_campaigns=" in output
        assert "by_campaign:" in output

    def test_rejects_non_iso_date(self, registry):
        output, is_error = registry.run("get_campaign_count", {"on": "yesterday"})
        assert is_error is True
        assert "YYYY-MM-DD" in output

    def test_rejects_future_date(self, registry):
        future = (TODAY + timedelta(days=1)).isoformat()
        output, is_error = registry.run("get_campaign_count", {"on": future})
        assert is_error is True
        assert "future" in output


class TestDeliveryReportTool:
    def test_returns_the_full_breakdown(self, registry):
        output, is_error = registry.run(
            "get_delivery_report", {"campaign": "Diwali Cashback", "on": "2026-07-28"}
        )
        assert is_error is False
        for field in ("sent=", "delivered=", "failed=", "pending=", "delivery_rate="):
            assert field in output

    def test_misheard_name_is_rejected_not_fuzzy_matched(self, registry):
        """The ASR trap: a near-miss must not silently resolve to the real campaign."""
        output, is_error = registry.run(
            "get_delivery_report", {"campaign": "Diwaali Cashbak", "on": "2026-07-28"}
        )
        assert is_error is True
        assert "No campaign named" in output
        assert "Diwali Cashback" in output  # offers the real options

    def test_case_and_whitespace_are_forgiven(self, registry):
        output, is_error = registry.run(
            "get_delivery_report", {"campaign": "  diwali   cashback ", "on": "2026-07-28"}
        )
        assert is_error is False
        assert "campaign=Diwali Cashback" in output


class TestStubCampaignSource:
    def test_is_deterministic(self):
        a = StubCampaignSource(today=TODAY).delivery_report("DMT Winback", TODAY)
        b = StubCampaignSource(today=TODAY).delivery_report("DMT Winback", TODAY)
        assert a == b

    def test_numbers_are_internally_consistent(self, source):
        report = source.delivery_report("AEPS Onboarding", TODAY)
        assert report.delivered + report.failed + report.pending == report.sent
        assert 0.0 <= report.delivery_rate <= 1.0

    def test_count_matches_its_own_breakdown(self, source):
        count = source.campaign_count(TODAY)
        assert count.total == sum(count.by_campaign.values())

    def test_different_dates_give_different_data(self, source):
        assert (
            source.delivery_report("DMT Winback", TODAY)
            != source.delivery_report("DMT Winback", TODAY - timedelta(days=1))
        )

    def test_rejects_dates_beyond_the_demo_window(self, source):
        with pytest.raises(DataSourceError, match="90 days"):
            source.campaign_count(TODAY - timedelta(days=91))

    def test_has_no_mutating_methods(self, source):
        """Read-only by design — a prompt must not be able to reach a write."""
        forbidden = [m for m in dir(source)
                     if any(v in m for v in ("send", "create", "update", "delete", "write"))
                     and not m.startswith("_")]
        assert forbidden == []
