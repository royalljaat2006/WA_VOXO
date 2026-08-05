"""Turn Meta's webhook JSON into typed messages we can act on.

The Cloud API wraps everything in four levels of nesting and mixes several
unrelated event kinds into the same endpoint:

    {"entry": [{"changes": [{"value": {
        "messages": [...],   # someone sent us something  ← what we want
        "statuses": [...],   # delivery/read receipts for messages WE sent
        "contacts": [...],
    }}]}]}

Status callbacks arrive far more often than real messages, so the parser
returns an empty list for them rather than treating them as input. Anything
malformed is skipped instead of raising: a webhook handler that 500s makes Meta
retry the same bad payload forever.

Phase 3 consumes `AudioMessage.media_id` to download and transcribe voice notes.
Voice payments consume `ContactMessage`, which is the one way a payee's number
reaches us without passing through speech recognition.

Group messages carry a `group_id`; 1:1 messages do not. That single field is the
discriminator the whole group path hangs off, and `reply_to` is the only address
a handler should ever answer — replying to `from_number` from a group would turn
a group answer into a surprise private message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    """Common fields on every message we receive."""

    message_id: str
    #: Sender's phone number in international format, no '+'. In a group this is
    #: the participant who spoke, not the group.
    from_number: str
    timestamp: str
    #: Set when the message came from a group. `None` means a 1:1 chat, which
    #: keeps every existing caller working unchanged.
    #:
    #: Keyword-only so that adding a defaulted field to this base class does not
    #: force a default onto every non-defaulted field in the subclasses below —
    #: which is what dataclass inheritance would otherwise demand.
    group_id: str | None = field(default=None, kw_only=True)

    @property
    def is_group(self) -> bool:
        return self.group_id is not None

    @property
    def reply_to(self) -> str:
        """Where a reply goes.

        A group reply addressed to the sender would arrive as a surprise private
        message, so this is the only address a handler should use.
        """
        return self.group_id or self.from_number


@dataclass(frozen=True)
class TextMessage(IncomingMessage):
    text: str


@dataclass(frozen=True)
class AudioMessage(IncomingMessage):
    media_id: str
    mime_type: str
    #: True for a recorded voice note; False for an attached audio file.
    is_voice_note: bool


@dataclass(frozen=True)
class SharedContact:
    """One entry from a shared contact card.

    `phone` is WhatsApp's own `wa_id` where it exists — a number the platform
    already normalised and verified, with no speech recognition anywhere near it.
    That is the whole point of accepting contact cards: it removes the digits
    from the error-prone path entirely.
    """

    phone: str
    name: str | None = None
    #: False when we had to fall back to the free-text `phone` field because the
    #: contact is not on WhatsApp. Still better than dictation, but not verified.
    is_wa_verified: bool = True


@dataclass(frozen=True)
class ContactMessage(IncomingMessage):
    """A shared contact card — the safest way to name a payee.

    More than one contact can be attached to a single message, so this holds a
    tuple. A payment flow must ask which one rather than picking, for the same
    reason payee names are never fuzzy-matched.
    """

    contacts: tuple[SharedContact, ...]

    @property
    def only(self) -> SharedContact | None:
        """The single contact, or None when the card is empty or ambiguous."""
        return self.contacts[0] if len(self.contacts) == 1 else None


@dataclass(frozen=True)
class UnsupportedMessage(IncomingMessage):
    """A kind we don't handle yet (image, document, location, ...).

    Kept rather than dropped so the handler can reply with something useful
    instead of leaving the sender staring at silence.
    """

    kind: str


def parse_webhook(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Extract every user-sent message from a webhook body.

    Returns an empty list for status callbacks, unknown shapes, and anything
    malformed — never raises.
    """
    messages: list[IncomingMessage] = []

    for entry in _as_list(payload.get("entry")):
        for change in _as_list(_get(entry, "changes")):
            value = _get(change, "value")
            if not isinstance(value, dict):
                continue
            for raw in _as_list(value.get("messages")):
                parsed = _parse_message(raw)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_message(raw: Any) -> IncomingMessage | None:
    if not isinstance(raw, dict):
        return None

    message_id = raw.get("id")
    from_number = raw.get("from")
    if not message_id or not from_number:
        return None

    common = {
        "message_id": message_id,
        "from_number": from_number,
        "timestamp": raw.get("timestamp", ""),
        # Meta puts the group on the message itself. Absent for a 1:1 chat,
        # which is the discriminator the whole group path hangs off.
        "group_id": _group_id(raw),
    }
    kind = raw.get("type", "unknown")

    if kind == "text":
        body = _get(raw, "text").get("body") if isinstance(_get(raw, "text"), dict) else None
        if body is None:
            return UnsupportedMessage(**common, kind="text")
        return TextMessage(**common, text=body)

    if kind == "audio":
        audio = _get(raw, "audio")
        if not isinstance(audio, dict) or not audio.get("id"):
            return UnsupportedMessage(**common, kind="audio")
        return AudioMessage(
            **common,
            media_id=audio["id"],
            mime_type=audio.get("mime_type", ""),
            # Present and true only for mic-recorded notes.
            is_voice_note=bool(audio.get("voice", False)),
        )

    if kind == "contacts":
        contacts = _parse_contacts(raw.get("contacts"))
        if not contacts:
            return UnsupportedMessage(**common, kind="contacts")
        return ContactMessage(**common, contacts=contacts)

    return UnsupportedMessage(**common, kind=kind)


def _parse_contacts(raw: Any) -> tuple[SharedContact, ...]:
    """Pull usable (phone, name) pairs out of a shared contact card.

    Meta's shape nests a list of phones inside a list of contacts, and either
    can be empty or missing fields. Entries without any number are dropped
    rather than kept as blanks — there is nothing to pay.
    """
    parsed: list[SharedContact] = []

    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue

        name = None
        name_block = _get(entry, "name")
        if isinstance(name_block, dict):
            name = name_block.get("formatted_name") or name_block.get("first_name")

        for phone_block in _as_list(entry.get("phones")):
            if not isinstance(phone_block, dict):
                continue
            # wa_id is already normalised to international digits by WhatsApp.
            # The free-text `phone` field is whatever the sender typed into
            # their address book, so it needs cleaning and cannot be trusted the
            # same way.
            wa_id = (phone_block.get("wa_id") or "").strip()
            if wa_id:
                parsed.append(SharedContact(phone=wa_id, name=name, is_wa_verified=True))
                break

            digits = "".join(c for c in str(phone_block.get("phone") or "") if c.isdigit())
            if digits:
                parsed.append(SharedContact(phone=digits, name=name, is_wa_verified=False))
                break

    return tuple(parsed)


def _group_id(raw: dict[str, Any]) -> str | None:
    """The group this message belongs to, if any.

    Meta has moved this field between shapes across versions, so both known
    spellings are accepted rather than picking one and being silently wrong on
    the other — an unrecognised group id makes a group message look like a 1:1,
    which is the failure that leaks a reply to the wrong place.
    """
    for key in ("group_id", "group"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            inner = value.get("id")
            if isinstance(inner, str) and inner:
                return inner

    context = raw.get("context")
    if isinstance(context, dict):
        inner = context.get("group_id")
        if isinstance(inner, str) and inner:
            return inner
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _get(container: Any, key: str) -> Any:
    return container.get(key) if isinstance(container, dict) else None
