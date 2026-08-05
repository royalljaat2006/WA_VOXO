"""The payment confirmation loop, with no transport in it.

Written once and shared by `PaymentHandler` (WhatsApp) and the demo API, because
this is the code that decides whether a message is a PIN — and a second copy of
that decision is a second chance to get it wrong.

    message ──► PaymentFlow.handle()
                  ├── pending intent + PIN        → execute, return a receipt
                  ├── pending intent + "cancel"   → drop it
                  ├── pending intent + anything   → drop it, forward the message
                  └── no pending intent           → forward the message

"Forward" means hand the text to the agent. Nothing that could be a PIN is ever
forwarded, which is the whole point of putting this in front of the model rather
than behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..audit import AuditTrail
from ..config import PaymentSettings
from .parsing import extract_pin, format_amount
from .payments import IntentStatus, PaymentGateway, PendingIntents, TransferIntent

logger = logging.getLogger(__name__)

#: Anything that means "stop". Matched against the whole normalised message, not
#: searched within it, so "cancel karke 500 Ramesh ko bhejo" is not a cancel — it
#: is a new instruction, and gets forwarded.
_CANCEL_PHRASES = frozenset({
    "cancel", "cancel karo", "cancel kar do", "stop", "no", "nahi", "nahin",
    "rehne do", "rahne do", "chhodo", "chodo", "band karo", "ruko", "mat bhejo",
    "नहीं", "रहने दो", "रुको", "बंद करो", "मत भेजो", "कैंसिल",
})


@dataclass(frozen=True)
class FlowOutcome:
    """What the caller should do with this message.

    Exactly one of `reply` and `forward` is meaningful: either the flow already
    answered, or the agent still has to.
    """

    #: Send this text and stop. None means the flow had nothing to say.
    reply: str | None = None
    #: Hand the message to the agent.
    forward: bool = False
    #: Prepended to whatever the agent answers. Used to mention a cancellation
    #: without making it a separate message.
    prefix: str = ""


class PaymentFlow:
    def __init__(
        self,
        gateway: PaymentGateway,
        pending: PendingIntents,
        settings: PaymentSettings,
        audit: AuditTrail | None = None,
    ) -> None:
        self._gateway = gateway
        self._pending = pending
        self._settings = settings
        self._audit = audit if audit is not None else AuditTrail(None)

    @property
    def pending(self) -> PendingIntents:
        return self._pending

    def awaiting_pin(self, payer: str) -> bool:
        """True while the next message from `payer` might be a secret.

        Callers use this to suppress logging and echoing during the window,
        rather than deciding after the fact — by then it has already been
        written down.
        """
        return self._pending.get(payer) is not None

    def handle(self, payer: str, text: str) -> FlowOutcome:
        intent = self._pending.get(payer)
        if intent is None:
            # A message that is nothing but digits is treated as a secret even
            # with no transfer pending. Making this conditional on our own state
            # being correct is what leaks: a model that says "PIN bolo" without
            # having called prepare_transfer leaves nothing pending, and the
            # user's next utterance — their PIN — would be forwarded straight
            # into the model's context. Observed in a real run, not theoretical.
            if extract_pin(text, self._settings.pin_lengths) is not None:
                logger.info("PIN-shaped message with no pending transfer — dropped")
                return FlowOutcome(reply=(
                    "Abhi koi transfer pending nahi hai, isliye maine wo nazarandaz "
                    "kar diya. Dobara boliye — kise aur kitna bhejna hai?"
                ))
            return FlowOutcome(forward=True)

        if _is_cancel(text):
            self._pending.clear(payer)
            logger.info("Intent %s cancelled by user", intent.intent_id)
            self._audit.cancelled(intent, reason="user_cancelled")
            return FlowOutcome(reply="Transfer cancel kar diya. ❌")

        pin = extract_pin(text, self._settings.pin_lengths)
        if pin is not None:
            # From here to the end of _confirm, `pin` must not be logged,
            # stored, echoed back, or passed to the agent.
            return self._confirm(intent, pin)

        # Something else entirely while a transfer was waiting. The intent is
        # dropped rather than left open: a PIN spoken later would otherwise
        # confirm a transfer the user has stopped thinking about.
        self._pending.clear(payer)
        logger.info("Intent %s dropped — user changed subject", intent.intent_id)
        self._audit.cancelled(intent, reason="user_changed_subject")
        return FlowOutcome(forward=True, prefix="Pichhla transfer cancel ho gaya.\n\n")

    # ------------------------------------------------------------ internals

    def _confirm(self, intent: TransferIntent, pin: str) -> FlowOutcome:
        intent.attempts += 1
        result = self._gateway.confirm_and_execute(intent, pin)
        del pin  # nothing below this line needs it

        if result.ok:
            intent.status = IntentStatus.COMPLETED
            intent.reference = result.reference
            self._pending.clear(intent.payer)
            logger.info(
                "Intent %s completed, ref=%s, amount_paise=%d",
                intent.intent_id, result.reference, intent.amount_paise,
            )
            # The other half of the pair, written before the user is told.
            self._audit.executed(
                intent, gateway=type(self._gateway).__name__, reference=result.reference
            )
            return FlowOutcome(reply=receipt(intent, self._gateway))

        remaining = self._settings.max_pin_attempts - intent.attempts
        if remaining <= 0:
            intent.status = IntentStatus.FAILED
            self._pending.clear(intent.payer)
            logger.warning("Intent %s cancelled after %d wrong PINs",
                           intent.intent_id, intent.attempts)
            self._audit.failed(
                intent, gateway=type(self._gateway).__name__, reason="pin_attempts_exhausted"
            )
            return FlowOutcome(reply=(
                f"❌ PIN {intent.attempts} baar galat. Transfer cancel ho gaya. "
                "Dobara shuru karne ke liye phir se boliye."
            ))

        logger.info("Intent %s — wrong PIN, %d attempt(s) left",
                    intent.intent_id, remaining)
        self._audit.pin_failed(intent, attempts_left=remaining)
        return FlowOutcome(reply=(
            f"❌ PIN galat hai. {remaining} koshish baaki — PIN dobara boliye."
        ))


# --------------------------------------------------------------- helpers

def _is_cancel(text: str) -> bool:
    normalised = " ".join((text or "").lower().split()).strip(" .!?।")
    return normalised in _CANCEL_PHRASES


def receipt(intent: TransferIntent, gateway: PaymentGateway) -> str:
    """The confirmation the user keeps.

    Deliberately close to language-neutral: an amount, an arrow, a name, a
    reference. Numbers and a name read the same in every script, so there is no
    translation step and therefore no chance of a translated receipt disagreeing
    with what was actually sent.

    Built here rather than by the model, because a receipt is the one message in
    the flow that has to be exactly true.
    """
    lines = [
        f"✅ ₹{format_amount(intent.amount_paise)} → {intent.payee.display}",
        f"Ref: {intent.reference}",
    ]
    if getattr(gateway, "is_stub", False):
        # Impossible to mistake a rehearsal for the real thing.
        lines.append("")
        lines.append("⚠️ TEST MODE — koi asli paisa transfer nahi hua.")
    return "\n".join(lines)
