from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from scoreboard.cicost import (
    JobRun,
    WorkflowRunRef,
    collect,
    cost_per_pr,
    parse_jobs,
    record_jobs,
    savings_if_removed,
)
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT

REPO = "apache/superset"
FIXTURE = (REPO_ROOT / "fixtures" / "ci-runs-sample.json").read_text()
WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 2, 1, tzinfo=UTC)


@dataclass
class FakeRunSource:
    """Stand-in for the two GitHub reads `collect` makes. No test touches the network."""

    runs: list[WorkflowRunRef] = field(default_factory=list)
    payloads: dict[int, str] = field(default_factory=dict)
    jobs_calls: list[int] = field(default_factory=list)
    pulls_for_sha: dict[str, int] = field(default_factory=dict)
    sha_lookups: list[str] = field(default_factory=list)

    def list_pull_request_runs(
        self, repo: str, since: datetime, until: datetime
    ) -> list[WorkflowRunRef]:
        return list(self.runs)

    def get_run_jobs(self, repo: str, run_id: int) -> str:
        self.jobs_calls.append(run_id)
        return self.payloads[run_id]

    def pull_request_for_sha(self, repo: str, sha: str) -> int | None:
        self.sha_lookups.append(sha)
        return self.pulls_for_sha.get(sha)


def jobs_for_pr(pr_number: int, run_id: int, day: datetime) -> list[JobRun]:
    """The fixture's job set re-dated onto one pull request, so per-PR arithmetic is exact."""
    base = parse_jobs(FIXTURE, REPO)
    offset = day - base[0].started_at.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        replace(
            job,
            run_id=run_id,
            pr_number=pr_number,
            started_at=job.started_at + offset,
            completed_at=job.completed_at + offset,
        )
        for job in base
    ]


def test_parse_jobs_skips_the_in_progress_job() -> None:
    jobs = parse_jobs(FIXTURE, REPO)
    assert [job.job for job in jobs if job.job == "docs-build"] == []
    assert len(jobs) == 11
    assert sum(job.minutes for job in jobs) == pytest.approx(170.1, abs=0.05)
    integration = next(job for job in jobs if job.workflow == "Python-Integration")
    assert integration.minutes == 57.6
    assert integration.pr_number is None
    assert integration.repo == REPO


def test_a_gate_job_stamped_backwards_costs_nothing_rather_than_refunding_minutes() -> None:
    """Skipped gate jobs come back with `completed_at` just before `started_at`."""
    job = parse_jobs(FIXTURE, REPO)[0]
    skipped = replace(job, completed_at=job.started_at - timedelta(seconds=4))
    assert skipped.minutes == 0.0


def test_record_jobs_is_idempotent(store: FactStore) -> None:
    jobs = jobs_for_pr(1, 500001, datetime(2026, 1, 5, tzinfo=UTC))
    record_jobs(store, jobs)
    record_jobs(store, jobs)
    rows = store.query("SELECT COUNT(*) AS n FROM fact_ci_job")
    assert int(rows[0]["n"]) == len(jobs)


def test_collect_records_pull_request_runs(store: FactStore) -> None:
    github = FakeRunSource(
        runs=[WorkflowRunRef(run_id=500001, pr_number=42, head_sha="a" * 40)],
        payloads={500001: FIXTURE},
    )
    assert collect(github, store, REPO, WINDOW_START, WINDOW_END) == 11
    assert github.jobs_calls == [500001]
    rows = store.query("SELECT DISTINCT pr_number FROM fact_ci_job")
    assert [int(row["pr_number"]) for row in rows] == [42]


def test_a_fork_run_is_attributed_through_its_head_commit(store: FactStore) -> None:
    """Runs from forks carry no pull request, and dropping them would measure only committers."""
    github = FakeRunSource(
        runs=[WorkflowRunRef(run_id=500001, pr_number=None, head_sha="b" * 40)],
        payloads={500001: FIXTURE},
        pulls_for_sha={"b" * 40: 4242},
    )
    collect(github, store, REPO, WINDOW_START, WINDOW_END)

    rows = store.query("SELECT DISTINCT pr_number FROM fact_ci_job")
    assert [int(row["pr_number"]) for row in rows] == [4242]
    assert github.sha_lookups == ["b" * 40]


