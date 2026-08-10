"""GitHub API client used for intake (issues, alerts) and for collection (PRs, reviews, checks).

Two properties are load-bearing:

- The fork and upstream are both readable, but only the fork is ever writable, and writes are
  additionally gated by configuration. Upstream is an intake source, not a deployment target.
- Collection is windowed and idempotent, so the baseline window and the post-deployment window
  are produced by the same code path with different arguments.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from .models import Event, EventType, Severity, WorkflowRunRef, digest_payload

logger = logging.getLogger(__name__)

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


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """How long GitHub is asking us to wait, or None if it is not asking.

    Two mechanisms, and both have to be read. The secondary limit sends `Retry-After`; the primary
    one sends `x-ratelimit-remaining: 0` with the reset as an epoch. A 403 with neither is an
    ordinary permission error and must not be retried.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return None
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                return None
    return None


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
        max_retries: int = 3,
        max_retry_wait: float = 120.0,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=api_url.rstrip("/"), timeout=timeout, headers=headers)
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """A GET that waits when GitHub asks it to, rather than failing the run.

        Collecting a ninety-day window over a busy repository is thousands of requests, and both
        the primary rate limit and the secondary abuse limit answer with a header saying when to
        come back. Raising on those turns a windowed collection into a partial one — worse than a
        slow one, because `collect` is meant to be idempotent and a run that dies halfway leaves
        the operator guessing which half is present.

        A plain 403 carries no such header and is returned immediately; only a documented wait is
        waited on, and only up to `max_retry_wait`, past which failing is the honest outcome.
        """
        for _ in range(self._max_retries):
            response = self._client.get(path, params=params)
            if response.status_code not in (403, 429):
                return response
            wait = _retry_after_seconds(response)
            if wait is None or wait > self._max_retry_wait:
                return response
            logger.warning("rate limited on %s; waiting %.0fs", path, wait)
            time.sleep(wait + 1.0)
        return self._client.get(path, params=params)

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            response = self._get(path, params={**params, "per_page": 100, "page": page})
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

    def _pull_request_detail(self, repo: str, number: int) -> dict[str, Any]:
        """The fields the list endpoint omits.

        `additions`, `deletions` and `changed_files` are returned only by the single-pull-request
        endpoint. Reading them off a list entry yields `None` for every pull request, and the `or
        0` that follows turns that into a diff-size column which reads as measured and is
        uniformly wrong — the failure the PRD's caveat list exists to prevent, arriving as a fact
        rather than as a caveat.
        """
        response = self._get(f"/repos/{repo}/pulls/{number}")
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def list_pull_requests(
        self, repo: str, since: datetime, until: datetime
    ) -> list[PullRequestFact]:
        """Pull requests opened in the window, with their diff size.

        The walk stops at the first pull request older than the window rather than paging to the
        beginning of the repository. The list is sorted newest-first, so everything past that
        point is older too; on a project with tens of thousands of pull requests the difference is
        a few requests against a few hundred, on every collection.

        Diff size then costs one request per pull request in the window, which is the price of the
        column being real. It is charged only for pull requests that survive the window filter.
        """
        facts: list[PullRequestFact] = []
        params = {"state": "all", "sort": "created", "direction": "desc"}
        for pull in self._paginate(f"/repos/{repo}/pulls", params):
            opened_at = _parse_time(pull.get("created_at"))
            if opened_at is None:
                continue
            if opened_at < since:
                break
            if opened_at > until:
                continue
            number = int(pull.get("number") or 0)
            detail = self._pull_request_detail(repo, number)
            user = pull.get("user") or {}
            facts.append(
                PullRequestFact(
                    pr_url=str(pull.get("html_url") or ""),
                    repo=repo,
                    number=number,
                    author=str(user.get("login") or ""),
                    is_bot=str(user.get("type") or "").lower() == "bot",
                    opened_at=opened_at,
                    merged_at=_parse_time(pull.get("merged_at")),
                    closed_at=_parse_time(pull.get("closed_at")),
                    additions=int(detail.get("additions") or 0),
                    deletions=int(detail.get("deletions") or 0),
                    changed_files=int(detail.get("changed_files") or 0),
                    review_rounds=0,
                    first_push_checks_passed=None,
                )
            )
        return facts

    def list_pull_request_runs(
        self, repo: str, since: datetime, until: datetime
    ) -> list[WorkflowRunRef]:
        """Workflow runs triggered by pull requests in a window, newest first.

        `event=pull_request` is asked of the API rather than filtered here, because a repository
        the size of Superset runs scheduled and push workflows whose minutes are not attributable
        to any pull request and would inflate the per-PR figure.
        """
        params: dict[str, Any] = {
            "event": "pull_request",
            "created": f"{since.date().isoformat()}..{until.date().isoformat()}",
        }
        runs: list[WorkflowRunRef] = []
        for run in self._paginate_key(f"/repos/{repo}/actions/runs", params, "workflow_runs"):
            pulls = run.get("pull_requests") or []
            number = int(pulls[0].get("number")) if pulls else None
            runs.append(
                WorkflowRunRef(
                    run_id=int(run.get("id") or 0),
                    pr_number=number,
                    head_sha=str(run.get("head_sha") or ""),
                )
            )
        return runs

    def pull_request_for_sha(self, repo: str, sha: str) -> int | None:
        """The pull request a commit belongs to, for runs the Actions API cannot attribute.

        `workflow_runs[].pull_requests` is populated only when the head branch lives in the same
        repository, so on a project whose contributions arrive from forks it is empty for almost
        every run. Attributing minutes by head commit instead is what makes a cost-per-pull-request
        figure describe the project rather than the handful of branches pushed by committers.
        """
        response = self._get(f"/repos/{repo}/commits/{sha}/pulls", params={"per_page": 1})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        pulls = response.json()
        if not isinstance(pulls, list) or not pulls:
            return None
        first = pulls[0]
        number = first.get("number") if isinstance(first, dict) else None
        return int(number) if number is not None else None

    def get_run_jobs(self, repo: str, run_id: int) -> str:
        response = self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs", params={"per_page": 100})
        response.raise_for_status()
        return response.text

    def _paginate_key(
        self, path: str, params: dict[str, Any], key: str
    ) -> Iterator[dict[str, Any]]:
        """Pagination for the Actions endpoints, which wrap their list in an envelope."""
        page = 1
        while True:
            response = self._get(path, params={**params, "per_page": 100, "page": page})
            response.raise_for_status()
            batch = response.json().get(key) or []
            if not batch:
                return
            yield from batch
            if len(batch) < 100:
                return
            page += 1

    def get_pull_request_body(self, repo: str, number: int) -> str:
        response = self._get(f"/repos/{repo}/pulls/{number}")
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
