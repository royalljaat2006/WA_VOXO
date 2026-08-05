"""Group behaviour: dormant by default, awake when summoned, asleep again after.

    @eko aaj kitne campaign gaye     ← wakes the group
    (bot answers)
    ...ordinary chatter...           ← ignored, and never downloaded
    (window expires)                 ← dormant again

A wrapper rather than a new handler in its own right: 1:1 messages pass through
untouched, so nothing about the existing behaviour changes by adding this.

### Why a group is dormant by default

Everything a Cloud API business is in a group with reaches its webhook, including
messages nobody addressed to it. Two reasons not to act on those:

- **Cost.** Transcribing every voice note in a group to discover it was not for
  us is a bill for nothing.
- **DPDP.** Processing and storing other people's speech because they happen to
  share a group with a bot is not something to do incidentally.

So a dormant group's voice notes are **not even downloaded**. Text is checked for
the wake word and otherwise dropped without being stored or logged.

### Payments do not happen in groups

This is the one place this file departs from the plan, and it departs on purpose.

The plan said only the group's creator may start a transfer. That limits *who*,
but the exposure is not about who — a PIN spoken as a voice note in a group is
audible to every participant, and stays in the chat for them to replay. The
creator's own PIN is the one being exposed.

So a payment request in a group is answered with "message me directly", and the
transfer happens in a 1:1 where the PIN has one listener. Reporting still works
in groups; only money moves out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .client import WhatsAppClient, WhatsAppError
from .handlers import MessageHandler
from .payloads import AudioMessage, IncomingMessage, TextMessage

logger = logging.getLogger(__name__)

#: Words that summon the bot. Matched case-insensitively anywhere in the text —
#: WhatsApp does not send a structured "mentions" field for Cloud API groups, so
#: a string match is the only signal available.
DEFAULT_WAKE_WORDS = ("@eko", "eko bot", "@bot", "हे eko", "एको")

#: Phrases that mean the user wants to send money. Matched only to redirect them
#: to a 1:1 chat — this is a routing hint, never an authorisation.
_PAYMENT_HINTS = (
    "bhej", "भेज", "transfer", "payment", "paisa", "पैसा", "paise", "पैसे",
    "send money", "pay ", "rupee", "rupay", "₹",
)


@dataclass
class GroupState:
    """One group's wakefulness.

    In memory and per process, like every other piece of state here. A restart
    puts every group back to sleep, which is the safe direction: the worst case
    is a user repeating "@eko".
    """

    awake_until: float = 0.0

    def is_awake(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) < self.awake_until


@dataclass
class GroupRegistry:
    #: How long a group stays awake after being summoned. Long enough for a
    #: follow-up ("aur kal ka?"), short enough that the bot is not quietly
    #: listening to a meeting.
    awake_for_s: float = 300.0
    _groups: dict[str, GroupState] = field(default_factory=dict)

    def wake(self, group_id: str) -> None:
        state = self._groups.setdefault(group_id, GroupState())
        state.awake_until = time.monotonic() + self.awake_for_s

    def is_awake(self, group_id: str) -> bool:
        state = self._groups.get(group_id)
        return state is not None and state.is_awake()

    def sleep(self, group_id: str) -> None:
        self._groups.pop(group_id, None)


class GroupHandler(MessageHandler):
    """Wraps any handler with group etiquette. 1:1 traffic passes straight through."""

    def __init__(
        self,
        inner: MessageHandler,
        client: WhatsAppClient,
        registry: GroupRegistry | None = None,
        wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
        allow_payments_in_groups: bool = False,
    ) -> None:
        self._inner = inner
        self._client = client
        self._registry = registry if registry is not None else GroupRegistry()
        self._wake_words = tuple(word.lower() for word in wake_words)
        # Present so the decision is visible and reversible rather than buried,
        # but the default is the only setting that keeps a spoken PIN private.
        self._allow_payments = allow_payments_in_groups

    @property
    def inner(self) -> MessageHandler:
        return self._inner

    def warmup(self) -> None:
        self._inner.warmup()

    def handle(self, message: IncomingMessage) -> None:
        if not message.is_group:
            self._inner.handle(message)
            return

        group_id = message.group_id or ""

        if isinstance(message, TextMessage):
            self._handle_group_text(message, group_id)
            return

        if isinstance(message, AudioMessage):
            # A dormant group's audio is not downloaded at all. Deciding after
            # transcription would mean paying for, and processing, speech nobody
            # addressed to us.
            if not self._registry.is_awake(group_id):
                logger.debug("Ignoring audio in dormant group %s", group_id)
                return
            self._inner.handle(message)
            return

        # Contact cards and everything else are only acted on while awake.
        if self._registry.is_awake(group_id):
            self._inner.handle(message)

    # ------------------------------------------------------------ internals

    def _handle_group_text(self, message: TextMessage, group_id: str) -> None:
        summoned = self._is_summons(message.text)

        if not summoned and not self._registry.is_awake(group_id):
            # Dropped without being stored or logged. Someone else's group
            # conversation is not ours to keep.
            return

        if summoned:
            self._registry.wake(group_id)

        if not self._allow_payments and _looks_like_payment(message.text):
            logger.info("Payment request in group %s — redirected to 1:1", group_id)
            self._send(
                group_id,
                "Paise bhejne ke liye mujhe direct message kijiye. Group mein PIN "
                "bolna sabko sunai dega — isliye transfer sirf private chat mein "
                "hota hai. Reports yahin puch sakte hain.",
            )
            return

        self._inner.handle(_without_wake_word(message, self._wake_words))

    def _is_summons(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(word in lowered for word in self._wake_words)

    def _send(self, group_id: str, text: str) -> None:
        try:
            self._client.send_group_text(group_id, text)
        except WhatsAppError:
            logger.exception("Could not reply to group %s", group_id)


def _looks_like_payment(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in _PAYMENT_HINTS)


def _without_wake_word(message: TextMessage, wake_words: tuple[str, ...]) -> TextMessage:
    """Strip the summons before the model sees it.

    "@eko aaj kitne campaign gaye" should reach the agent as the question it is.
    Left in, the model spends attention deciding whether "@eko" is part of it.
    """
    text = message.text
    for word in wake_words:
        index = text.lower().find(word)
        if index != -1:
            text = (text[:index] + text[index + len(word):]).strip(" ,:—-")
            break
    return TextMessage(
        message_id=message.message_id,
        from_number=message.from_number,
        timestamp=message.timestamp,
        group_id=message.group_id,
        text=text or message.text,
    )
