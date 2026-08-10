from __future__ import annotations

import hashlib
import hmac

import pytest

from scoreboard.github import WriteNotPermittedError, assert_writable
from scoreboard.security import SignatureError, verify_github_signature

SECRET = "shhh"
BODY = b'{"action":"opened"}'


def signature(secret: str = SECRET, body: bytes = BODY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_is_accepted() -> None:
    verify_github_signature(SECRET, BODY, signature())


def test_missing_secret_rejects_rather_than_accepts() -> None:
    with pytest.raises(SignatureError, match="no webhook secret"):
        verify_github_signature(None, BODY, signature())


def test_missing_header_is_rejected() -> None:
    with pytest.raises(SignatureError):
        verify_github_signature(SECRET, BODY, None)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(SignatureError, match="mismatch"):
        verify_github_signature(SECRET, BODY, signature(secret="wrong"))


def test_tampered_body_is_rejected() -> None:
    with pytest.raises(SignatureError, match="mismatch"):
        verify_github_signature(SECRET, b'{"action":"closed"}', signature())


def test_unsupported_algorithm_is_rejected() -> None:
    digest = hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()  # noqa: S324
    with pytest.raises(SignatureError, match="algorithm"):
        verify_github_signature(SECRET, BODY, f"sha1={digest}")


def test_upstream_write_is_refused_by_default() -> None:
    with pytest.raises(WriteNotPermittedError):
        assert_writable("apache/superset", allow_upstream_write=False)


def test_upstream_write_allowed_only_on_explicit_opt_in() -> None:
    assert_writable("apache/superset", allow_upstream_write=True)
    assert_writable("jethac/superset", allow_upstream_write=False)
