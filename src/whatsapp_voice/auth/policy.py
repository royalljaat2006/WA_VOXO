"""Who is allowed to ask, and what they are allowed to see.

The threat model, stated plainly so the limits are not mistaken for security:

WhatsApp guarantees the sender's phone number is genuine — Meta verified it, and it
cannot be spoofed on an inbound message. So "which number sent this" is
trustworthy. What it does **not** tell us is who is holding that phone. A borrowed,
stolen, or unlocked handset is indistinguishable from its owner.

That is why reporting is read-only. Number-based identity is strong enough to
decide "may this person see a delivery report", and not strong enough to authorise
anything that changes state or moves money.

Payments therefore sit on a **separate axis** from `Role`, not on top of it.
Reading every report and moving money are different powers, and an `admin` who can
see everything still cannot send a rupee unless the allowlist says `admin+pay`.
Even then the number alone does not authorise the transfer — a PIN does. The flag
decides who is allowed to *try*.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_NON_DIGITS = re.compile(r"\D")


def normalise_number(raw: str) -> str:
    """Strip everything but digits so '+91 98765-43210' matches '919876543210'.

    Numbers arrive from three places that format differently — Meta's webhook, a
    hand-edited `.env`, and whatever a colleague pastes into chat. Comparing them
    raw is a silent source of "why am I denied".
    """
    return _NON_DIGITS.sub("", raw or "")


class Role(Enum):
    """What a caller may see. Ordered from least to most access."""

    #: Aggregate numbers only — how many campaigns, overall delivery rates.
    VIEWER = "viewer"
    #: Everything a viewer sees, plus per-campaign detail.
    ANALYST = "analyst"
    #: All reporting data. Still read-only — there is no write role by design.
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def allows(self, required: Role) -> bool:
        return self.rank >= required.rank


_ROLE_RANK = {Role.VIEWER: 0, Role.ANALYST: 1, Role.ADMIN: 2}


@dataclass(frozen=True)
class Caller:
    """An authorised caller."""

    number: str
    role: Role
    #: Whether this number may initiate a transfer at all. Off unless the
    #: allowlist says so explicitly — a role is about what you may *see*, and a
    #: typo in the role name must never hand out the ability to move money.
    can_pay: bool = False

    def may(self, required: Role) -> bool:
        return self.role.allows(required)


#: Who the current message is from, for code too far from the transport to be
#: passed a `Caller` directly — specifically the payment tools, which must refuse
#: a caller without `can_pay`.
#:
#: A ContextVar rather than an attribute, for the same reason as
#: `agent.payments.current_payer`: handlers are shared across concurrent
#: background tasks, and an attribute set by one caller would be read by another.
#: None means no verified caller, which the payment path treats as "not allowed".
current_caller: ContextVar[Caller | None] = ContextVar("current_caller", default=None)


class Decision(Enum):
    ALLOW = "allow"
    #: Number is not on the allowlist.
    UNKNOWN_NUMBER = "unknown_number"
    #: Known number, but asking too often.
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class AuthResult:
    decision: Decision
    caller: Caller | None = None
    #: What to tell the user. Deliberately vague for an unknown number — see below.
    message: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class AccessPolicy:
    """The allowlist and role map.

    Parsed from a single env var so there is no database to stand up for a demo:

        WHATSAPP_ALLOWED_NUMBERS=919876543210:admin+pay,919000000001:viewer

    A bare number with no role gets the default (viewer) — least privilege, so a
    typo in the config cannot silently grant admin. The `+pay` suffix is the only
    way to permit a transfer, and it is deliberately ugly: nobody adds it by
    accident.
    """

    def __init__(self, entries: dict[str, Role] | None = None,
                 default_role: Role = Role.VIEWER,
                 payers: set[str] | None = None) -> None:
        self._entries = {normalise_number(n): r for n, r in (entries or {}).items()}
        self._default_role = default_role
        self._payers = {normalise_number(n) for n in (payers or set())}

    @classmethod
    def parse(cls, spec: str, default_role: Role = Role.VIEWER) -> AccessPolicy:
        """Build from the `number:role,number:role` env-var form.

        An unparseable role is a hard error rather than a silent downgrade: a
        deployment that mistypes `analsyt` should fail loudly at startup, not
        quietly hand out less access than intended.
        """
        entries: dict[str, Role] = {}
        payers: set[str] = set()

        for chunk in (spec or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue

            number, _, role_name = chunk.partition(":")
            number = normalise_number(number)
            if not number:
                raise ValueError(f"Entry {chunk!r} has no phone number")

            role_name = role_name.strip().lower()
            # "+pay" is a capability, not a role — see the module docstring.
            if role_name.endswith("+pay"):
                payers.add(number)
                role_name = role_name[: -len("+pay")].rstrip("+").strip()

            if not role_name:
                entries[number] = default_role
                continue

            try:
                entries[number] = Role(role_name)
            except ValueError as exc:
                valid = ", ".join(r.value for r in Role)
                raise ValueError(
                    f"Unknown role {role_name!r} for {number}. Valid roles: {valid} "
                    "(optionally suffixed '+pay' to permit transfers)"
                ) from exc

        return cls(entries, default_role=default_role, payers=payers)

    def lookup(self, number: str) -> Caller | None:
        number = normalise_number(number)
        role = self._entries.get(number)
        if role is None:
            return None
        return Caller(number=number, role=role, can_pay=number in self._payers)

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def __len__(self) -> int:
        return len(self._entries)


class FileBackedAccessPolicy:
    """An allowlist that reloads from a file when it changes.

    For a team that grows or turns over often, editing `WHATSAPP_ALLOWED_NUMBERS`
    in `.env` means a redeploy per person. This instead re-parses the file only
    when its mtime moves, so adding or removing a number takes effect on the
    *next* message — no restart, and no cost on the common case where nothing
    changed since the last check.

    Same `number:role` / `+pay` syntax as `AccessPolicy.parse`, one entry per
    line. Blank lines and anything after a `#` are ignored, so the file can
    carry a name/reason next to each number:

        918287151832:admin+pay   # Priya, ops lead
        919000000001             # bare number -> default_role

    Duck-types `AccessPolicy` (`lookup`, `is_empty`, `__len__`) so `AccessGuard`
    does not need to know which kind of policy it was given.
    """

    def __init__(self, path: str, default_role: Role = Role.VIEWER) -> None:
        self._path = Path(path)
        self._default_role = default_role
        self._mtime: float | None = None
        self._policy = AccessPolicy()

    def _reload_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._mtime is not None:
                logger.warning(
                    "Allowlist file %s is now unreadable — keeping the last "
                    "known list rather than locking everyone out", self._path,
                )
            return

        if mtime == self._mtime:
            return

        lines = (
            line.split("#", 1)[0].strip()
            for line in self._path.read_text().splitlines()
        )
        spec = ",".join(line for line in lines if line)
        self._policy = AccessPolicy.parse(spec, default_role=self._default_role)
        self._mtime = mtime
        logger.info("Reloaded %s from %s (%d number(s))",
                     type(self).__name__, self._path, len(self._policy))

    def lookup(self, number: str) -> Caller | None:
        self._reload_if_changed()
        return self._policy.lookup(number)

    @property
    def is_empty(self) -> bool:
        self._reload_if_changed()
        return self._policy.is_empty

    def __len__(self) -> int:
        self._reload_if_changed()
        return len(self._policy)
