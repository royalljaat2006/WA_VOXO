"""eko's real payment API, behind the `PaymentGateway` boundary.

**This is the file that turns the POC into something that moves money.** Nothing
above it changes: the PIN gate, the caps, the amount parser and the tools all talk
to `PaymentGateway` and do not care what is behind it.

Written against the eko ICICI fund-transfer contract:

    POST {base}/ekoicici/v1/request        (base: https://api.eko.in — no port)
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
that is only noticed at reconciliation. **Confirmed with eko's platform team:
`amount` is rupees, not paise.**

### Confirmed with eko's platform team

- **`amount` is rupees**, not paise (see above).
- **`client_ref_id` must be unique per transfer** — eko uses it for
  reconciliation, which is why it is set to `intent.intent_id` (stable per
  transfer, so a retry cannot mint a second one).
- **No payee-name lookup.** eko will not return a recipient name, so
  `resolve_payee` always falls back to `PayeeSource.DICTATED` and the digits
  get read back aloud — this is not a degraded path, it is the only path.
  `EKO_VERIFY_PATH` / `field_payee_name` are not worth wiring up for a name.
- **`developer_key`, `secret_key`, `secret_key_timestamp` must always be
  present as headers, even with an empty value** — see `eko_auth_headers`.
- **`base_url` has no port.** `https://api.eko.in/ekoicici/v1/request` — an
  earlier guess of `:25002` was wrong, corrected against a real working curl.
- **`customer_vpa` identifies the receiving party**, and **`customer_id` is a
  fixed merchant-side value** (`EKO_CUSTOMER_ID`) — not the payee's mobile.
  Nothing else about the recipient is required to complete a transfer.

  `customer_id` sits with `user_code` and `initiator_id` as part of who *we*
  are. Its name is the trap: this file read it as the recipient's number
  because one confirmed curl happened to carry a `<mobile>@upi` VPA, so
  `customer_id` and the VPA's local part held the same digits and looked
  linked. They are not.

  The cost of that misreading: the flow demanded a 10-digit mobile for every
  payee, so an ordinary UPI ID like `name@okicici` — which carries no phone
  number at all — could not be paid without interrogating the sender for a
  number eko never wanted. The payee's `phone` is now display and caps only;
  it never reaches the wire.

### Transaction Inquiry — confirmed line by line against a real curl

- **The `{reference}` in the inquiry URL is eko's own TID**, the one returned
  in `data.tid` after a transfer request succeeds — not our `client_ref_id`,
  and not the intent id. `PendingSettlement.reference` is already populated
  from that field, so no change was needed here — this just confirms the
  chain is right end to end.
- **`initiator_id` is the same fixed merchant mobile as the transfer request**
  — one value, not per-transaction. Already correct.
- **`client_ref_id` on the inquiry is a fresh identifier per *call*, not per
  transaction** — it names this question, not the transfer being asked about
  (the TID in the path already does that). A new one is generated on every
  `inquire()`.
- **`developer_key` / `secret-key` / `secret-key-timestamp` are sent as
  headers with empty values** when unconfigured, not omitted — this was
  already the behavior (`eko_auth_headers`), now confirmed rather than
  inferred.
- **No `Content-Type` on the inquiry request** — it is a GET with no body;
  only the transfer POST needs it.

### Transaction Inquiry — `https://api.eko.in/ekoicici/v3`, the live endpoint

Four attempts landed here. The first two were guesses made from this side;
the third and fourth came as real curls from eko.

1. `eps.eko.in` — summarised the docs page's on-screen text. Wrong: it is
   eko's documentation *website*, not an API gateway. Every path there
   returns HTTP 200 with an HTML page carrying a Google Tag Manager script,
   four calls running.
2. `api.eko.in` — fell back to the transfer endpoint's host, since it
   behaves like a real gateway (402 "Auth parameters missing" on every path,
   including nonsense ones). Defensible but still a guess: its auth-before-
   routing meant it could never confirm a specific path.
3. `staging.eko.in/ekoapi/v3` — eko sent a working curl directly. Confirmed
   the request *shape* — this is where the "`tid` is a query parameter, not
   a path segment" discovery below came from — but eko later confirmed this
   host is sandbox-only: a real production TID came back "not found" here
   while eko's own sandbox sample resolved fine.
4. `api.eko.in/ekoicici/v3/tools/reference/transaction` — eko's worked curl
   against the live environment. Same host as the transfer endpoint,
   different path from both the transfer's own (`ekoicici/v1`) and the
   sandbox's (`ekoapi/v3`).

The path-shape discovery, made against the sandbox host in attempt 3 and
carried forward here: a live probe kept returning 404 "Endpoint not found"
even after the host itself was right — not because the host was wrong, but
because `tid` was being sent as a URL path segment (`.../transaction/{tid}`,
matching eko's example curl literally) instead of a query parameter. Hitting
the bare path with no `tid` returned a real eko error body instead of a 404,
which exposed the actual shape: `?tid={tid}&initiator_id=…&client_ref_id=…`.

### Confirmed directly by eko, 2026-08-14

- **The sandbox/live split above**, and the live base URL and path.
- **`initiator_id` for inquiry is per-payer, same as the transfer request**
  — confirmed explicitly rather than assumed, since eko's sandbox curl had
  separately said to use a fixed value here, and getting this wrong once
  already produced a real failure ("Invalid Booklet Number") on the transfer
  side. See `_initiator_id_for`.
- **The `requests`-vs-curl 401 (see `_curl_get`) may have been sandbox-specific.**
  Worth re-testing plain `requests` against the live host now that it is
  configured — the curl subprocess workaround may no longer be needed.
- **Inquiry rate limit: 25 requests/second, maximum.** Worth keeping in mind
  once anything beyond the settlement poller starts calling `inquire()` on
  demand (an on-request "check my status" tool, for instance) — nothing here
  currently enforces this ceiling, because nothing has come close to it yet.
- **`secret_key` is never required for inquiry** — confirmed, not just
  assumed. Sending it empty (see `eko_auth_headers`) is correct permanently,
  not a placeholder waiting on a real HMAC.
- The daily-limit endpoint (`EKO_SPENT_TODAY_PATH`) is explicitly out of
  scope for now, per eko — not being chased further until asked for again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import subprocess
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
    current_payer,
    prepare_intent,
)

logger = logging.getLogger(__name__)


class EkoPaymentGateway(PaymentGateway):
    is_stub = False

    @property
    def resolves_names(self) -> bool:
        """Only when a verify endpoint is configured.

        eko's platform team confirmed the transfer API never returns a payee
        name, so without `EKO_VERIFY_PATH` a lookup would echo back the UPI
        ID the user just said — a wasted model turn, and one more place for
        the model to stop before preparing the transfer.
        """
        return bool(self._settings.verify_path)

    def __init__(
        self, settings: EkoSettings, session: Any = None, curl_get: Any = None
    ) -> None:
        missing = settings.missing_fields(for_transfers=True)
        if missing:
            raise PaymentError(
                "PAYMENT_GATEWAY=eko but the eko API is not fully configured. "
                f"Missing: {', '.join(missing)}. See the eko section in .env."
            )
        self._settings = settings
        self._session = session or requests.Session()
        #: Overridable only for tests — see `inquire`'s docstring for why the
        #: inquiry call goes via curl rather than through `self._session`.
        self._curl_get = curl_get or _curl_get
        logger.warning(
            "eko payment gateway active (%s, user_code=%s) — THIS MOVES REAL MONEY.",
            settings.base_url, settings.user_code,
        )

    # ---------------------------------------------------------------- reads

    def resolve_payee(self, phone: str) -> Payee:
        """Recipient mobile → payee.

        eko's transfer request takes `recipient_id_type: mobile_number`, so a
        mobile is all that is strictly needed — confirmed against a real
        transfer curl; there is no path that accepts a VPA instead. A VPA a
        user also states is attached separately, in `prepare_transfer` — see
        `agent/payment_tools.py`.

        eko's platform team confirmed there is no payee-name lookup — a name
        is never returned — so this always comes back as
        `PayeeSource.DICTATED`, `needs_digit_readback` is always true, and the
        digits get read back aloud. That is not a degraded fallback; it is the
        only path there is.
        """
        digits = "".join(c for c in (phone or "") if c.isdigit())
        if len(digits) < 10:
            raise PaymentError(f"'{phone}' is not a usable mobile number.")
        digits = digits[-10:]  # eko wants the 10-digit mobile, no country code

        if not self._settings.verify_path:
            return Payee(phone=digits, source=PayeeSource.DICTATED)

        body = self._post(self._settings.verify_path, {
            "initiator_id": _initiator_id_for(current_payer.get()),
            "user_code": self._settings.user_code or "",
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
            "initiator_id": _initiator_id_for(payer),
            "user_code": self._settings.user_code or "",
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
            "user_code": self._settings.user_code or "",
            # The WhatsApp payer's own number, not a fixed merchant value —
            # see `_initiator_id_for`. `customer_id` right below is the one
            # that stays fixed; do not conflate the two.
            "initiator_id": _initiator_id_for(intent.payer),
            # Merchant identity, not the payee — a fixed value issued with the
            # rest of the credentials. It reads like a recipient field, which is
            # exactly the trap: taking it for the payee's mobile is what made
            # this flow demand a phone number for every transfer.
            "recipient_id_type": "mobile_number",
            "customer_id": self._settings.customer_id or "",
            "recipient_name": intent.payee.resolved_name or self._settings.recipient_name,
            "amount": _rupees(intent.amount_paise),
            "is_consent": "1",
            "latlong": self._settings.latlong,
            "realsourceip": self._settings.source_ip,
            "next_allowed_limit": self._settings.next_allowed_limit,
            # Derived from our intent id, so a retry cannot pay twice. Confirmed
            # with eko's platform team: client_ref_id must be unique per
            # transfer and is used for reconciliation.
            "client_ref_id": _client_ref_id(intent.intent_id),
            # The spoken PIN. Nothing else in this module may touch it.
            "pintwin": pin,
        }
        # `customer_vpa` identifies the receiving party on its own; nothing else
        # about the recipient is needed to complete the transfer. Sent unmasked
        # — the display form is for humans, and eko cannot route on •••@handle.
        if intent.payee.vpa:
            payload["customer_vpa"] = intent.payee.vpa

        del pin

        # The exact request, so a failure can be diffed against a known-good
        # curl without guessing what we sent. `pintwin` is replaced, not
        # shortened — a masked PIN of the right length still leaks its length.
        logger.info(
            "eko transfer request intent=%s body=%s",
            intent.intent_id,
            json.dumps({**payload, "pintwin": "[REDACTED-PIN]"}, ensure_ascii=False),
        )

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
        # Logged raw and in full, before any parsing — the branches below read
        # only `status` and `message`, so anything eko says outside those two
        # fields (an error detail, a limit, a reason code) would otherwise be
        # discarded before anyone could see it. Logged for successes too: the
        # question after a real transfer is usually "what exactly came back".
        logger.info(
            "eko transfer response intent=%s http=%s body=%s",
            intent.intent_id, response.status_code, _raw_body(response),
        )

        body = _json(response) if response.content else {}
        # NOT `_field(...) or ""` — eko sends `"status": 0` as a JSON *number*,
        # and `0` is falsy, so the `or` fallback silently replaced success with
        # an empty string. `""` is not in `success_statuses`, so every
        # successful transfer was read as a failure and reported to the sender
        # as a wrong PIN, while the money moved. Only a *missing* field may
        # fall back; a present falsy one is a real value.
        status = _as_text(_field(body, self._settings.field_status))
        message = _as_text(_field(body, self._settings.field_message))

        if _is_pin_failure(response.status_code, status, message):
            # Said plainly and without detail: a wrong PIN should not tell the
            # sender anything about the PIN.
            return TransferResult(ok=False, pin_rejected=True, message="PIN galat hai.")

        if not response.ok or status not in self._settings.success_statuses:
            # `message` was already in the TransferResult below but never
            # logged — every non-PIN failure showed only a status code here,
            # with eko's own explanation of it thrown away. That is why
            # status 1123 was a mystery the first time it was seen.
            logger.error(
                "eko transfer failed intent=%s http=%s status=%s message=%s",
                intent.intent_id, response.status_code, status or "-", message or "-",
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

        # `status` said eko accepted the request. `tx_status` says what happened
        # to the money, and they are not the same answer — see the field's
        # comment in config.py. Reading only the first is what let a pending
        # transfer be receipted as done and then quietly reverse.
        tx_status = _as_text(_field(body, self._settings.field_tx_status))

        if tx_status in self._settings.failed_tx_statuses:
            logger.error(
                "eko transfer failed downstream intent=%s ref=%s tx_status=%s message=%s",
                intent.intent_id, reference, tx_status, message or "-",
            )
            return TransferResult(ok=False, reference=str(reference), message=(
                f"Transfer fail ho gaya{f': {message}' if message else '.'}"
            ))

        if tx_status not in self._settings.settled_tx_statuses:
            # eko's documented instruction, followed literally: not settled, not
            # failed — poll Transaction Inquiry until it resolves. There is no
            # webhook, so nothing here can learn the outcome on its own yet.
            logger.warning(
                "eko transfer PENDING intent=%s ref=%s tx_status=%s message=%s "
                "— not settled; confirm via eko's Transaction Inquiry API",
                intent.intent_id, reference, tx_status or "-", message or "-",
            )
            return TransferResult(
                ok=False, pending=True, reference=str(reference),
                message=(message or "Transaction initiated"),
            )

        logger.info("eko transfer complete intent=%s ref=%s tx_status=%s",
                    intent.intent_id, reference, tx_status)
        return TransferResult(ok=True, reference=str(reference))

    def inquire(self, reference: str, payer: str) -> TransferResult:
        """Ask eko what became of an initiated transfer.

        `payer` is the WhatsApp number that initiated it — `initiator_id` is
        now this payer's own number (see `_initiator_id_for`), not a fixed
        merchant value, so it has to be supplied here explicitly. The caller
        (`agent/settlement.py::poll_once`) has it on `PendingSettlement`.

        `GET {inquiry_base_url}{inquiry_path}?tid=…&initiator_id=…` — confirmed
        directly by eko. Same host as the transfer endpoint (`api.eko.in`),
        but a **different path** (`ekoicici/v3`, not the transfer's
        `ekoicici/v1`) — see `EkoSettings.inquiry_path` for how that was
        found, including the sandbox host it took to get there. eko has no
        webhook for the initiated → settled transition, so this is the only
        way to find out, and their documentation is explicit that a
        transaction must not be called failed on a timeout — inquire, don't
        retry.

        **`tid` is a query parameter, not a URL path segment** — eko's own
        example curl showed it appended to the path
        (`.../transaction/{tid}`), and that shape reliably 404ed
        ("Endpoint not found") against the live gateway, for every `tid`
        tried, including eko's own documented sample. Hitting the bare path
        with no `tid` at all returned a real, well-formed eko error body
        instead of a 404 — that's what exposed the real contract: `tid` goes
        in the query string, same as `initiator_id` and `client_ref_id`.

        Reads the same `tx_status` field with the same settled/failed lists as
        `confirm_and_execute`, so the two paths cannot disagree about what a
        code means — with one exception: eko reuses `tx_status: "1"` ("Fail")
        for *"we have no such tid"* as well as for a genuinely failed
        transfer, distinguished only by `status: 69` /
        `message: "failed!inquired.tx.not.found"`. Telling a payer their
        money is lost because eko simply doesn't have this tid yet (or on
        this host) would be a real mistake with real money, so a not-found
        response is treated as still-pending, not failed.

        Raises `PaymentError` when eko cannot be reached — the caller retries
        rather than concluding anything.

        **Goes out via the system `curl` binary, not `requests`.** eko's
        *sandbox* inquiry host (`staging.eko.in`) returned 401 "Unauthorized"
        for every `requests`-issued call and 200 for an otherwise
        byte-identical curl call — same URL, same headers, same params, same
        timestamp, reproduced twice with a `requests` call sandwiched between
        two successful curl calls — consistent with TLS/HTTP client
        fingerprinting blocking Python's request library on that host. The
        transfer endpoint, on the *same host* `inquiry_base_url` now points
        at (`api.eko.in`), has no such problem with `requests`. Whether that
        means the fingerprinting issue is gone now that inquiry is on this
        host too, or specific to the sandbox host rather than the client
        library generally, is not yet re-tested — kept on curl until it is.
        See `_curl_get`.
        """
        if not self._settings.inquiry_path:
            raise PaymentError("EKO_INQUIRY_PATH is not set — cannot inquire.")

        base = self._settings.inquiry_base_url.rstrip("/")
        path = self._settings.inquiry_path.strip("/")
        url = f"{base}/{path}"
        params = {
            "tid": reference,
            "initiator_id": _initiator_id_for(payer),
            # eko's docs mark this optional but show it on every example
            # call, so it is sent — a fresh value per inquiry, not the
            # transfer's own client_ref_id. It identifies this *question*,
            # not the transaction being asked about; `tid` already does that.
            "client_ref_id": uuid.uuid4().hex[:20],
        }
        try:
            response = self._curl_get(
                url, params, self._headers(inquiry=True), self._settings.timeout_s
            )
        except PaymentError:
            raise
        except (subprocess.SubprocessError, OSError) as exc:
            raise PaymentError(f"eko inquiry unreachable: {type(exc).__name__}") from exc

        logger.info(
            "eko inquiry ref=%s http=%s body=%s",
            reference, response.status_code, _raw_body(response),
        )

        if not response.ok:
            raise PaymentError(f"eko inquiry returned HTTP {response.status_code}")

        body = _json(response) if response.content else {}
        top_status = _as_text(_field(body, self._settings.field_status))
        tx_status = _as_text(_field(body, self._settings.field_tx_status))
        message = _as_text(
            _field(body, self._settings.field_message)
        ) or _as_text(_field(body, "data.txstatus_desc"))

        if top_status == "69" or "not.found" in message.lower():
            return TransferResult(
                ok=False, pending=True, reference=reference, message=message or None
            )
        if tx_status in self._settings.settled_tx_statuses:
            return TransferResult(ok=True, reference=reference, message=message or None)
        if tx_status in self._settings.failed_tx_statuses:
            return TransferResult(ok=False, reference=reference, message=message or None)
        return TransferResult(
            ok=False, pending=True, reference=reference, message=message or None
        )

    def _headers(self, inquiry: bool = False) -> dict[str, str]:
        headers = eko_auth_headers(self._settings)
        if inquiry:
            # eko documents the inquiry endpoint with `secret-key` /
            # `secret-key-timestamp` where the transfer endpoint takes
            # underscores. Sending both spellings rather than guessing which is
            # right: an extra unread header is harmless, a missing one is a 401.
            if self._settings.inquiry_hyphenated_headers:
                headers = {
                    **headers,
                    "secret-key": headers.get("secret_key", ""),
                    "secret-key-timestamp": headers.get("secret_key_timestamp", ""),
                }
            # Confirmed with eko: inquiry is a GET with no body, so
            # `Content-Type` does not apply — only the transfer POST needs it.
            headers.pop("Content-Type", None)
        return headers

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

    eko's platform team confirmed all three headers must always be *present* on
    every request, even before credentials are issued — so with neither
    `auth_key` nor `static_secret_key` set, `secret_key` is sent as an empty
    string rather than an HMAC of an empty key (which would be a non-empty,
    meaningless value masquerading as a real secret).

    Module-level because the reporting source needs the identical headers. A
    second copy would be a second place to fix when eko's scheme is confirmed.
    """
    timestamp = str(int(time.time() * 1000))

    if settings.static_secret_key:
        secret = settings.static_secret_key
    elif settings.auth_key:
        encoded = base64.b64encode(settings.auth_key.encode()).decode()
        secret = base64.b64encode(
            hmac.new(encoded.encode(), timestamp.encode(), hashlib.sha256).digest()
        ).decode()
    else:
        secret = ""

    return {
        "developer_key": settings.developer_key or "",
        "secret_key": secret,
        "secret_key_timestamp": timestamp,
        "Content-Type": "application/json",
    }


