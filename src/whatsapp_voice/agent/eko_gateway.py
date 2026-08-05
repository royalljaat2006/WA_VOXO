"""eko's real payment API, behind the `PaymentGateway` boundary.

**This is the file that turns the POC into something that moves money.** Nothing
above it changes: the PIN gate, the caps, the amount parser and the tools all talk
to `PaymentGateway` and do not care what is behind it.

Written against the eko ICICI fund-transfer contract:

    POST {base}/ekoicici/v1/request
    headers: developer_key, secret_key, secret_key_timestamp
    body:    { amount, customer_id, initiator_id, pintwin, client_ref_id, … }

### pintwin is the PIN

eko's `pintwin` field is exactly what the spoken PIN feeds. It is the reason the
whole flow above this file exists: the user says four digits, `PaymentFlow`
intercepts them before the model or the log can see them, and they arrive here —
the only place they are allowed to go.

So `pintwin` is a secret everywhere in this module. It is placed in the request
body and nowhere else: never logged, never stored on the intent, never included
in an exception message, and deleted from the local frame as soon as the request
is built.

### Amounts

eko takes **rupees**, this codebase carries **paise**. The conversion happens once,
here, in `_rupees`. It refuses anything that is not a whole number of rupees
rather than rounding — silently dropping paise off a transfer is the kind of bug
that is only noticed at reconciliation.

### What still needs confirming with eko

Three things are implemented from the documented scheme but have not been run
against a sandbox. Each is called out at its call site:

1. The `secret_key` HMAC construction.
2. Whether `amount` is rupees (assumed) or paise.
3. Whether `client_ref_id` is honoured as an idempotency key. If it is not, a
   network timeout on a transfer is a double-payment risk.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from typing import Any

import requests

from ..config import EkoSettings
from .payments import (
    Payee,
    PayeeSource,
    PaymentError,
    PaymentGateway,
    TransferIntent,
    TransferResult,
    prepare_intent,
)

logger = logging.getLogger(__name__)


class EkoPaymentGateway(PaymentGateway):
    is_stub = False

    def __init__(self, settings: EkoSettings, session: Any = None) -> None:
        missing = settings.missing_fields()
        if missing:
            raise PaymentError(
                "PAYMENT_GATEWAY=eko but the eko API is not fully configured. "
                f"Missing: {', '.join(missing)}. See the eko section in .env."
            )
        self._settings = settings
        self._session = session or requests.Session()
        logger.warning(
            "eko payment gateway active (%s, user_code=%s) — THIS MOVES REAL MONEY.",
            settings.base_url, settings.user_code,
        )

    # ---------------------------------------------------------------- reads

    def resolve_payee(self, phone: str) -> Payee:
        """Recipient mobile → payee.

        eko's transfer request takes `recipient_id_type: mobile_number`, so a
        mobile is all that is strictly needed. A *name* is what makes a wrong
        number catchable by a human, though, so when a verification endpoint is
        configured we ask for one.

        Without `EKO_VERIFY_PATH` the payee comes back unnamed, which makes
        `needs_digit_readback` true and forces the digits to be read aloud. That
        is the honest fallback: no name means no name, not a guess.
        """
        digits = "".join(c for c in (phone or "") if c.isdigit())
        if len(digits) < 10:
            raise PaymentError(f"'{phone}' is not a usable mobile number.")
        digits = digits[-10:]  # eko wants the 10-digit mobile, no country code

        if not self._settings.verify_path:
            return Payee(phone=digits, source=PayeeSource.DICTATED)

        body = self._post(self._settings.verify_path, {
            "initiator_id": self._settings.initiator_id,
            "user_code": self._settings.user_code,
            "recipient_id_type": "mobile_number",
            "customer_id": digits,
        })
        name = _field(body, self._settings.field_payee_name)

        return Payee(
            phone=digits,
            resolved_name=str(name) if name else None,
            masked_account=_mask(_field(body, self._settings.field_payee_vpa)),
            # Absent a definitive answer, treat everyone as new. That applies the
            # tighter first-time cap rather than the looser one.
            is_new=not bool(_field(body, self._settings.field_payee_known)),
            source=PayeeSource.DIRECTORY if name else PayeeSource.DICTATED,
        )

    def spent_today_paise(self, payer: str) -> int:
        """Today's total, from eko's ledger where one is available.

        Counting locally would reset on every deploy and would miss transfers
        made through any other channel, so a local count is not a daily cap — it
        just looks like one.
        """
        if not self._settings.spent_today_path:
            logger.warning(
                "EKO_SPENT_TODAY_PATH is not set — the daily cap only counts what "
                "this bot sent since it started. eko's own next_allowed_limit is "
                "the real ceiling."
            )
            return 0

        body = self._post(self._settings.spent_today_path, {
            "initiator_id": self._settings.initiator_id,
            "user_code": self._settings.user_code,
        })
        return int(_field(body, self._settings.field_spent_today) or 0)

    # --------------------------------------------------------------- writes

    def prepare_transfer(self, payer: str, payee: Payee, amount_paise: int) -> TransferIntent:
        """Build an unauthorised intent. Sends nothing to eko.

        Deliberately local: there is no reason to tell eko about a transfer the
        user has not confirmed, and doing so would leave reconcilable state
        behind every abandoned sentence.
        """
        if amount_paise <= 0:
            raise PaymentError("Amount zero se zyada hona chahiye.")
        _rupees(amount_paise)  # fail here, not after the PIN has been spoken
        return prepare_intent(payer, payee, amount_paise, self._settings.intent_ttl_s)

    def confirm_and_execute(self, intent: TransferIntent, pin: str) -> TransferResult:
        """Verify the PIN with eko and execute the transfer.

        ⚠️ Not reachable from any tool. `pin` becomes eko's `pintwin` and goes
        nowhere else.
        """
        payload = {
            "bc": self._settings.bc,
            "org_id": self._settings.org_id,
            "source": self._settings.source,
            "version": self._settings.version,
            "locale": self._settings.locale,
            "lang": self._settings.lang,
            "service_code": self._settings.service_code,
            "interaction_type_id": self._settings.interaction_type_id,
            "user_code": self._settings.user_code,
            "initiator_id": self._settings.initiator_id,
            "recipient_id_type": "mobile_number",
            "customer_id": intent.payee.phone,
            "recipient_name": intent.payee.resolved_name or "",
            "amount": _rupees(intent.amount_paise),
            "is_consent": "1",
            "latlong": self._settings.latlong,
            "realsourceip": self._settings.source_ip,
            "next_allowed_limit": self._settings.next_allowed_limit,
            # Our intent id, so a retry cannot pay twice — assuming eko honours
            # it as an idempotency key. Confirm this before go-live: without it,
            # a timeout on this request is a double-payment risk.
            "client_ref_id": intent.intent_id,
            # The spoken PIN. Nothing else in this module may touch it.
            "pintwin": pin,
        }
        if intent.payee.masked_account and self._settings.send_vpa:
            payload["customer_vpa"] = intent.payee.masked_account

        del pin

        try:
            response = self._session.post(
                self._url(self._settings.transfer_path),
                json=payload,
                headers=self._headers(),
                timeout=self._settings.timeout_s,
            )
        except requests.RequestException as exc:
            # The transfer may or may not have happened. Say exactly that —
            # claiming failure when money moved is the worse of the two lies.
            logger.error("eko transfer request failed for intent %s: %s",
                         intent.intent_id, type(exc).__name__)
            return TransferResult(ok=False, message=(
                "Network problem — main confirm nahi kar pa raha ki transfer hua "
                "ya nahi. Statement check karke hi dobara koshish kijiye."
            ))
        finally:
            payload["pintwin"] = ""
            del payload

        return self._read_transfer_response(response, intent)

    # ------------------------------------------------------------ internals

    def _read_transfer_response(self, response: Any, intent: TransferIntent) -> TransferResult:
        body = _json(response) if response.content else {}
        status = str(_field(body, self._settings.field_status) or "")
        message = str(_field(body, self._settings.field_message) or "")

        if _is_pin_failure(response.status_code, status, message):
            # Said plainly and without detail: a wrong PIN should not tell the
            # sender anything about the PIN.
            return TransferResult(ok=False, message="PIN galat hai.")

        if not response.ok or status not in self._settings.success_statuses:
            logger.error(
                "eko transfer failed intent=%s http=%s status=%s",
                intent.intent_id, response.status_code, status or "-",
            )
            return TransferResult(ok=False, message=(
                f"Transfer nahi ho paya{f': {message}' if message else '.'}"
            ))

        reference = _field(body, self._settings.field_reference)
        if not reference:
            # A success we cannot reference is a success we cannot support.
            raise PaymentError(
                f"eko returned success with no '{self._settings.field_reference}'. "
                "Refusing to report a transfer that cannot be traced."
            )

        logger.info("eko transfer complete intent=%s ref=%s", intent.intent_id, reference)
        return TransferResult(ok=True, reference=str(reference))

    def _headers(self) -> dict[str, str]:
        return eko_auth_headers(self._settings)

    def _url(self, path: str) -> str:
        return f"{self._settings.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {**payload, "client_ref_id": uuid.uuid4().hex[:20]}
        try:
            response = self._session.post(
                self._url(path), json=payload, headers=self._headers(),
                timeout=self._settings.timeout_s,
            )
        except requests.RequestException as exc:
            raise PaymentError(f"eko API unreachable: {type(exc).__name__}") from exc

        if not response.ok:
            raise PaymentError(f"eko API returned HTTP {response.status_code}")
        return _json(response)


# --------------------------------------------------------------- helpers

def eko_auth_headers(settings: EkoSettings) -> dict[str, str]:
    """eko's three-header auth.

    `secret_key` is an HMAC of the timestamp keyed by the base64 of the auth key,
    base64-encoded — eko's documented scheme, implemented here but not yet
    exercised against a sandbox. If eko issues a static secret instead, set
    `EKO_STATIC_SECRET_KEY` and this computation is skipped.

    Module-level because the reporting source needs the identical headers. A
    second copy would be a second place to fix when eko's scheme is confirmed.
    """
    timestamp = str(int(time.time() * 1000))

    if settings.static_secret_key:
        secret = settings.static_secret_key
    else:
        encoded = base64.b64encode((settings.auth_key or "").encode()).decode()
        secret = base64.b64encode(
            hmac.new(encoded.encode(), timestamp.encode(), hashlib.sha256).digest()
        ).decode()

    return {
        "developer_key": settings.developer_key or "",
        "secret_key": secret,
        "secret_key_timestamp": timestamp,
        "Content-Type": "application/json",
    }


def _rupees(amount_paise: int) -> str:
    """Paise → the rupee string eko's `amount` field takes.

    Refuses a fractional rupee rather than rounding it. eko's examples show whole
    rupees ("10"), and quietly dropping paise off a transfer is a discrepancy
    nobody finds until reconciliation.
    """
    if amount_paise % 100:
        raise PaymentError(
            f"₹{amount_paise / 100:.2f} — eko accepts whole rupees only. "
            "Ask the user for a round amount."
        )
    return str(amount_paise // 100)


def _is_pin_failure(http_status: int, status: str, message: str) -> bool:
    """Whether this response means "wrong PIN" rather than "something broke".

    Kept generous on purpose: telling a user their PIN was wrong when the service
    was down is a small annoyance, whereas telling them the service is down when
    the PIN was wrong leaves them with no way forward.
    """
    if http_status in (401, 403):
        return True
    haystack = f"{status} {message}".lower()
    return any(
        term in haystack
        for term in ("pintwin", "invalid pin", "incorrect pin", "wrong pin")
    )


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise PaymentError("eko API returned something that is not JSON") from exc
    return body if isinstance(body, dict) else {"data": body}


def _field(body: dict[str, Any], path: str) -> Any:
    """Read a dotted path like `data.recipient_name` out of a response.

    The exact response shape is configuration rather than a constant, so an eko
    field sitting one level deeper than expected is an `.env` edit. Missing
    returns None; the caller decides whether that absence is fatal.
    """
    if not path:
        return None
    current: Any = body
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _mask(value: Any) -> str | None:
    """`satyabhan-39@ptyes` → `•••@ptyes`; an account number → `•••1234`.

    Enough for a human to recognise, not enough to be worth intercepting.
    """
    if not value:
        return None
    text = str(value)
    if "@" in text:
        return f"•••@{text.rsplit('@', 1)[1]}"
    digits = "".join(c for c in text if c.isalnum())
    return f"•••{digits[-4:]}" if len(digits) >= 4 else None
