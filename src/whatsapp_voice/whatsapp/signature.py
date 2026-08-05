"""Verify that a webhook POST really came from Meta.

The webhook endpoint is publicly reachable — anyone who learns the URL can POST
to it. Meta signs every delivery with an HMAC-SHA256 of the raw request body,
keyed on the app secret, in the `X-Hub-Signature-256` header. Checking that
signature is the only thing separating real messages from forged ones.

Two rules this module exists to enforce:

1. Hash the **raw bytes** as received. Re-serialising the parsed JSON changes
   the bytes (key order, whitespace) and the signature will never match.
2. Compare with `hmac.compare_digest`, not `==`. A plain comparison returns
   early on the first differing byte, which leaks the expected signature to an
   attacker who can time the responses.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def compute_signature(payload: bytes, app_secret: str) -> str:
    """Return the header value Meta would send for this body."""
    digest = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def is_valid_signature(payload: bytes, header: str | None, app_secret: str) -> bool:
    """True if `header` is a valid signature of `payload` under `app_secret`.

    `payload` must be the exact bytes of the request body.
    """
    if not header or not header.startswith(_PREFIX):
        return False
    return hmac.compare_digest(compute_signature(payload, app_secret), header)