def _client_ref_id(intent_id: str) -> str:
    """Our 12-char hex intent id, in the 20-digit numeric shape eko's own
    working request used (`69837231780481350676`).

    A pure function of the intent id, so a retry of the same transfer still
    produces the same reference and cannot pay twice — that property is the
    whole point of the field and must survive the reformatting.
    """
    return f"9{int(intent_id, 16):019d}"[:20]


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


def _initiator_id_for(payer: str) -> str:
    """The WhatsApp payer's own number, in the 10-digit shape `initiator_id`
    needs — `919876543210` → `9876543210`.

    `initiator_id` is no longer a single fixed merchant value: every real
    transfer and inquiry now identifies *this specific payer* as the
    initiator, not the merchant account behind the bot. Raises rather than
    truncating something implausible into shape — a payer id that is not a
    real phone number (the demo path's `demo:<session_id>`, for instance)
    must not silently become a wrong-but-mobile-shaped `initiator_id` sent to
    eko for a real transfer.
    """
    digits = "".join(c for c in (payer or "") if c.isdigit())
    if len(digits) < 10:
        raise PaymentError(
            f"Cannot derive an initiator_id from payer {payer!r} — not a "
            "usable mobile number."
        )
    return digits[-10:]


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


#: Enough for any eko response, short enough that a runaway HTML error page
#: cannot flood the log or the /ops ring buffer.
_MAX_LOGGED_BODY = 2000


