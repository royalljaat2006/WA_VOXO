"""Logging configuration.

Two formats, because logs are read by different things in different places:

- **text** for a terminal, where a human is watching it scroll.
- **json** for a hosting platform, where a log aggregator needs to filter and
  search structured fields.

Also installs a redaction filter. This project's logs pass through phone numbers,
PINs and, if anything is careless, access tokens — all worse in a log aggregator
than anywhere else, because that is the one place data gets retained by default
and shared widely.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import traceback
from collections import deque
from datetime import UTC, datetime
from typing import Any

#: Bearer tokens and Meta access tokens. Meta's start with "EAA".
_TOKEN = re.compile(r"\b(EAA[A-Za-z0-9_\-]{10,}|sk-[A-Za-z0-9_\-]{10,})\b")
#: 10–15 consecutive digits: a phone number in any of the formats we handle.
_PHONE = re.compile(r"\b(\d{6})(\d{4,9})\b")
#: Digits following a PIN-ish word, in any of the languages we accept. The real
#: protection is that `PaymentIntentHandler` never passes a PIN to a logger at
#: all; this is the backstop for a careless call site added later, and for
#: third-party libraries logging a payload we handed them.
_PIN_CONTEXT = re.compile(
    r"((?:pin|passcode|otp|पिन)\W{0,4})([\d०-९](?:[\s\-]?[\d०-९]){3,7})",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Mask credentials and PINs entirely, phone numbers down to their last four.

    Applied to the formatted message, so it catches numbers that arrive inside an
    f-string or a third-party library's log line, not just our own call sites.

    Order matters: PIN context is masked before the phone rule runs, so a spoken
    PIN long enough to look like a phone number is still fully removed rather
    than partially revealed.
    """
    text = _TOKEN.sub("[REDACTED-TOKEN]", text)
    text = _PIN_CONTEXT.sub(lambda m: m.group(1) + "[REDACTED-PIN]", text)
    return _PHONE.sub(lambda m: "…" + m.group(2)[-4:], text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render args in now, then redact — otherwise a number passed as a
        # lazy %s argument slips through unredacted.
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, which is what log aggregators expect."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records in memory, for the `/ops` activity page.

    A log *file* only exists because whatever started the process happened to
    redirect stdout there (`nohup ... > uvicorn.log`) — that is an operator
    choice, not something code should assume. This handler makes "what has this
    process logged recently" available from inside the process itself, so the
    ops page works the same way locally, under `--reload`, or behind any
    deployment's own redirection.

    Not given its own `RedactingFilter`: handlers on one logger share the same
    `LogRecord` instance, and the stdout handler's filter (added first, in
    `configure`) already mutates `record.msg` in place before this handler's
    `emit` ever sees it.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        with self._lock:
            self._buffer.append(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy, oldest first — safe to iterate while logging continues."""
        with self._lock:
            return list(self._buffer)


#: Set by `configure`. Module-level because the ops page has no other way to
#: reach "whatever handler is currently installed on the root logger" —
#: mirrors how `logging.getLogger()` itself is a module-level singleton.
_ring_buffer: RingBufferHandler | None = None


def get_ring_buffer() -> RingBufferHandler | None:
    """The in-memory activity buffer, or None before `configure` has run."""
    return _ring_buffer


def configure(level: str = "INFO", fmt: str = "text") -> None:
    """Install handlers on the root logger. Idempotent.

    Called once at startup by `tools/run_server.py` and by the container
    entrypoint. Safe to call twice — existing handlers are replaced rather than
    added to, so a reload does not double every line.
    """
    global _ring_buffer
    root = logging.getLogger()
    root.setLevel(level.upper())

    for existing in root.handlers[:]:
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter() if fmt.lower() == "json"
        else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)

    # Added after the stdout handler, so redaction has already happened to the
    # shared LogRecord by the time this one's emit() runs — see the class
    # docstring.
    _ring_buffer = RingBufferHandler()
    root.addHandler(_ring_buffer)

    # uvicorn installs its own handlers; route them through ours so access logs
    # get redacted too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # httpx logs the full request URL at INFO, which for media downloads includes
    # a signed Meta URL. Not useful, and not something to retain.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
