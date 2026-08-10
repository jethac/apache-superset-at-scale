"""Tests for the two live Actions reads, served by an in-process transport.

The point of these is the shape of the request and the shape of the envelope: both are decided by
api.github.com rather than by this repository, so they are the parts a refactor can quietly break
without any other test noticing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from scoreboard.github import HttpGitHubClient

REPO = "apache/superset"
WINDOW_START = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
WINDOW_END = datetime(2026, 2, 1, 18, 5, tzinfo=UTC)
HEAD_SHA = "9f2b3c4d5e6f70819a2b3c4d5e6f70819a2b3c4d"

Handler = Callable[[httpx.Request], httpx.Response]


def run_payload(run_id: int, pr_numbers: list[int], head_sha: str = HEAD_SHA) -> dict[str, object]:
    """One entry of `workflow_runs`, trimmed to the fields the client reads and their neighbours."""
    return {
        "id": run_id,
        "name": "CI",
        "head_branch": "feature",
        "head_sha": head_sha,
        "run_number": 4213,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "workflow_id": 161335,
        "created_at": "2026-01-14T11:02:31Z",
        "updated_at": "2026-01-14T11:48:02Z",
        "pull_requests": [
            {
                "url": f"https://api.github.com/repos/{REPO}/pulls/{number}",
                "id": 2000000 + number,
                "number": number,
                "head": {"ref": "feature", "sha": head_sha},
                "base": {"ref": "master", "sha": "0" * 40},
            }
            for number in pr_numbers
        ],
    }


def envelope(runs: list[dict[str, object]], total_count: int | None = None) -> str:
    return json.dumps(
        {"total_count": len(runs) if total_count is None else total_count, "workflow_runs": runs}
    )


def client_with(handler: Handler) -> HttpGitHubClient:
    """A real client whose transport answers from the test instead of from the network."""
    client = HttpGitHubClient(token="test-token")
    client.close()
    client._client = httpx.Client(  # noqa: SLF001 - swapping the transport is the whole point
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    return client


def test_pull_request_runs_are_asked_of_github_rather_than_filtered_here() -> None:
    """Scheduled and push runs are not attributable to a pull request, so they must never arrive."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=envelope([run_payload(500001, [42])]))

    client_with(handler).list_pull_request_runs(REPO, WINDOW_START, WINDOW_END)

    assert len(requests) == 1
    assert requests[0].url.path == f"/repos/{REPO}/actions/runs"
    params = requests[0].url.params
    assert params["event"] == "pull_request"
    assert params["created"] == "2026-01-01..2026-02-01"
    assert params["per_page"] == "100"
    assert params["page"] == "1"


def test_a_run_is_attributed_to_the_first_pull_request_it_names() -> None:
    """A run can list several pull requests; the one it was triggered by comes first."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=envelope([run_payload(500001, [42, 43])]))

    runs = client_with(handler).list_pull_request_runs(REPO, WINDOW_START, WINDOW_END)

    assert len(runs) == 1
    assert runs[0].run_id == 500001
    assert runs[0].pr_number == 42
    assert runs[0].head_sha == HEAD_SHA


def test_a_run_with_no_pull_request_is_recorded_with_a_null_number() -> None:
    """GitHub often cannot attribute a fork's run, and dropping those would lose real minutes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=envelope([run_payload(500002, [])]))

    runs = client_with(handler).list_pull_request_runs(REPO, WINDOW_START, WINDOW_END)

    assert [run.pr_number for run in runs] == [None]
    assert runs[0].run_id == 500002


def test_the_walk_reads_past_the_first_hundred_runs() -> None:
    """A busy day in Superset exceeds one page, and a truncated window understates the toll."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        count = 100 if page == "1" else 30
        offset = 0 if page == "1" else 100
        runs = [run_payload(600000 + offset + index, [index]) for index in range(count)]
        return httpx.Response(200, text=envelope(runs, total_count=130))

    runs = client_with(handler).list_pull_request_runs(REPO, WINDOW_START, WINDOW_END)

    assert pages == ["1", "2"]
    assert len(runs) == 130
    assert runs[-1].run_id == 600129


def test_an_empty_envelope_ends_the_walk() -> None:
    """A window whose last page is exactly full must not loop on an endpoint that keeps replying."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        if page != "1":
            return httpx.Response(200, text=envelope([]))
        page_one = [run_payload(700000 + index, [1]) for index in range(100)]
        return httpx.Response(200, text=envelope(page_one))

    runs = client_with(handler).list_pull_request_runs(REPO, WINDOW_START, WINDOW_END)

    assert pages == ["1", "2"]
    assert len(runs) == 100


def test_run_jobs_are_returned_as_the_body_github_sent() -> None:
    """The jobs payload is parsed elsewhere, so the client must not reshape it on the way past."""
    body = json.dumps(
        {
            "total_count": 1,
            "jobs": [
                {
                    "id": 900001,
                    "run_id": 500001,
                    "workflow_name": "Python-Unit",
                    "name": "test-postgres (3.11)",
                    "head_sha": HEAD_SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-01-14T11:03:00Z",
                    "completed_at": "2026-01-14T11:26:24Z",
                }
            ],
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=body)

    payload = client_with(handler).get_run_jobs(REPO, 500001)

    assert payload == body
    assert requests[0].url.path == f"/repos/{REPO}/actions/runs/500001/jobs"
    assert requests[0].url.params["per_page"] == "100"


def test_a_head_commit_names_the_pull_request_the_run_belongs_to() -> None:
    """The Actions API leaves `pull_requests` empty for fork branches; the commit still knows."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"number": 4242}, {"number": 9}])

    assert client_with(handler).pull_request_for_sha(REPO, HEAD_SHA) == 4242
    assert requests[0].url.path == f"/repos/{REPO}/commits/{HEAD_SHA}/pulls"


def test_a_commit_on_no_pull_request_resolves_to_nothing() -> None:
    """Pushes to master run the same workflows and must not be attributed to a pull request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    assert client_with(handler).pull_request_for_sha(REPO, HEAD_SHA) is None


def test_a_commit_github_cannot_find_resolves_to_nothing() -> None:
    """A commit garbage-collected after a force-push 404s, which is missing data, not an outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    assert client_with(handler).pull_request_for_sha(REPO, HEAD_SHA) is None


def test_a_failed_jobs_read_raises_instead_of_returning_an_error_document() -> None:
    """An error body would parse as zero jobs, and a run would silently cost nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(httpx.HTTPStatusError):
        client_with(handler).get_run_jobs(REPO, 500001)
