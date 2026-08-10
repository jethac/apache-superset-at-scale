"""Webhook authentication.

A webhook endpoint is an unauthenticated door into a system that spends money and writes code,
so signature verification is mandatory rather than configurable: with no secret configured the
endpoint refuses every request instead of accepting every request.
"""

from __future__ import annotations

import hashlib
import hmac


class SignatureError(Exception):
    """Raised when a webhook delivery cannot be proven to come from the configured sender."""


def verify_github_signature(secret: str | None, body: bytes, header: str | None) -> None:
    """Verify a GitHub `X-Hub-Signature-256` header over the raw request body.

    Raises `SignatureError` on any failure. The comparison is constant-time; the body must be the
    exact bytes received, since re-serialising JSON changes the digest.
    """
    if not secret:
        raise SignatureError("no webhook secret configured")
    if not header:
        raise SignatureError("missing signature header")
    algorithm, _, provided = header.partition("=")
    if algorithm != "sha256" or not provided:
        raise SignatureError("unsupported signature algorithm")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("signature mismatch")
