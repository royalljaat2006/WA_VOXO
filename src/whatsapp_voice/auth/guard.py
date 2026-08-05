"""The single gate every message passes through.

One object so there is one place to read, one place to test, and one place an
audit can point at — rather than an allowlist check in the handler, a rate check
in the agent, and a role check somewhere else.
"""

from __future__ import annotations

import logging

from ..config import AuthSettings
from ..store import MemoryStore
from .policy import AccessPolicy, AuthResult, Caller, Decision, Role
from .rate_limit import SharedRateLimiter, SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

#: Deliberately says nothing about whether the number is known, what this service
#: does, or who to contact. An unknown caller should not be able to use the reply
#: to confirm they have found an eko internal system.
UNKNOWN_NUMBER_REPLY = "Sorry, I can't help with that."


class AccessGuard:
    def __init__(
        self,
        policy: AccessPolicy,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        enabled: bool = True,
    ) -> None:
        self._policy = policy
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self._enabled = enabled

        if not enabled:
            logger.warning(
                "AUTH IS DISABLED — every number that reaches this server will be "
                "answered. Never run this way with real data."
            )
        elif policy.is_empty:
            logger.warning(
                "Allowlist is empty — every request will be denied. Set "
                "WHATSAPP_ALLOWED_NUMBERS (docs/phase-5-auth.md)."
            )
        else:
            logger.info("Auth enabled for %d number(s)", len(policy))

    @classmethod
    def from_settings(cls, settings: AuthSettings, store: object | None = None) -> AccessGuard:
        """Build from settings, sharing counts when a store is given.

        Without a store the sliding window is used, which is more precise but
        per-process. With one, counts are shared — coarser, and correct across
        workers. Precision is the right thing to trade away: a limit silently
        multiplied by the worker count is not a limit.
        """
        limiter = (
            SharedRateLimiter(store, settings.rate_limit, settings.rate_limit_window_s)
            if store is not None and not isinstance(store, MemoryStore)
            else SlidingWindowRateLimiter(
                limit=settings.rate_limit, window_s=settings.rate_limit_window_s
            )
        )
        return cls(
            policy=AccessPolicy.parse(
                settings.allowed_numbers, default_role=Role(settings.default_role)
            ),
            rate_limiter=limiter,
            enabled=settings.enabled,
        )

    def check(self, number: str) -> AuthResult:
        """Decide whether this sender may be answered.

        Order matters: identity before rate. Checking the rate first would let an
        unknown number consume limiter memory, and would leak — a different reply
        for "too many requests" versus "unknown" tells a prober their number is
        recognised.
        """
        if not self._enabled:
            # Auth off means every check passes, payments included. That is what
            # "disabled" has to mean for local testing to work at all — and it is
            # why the constructor warns as loudly as it does.
            return AuthResult(
                decision=Decision.ALLOW,
                caller=Caller(number=number, role=Role.ADMIN, can_pay=True),
            )

        caller = self._policy.lookup(number)
        if caller is None:
            logger.warning("Denied unknown number %s", _mask(number))
            return AuthResult(
                decision=Decision.UNKNOWN_NUMBER,
                message=UNKNOWN_NUMBER_REPLY,
            )

        if not self._rate_limiter.check(caller.number):
            wait_minutes = max(1, round(self._rate_limiter.retry_after_s(caller.number) / 60))
            logger.warning("Rate limited %s", _mask(number))
            return AuthResult(
                decision=Decision.RATE_LIMITED,
                caller=caller,
                message=(
                    "You've hit the limit for now. Please try again in about "
                    f"{wait_minutes} minute{'s' if wait_minutes != 1 else ''}."
                ),
            )

        return AuthResult(decision=Decision.ALLOW, caller=caller)


def _mask(number: str) -> str:
    """Log the last four digits only.

    Full numbers in log files are a data-protection problem of their own; the last
    four are enough to correlate with a support conversation.
    """
    return f"…{number[-4:]}" if len(number) > 4 else "…"
