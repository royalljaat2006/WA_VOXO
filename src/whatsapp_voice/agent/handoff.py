"""Authorising a transfer in the payer's own app instead of by spoken PIN.

### Why this exists alongside the PIN gate

A spoken PIN is a **knowledge** factor. The RBI *Authentication Mechanisms for
Digital Payment Transactions Directions, 2025* — enforceable 1 April 2026 —
require two factors from Knowledge / Possession / Inherence, at least one of them
dynamic and unique to the transaction. Four digits spoken into a microphone are
neither dynamic nor transaction-unique.

So the spoken PIN is a POC mechanism. This module is the production one: the
transfer is authorised in an app the payer already trusts, and we never see the
credential at all. NPCI's rule points the same way — only the payer's own UPI app
may collect a UPI PIN, which means we *must not* be the one asking.

    spoken PIN   →  we hold a secret for a moment            POC only
    handoff      →  the payer's app holds it, we never do     production

### The shape

`build_handoff` turns an intent into something the user can act on — a UPI deep
link today, a bank's own link or an OTP flow later. The user acts; the rail tells
us what happened via `POST /payments/callback`.

Which mechanism is right is eko's and compliance's decision, not ours. What this
file guarantees is that the decision is a `kind` on one dataclass rather than a
rewrite.

### The two rules the callback lives by

**Signed.** The endpoint is public, and it says money moved. Unsigned, anyone who
finds the URL can mint receipts.

**Idempotent on `intent_id`.** Rails retry. Two callbacks for one transfer must
produce one receipt and one audit record, or reconciliation shows a payment twice
and the user is told twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from urllib.parse import quote, urlencode

from .payments import TransferIntent

logger = logging.getLogger(__name__)


class HandoffKind(StrEnum):
    """How the payer is asked to authorise.

    A closed set, so the handler can be exhaustive and a new mechanism has to be
    added deliberately rather than appearing as a string nobody handles.
    """

    #: `upi://pay?…` — opens the payer's UPI app, which collects the PIN itself.
    UPI_LINK = "upi_link"
    #: A hosted page, for a bank whose flow is not a UPI intent.
    WEB_LINK = "web_link"
    #: An OTP the rail sends and the user types back.
    OTP = "otp"
    #: The POC path: the user says four digits and we verify them.
    SPOKEN_PIN = "spoken_pin"


@dataclass(frozen=True)
class HandoffInstruction:
    """What to send the user, and what to expect back.

    `display_text` is separate from `payload` because a group cannot receive
    interactive messages — the link has to survive being plain text.
    """

    kind: HandoffKind
    #: The link or code itself.
    payload: str
    #: The message to send, with the payload already in it.
    display_text: str
    #: True when this can be sent as a tappable button (1:1 only, never a group).
    supports_button: bool = False


def build_handoff(
    intent: TransferIntent,
    kind: HandoffKind = HandoffKind.SPOKEN_PIN,
    payee_vpa: str | None = None,
    web_base_url: str | None = None,
) -> HandoffInstruction:
    """Turn an intent into something the payer can act on.

    Defaults to the spoken PIN, so nothing changes until a mechanism is chosen.
    """
    if kind is HandoffKind.UPI_LINK:
        return _upi_link(intent, payee_vpa)
    if kind is HandoffKind.WEB_LINK:
        return _web_link(intent, web_base_url)
    if kind is HandoffKind.OTP:
        return HandoffInstruction(
            kind=kind,
            payload="",
            display_text=(
                f"₹{_rupees(intent.amount_paise)} → {intent.payee.display}\n"
                "Aapke number pe OTP aaya hai — wahi bhej dijiye."
            ),
        )

    return HandoffInstruction(
        kind=HandoffKind.SPOKEN_PIN,
        payload="",
        display_text=(
            f"₹{_rupees(intent.amount_paise)} → {intent.payee.display}\n"
            "Confirm karne ke liye apna PINTWIN boliye."
        ),
    )


def _upi_link(intent: TransferIntent, payee_vpa: str | None) -> HandoffInstruction:
    """A `upi://pay` deep link.

    The payer's own app collects the PIN — which is NPCI's rule, and also means
    the credential never passes through us.

    `tr` carries our `intent_id`, so the callback can be matched to the transfer
    without trusting anything else in it.
    """
    if not payee_vpa:
        raise ValueError(
            "A UPI link needs the payee's VPA. Without it there is nothing to "
            "address the payment to — ask eko's resolve endpoint for one, or use "
            "a different handoff kind."
        )

    query = urlencode(
        {
            "pa": payee_vpa,
            "pn": intent.payee.resolved_name or "Payee",
            "am": _rupees(intent.amount_paise),
            "cu": "INR",
            "tr": intent.intent_id,
            "tn": f"eko transfer {intent.intent_id}",
        },
        quote_via=quote,
    )
    link = f"upi://pay?{query}"

    return HandoffInstruction(
        kind=HandoffKind.UPI_LINK,
        payload=link,
        display_text=(
            f"₹{_rupees(intent.amount_paise)} → {intent.payee.display}\n\n"
            f"Confirm karne ke liye ye link kholiye — PIN aapke apne UPI app mein "
            f"jayega, hamare paas nahi:\n{link}"
        ),
        # Only in a 1:1 chat. Groups never receive interactive messages, which is
        # why display_text carries the link in the body as well.
        supports_button=True,
    )


def _web_link(intent: TransferIntent, base_url: str | None) -> HandoffInstruction:
    if not base_url:
        raise ValueError("A web handoff needs PAYMENT_HANDOFF_URL to be set.")

    link = f"{base_url.rstrip('/')}/authorise/{intent.intent_id}"
    return HandoffInstruction(
        kind=HandoffKind.WEB_LINK,
        payload=link,
        display_text=(
            f"₹{_rupees(intent.amount_paise)} → {intent.payee.display}\n\n"
            f"Confirm karne ke liye:\n{link}"
        ),
        supports_button=True,
    )


def _rupees(paise: int) -> str:
    """Paise → `2000.00`, the two-decimal form a UPI `am` parameter requires.

    Decimal rather than float division: `am` is what gets debited, and a float
    that renders as 1999.99 is a different transaction from the one the user
    confirmed.
    """
    return f"{Decimal(paise) / Decimal(100):.2f}"
