"""Intercepts allowlist commands from super-users; everything else passes through.

A wrapper, like `GroupHandler`, so adding it changes nothing about the existing
behaviour for everybody else.

    /adduser 919812345678 viewer     ← super-user, 1:1, text     → handled here
    "aaj kitne campaign gaye?"       ← anything else             → inner handler

### Three restrictions, each for its own reason

**Text only.** A command is never taken from a voice note. Speech recognition
mangles digits routinely — that is why a dictated payee number has to be read
back before money moves — and a misheard digit here would grant access to a
number nobody chose. There is no read-back step for commands, so the input has to
be exact by construction.

**Never in a group.** The reply lists who has access to an internal eko system,
and a group is the wrong audience for that. It also means a command cannot be
run by someone quoting it into a group the super-user happens to be in.

**Super-users only, and silently.** A command from anyone else is passed through
as ordinary text rather than refused. Telling a caller "you are not a super-user"
confirms that the capability exists and that they found the right service — the
same reasoning as `UNKNOWN_NUMBER_REPLY`.
"""

from __future__ import annotations

import logging

from ..auth.admin_flow import AdminFlow, looks_like_command
from ..auth.policy import normalise_number
from .client import WhatsAppClient, WhatsAppError
from .handlers import MessageHandler
from .payloads import IncomingMessage, TextMessage

logger = logging.getLogger(__name__)


class AdminHandler(MessageHandler):
    def __init__(
        self,
        inner: MessageHandler,
        client: WhatsAppClient,
        flow: AdminFlow,
        superusers: frozenset[str] | set[str],
    ) -> None:
        self._inner = inner
        self._client = client
        self._flow = flow
        self._superusers = frozenset(normalise_number(n) for n in superusers)

    @property
    def inner(self) -> MessageHandler:
        return self._inner

    def warmup(self) -> None:
        self._inner.warmup()

    def handle(self, message: IncomingMessage) -> None:
        if not self._is_command_for_us(message):
            self._inner.handle(message)
            return

        outcome = self._flow.handle(message.from_number, message.text)
        if outcome.forward or outcome.reply is None:
            self._inner.handle(message)
            return

        logger.info("Admin command from %s", _mask(message.from_number))
        self._send(message, outcome.reply)

    # ------------------------------------------------------------ internals

    def _is_command_for_us(self, message: IncomingMessage) -> bool:
        return (
            isinstance(message, TextMessage)
            and not message.is_group
            and normalise_number(message.from_number) in self._superusers
            and looks_like_command(message.text)
        )

    def _send(self, message: IncomingMessage, text: str) -> None:
        try:
            self._client.send_text(message.reply_to, text)
        except WhatsAppError:
            # The change itself already happened and is in the audit trail; a
            # failed confirmation must not look like a failed command.
            logger.exception("Could not send admin reply to %s", _mask(message.from_number))


def _mask(number: str) -> str:
    return f"…{number[-4:]}" if len(number) > 4 else "…"
