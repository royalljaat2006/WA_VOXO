"""Following a transfer that eko accepted but has not settled.

eko answers two different questions in two different fields: `status` says the
request was accepted, `tx_status` says what happened to the money. A transfer
can sit at `tx_status: "2"` — initiated — for a while and then land *or*
reverse. eko publishes no webhook for the transition, so the only way to learn
the outcome is to ask: poll their Transaction Inquiry API until `tx_status`
becomes settled or failed.

This module is the "ask repeatedly, then tell the payer" half. It owns **when**
to ask and **what to say**; the HTTP call belongs to `EkoPaymentGateway.inquire`
and the actual sending belongs to the WhatsApp layer, which is what keeps this
file testable without a network or a phone.

### Why a payer can be told twice

A settlement message arrives long after the conversation that caused it — the
payer has probably moved on. So it names the amount and the reference rather
than saying "your transfer", and it is sent exactly once: the record is removed
before the message goes out, and re-adding it is not possible because the
intent it came from is already closed.

### What this does not do

It does not retry the transfer. An initiated transfer is *already at the bank*;
sending another one would be a second payment, not a repeat of the first. The
only actions here are "ask again later" and "tell the payer what happened".
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..store import KeyValueStore
from .parsing import format_amount

logger = logging.getLogger(__name__)

#: Index of outstanding references. The stores expose get/set/delete but no
#: scan, so the set of pending transfers is kept as one value beside them.
_INDEX_KEY = "settlements"
_RECORD_PREFIX = "settlement:"


@dataclass(frozen=True)
class PendingSettlement:
    """A transfer eko accepted, whose outcome is still unknown.

    Carries everything the eventual message needs, because by the time it is
    sent the intent is gone and the conversation has moved on.
    """

    reference: str
    payer: str
    amount_paise: int
    payee_display: str
    #: `time.time()`, not `monotonic` — this outlives the process that made it.
    created_at: float = field(default_factory=time.time)
    attempts: int = 0

    def age_s(self, now: float | None = None) -> float:
        return (now or time.time()) - self.created_at


@dataclass(frozen=True)
class SettlementOutcome:
    """What one poll concluded, and what the payer should be told."""

    resolved: bool
    message: str | None = None
    #: Distinguishes "landed" from "reversed" for the audit trail; None while
    #: still pending.
    settled: bool | None = None


class SettlementQueue:
    """Outstanding settlements, in whatever store the deployment has.

    Store-backed rather than in-process so a restart does not silently forget a
    transfer that is still in flight — the payer would then never hear the
    outcome, which is the worst of the three possible endings.
    """

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    def add(self, settlement: PendingSettlement) -> None:
        self._store.set(_RECORD_PREFIX + settlement.reference, json.dumps(asdict(settlement)))
        refs = self._references()
        if settlement.reference not in refs:
            refs.append(settlement.reference)
            self._store.set(_INDEX_KEY, json.dumps(refs))
        logger.info(
            "Settlement pending ref=%s payer=%s amount_paise=%d",
            settlement.reference, settlement.payer, settlement.amount_paise,
        )

    def remove(self, reference: str) -> None:
        self._store.delete(_RECORD_PREFIX + reference)
        refs = [r for r in self._references() if r != reference]
        self._store.set(_INDEX_KEY, json.dumps(refs))

    def all(self) -> list[PendingSettlement]:
        out: list[PendingSettlement] = []
        for reference in self._references():
            raw = self._store.get(_RECORD_PREFIX + reference)
            if raw is None:
                # Index and records drifted apart — a TTL expiry, or a delete
                # that lost a race. Drop the dangling reference rather than
                # carrying it forever.
                self.remove(reference)
                continue
            try:
                out.append(PendingSettlement(**json.loads(raw)))
            except (TypeError, ValueError):
                logger.warning("Unreadable settlement record ref=%s — dropping", reference)
                self.remove(reference)
        return out

    def touch(self, settlement: PendingSettlement) -> None:
        """Record one more failed attempt, keeping it queued."""
        from dataclasses import replace

        self.add(replace(settlement, attempts=settlement.attempts + 1))

    def _references(self) -> list[str]:
        raw = self._store.get(_INDEX_KEY)
        if not raw:
            return []
        try:
            refs = json.loads(raw)
        except ValueError:
            return []
        return [str(r) for r in refs] if isinstance(refs, list) else []

    def __len__(self) -> int:
        return len(self._references())


def poll_once(
    settlement: PendingSettlement,
    gateway: Any,
    give_up_after_s: float,
) -> SettlementOutcome:
    """Ask eko about one transfer, and decide what that means.

    Pure apart from the gateway call, so every branch below is testable without
    a network: settled, reversed, still pending, gateway unreachable, and the
    give-up path.
    """
    try:
        result = gateway.inquire(settlement.reference, settlement.payer)
    except Exception as exc:  # noqa: BLE001 - an inquiry failure must not kill the loop
        logger.warning(
            "Settlement inquiry failed ref=%s: %s — will retry",
            settlement.reference, type(exc).__name__,
        )
        return _keep_waiting_unless_too_old(settlement, give_up_after_s)

    amount = f"₹{format_amount(settlement.amount_paise)}"

    if result.ok:
        # The message the payer has been waiting for. Says "successful" in as
        # many words: the earlier reply told them it was *not* confirmed, so
        # this one has to unambiguously replace that, not merely update it.
        return SettlementOutcome(resolved=True, settled=True, message=(
            f"✅ *Transaction successful!*\n"
            f"{amount} → {settlement.payee_display} pahunch gaya.\n"
            f"Ref: {settlement.reference}"
        ))

    if not result.pending:
        # eko says failed. The money did not move, or moved and came back.
        detail = f"\n{result.message}" if result.message else ""
        return SettlementOutcome(resolved=True, settled=False, message=(
            f"❌ {amount} → {settlement.payee_display} nahi ja paya. "
            f"Paisa wapas aa jayega.\nRef: {settlement.reference}{detail}"
        ))

    return _keep_waiting_unless_too_old(settlement, give_up_after_s)


def _keep_waiting_unless_too_old(
    settlement: PendingSettlement, give_up_after_s: float
) -> SettlementOutcome:
    """Still unknown. Keep asking — the only thing that ends this is an answer.

    There is deliberately no small attempt cap. An initiated transfer cannot be
    recalled: eko has no cancel endpoint, and its refund API covers
    already-failed cash transactions with customer OTP, not money in flight. So
    stopping early would not stop the transfer — it would only stop us watching
    it, and leave the payer with "not confirmed" as the last thing they ever
    heard.

    The age bound is a backstop, not a policy. When it fires, the outcome is
    *unknown* (`settled=None`) rather than failed: eko never said the transfer
    failed, and recording that it did would put a false line in the audit.
    """
    if settlement.age_s() < give_up_after_s:
        return SettlementOutcome(resolved=False)

    amount = f"₹{format_amount(settlement.amount_paise)}"
    hours = give_up_after_s / 3600
    logger.error(
        "Settlement still unresolved after %.1fh ref=%s payer=%s (%d attempts) "
        "— outcome unknown, stopping",
        hours, settlement.reference, settlement.payer, settlement.attempts,
    )
    return SettlementOutcome(resolved=True, settled=None, message=(
        f"⚠️ {amount} → {settlement.payee_display} ka status {hours:.0f} ghante "
        f"baad bhi confirm nahi hua.\nRef: {settlement.reference}\n\n"
        "Ab main track nahi kar paunga — apna statement check kar lijiye."
    ))
