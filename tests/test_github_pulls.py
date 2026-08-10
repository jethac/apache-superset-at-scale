"""Tests for the pull-request read path, served by an in-process transport.

Three properties here are decided by api.github.com rather than by this repository, which is what
makes them worth pinning: the list is newest-first, it omits the diff-size fields entirely, and it
signals rate limiting in headers rather than in the body. A refactor can break any of the three
without another test noticing, and the resulting damage is silent — a shorter walk that misses
pull requests, a diff-size column of zeroes that reads as measured, or a collection that dies
halfway and leaves the operator guessing which half is present.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from scoreboard.github import HttpGitHubClient

REPO = "apache/superset"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(days=90)

Handler = Callable[[httpx.Request], httpx.Response]


def client_with(handler: Handler) -> HttpGitHubClient:
    """A real client whose transport answers from the test instead of from the network."""
    client = HttpGitHubClient(token="test-token")
    client.close()
    client._client = httpx.Client(  # noqa: SLF001 - swapping the transport is the whole point
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    return client


def list_entry(number: int, opened_at: datetime) -> dict[str, object]:
    """One entry of the pull-request list, with the fields that endpoint actually returns.

    Deliberately no `additions`, `deletions` or `changed_files`: the list endpoint does not send
    them, and a fixture that invents them would let the bug this file guards against pass.
    """
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": opened_at.isoformat().replace("+00:00", "Z"),
        "merged_at": None,
        "closed_at": None,
        "state": "open",
        "user": {"login": "contributor", "type": "User"},
    }


def detail_payload(number: int) -> dict[str, object]:
    """The single-pull-request response, which is the only place the diff size exists."""
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
        "additions": 120 + number,
        "deletions": 30 + number,
        "changed_files": 4,
    }


def test_the_walk_stops_at_the_first_pull_request_older_than_the_window() -> None:
    """The list is newest-first, so everything past the first old one is older too.

    Without the break this pages to the beginning of the repository — on a project with tens of
    thousands of pull requests, hundreds of requests per collection to find a few dozen.
    """
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/" in request.url.path:
            number = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=detail_payload(number))
        page = int(request.url.params.get("page", "1"))
        pages.append(page)
        if page == 1:
            # A full page inside the window, so the walk has a reason to ask for page 2.
            body = [list_entry(1000 + i, NOW - timedelta(hours=i)) for i in range(100)]
        else:
            # Page 2 opens outside the window; nothing beyond it can be inside.
            body = [list_entry(2000 + i, WINDOW_START - timedelta(days=i + 1)) for i in range(100)]
        return httpx.Response(200, json=body)

    client = client_with(handler)
    facts = client.list_pull_requests(REPO, WINDOW_START, NOW)

    assert pages == [1, 2], "the walk must stop once the window is passed, not page to the start"
    assert len(facts) == 100
    assert all(fact.opened_at >= WINDOW_START for fact in facts)


def test_diff_size_comes_from_the_endpoint_that_returns_it() -> None:
    """`additions` and friends exist only on the single-pull-request endpoint.

    Reading them off a list entry yields `None`, and the `or 0` that follows turns that into a
    column that reads as measured and is uniformly wrong.
    """
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/" in request.url.path:
            detail_requests.append(request.url.path)
            number = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=detail_payload(number))
        if int(request.url.params.get("page", "1")) > 1:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[list_entry(77, NOW - timedelta(days=1))])

    client = client_with(handler)
    facts = client.list_pull_requests(REPO, WINDOW_START, NOW)

    assert detail_requests == [f"/repos/{REPO}/pulls/77"]
    assert facts[0].additions == 197
    assert facts[0].deletions == 107
    assert facts[0].changed_files == 4


def test_diff_size_is_not_fetched_for_pull_requests_outside_the_window() -> None:
    """The extra request is the price of the column being real; it is not paid for filtered rows."""
    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pulls/" in request.url.path:
            detail_requests.append(request.url.path)
            return httpx.Response(200, json=detail_payload(1))
        if int(request.url.params.get("page", "1")) > 1:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                list_entry(1, NOW + timedelta(days=5)),  # opened after the window closes
                list_entry(2, NOW - timedelta(days=1)),  # inside
            ],
        )

    client = client_with(handler)
    facts = client.list_pull_requests(REPO, WINDOW_START, NOW)

    assert [fact.number for fact in facts] == [2]
    assert detail_requests == [f"/repos/{REPO}/pulls/2"]


def test_a_rate_limited_read_waits_and_retries_rather_than_failing_the_run() -> None:
    """GitHub answers a secondary limit with `Retry-After`; honouring it keeps collection whole."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "slow down"})
        return httpx.Response(200, json=[])

    client = client_with(handler)
    events = client.list_issues(REPO)

    assert len(attempts) == 2, "the first answer asked us to wait; the read must come back"
    assert events == []


def test_an_ordinary_forbidden_is_not_retried() -> None:
    """A 403 with no rate-limit header is a permission error; retrying it wastes the budget."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    client = client_with(handler)
    try:
        client.list_issues(REPO)
    except httpx.HTTPStatusError:
        pass
    else:  # pragma: no cover - the call must raise
        raise AssertionError("a forbidden read should surface, not be swallowed")

    assert len(attempts) == 1, "no rate-limit header means nothing to wait for"


def test_the_list_is_requested_newest_first() -> None:
    """The early break is only correct while the ordering it assumes is the one being asked for."""
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[])

    client = client_with(handler)
    client.list_pull_requests(REPO, WINDOW_START, NOW)

    assert seen[0].params.get("sort") == "created"
    assert seen[0].params.get("direction") == "desc"
    assert seen[0].params.get("state") == "all"
    assert json.loads(json.dumps(str(seen[0].path))) == f"/repos/{REPO}/pulls"