def _raw_body(response: Any) -> str:
    """eko's response exactly as it arrived, for the log.

    Deliberately not `response.json()` — a failure that returns HTML or an empty
    body is precisely the case worth seeing verbatim, and parsing it first would
    turn it into an exception instead of a log line.
    """
    try:
        content = response.content or b""
        text = (
            content.decode("utf-8", errors="replace")
            if isinstance(content, bytes) else str(content)
        )
    except Exception:  # noqa: BLE001 - logging must never break a transfer
        return "<unreadable>"
    text = " ".join(text.split())
    if not text:
        return "<empty>"
    return text if len(text) <= _MAX_LOGGED_BODY else f"{text[:_MAX_LOGGED_BODY]}…[truncated]"


def _json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise PaymentError("eko API returned something that is not JSON") from exc
    return body if isinstance(body, dict) else {"data": body}


class _CurlResponse:
    """`requests.Response`-shaped enough for `_json`/`_raw_body` to read — the
    two things `inquire` needs from whatever made the HTTP call."""

    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


#: Written to stdout after the response body so curl's exit status can be
#: read back out of one captured stream — arbitrary but distinctive enough
#: that no real eko response body could contain it by coincidence.
_CURL_STATUS_MARKER = "__EKO_CURL_HTTP_STATUS__"


