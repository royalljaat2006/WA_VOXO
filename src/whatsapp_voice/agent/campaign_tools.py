"""The campaign reporting tools.

Each tool is thin on purpose: parse the model's arguments, call the data source,
return a compact factual string. No prose and no formatting — the model writes
the sentence the user reads, in whatever language they spoke.

Dates arrive as ISO strings (`2026-07-28`) rather than words like "today", so the
model resolves relative dates before calling. The system prompt supplies today's
date for that; putting the parsing here would mean guessing the user's timezone
inside a tool.
"""

from __future__ import annotations

from datetime import date

from .datasource import CampaignDataSource, DataSourceError
from .tools import Tool, ToolError

#: Shared by every tool that takes a date. Kept as a module constant so the
#: wording — and therefore the serialised tool list — stays identical.
_DATE_PROPERTY = {
    "type": "string",
    "format": "date",
    "description": "The date in YYYY-MM-DD form. Resolve relative words like "
                   "'aaj', 'kal', or 'yesterday' before calling.",
}


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ToolError(
            f"{value!r} is not a valid date. Use YYYY-MM-DD."
        ) from exc


class CampaignCountTool(Tool):
    name = "get_campaign_count"
    description = (
        "How many campaigns were sent on a given date, with a per-campaign split. "
        "Call this for questions like 'aaj kitne campaign gaye?' or 'how many "
        "campaigns went out yesterday?'."
    )
    input_schema = {
        "type": "object",
        "properties": {"on": _DATE_PROPERTY},
        "required": ["on"],
        "additionalProperties": False,
    }

    def __init__(self, source: CampaignDataSource) -> None:
        self._source = source

    def run(self, on: str) -> str:
        try:
            count = self._source.campaign_count(_parse_date(on))
        except DataSourceError as exc:
            raise ToolError(str(exc)) from exc

        lines = [f"date={count.on.isoformat()}", f"total_campaigns={count.total}"]
        if count.by_campaign:
            breakdown = ", ".join(f"{k}={v}" for k, v in count.by_campaign.items())
            lines.append(f"by_campaign: {breakdown}")
        return "\n".join(lines)


class DeliveryReportTool(Tool):
    name = "get_delivery_report"
    description = (
        "Delivery breakdown for one named campaign on one date — sent, delivered, "
        "failed, pending, and the delivery rate. Call this for 'delivery report "
        "bhej de' or 'kitne deliver hue?'. Requires an exact campaign name; if the "
        "user did not name one, call list_campaigns first or ask them."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "campaign": {
                "type": "string",
                "description": "Exact campaign name. Do not guess at a near match.",
            },
            "on": _DATE_PROPERTY,
        },
        "required": ["campaign", "on"],
        "additionalProperties": False,
    }

    def __init__(self, source: CampaignDataSource) -> None:
        self._source = source

    def run(self, campaign: str, on: str) -> str:
        try:
            report = self._source.delivery_report(campaign, _parse_date(on))
        except DataSourceError as exc:
            raise ToolError(str(exc)) from exc

        return "\n".join([
            f"campaign={report.campaign}",
            f"date={report.on.isoformat()}",
            f"sent={report.sent}",
            f"delivered={report.delivered}",
            f"failed={report.failed}",
            f"pending={report.pending}",
            f"delivery_rate={report.delivery_rate:.1%}",
        ])


class ListCampaignsTool(Tool):
    name = "list_campaigns"
    description = (
        "The campaign names active on a date. Call this when the user asks about "
        "'the campaign' without naming one, or when a name they gave does not "
        "match — then ask them which one they meant."
    )
    input_schema = {
        "type": "object",
        "properties": {"on": _DATE_PROPERTY},
        "required": ["on"],
        "additionalProperties": False,
    }

    def __init__(self, source: CampaignDataSource) -> None:
        self._source = source

    def run(self, on: str) -> str:
        try:
            campaigns = self._source.list_campaigns(_parse_date(on))
        except DataSourceError as exc:
            raise ToolError(str(exc)) from exc

        if not campaigns:
            return f"date={on}\ncampaigns: none"
        return f"date={on}\ncampaigns: {', '.join(campaigns)}"


def campaign_tools(source: CampaignDataSource) -> list[Tool]:
    """Every campaign tool, bound to one data source."""
    return [
        CampaignCountTool(source),
        DeliveryReportTool(source),
        ListCampaignsTool(source),
    ]
