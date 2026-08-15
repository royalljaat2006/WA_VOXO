"""Where per-conversation state lives, and why it cannot be a dict.

Five things in this codebase remember something between messages: seen message
ids, pending transfers, settlements awaiting eko's answer, shared contacts,
group wakefulness and conversation history. A dict on an object is correct for
one process and wrong for two.

The failures are not equally bad, and that ordering decided what got migrated
first:

    PendingIntents    worker A prepares the transfer, worker B gets the PIN and
                      finds nothing pending. The payment simply does not work.
                      Store-backed — see `agent/payments.py::PendingIntents`.
    SettlementQueue   a transfer eko accepted is never followed up on if the
                      worker tracking it restarts. Store-backed — see
                      `agent/settlement.py`.
    SeenMessages      Meta's retry lands on a different worker, which has never
                      seen the id. The user gets two replies to one voice note.
                      Store-backed — see `whatsapp/app.py`.
    ─────────────── everything below degrades rather than breaks, and is
                     still plain in-process state ───────────────
    ContactBook       a shared contact is not recognised, so the digits get
                      read back unnecessarily. Annoying, and safe.
    GroupRegistry     a woken group looks dormant to another worker.
    Conversations     a follow-up loses its thread. Noted in
                      docs/phase-6-deploy.md as a deployment prerequisite.

So `KeyValueStore` is a small interface over exactly what those need — get, set
with a TTL, delete, and an atomic "claim this id" for deduplication. Not a cache
abstraction, and not a database: the smallest thing that makes the top three
correct across processes.

`MemoryStore` is the default and behaves identically to the dicts it replaces.
`RedisStore` is what a deployment with more than one worker must configure —
and `guard_worker_count` refuses to start without it, because the alternative is
a bot that replies twice and loses PINs while looking perfectly healthy.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class KeyValueStore(Protocol):
    """The state operations this project actually performs."""

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def claim(self, key: str, ttl_s: float) -> bool:
        """Atomically take ownership of `key`. True the first time only.

        This is deduplication, and it has to be one operation. A get-then-set
        would let two workers both find a message id absent and both reply — the
        exact duplicate this exists to prevent.
        """
        ...

    def incr(self, key: str, ttl_s: float) -> int:
        """Increment and return the new count, setting a TTL on first write.

        Rate limiting, where a read-modify-write across workers would let the
        limit be exceeded by however many workers there are.
        """
        ...


class MemoryStore(KeyValueStore):
    """In-process state. Correct for one worker, and the default.

    Thread-safe because handlers run in background threads: two of them
    incrementing the same counter without a lock would lose one.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, float | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires = entry
            if expires is not None and time.monotonic() > expires:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        with self._lock:
            self._data[key] = (value, None if ttl_s is None else time.monotonic() + ttl_s)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def claim(self, key: str, ttl_s: float) -> bool:
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                _, expires = entry
                if expires is None or time.monotonic() <= expires:
                    return False
            self._data[key] = (True, time.monotonic() + ttl_s)
            return True

    def incr(self, key: str, ttl_s: float) -> int:
        with self._lock:
            entry = self._data.get(key)
            expired = entry is not None and entry[1] is not None and time.monotonic() > entry[1]
            if entry is None or expired:
                self._data[key] = (1, time.monotonic() + ttl_s)
                return 1
            count = int(entry[0]) + 1
            self._data[key] = (count, entry[1])
            return count

    def __len__(self) -> int:
        return len(self._data)


class RedisStore(KeyValueStore):
    """State shared across workers.

    Values are JSON, not pickle: an attacker who can write to Redis should not
    thereby be able to execute code in the payment process, and JSON keeps the
    contents readable when someone is trying to work out what went wrong.

    Keys are namespaced so this can share a Redis with anything else.
    """

    def __init__(self, url: str, namespace: str = "wa-voice", client: Any = None) -> None:
        if client is None:
            import redis  # imported here so redis is optional unless used

            client = redis.Redis.from_url(url, decode_responses=True)
        self._redis = client
        self._prefix = f"{namespace}:"
        logger.info("Shared state: Redis at %s (namespace %s)", _safe(url), namespace)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        raw = self._redis.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # A value written by an older version, or by something else sharing
            # the namespace. Treat as absent rather than crashing a payment.
            logger.warning("Unreadable value at %s — treating as missing", key)
            return None

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if ttl_s is None:
            self._redis.set(self._key(key), payload)
        else:
            self._redis.set(self._key(key), payload, ex=max(1, int(ttl_s)))

    def delete(self, key: str) -> None:
        self._redis.delete(self._key(key))

    def claim(self, key: str, ttl_s: float) -> bool:
        # SET NX EX is the whole point: one round trip, and only one caller wins.
        return bool(self._redis.set(self._key(key), "1", nx=True, ex=max(1, int(ttl_s))))

    def incr(self, key: str, ttl_s: float) -> int:
        namespaced = self._key(key)
        count = int(self._redis.incr(namespaced))
        if count == 1:
            # Only the first writer sets the expiry, so the window slides from
            # the first request rather than being extended by every one.
            self._redis.expire(namespaced, max(1, int(ttl_s)))
        return count


def build_store(url: str | None, namespace: str = "wa-voice") -> KeyValueStore:
    """Redis when a URL is configured, memory otherwise."""
    if not url:
        return MemoryStore()
    return RedisStore(url, namespace=namespace)


def guard_worker_count(workers: int, store: KeyValueStore) -> None:
    """Refuse to run several workers on per-process state.

    Without this the failure is silent and confusing: the bot replies twice to
    some messages and loses PINs on others, all while every health check
    passes. Failing at startup with the reason is strictly better than being
    debugged in production.
    """
    if workers <= 1 or isinstance(store, RedisStore):
        return

    raise RuntimeError(
        f"WEB_CONCURRENCY={workers} but no REDIS_URL is set.\n"
        "Deduplication and pending transfers are per-process without it, so "
        "with several workers a voice note gets two replies and a spoken PIN "
        "finds no transfer waiting.\n"
        "Set REDIS_URL, or run one worker."
    )


def _safe(url: str) -> str:
    """Redis URLs carry a password. Log the host, never the credentials."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"
    return url