def _curl_get(
    url: str, params: dict[str, str], headers: dict[str, str], timeout_s: float
) -> _CurlResponse:
    """GET via the system `curl` binary — see `inquire`'s docstring for why
    `requests` cannot be used against eko's staging inquiry host.

    `subprocess.run` with a list of args, never `shell=True` — every value
    here is either our own fixed config or a freshly generated hex id, but
    building the command this way means an eko-supplied value could never
    end up interpreted by a shell even if one were plumbed in here later.
    """
    args = [
        "curl", "-s", "--compressed", "-G", url,
        "-m", str(max(1, int(timeout_s))),
        "-w", f"\n{_CURL_STATUS_MARKER}%{{http_code}}",
    ]
    for key, value in params.items():
        args += ["--data-urlencode", f"{key}={value}"]
    for key, value in headers.items():
        args += ["-H", f"{key}: {value}"]
    result = subprocess.run(
        args, capture_output=True, timeout=timeout_s + 5, check=False
    )
    if result.returncode != 0:
        raise PaymentError(
            f"eko inquiry: curl exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()[:200]}"
        )
    raw = result.stdout
    marker = _CURL_STATUS_MARKER.encode()
    idx = raw.rfind(marker)
    if idx == -1:
        raise PaymentError("eko inquiry: curl produced no status code")
    body, status_text = raw[:idx].rstrip(b"\n"), raw[idx + len(marker):]
    return _CurlResponse(int(status_text.strip()), body)


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


def _as_text(value: Any) -> str:
    """A response field as a string, treating only *absence* as empty.

    `str(value or "")` looks equivalent and is not: eko's success code is the
    number `0`, which is falsy, so `or` throws it away and the caller compares
    `""` against the success list. Written out so the next person cannot
    re-introduce it by "simplifying" this back to an `or`.
    """
    return "" if value is None else str(value)


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
