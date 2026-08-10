"""GitHub API client used for intake (issues, alerts) and for collection (PRs, reviews, checks).

Two properties are load-bearing:

- The fork and upstream are both readable, but only the fork is ever writable, and writes are
  additionally gated by configuration. Upstream is an intake source, not a deployment target.
- Collection is windowed and idempotent, so the baseline window and the post-deployment window
  are produced by the same code path with different arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from .models import Event, EventType, Severity, digest_payload

UPSTREAM_REPOS = frozenset({"apache/superset"})


@dataclass(frozen=True)
class PullRequestFact:
    pr_url: str
    repo: str
    number: int
    author: str
    is_bot: bool
    opened_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None
    additions: int
    deletions: int
    changed_files: int
    review_rounds: int
    first_push_checks_passed: bool | None


class GitHubClient(Protocol):
    def list_issues(self, repo: str, since: datetime | None = None) -> list[Event]: ...

    def list_pull_requests(
        self, repo: str, since: datetime, until: datetime
    ) -> list[PullRequestFact]: ...

    def get_pull_request_body(self, repo: str, number: int) -> str: ...

    def update_pull_request_body(self, repo: str, number: int, body: str) -> None: ...

    def mark_ready_for_review(self, repo: str, number: int) -> None: ...


def parse_pr_url(pr_url: str) -> tuple[str, int]:
    """`https://github.com/owner/name/pull/12` -> `("owner/name", 12)`."""
    parts = pr_url.rstrip("/").split("/")
    if len(parts) < 5 or parts[-2] != "pull":
        raise ValueError(f"not a pull request URL: {pr_url}")
    return f"{parts[-4]}/{parts[-3]}", int(parts[-1])


class WriteNotPermittedError(PermissionError):
    """Raised on any attempt to write to a repository the deployment may only read."""


def assert_writable(repo: str, allow_upstream_write: bool) -> None:
    if repo in UPSTREAM_REPOS and not allow_upstream_write:
        raise WriteNotPermittedError(
            f"{repo} is an intake source only; set ALLOW_UPSTREAM_WRITE to override"
        )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HttpGitHubClient:
    """Real client. A token is optional for public reads, but advisable for rate limits."""

    def __init__(
        self,
        token: str | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=api_url.rstrip("/"), timeout=timeout, headers=headers)

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            response = self._client.get(path, params={**params, "per_page": 100, "page": page})
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                return
            yield from batch
            if len(batch) < 100:
                return
            page += 1

    def list_issues(self, repo: str, since: datetime | None = None) -> list[Event]:
        params: dict[str, Any] = {"state": "open", "sort": "created", "direction": "desc"}
        if since:
            params["since"] = since.isoformat()
        events: list[Event] = []
        for issue in self._paginate(f"/repos/{repo}/issues", params):
            if "pull_request" in issue:
                continue
            user = issue.get("user") or {}
            events.append(
                Event(
                    event_id=f"gh-issue-{repo}-{issue.get('number')}",
                    event_type=EventType.ISSUE,
                    repo=repo,
                    number=issue.get("number"),
                    title=str(issue.get("title") or ""),
                    body=str(issue.get("body") or "")[:4000],
                    labels=[str(label.get("name", "")) for label in issue.get("labels") or []],
                    author=str(user.get("login") or ""),
                    author_is_bot=str(user.get("type") or "").lower() == "bot",
                    severity=Severity.NONE,
                    created_at=_parse_time(issue.get("created_at")) or datetime.now(),
                    url=str(issue.get("html_url") or ""),
                    raw_digest=digest_payload(issue),
                )
            )
        return events

    def list_pull_requests(
        self, repo: str, since: datetime, until: datetime
    ) -> list[PullRequestFact]:
        facts: list[PullRequestFact] = []
        params = {"state": "all", "sort": "created", "direction": "desc"}
        for pull in self._paginate(f"/repos/{repo}/pulls", params):
            opened_at = _parse_time(pull.get("created_at"))
            if opened_at is None or not since <= opened_at <= until:
                continue
            user = pull.get("user") or {}
            facts.append(
                PullRequestFact(
                    pr_url=str(pull.get("html_url") or ""),
                    repo=repo,
                    number=int(pull.get("number") or 0),
                    author=str(user.get("login") or ""),
                    is_bot=str(user.get("type") or "").lower() == "bot",
                    opened_at=opened_at,
                    merged_at=_parse_time(pull.get("merged_at")),
                    closed_at=_parse_time(pull.get("closed_at")),
                    additions=int(pull.get("additions") or 0),
                    deletions=int(pull.get("deletions") or 0),
                    changed_files=int(pull.get("changed_files") or 0),
                    review_rounds=0,
                    first_push_checks_passed=None,
                )
            )
        return facts

    def get_pull_request_body(self, repo: str, number: int) -> str:
        response = self._client.get(f"/repos/{repo}/pulls/{number}")
        response.raise_for_status()
        return str(response.json().get("body") or "")

    def update_pull_request_body(self, repo: str, number: int, body: str) -> None:
        response = self._client.patch(f"/repos/{repo}/pulls/{number}", json={"body": body})
        response.raise_for_status()

    def mark_ready_for_review(self, repo: str, number: int) -> None:
        """Undrafting is GraphQL-only; the REST pulls endpoint cannot clear the draft flag."""
        node_id = self._client.get(f"/repos/{repo}/pulls/{number}").json().get("node_id")
        if not node_id:
            raise RuntimeError(f"could not resolve node id for {repo}#{number}")
        response = self._client.post(
            "/graphql",
            json={
                "query": (
                    "mutation($id: ID!) {"
                    " markPullRequestReadyForReview(input: {pullRequestId: $id})"
                    " { pullRequest { isDraft } } }"
                ),
                "variables": {"id": node_id},
            },
        )
        response.raise_for_status()
        errors = response.json().get("errors")
        if errors:
            raise RuntimeError(f"markPullRequestReadyForReview failed: {errors}")

    def close(self) -> None:
        self._client.close()


@dataclass
class FakeGitHubClient:
    """In-memory stand-in for offline simulation and tests."""

    issues: dict[str, list[Event]] = field(default_factory=dict)
    pull_requests: dict[str, list[PullRequestFact]] = field(default_factory=dict)
    bodies: dict[tuple[str, int], str] = field(default_factory=dict)
    drafts: set[tuple[str, int]] = field(default_factory=set)

    def list_issues(self, repo: str, since: datetime | None = None) -> list[Event]:
        events = self.issues.get(repo, [])
        if since is None:
            return list(events)
        return [event for event in events if event.created_at >= since]

    def list_pull_requests(
        self, repo: str, since: datetime, until: datetime
    ) -> list[PullRequestFact]:
        return [
            fact for fact in self.pull_requests.get(repo, []) if since <= fact.opened_at <= until
        ]

    def get_pull_request_body(self, repo: str, number: int) -> str:
        return self.bodies.get((repo, number), "")

    def update_pull_request_body(self, repo: str, number: int, body: str) -> None:
        self.bodies[(repo, number)] = body

    def mark_ready_for_review(self, repo: str, number: int) -> None:
        self.drafts.discard((repo, number))
