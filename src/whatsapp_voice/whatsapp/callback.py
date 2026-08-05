"""`POST /payments/callback` — the rail telling us a transfer resolved.

Used by the handoff mechanisms in `agent/handoff.py`, where the payer authorises
in their own app and we are informed afterwards. The spoken-PIN path does not need
this: it already knows the outcome synchronously.

Three rules, and every one of them is about money:

**Signed.** This endpoint is public and it asserts that money moved. Unsigned,
anyone who finds the URL can mint receipts. HMAC-SHA256 over the **raw body**, for
the same reason Meta's webhook is: re-serialising JSON changes the bytes and
therefore the MAC.

**Idempotent on `intent_id`.** Rails retry. Two callbacks for one transfer have to
produce one receipt and one audit record — otherwise reconciliation shows the
payment twice and the user is told twice. The claim goes through the shared store,
so it holds across workers too.

**200 fast.** Same reasoning as the Meta webhook: a rail that does not get a prompt
200 retries, and a slow reply turns one callback into several.

An unknown `intent_id` is rejected rather than logged and forgotten. It means
either a replay after expiry or something forged, and both deserve a 404 rather
than silence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response, status

from ..agent.payment_flow import receipt
from ..agent.payments import IntentStatus, PaymentGateway, PendingIntents
from ..audit import AuditTrail
from ..config import Settings
from .client import WhatsAppClient, WhatsAppError

logger = logging.getLogger(__name__)

CALLBACK_SIGNATURE_HEADER = "X-Eko-Signature-256"


def compute_signature(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def is_valid_signature(payload: bytes, header: str | None, secret: str) -> bool:
    """Constant-time compare, so a wrong signature cannot be narrowed by timing."""
    if not header or not secret:
        return False
    return hmac.compare_digest(compute_signature(payload, secret), header)


def mount_payment_callback(
    app: FastAPI,
    settings: Settings,
    gateway: PaymentGateway,
    pending: PendingIntents,
    client: WhatsAppClient,
    audit: AuditTrail,
    store: Any,
) -> None:
    """Attach the callback route.

    Only called when a handoff mechanism is configured. A deployment that
    authorises by spoken PIN has no rail to call back, so the endpoint does not
    exist — one less public surface asserting that money moved.
    """
    secret = settings.payments.callback_secret

    if not secret:
        raise ValueError(
            "PAYMENT_HANDOFF_KIND is set but PAYMENT_CALLBACK_SECRET is not. "
            "An unsigned callback endpoint lets anyone who finds the URL claim a "
            "transfer succeeded."
        )

    @app.post("/payments/callback")
    async def payment_callback(request: Request, background: BackgroundTasks) -> Response:
        raw = await request.body()

        if not is_valid_signature(raw, request.headers.get(CALLBACK_SIGNATURE_HEADER), secret):
            logger.warning("Rejected payment callback with an invalid signature")
            return Response(status_code=status.HTTP_403_FORBIDDEN)

        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            logger.warning("Payment callback body was not JSON")
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        intent_id = str(body.get("intent_id") or body.get("tr") or "").strip()
        if not intent_id:
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        # One claim per intent, through the shared store so it holds across
        # workers. A retried callback loses the race and returns 200 without
        # doing anything — which is what idempotent means here.
        if not store.claim(f"callback:{intent_id}", ttl_s=24 * 3600):
            logger.info("Duplicate callback for intent %s — ignored", intent_id)
            return Response(status_code=status.HTTP_200_OK)

        background.add_task(
            _resolve, intent_id, body, gateway, pending, client, audit
        )
        return Response(status_code=status.HTTP_200_OK)


def _resolve(
    intent_id: str,
    body: dict[str, Any],
    gateway: PaymentGateway,
    pending: PendingIntents,
    client: WhatsAppClient,
    audit: AuditTrail,
) -> None:
    """Apply one callback. Runs after the 200 has gone out."""
    intent = pending.find_by_id(intent_id)
    if intent is None:
        # Either a replay long after expiry or something forged. Recorded, because
        # a callback for a transfer we have no record of is worth knowing about.
        logger.warning("Callback for unknown intent %s", intent_id)
        audit.record("callback_unknown_intent", intent_id=intent_id)
        return

    succeeded = _succeeded(body)
    reference = str(body.get("reference") or body.get("txn_id") or "") or None
    gateway_name = type(gateway).__name__

    if not succeeded:
        intent.status = IntentStatus.FAILED
        pending.clear(intent.payer)
        audit.failed(intent, gateway=gateway_name,
                     reason=str(body.get("message") or "rail reported failure"))
        _send(client, intent.payer, "❌ Transfer nahi ho paya. Dobara koshish kijiye.")
        return

    intent.status = IntentStatus.COMPLETED
    intent.reference = reference
    pending.clear(intent.payer)
    audit.executed(intent, gateway=gateway_name, reference=reference)
    logger.info("Callback completed intent %s ref=%s", intent_id, reference)
    _send(client, intent.payer, receipt(intent, gateway))


def _succeeded(body: dict[str, Any]) -> bool:
    """Whether the rail is reporting success.

    Defaults to **failure** when the field is missing or unrecognised. Telling a
    user their money arrived when we do not know is the worse mistake, and it is
    the one they act on.
    """
    for key in ("status", "state", "result"):
        value = body.get(key)
        if value is None:
            continue
        return str(value).strip().lower() in {"0", "success", "succeeded", "completed", "ok"}
    return False


def _send(client: WhatsAppClient, to: str, text: str) -> None:
    try:
        client.send_text(to, text)
    except WhatsAppError:
        # The transfer resolved either way; the audit record is already written.
        logger.exception("Could not deliver the payment outcome to %s", to)
