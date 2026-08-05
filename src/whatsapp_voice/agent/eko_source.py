"""eko's reporting API, behind the `CampaignDataSource` boundary.

**This is the file that replaces the fake numbers.** Nothing above it changes: the
tools, the agent and the handlers talk to `CampaignDataSource` and do not care
what is behind it.

### Read-only, structurally

The protocol has no `send_campaign` and no `update` of any kind, so neither does
this. That is not politeness — the agent authenticates a caller by phone number,
which is not strong enough to authorise a state change, and an interface with no
write method cannot be talked into one by a prompt.

### Status: the shape is known, the specifics are not

eko's transfer API is a single dispatch endpoint — `POST /ekoicici/v1/request`,
three auth headers, and a body whose `service_code` and `interaction_type_id`
select the operation. This is written against that same envelope, because it is
the pattern eko uses and reusing it costs nothing if it turns out to be right.

What is **not** known is which codes select a report, and where the numbers sit
inside the response. So every one of those is configuration (`EKO_REPORT_*` in
`.env`) rather than a constant: getting a real curl for the reporting API should
be an `.env` edit, not a rewrite. If the shape turns out to differ more than that,
this is the one file to open.

Until it is configured, `build_data_source` keeps returning the stub, and
`/health` keeps reporting `stub_data: true`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

import requests

from ..config import EkoSettings
from .datasource import CampaignCount, DataSourceError, DeliveryReport
from .eko_gateway import _field, _json, eko_auth_headers

logger = logging.getLogger(__name__)


class EkoCampaignSource:
    """Reads campaign and delivery data from eko over HTTP.

    Shares `EkoSettings` with the payment gateway because it is the same API,
    the same credentials and the same auth headers — two settings blocks for one
    upstream would be two places to keep a base URL in sync.
    """

    def __init__(self, settings: EkoSettings, session: Any = None) -> None:
        missing = settings.missing_report_fields()
        if missing:
            raise DataSourceError(
                "AGENT_DATA_SOURCE=eko but the reporting API is not configured. "
                f"Missing: {', '.join(missing)}. See the eko section in .env."
            )
        self._settings = settings
        self._session = session or requests.Session()
        logger.info("Campaign data: eko reporting API at %s", settings.base_url)

    # ------------------------------------------------------------------ reads

    def campaign_count(self, on: date) -> CampaignCount:
        body = self._request(self._settings.report_count_code, on)

        total = _field(body, self._settings.field_campaign_total)
        if total is None:
            # Better to say "eko did not tell us" than to report zero campaigns
            # on a day that had some. A wrong number is worse than an error.
            raise DataSourceError(
                f"eko's response had no '{self._settings.field_campaign_total}'. "
                "Correct EKO_FIELD_CAMPAIGN_TOTAL to match the real response."
            )

        split = _field(body, self._settings.field_campaign_split)
        return CampaignCount(
            on=on,
            total=int(total),
            by_campaign={str(k): int(v) for k, v in split.items()}
            if isinstance(split, dict) else None,
        )

    def delivery_report(self, campaign: str, on: date) -> DeliveryReport:
        body = self._request(self._settings.report_delivery_code, on, campaign=campaign)

        sent = _field(body, self._settings.field_sent)
        if sent is None:
            raise DataSourceError(
                f"No delivery data for '{campaign}' on {on.isoformat()}. "
                "Check the campaign name — it is matched exactly, not fuzzily."
            )

        # Each figure is read on its own rather than derived from the others.
        # Deriving `pending = sent - delivered - failed` would silently absorb any
        # disagreement in eko's own numbers, and a reconciliation gap is exactly
        # the thing worth seeing.
        return DeliveryReport(
            campaign=str(_field(body, self._settings.field_campaign_name) or campaign),
            on=on,
            sent=int(sent),
            delivered=int(_field(body, self._settings.field_delivered) or 0),
            failed=int(_field(body, self._settings.field_failed) or 0),
            pending=int(_field(body, self._settings.field_pending) or 0),
        )

    def list_campaigns(self, on: date) -> list[str]:
        """Campaign names active on a date.

        Used to resolve an ambiguous reference by *offering* the names, never by
        picking the closest one — ASR mangles names, and a confidently wrong
        report is worse than an error.
        """
        body = self._request(self._settings.report_list_code, on)
        names = _field(body, self._settings.field_campaign_list)

        if names is None:
            return []
        if isinstance(names, dict):
            return sorted(str(name) for name in names)
        if isinstance(names, list):
            return [
                str(item.get(self._settings.field_list_item_name, item))
                if isinstance(item, dict) else str(item)
                for item in names
            ]
        return []

    # -------------------------------------------------------------- internals

    def _request(self, service_code: str, on: date, campaign: str | None = None) -> dict:
        """One call on eko's dispatch endpoint.

        `service_code` selects which report. The rest of the envelope is the same
        merchant identity the transfer API takes, which is why both share
        `EkoSettings`.
        """
        payload = {
            "initiator_id": self._settings.initiator_id,
            "user_code": self._settings.user_code,
            "service_code": service_code,
            "interaction_type_id": self._settings.report_interaction_type_id,
            "org_id": self._settings.org_id,
            "locale": self._settings.locale,
            self._settings.field_req_date: on.isoformat(),
            "client_ref_id": uuid.uuid4().hex[:20],
        }
        if campaign is not None:
            payload[self._settings.field_req_campaign] = campaign

        url = (
            f"{self._settings.base_url.rstrip('/')}/"
            f"{self._settings.report_path.lstrip('/')}"
        )

        try:
            response = self._session.post(
                url, json=payload, headers=self._headers(),
                timeout=self._settings.timeout_s,
            )
        except requests.RequestException as exc:
            raise DataSourceError(
                f"eko reporting API unreachable: {type(exc).__name__}"
            ) from exc

        if not response.ok:
            raise DataSourceError(
                f"eko reporting API returned HTTP {response.status_code}"
            )

        body = _json(response)
        status = str(_field(body, self._settings.field_status) or "")
        if status and status not in self._settings.success_statuses:
            message = _field(body, self._settings.field_message) or "no reason given"
            raise DataSourceError(f"eko refused the report request: {message}")
        return body

    def _headers(self) -> dict[str, str]:
        """The same three-header auth the transfer API uses — one implementation,
        so a change to eko's scheme is made once."""
        return eko_auth_headers(self._settings)
