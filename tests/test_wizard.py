from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from scoreboard import wizard
from scoreboard.wizard import check_devin_repo_access, check_devin_token, write_scope

SCOPE = """\
# A comment worth keeping: it explains why the rule below is narrow.
version: 1
defaults:
  target_repo: jethac/superset
rules:
  - id: upstream-bug
    when:
      repo: [apache/superset]
    then:
      stream: bugfix
"""


def _responder(status: int) -> object:
    def get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(status, request=httpx.Request("GET", url))

    return get


def test_a_github_token_in_the_devin_field_is_named_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The likeliest paste error, and the one whose 403 misdirects hardest."""

    def fail(url: str, **kwargs: object) -> httpx.Response:
        raise AssertionError("no request should be made for an obviously wrong credential")

    monkeypatch.setattr(httpx, "get", fail)
    result = check_devin_token("github_pat_11ABCDEF")
    assert not result.ok
    assert "GitHub token" in result.detail


def test_a_working_key_is_accepted_without_an_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", _responder(200))
    assert check_devin_token("apk_user_whatever").ok


def test_a_rejected_key_is_reported_as_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", _responder(403))
    result = check_devin_token("apk_user_whatever")
    assert not result.ok
    assert result.blocking


def test_an_unlistable_organisation_does_not_block_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user key can create sessions but not enumerate the org, so 403 answers nothing."""

    def forbidden(self: object) -> list[object]:
        request = httpx.Request("GET", "https://api.devin.ai/v3beta1/organizations/o/repositories")
        raise httpx.HTTPStatusError(
            "403", request=request, response=httpx.Response(403, request=request)
        )

    monkeypatch.setattr(wizard.HttpDevinClient, "list_repositories", forbidden)
    result = check_devin_repo_access("apk_user_whatever", "org-1", "jethac/superset")
    assert not result.ok
    assert not result.blocking
    assert wizard.DEVIN_SETTINGS_URL in result.detail


def test_accepting_the_defaults_leaves_the_scope_file_alone(tmp_path: Path) -> None:
    """Rewriting it would round-trip the comments away for no change in meaning."""
    path = tmp_path / "scope.yaml"
    path.write_text(SCOPE, encoding="utf-8")
    write_scope("jethac/superset", ["apache/superset"], path)
    assert path.read_text(encoding="utf-8") == SCOPE


def test_narrowing_intake_rewrites_the_rule(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(SCOPE, encoding="utf-8")
    write_scope("jethac/other", ["apache/superset"], path)
    assert "jethac/other" in path.read_text(encoding="utf-8")
