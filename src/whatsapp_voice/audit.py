"""The append-only record of every attempt to move money.

Logs answer "what happened?". An audit trail answers "what happened, and can you
prove it?" — which is a different question with different rules:

- **Append-only.** Nothing here is ever edited or deleted by this code. A record
  that can be rewritten is not evidence.
- **Written before *and* after.** An intent is recorded when it is prepared and
  again when it resolves. A process that dies mid-transfer therefore leaves a
  `prepared` line with no outcome — which is exactly the case someone needs to
  investigate, and exactly the case a write-on-success-only design loses.
- **Flushed and fsynced per line.** Buffering would lose the last few records in
  the crash they exist to explain.
- **Never the PIN.** Not the value, not its length, not a hash. There is no
  question an auditor can ask that a stored PIN answers.

Phone numbers *are* recorded in full, unlike the application log where they are
masked. An audit trail whose subject cannot be identified is not one. That makes
this file personal data under DPDP: it belongs on encrypted storage with a stated
retention period, and it is not something to tail into a chat window.

### Format

One JSON object per line (JSONL), because it survives partial writes, appends
without parsing what came before, and every log tool already reads it.

    {"ts": "2026-08-04T01:12:33.481Z", "event": "transfer_executed",
     "intent_id": "a1b2c3d4e5f6", "payer": "919406921732",
     "payee": "9002331157", "payee_name": "RAM", "amount_paise": 200000,
     "payee_source": "contact_card", "gateway": "eko", "reference": "TXN99"}
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditEvent:
    """The vocabulary. A closed set, so a query can rely on it."""

    PREPARED = "intent_prepared"
    PIN_FAILED = "pin_attempt_failed"
    EXECUTED = "transfer_executed"
    FAILED = "transfer_failed"
    CANCELLED = "intent_cancelled"
    EXPIRED = "intent_expired"
    #: A caller tried to pay without the capability, or with no verified caller.
    REFUSED = "transfer_refused"
    #: Allowlist changes made by a super-user — see `AuditTrail.roster_changed`.
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"


class AuditTrail:
    """Appends one JSON line per event to a file that is never rewritten.

    A no-op when no path is configured, so a developer running locally is not
    forced to produce an audit file — but `PAYMENT_GATEWAY=eko` refuses to start
    without one, because real money with no trail is the combination that is
    actually dangerous.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        # Handlers run in background threads, and two interleaved writes would
        # produce one corrupt line — the one thing this file cannot afford.
        self._lock = threading.Lock()

        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Audit trail: %s", path)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(self, event: str, **fields: Any) -> None:
        """Append one event. Never raises.

        An audit write failing must not take down a payment that has already
        happened — but it must be loud, because the trail is now incomplete and
        somebody has to know that.
        """
        if self._path is None:
            return

        line = json.dumps(
            {"ts": _now(), "event": event, **_clean(fields)},
            ensure_ascii=False,
            sort_keys=True,
        )

        try:
            with self._lock, self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                # Buffered writes would lose exactly the records that explain a
                # crash, which is when they matter most.
                os.fsync(handle.fileno())
        except OSError:
            logger.exception("AUDIT WRITE FAILED for %s — the trail is incomplete", event)

    # ------------------------------------------------- convenience recorders

    def prepared(self, intent: Any, gateway: str) -> None:
        """Before anything can be authorised. Half of the before/after pair."""
        self.record(
            AuditEvent.PREPARED,
            gateway=gateway,
            **_intent_fields(intent),
        )

    def executed(self, intent: Any, gateway: str, reference: str | None) -> None:
        self.record(
            AuditEvent.EXECUTED,
            gateway=gateway,
            reference=reference,
            **_intent_fields(intent),
        )

    def failed(self, intent: Any, gateway: str, reason: str) -> None:
        self.record(
            AuditEvent.FAILED,
            gateway=gateway,
            reason=reason,
            **_intent_fields(intent),
        )

    def pin_failed(self, intent: Any, attempts_left: int) -> None:
        """The attempt is recorded; the PIN itself never is."""
        self.record(
            AuditEvent.PIN_FAILED,
            attempts_left=attempts_left,
            **_intent_fields(intent),
        )

    def cancelled(self, intent: Any, reason: str) -> None:
        self.record(AuditEvent.CANCELLED, reason=reason, **_intent_fields(intent))

    def refused(self, payer: str, reason: str, **extra: Any) -> None:
        """A transfer that never became an intent — no capability, no caller."""
        self.record(AuditEvent.REFUSED, payer=payer, reason=reason, **extra)

    def roster_changed(self, action: str, actor: str, subject: str, **extra: Any) -> None:
        """Who granted or revoked whose access, and when.

        Not money, but the same character of evidence and the same rules: an
        investigation into a transfer needs to know who could reach the system
        that day, and a record of that which can be edited afterwards is not
        evidence. `actor` is the super-user who sent the command.
        """
        self.record(action, actor=actor, subject=subject, **extra)


def _intent_fields(intent: Any) -> dict[str, Any]:
    """The subset of a TransferIntent that belongs in the record.

    Taken field by field rather than by dumping the object, so a field added to
    `TransferIntent` later cannot silently start appearing in the audit file.
    """
    payee = getattr(intent, "payee", None)
    fields: dict[str, Any] = {
        "intent_id": getattr(intent, "intent_id", None),
        "payer": getattr(intent, "payer", None),
        "amount_paise": getattr(intent, "amount_paise", None),
        "status": str(getattr(intent, "status", "") or ""),
    }
    if payee is not None:
        fields["payee"] = getattr(payee, "phone", None)
        fields["payee_name"] = getattr(payee, "resolved_name", None)
        source = getattr(payee, "source", None)
        fields["payee_source"] = getattr(source, "value", None) or str(source or "")
        fields["payee_is_new"] = getattr(payee, "is_new", None)
    return fields


#: Anything whose name suggests a secret is dropped rather than written, so a
#: caller that passes one by mistake cannot put it in the permanent record.
_FORBIDDEN = ("pin", "pintwin", "otp", "password", "secret", "token", "auth_key")


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}
    for key, value in fields.items():
        if any(word in key.lower() for word in _FORBIDDEN):
            logger.error("Refused to write %r to the audit trail — it looks like a secret", key)
            continue
        if value is not None:
            cleaned[key] = value
    return cleaned


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