def test_a_commit_is_asked_about_once_however_many_runs_it_has(store: FactStore) -> None:
    """Every run costs an API call already; re-asking per run would multiply the rate-limit bill."""
    head = "c" * 40
    github = FakeRunSource(
        runs=[
            WorkflowRunRef(run_id=500001, pr_number=None, head_sha=head),
            WorkflowRunRef(run_id=500002, pr_number=None, head_sha=head),
        ],
        payloads={500001: FIXTURE, 500002: FIXTURE},
        pulls_for_sha={head: 77},
    )
    collect(github, store, REPO, WINDOW_START, WINDOW_END)

    assert github.sha_lookups == [head]


def test_a_run_belonging_to_no_pull_request_is_still_recorded(store: FactStore) -> None:
    """The table is an account of what the runners did, not only of what was attributable."""
    github = FakeRunSource(
        runs=[WorkflowRunRef(run_id=500001, pr_number=None, head_sha="d" * 40)],
        payloads={500001: FIXTURE},
    )
    assert collect(github, store, REPO, WINDOW_START, WINDOW_END) == 11
    rows = store.query("SELECT pr_number FROM fact_ci_job")
    assert all(row["pr_number"] is None for row in rows)


def test_median_ignores_a_duplicated_retry_run(store: FactStore) -> None:
    """A retried pull request pays twice; the median change still pays once."""
    day = datetime(2026, 1, 5, tzinfo=UTC)
    for pr_number in range(1, 6):
        record_jobs(store, jobs_for_pr(pr_number, 500000 + pr_number, day))
    record_jobs(store, jobs_for_pr(3, 600003, day + timedelta(hours=3)))

    points = cost_per_pr(store, REPO, period="week")
    assert len(points) == 1
    point = points[0]
    assert point.period_start == day
    assert point.prs == 5
    assert point.median_minutes_per_pr == pytest.approx(170.1, abs=0.05)

    e2e = next(cost for cost in point.by_workflow if cost.workflow == "E2E")
    assert e2e.median_minutes == pytest.approx(34.8, abs=0.05)
    assert e2e.jobs == 18


def test_cost_per_pr_buckets_by_period(store: FactStore) -> None:
    record_jobs(store, jobs_for_pr(1, 500001, datetime(2026, 1, 5, tzinfo=UTC)))
    record_jobs(store, jobs_for_pr(2, 500002, datetime(2026, 1, 19, tzinfo=UTC)))
    assert [point.period_start.date().isoformat() for point in cost_per_pr(store, REPO)] == [
        "2026-01-05",
        "2026-01-19",
    ]
    monthly = cost_per_pr(store, REPO, period="month")
    assert len(monthly) == 1
    assert monthly[0].prs == 2


def test_savings_if_removed_prices_the_cypress_matrix(store: FactStore) -> None:
    day = datetime(2026, 1, 5, tzinfo=UTC)
    for pr_number in range(1, 8):
        record_jobs(store, jobs_for_pr(pr_number, 500000 + pr_number, day))

    minutes, fraction = savings_if_removed(store, REPO, ["E2E/cypress-matrix"])
    assert minutes == pytest.approx(20.3, abs=0.05)
    assert fraction == pytest.approx(0.12, abs=0.005)


def test_savings_if_removed_on_an_empty_store(store: FactStore) -> None:
    record_jobs(store, [])
    assert savings_if_removed(store, REPO, ["E2E/cypress-matrix"]) == (0.0, 0.0)


def test_savings_if_removed_accepts_a_whole_workflow(store: FactStore) -> None:
    record_jobs(store, jobs_for_pr(1, 500001, datetime(2026, 1, 5, tzinfo=UTC)))
    minutes, _ = savings_if_removed(store, REPO, ["Python-Unit"])
    assert minutes == pytest.approx(23.4, abs=0.05)
