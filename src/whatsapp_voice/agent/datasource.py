"""The boundary between this agent and eko's campaign systems.

**This is the file to change when real API access arrives.** Everything else —
the tools, the agent, the WhatsApp layer — talks to `CampaignDataSource` and does
not care what is behind it.

The protocol below is a guess at shape, not a description of eko's real API. It
was written to make the demo work end to end; when the real endpoints are
available, write an `EkoApiCampaignSource` implementing this protocol and swap it
in. If the real API disagrees with these shapes (it probably will), change the
protocol and the two small tools in `campaign_tools.py` — nothing above them
moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CampaignCount:
    on: date
    total: int
    #: Optional per-campaign split, when the source can provide it.
    by_campaign: dict[str, int] | None = None


@dataclass(frozen=True)
class DeliveryReport:
    campaign: str
    on: date
    sent: int
    delivered: int
    failed: int
    pending: int

    @property
    def delivery_rate(self) -> float:
        return (self.delivered / self.sent) if self.sent else 0.0


class DataSourceError(Exception):
    """The upstream system could not answer — unknown campaign, no data, API down."""


@runtime_checkable
class CampaignDataSource(Protocol):
    """What the agent needs from eko's systems. Read-only by design.

    There is deliberately no `send_campaign` or any other mutating method. The
    agent authenticates a caller by phone number, which is not strong enough to
    authorise a state change (see docs/phase-5-auth.md). Keeping the write side
    absent from the interface means it cannot be reached by a prompt.
    """

    def campaign_count(self, on: date) -> CampaignCount:
        """How many campaigns went out on a given date."""
        ...

    def delivery_report(self, campaign: str, on: date) -> DeliveryReport:
        """Delivery breakdown for one campaign on one date."""
        ...

    def list_campaigns(self, on: date) -> list[str]:
        """Campaign names active on a date — used to resolve ambiguous references."""
        ...


class StubCampaignSource:
    """Deterministic fake data, so the whole pipeline can be demonstrated before
    API access exists.

    Deterministic rather than random: a demo that shows different numbers each
    run is impossible to sanity-check, and a reviewer cannot tell a real bug from
    noise. Values derive from the date, so the same question always gives the
    same answer.

    Every reply from this source is prefixed as demo data — if a stub number ever
    reaches a real stakeholder, it should be obvious that it is not real.
    """

    #: Names shaped like real campaign names, so transcription of them is
    #: exercised realistically during the demo.
    CAMPAIGNS = (
        "Diwali Cashback",
        "AEPS Onboarding",
        "Merchant Referral",
        "DMT Winback",
    )

    def __init__(self, today: date | None = None) -> None:
        self._today = today or date.today()

    # ---------------------------------------------------------------- helpers

    def _resolve(self, name: str) -> str:
        """Case- and whitespace-insensitive lookup. Deliberately NOT fuzzy.

        A near-match on a misheard campaign name would produce a confidently
        wrong report, which is worse than asking. The agent's prompt tells it to
        confirm rather than guess; this enforces that at the data layer.
        """
        wanted = " ".join(name.lower().split())
        for campaign in self.CAMPAIGNS:
            if " ".join(campaign.lower().split()) == wanted:
                return campaign
        raise DataSourceError(
            f"No campaign named {name!r}. Active campaigns: {', '.join(self.CAMPAIGNS)}"
        )

    def _check_date(self, on: date) -> None:
        if on > self._today:
            raise DataSourceError(f"{on.isoformat()} is in the future — no data yet.")
        if on < self._today - timedelta(days=90):
            raise DataSourceError("Demo data only covers the last 90 days.")

    @staticmethod
    def _seed(on: date, salt: int = 0) -> int:
        return on.toordinal() + salt

    # ------------------------------------------------------------- interface

    def campaign_count(self, on: date) -> CampaignCount:
        self._check_date(on)
        active = self.list_campaigns(on)
        by_campaign = {
            name: 2 + (self._seed(on, i) % 5) for i, name in enumerate(active)
        }
        return CampaignCount(
            on=on, total=sum(by_campaign.values()), by_campaign=by_campaign
        )

    def delivery_report(self, campaign: str, on: date) -> DeliveryReport:
        self._check_date(on)
        resolved = self._resolve(campaign)
        seed = self._seed(on, len(resolved))

        sent = 1000 + (seed % 9000)
        failed = seed % 40
        pending = seed % 25
        return DeliveryReport(
            campaign=resolved,
            on=on,
            sent=sent,
            delivered=sent - failed - pending,
            failed=failed,
            pending=pending,
        )

    def list_campaigns(self, on: date) -> list[str]:
        self._check_date(on)
        # A rotating subset, so "which campaigns ran" is not the same every day.
        count = 2 + (self._seed(on) % 3)
        start = self._seed(on) % len(self.CAMPAIGNS)
        return [self.CAMPAIGNS[(start + i) % len(self.CAMPAIGNS)] for i in range(count)]
