"""The loop that asks eko about pending transfers and tells the payer.

Transport half of `agent/settlement.py`: that module decides *when* to give up
and *what to say*, this one owns the timer, the thread, and the WhatsApp send.
Split that way so the decisions are testable without a network or a clock.

Runs in a daemon thread rather than an asyncio task because the gateway and the
WhatsApp client are both synchronous `requests` callers — driving them from the
event loop would block every webhook for the duration of an inquiry, which is
the one thing the transport layer must never do (Meta retries anything slow).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..agent.settlement import SettlementQueue, poll_once
from ..audit import AuditTrail

logger = logging.getLogger(__name__)


class SettlementPoller:
    """Polls outstanding settlements on an interval until each resolves."""

    def __init__(
        self,
        queue: SettlementQueue,
        gateway: Any,
        client: Any,
        interval_s: float,
        give_up_after_s: float,
        audit: AuditTrail | None = None,
        store: Any = None,
    ) -> None:
        self._queue = queue
        self._gateway = gateway
        self._client = client
        self._interval = interval_s
        self._give_up_after = give_up_after_s
        self._audit = audit if audit is not None else AuditTrail(None)
        self._store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="settlement-poller", daemon=True
        )
        self._thread.start()
        logger.info(
            "Settlement poller started (every %.0fs, tracking until settled or %.1fh)",
            self._interval, self._give_up_after / 3600,
        )

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ internals

    def _run(self) -> None:
        # First sweep is delayed by one interval: a transfer that just went
        # pending has not had time to settle, and asking immediately only burns
        # an inquiry.
        while not self._stop.wait(self._interval):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - a bad sweep must not kill the loop
                logger.exception("Settlement sweep failed — continuing")

    def sweep(self) -> int:
        """One pass over every outstanding settlement. Returns how many resolved.

        Public so a test — or an operator — can run a sweep without waiting for
        the timer.
        """
        outstanding = self._queue.all()
        if not outstanding:
            return 0

        # With several workers, one lease means one poller. Without a shared
        # store `claim` is per-process and this is a no-op, which is correct:
        # a single worker is the only configuration that reaches that branch
        # (see `guard_worker_count`).
        if self._store is not None and not self._store.claim(
            "settlement-sweep", ttl_s=max(self._interval - 1, 1)
        ):
            return 0

        resolved = 0
        for settlement in outstanding:
            outcome = poll_once(settlement, self._gateway, self._give_up_after)
            if not outcome.resolved:
                self._queue.touch(settlement)
                continue

            # Removed *before* the message goes out. A send that fails then
            # loses one notification; the other order would re-notify on every
            # sweep forever.
            self._queue.remove(settlement.reference)
            resolved += 1
            self._record(settlement, outcome)

            if outcome.message:
                try:
                    self._client.send_text(settlement.payer, outcome.message)
                except Exception:  # noqa: BLE001 - one bad send, not the sweep
                    logger.exception(
                        "Could not deliver settlement outcome ref=%s payer=%s",
                        settlement.reference, settlement.payer,
                    )
        return resolved

    def _record(self, settlement: Any, outcome: Any) -> None:
        self._audit.record(
            "settlement_resolved",
            reference=settlement.reference,
            payer=settlement.payer,
            amount_paise=settlement.amount_paise,
            settled=outcome.settled,
            attempts=settlement.attempts,
        )
        logger.info(
            "Settlement resolved ref=%s settled=%s after %d attempt(s)",
            settlement.reference, outcome.settled, settlement.attempts,
        )
