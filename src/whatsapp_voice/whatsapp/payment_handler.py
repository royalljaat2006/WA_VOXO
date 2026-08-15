"""Voice-initiated payment over WhatsApp.

Extends `AgentHandler` because the front half is unchanged — fetch, convert,
transcribe, then let the model work out what was asked. What this handler adds is
a gate in front of the model:

    voice note ──► transcript ──┬── PaymentFlow ──► receipt / retry / cancel
                                └── forward ─────► agent ──► prepare_transfer

The decision itself lives in `agent/payment_flow.py`, transport-free, so the demo
API runs the identical logic. This file is only the WhatsApp end of it: pull the
sender out of the message, keep the PIN out of the log, send the reply.

### Why a gate instead of a tool

A PIN is a secret. It must not enter the model's context, the conversation
history, or the logs — three places it would otherwise land automatically. So the
transcript is inspected *before* anything else touches it, and when it turns out
to be a PIN it is consumed and never propagates.

That is also why there is no `execute_transfer` tool: the only code path that
moves money starts with a PIN this handler intercepted, and the model has no way
to reach it. A prompt injected into a transcript can, at worst, prepare an intent
the user is then asked to confirm out loud.
"""

from __future__ import annotations

import logging

from ..agent import Agent
from ..agent.payment_flow import PaymentFlow
from ..agent.payments import ContactBook, PaymentGateway, PendingIntents, current_payer
from ..audit import AuditTrail
from ..config import PaymentSettings
from ..stt import Transcriber
from ..stt.base import Transcript
from .client import WhatsAppClient
from .handlers import AgentHandler
from .payloads import (
    AudioMessage,
    ContactMessage,
    IncomingMessage,
    TextMessage,
    UnsupportedMessage,
)

logger = logging.getLogger(__name__)


class PaymentHandler(AgentHandler):
    """AgentHandler plus the payment confirmation loop."""

    def __init__(
        self,
        client: WhatsAppClient,
        transcriber: Transcriber,
        agent: Agent,
        gateway: PaymentGateway,
        pending: PendingIntents,
        settings: PaymentSettings,
        contacts: ContactBook | None = None,
        audit: AuditTrail | None = None,
        echo_transcript: bool = True,
        settlements: object = None,
    ) -> None:
        super().__init__(client, transcriber, agent, echo_transcript)
        self._flow = PaymentFlow(gateway, pending, settings, audit, settlements)
        self._contacts = contacts if contacts is not None else ContactBook()

    # ------------------------------------------------------------- routing

    def handle(self, message: IncomingMessage) -> None:
        if isinstance(message, AudioMessage):
            self._handle_audio(message)
            return

        if isinstance(message, TextMessage):
            self._route(message, message.text, None)
            return

        if isinstance(message, ContactMessage):
            self._handle_contact(message)
            return

        kind = message.kind if isinstance(message, UnsupportedMessage) else "message"
        self._reply(
            message,
            f"Sorry, I can't handle a {kind} yet. Send a voice note or type your question.",
        )

    def _handle_contact(self, message: ContactMessage) -> None:
        """A shared contact card — the safest way to name a payee.

        The number is recorded as WhatsApp-verified and then described to the
        model as an ordinary turn, so the conversation carries on normally
        ("kitna bhejna hai?"). What changes is that when the model later resolves
        this number, the contact book says the digits never went through ASR, so
        no read-back is asked for.
        """
        payer = message.from_number
        current_payer.set(payer)

        if not message.contacts:
            self._reply(message, "Us contact mein koi number nahi mila. Dobara bhejiye?")
            return

        for contact in message.contacts:
            self._contacts.remember(payer, contact.phone, contact.name)

        if message.only is None:
            # Several contacts at once. Picking one would be exactly the kind of
            # confident guess the rest of this flow exists to avoid.
            names = ", ".join(c.name or c.phone for c in message.contacts)
            self._reply(
                message,
                f"Aapne {len(message.contacts)} contact bheje — {names}. "
                "Kis ko bhejna hai?",
            )
            return

        contact = message.only
        described = contact.name or contact.phone
        logger.info("Contact card from %s: %s (wa_verified=%s)",
                    payer, contact.phone, contact.is_wa_verified)

        self._answer(
            message,
            f"[The user shared a contact card: {described}, number {contact.phone}. "
            "Use this number as the payee. Ask how much they want to send.]",
        )

    def _on_transcript(
        self, message: AudioMessage, transcript: str, language: str | None = None
    ) -> None:
        self._route(message, transcript, language)

    def _log_transcript(self, message: AudioMessage, result: Transcript) -> None:
        """Suppress the transcript while a payment is awaiting a PIN.

        In that window the next voice note is very likely to be nothing but the
        PIN, and by the time we know for certain, it has already been written to
        the log. So the check is on the window, not on the content.
        """
        if self._flow.awaiting_pin(message.from_number):
            logger.info(
                "Transcribed %s in %.1fs [%s]: <withheld — payment awaiting PIN>",
                message.message_id, result.latency_s, result.language or "?",
            )
            return
        super()._log_transcript(message, result)

    def _low_yield_reply(self, message: AudioMessage, text: str) -> str:
        """Never echo a partial transcript back to the user while a PIN could
        be mid-flight.

        This runs from `_handle_audio`, *before* `_on_transcript`/`_route` —
        the only point in the whole flow that decides whether `text` is a
        PIN — ever sees this message. Without this override, a spoken PIN
        that happened to transcribe short (quiet, quick, background noise —
        exactly what "low yield" means) would be read straight back to the
        user in the reply itself: "sirf itna suna: '1234'". Same reasoning as
        `_log_transcript`, applied to an outgoing message instead of a log
        line — the check is on the window, not on whether this specific
        fragment looks PIN-shaped.
        """
        if self._flow.awaiting_pin(message.from_number):
            return "Awaaz saaf nahi aayi. Thoda dobara boliye?"
        return super()._low_yield_reply(message, text)

    def _route(self, message: IncomingMessage, text: str, language: str | None) -> None:
        # The individual sender, never the group — money comes out of a person's
        # account, and a group has none.
        payer = message.from_number
        # Set here, at the one point where a verified sender is known, rather
        # than threaded down through the agent.
        current_payer.set(payer)

        # Whether the echo is safe depends on the state *before* the flow runs —
        # afterwards the intent has already been cleared.
        was_awaiting = self._flow.awaiting_pin(payer)
        outcome = self._flow.handle(payer, text)

        if outcome.reply is not None:
            self._reply(message, outcome.reply)
            return

        prefix = outcome.prefix
        if self._echo_transcript and language is not None and not was_awaiting:
            # Only voice notes carry a language, and only a message that was
            # never a PIN candidate is safe to quote back.
            prefix = f"🎤 “{text}”\n\n{prefix}"

        self._answer(message, text, prefix=prefix, language=language)
