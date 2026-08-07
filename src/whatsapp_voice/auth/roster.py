"""The write side of the allowlist file that `FileBackedAccessPolicy` reads.

Separate class, and a separate import, because reading and writing an allowlist
are different privileges. Nothing in the request path holds a `Roster`; only the
admin command gate does.

### What this refuses to do

**It never writes `+pay`.** Granting the ability to move money is deliberately
not reachable from a WhatsApp message — see `auth/admin_flow.py` for the
reasoning. `add` raises rather than silently dropping the suffix, so a caller
that tries cannot mistake refusal for success.

**Revoking is always allowed**, including for a `+pay` entry. Removing access is
the fail-safe direction: during an incident — a lost handset, someone leaving —
the useful capability is being able to cut access from a phone, immediately,
without finding an SSH session first.

### Writes are atomic

`FileBackedAccessPolicy` re-reads this file from another thread whenever its
mtime moves. A partial write would be read as a *shorter allowlist* — silently
locking people out, or worse, mid-line garbage that fails to parse. So a change
is written to a temporary file in the same directory and then `os.replace`d over
the original, which is atomic on POSIX.

Comments and ordering in the file are preserved, because a human maintains it
too and their notes about who each number belongs to are the only thing making a
list of bare digits readable.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from .policy import Role, normalise_number

logger = logging.getLogger(__name__)

#: The capability suffix this class will not write. Kept as a constant so the
#: refusal and the parser cannot drift apart.
PAY_SUFFIX = "+pay"


class RosterError(RuntimeError):
    """A change was refused. The message is written for the person who asked."""


class Roster:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        # Handlers run in background threads; two interleaved read-modify-writes
        # would lose one of the two changes.
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> dict[str, str]:
        """number -> role spec, e.g. {"918287151832": "admin+pay"}.

        Reads the file directly rather than going through the policy, because a
        listing should show what is actually on disk right now.
        """
        found: dict[str, str] = {}
        for line in self._read_lines():
            parsed = _parse_entry(line)
            if parsed is not None:
                number, spec = parsed
                found[number] = spec
        return found

    def add(self, number: str, role: Role, *, note: str = "") -> str:
        """Add or update an entry. Returns the normalised number.

        Raises `RosterError` for anything that is not a usable mobile number, and
        for any attempt to grant `+pay`.
        """
        digits = normalise_number(number)
        if len(digits) < 10:
            raise RosterError(f"'{number}' sahi mobile number nahi lag raha.")

        with self._lock:
            existing = self.entries().get(digits)
            if existing and existing.endswith(PAY_SUFFIX):
                # Rewriting it here would silently strip a capability someone
                # granted deliberately on the server. Say so instead.
                raise RosterError(
                    f"{digits} ke paas already payment permission hai. Ise badalne "
                    "ke liye server par file edit karni padegi."
                )

            line = f"{digits}:{role.value}"
            if note:
                line += f"   # {note}"

            lines = [
                existing_line for existing_line in self._read_lines()
                if _number_of(existing_line) != digits
            ]
            lines.append(line)
            self._write_lines(lines)

        logger.info("Roster: %s set to %s", digits, role.value)
        return digits

    def remove(self, number: str) -> str:
        """Remove an entry, whatever its role or capability. Returns the number."""
        digits = normalise_number(number)
        if not digits:
            raise RosterError(f"'{number}' sahi mobile number nahi lag raha.")

        with self._lock:
            lines = self._read_lines()
            kept = [line for line in lines if _number_of(line) != digits]
            if len(kept) == len(lines):
                raise RosterError(f"{digits} list mein nahi hai.")
            self._write_lines(kept)

        logger.info("Roster: %s removed", digits)
        return digits

    # ------------------------------------------------------------ internals

    def _read_lines(self) -> list[str]:
        try:
            return self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _write_lines(self, lines: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory, so the replace below is a rename within one filesystem
        # rather than a copy that could be observed half-done.
        fd, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines).rstrip("\n") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise


def _parse_entry(line: str) -> tuple[str, str] | None:
    """`"918287151832:admin+pay  # Priya"` -> `("918287151832", "admin+pay")`."""
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return None
    number, _, spec = stripped.partition(":")
    digits = normalise_number(number)
    if not digits:
        return None
    return digits, spec.strip().lower()


def _number_of(line: str) -> str | None:
    parsed = _parse_entry(line)
    return parsed[0] if parsed else None
