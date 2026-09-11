"""Getting a fresh PINTWIN key onto the payer's own phone.

### What this is for

A payer cannot say their PINTWIN until they have one, and today obtaining it
is entirely manual: they leave the chat, open eko Connect in a browser, read
the scramble key off the strip, and work the twin out in their head. That
detour costs a minute or two per transfer and runs against the intent's
3-minute expiry.

The pinTwin Android app already knows how to fetch a key and show it in the
notification shade — it just has no way of being *told* to. This module is
the one thing this codebase can contribute to closing that gap: at the moment
a transfer is prepared and the bot asks for a PINTWIN, it also asks eko's
backend to push a key to that payer's phone.

### Deliberately unable to break a payment

Every method here swallows its failures. If the push service is down, slow,
or not configured, the payer simply obtains the key the way they do today —
so a broken side channel must never fail a transfer that is otherwise fine.
That is why `push` returns a bool nobody is required to check, and why the
call site treats it as fire-and-forget.

### Off until eko confirms the contract

`enabled` defaults to False, and the request body below is a **guess** —
eko has not published this endpoint yet, and may not have one at all. When
they answer, expect to correct the field names here rather than to rewrite
the seam: the shape is deliberately small and configuration-driven, in the
same spirit as `EkoSettings`' dotted response paths.

The open question that decides how much work remains is whether eko can
already push to a customer's phone. If they can, only this file is needed.
If they cannot, the Android app needs a push receiver and a new release,
which is not something this repository can do.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import requests

from ..config import PinTwinSettings
from .parsing import payer_mobile

logger = logging.getLogger(__name__)


class KeyPusher(Protocol):
    """Asks for a scramble key to be delivered to a payer's phone."""

    def push(self, payer: str, intent_id: str) -> bool:
        """True when the request was accepted. Never raises."""
        ...


class NullKeyPusher:
    """The default. Does nothing, tells nobody, and always says so.

    A deployment without the push configured behaves exactly as it does
    today, with no extra network call in the payment path.
    """

    def push(self, payer: str, intent_id: str) -> bool:
        return False


class HttpKeyPusher:
    """Posts to eko's (not yet published) key-push endpoint.

    Keyed by the payer's own mobile number, which is the same identity the
    transfer travels under (`initiator_id`) and the same number the app is
    logged in with — one person, one phone, one registration.
    """

    def __init__(self, settings: PinTwinSettings, session: Any = None) -> None:
        self._settings = settings
        self._session = session if session is not None else requests.Session()

    def push(self, payer: str, intent_id: str) -> bool:
        mobile = payer_mobile(payer)
        if mobile is None:
            # The demo path's `demo:<session>` lands here. Nothing to push to,
            # and inventing a number would aim a real notification at a
            # stranger.
            logger.debug("PINTWIN push skipped — %r is not a mobile number", payer)
            return False

        url = self._settings.push_url
        if not url:
            logger.debug("PINTWIN push skipped — no PINTWIN_PUSH_URL configured")
            return False

        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"

        try:
            response = self._session.post(
                url,
                json={"mobile": mobile, "reference": intent_id},
                headers=headers,
                timeout=self._settings.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - a side channel may never raise
            logger.warning(
                "PINTWIN push failed for intent %s: %s — payer can still fetch "
                "the key manually", intent_id, type(exc).__name__,
            )
            return False

        ok = 200 <= getattr(response, "status_code", 0) < 300
        logger.info(
            "PINTWIN push %s for intent %s (http=%s)",
            "sent" if ok else "refused", intent_id,
            getattr(response, "status_code", "?"),
        )
        return ok


def build_key_pusher(
    settings: PinTwinSettings | None, session: Any = None
) -> KeyPusher:
    """The pusher this configuration enables — `NullKeyPusher` unless asked."""
    if settings is None or not settings.enabled:
        return NullKeyPusher()
    if not settings.push_url:
        logger.warning(
            "PINTWIN_PUSH_ENABLED is set but PINTWIN_PUSH_URL is empty — "
            "no key will be pushed, and payers will fetch keys manually."
        )
        return NullKeyPusher()
    logger.info("PINTWIN key push enabled → %s", settings.push_url)
    return HttpKeyPusher(settings, session=session)
