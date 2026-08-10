"""Measure the CI a pull request has to buy, per workflow and per job.

Debt going down is only half the deployment's case. The other half is that the fixed toll every
change pays to merge goes down too, and that toll is a measurement rather than an opinion: it is
the billable wall time of the jobs GitHub actually ran on pull-request commits.

Two things follow from what the number is used for. Medians, not means: a handful of retried runs
would otherwise decide the headline, and a retry is noise about the pipeline rather than signal
about what a change costs. And the per-job breakdown is stored, not just the per-workflow total,
because retirement arguments are made against individual jobs — `savings_if_removed` computes what
dropping `E2E/cypress-matrix` is worth instead of asserting it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median
from typing import Literal, Protocol

from .models import WorkflowRunRef
from .store import FactStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_ci_job (
    repo          TEXT NOT NULL,
    run_id        INTEGER NOT NULL,
    job           TEXT NOT NULL,
    workflow      TEXT NOT NULL,
    pr_number     INTEGER,
    head_sha      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    completed_at  TEXT NOT NULL,
    conclusion    TEXT NOT NULL,
    minutes       REAL NOT NULL,
    PRIMARY KEY (repo, run_id, job)
);

CREATE INDEX IF NOT EXISTS idx_ci_job_pr ON fact_ci_job (repo, pr_number, started_at);
"""


@dataclass(frozen=True)
class JobRun:
    run_id: int
    repo: str
    workflow: str
    job: str
    pr_number: int | None
    head_sha: str
    started_at: datetime
    completed_at: datetime
    conclusion: str

    @property
    def minutes(self) -> float:
        """Billable wall time. GitHub bills the job's elapsed time, so queueing is excluded.

        A job that never ran — skipped by a conditional, or cancelled before it started — is
        sometimes stamped with a `completed_at` fractionally before its `started_at`. Those
        negatives are floored at zero rather than summed: a gate job that did no work bought no
        minutes, and left signed it would quietly refund the workflows around it.
        """
        elapsed = (self.completed_at - self.started_at).total_seconds() / 60.0
        return round(max(elapsed, 0.0), 1)


@dataclass(frozen=True)
class WorkflowCost:
    workflow: str
    jobs: int
    median_minutes: float
    total_minutes: float


@dataclass(frozen=True)
class CostPoint:
    period_start: datetime
    repo: str
    prs: int
    median_minutes_per_pr: float
    by_workflow: list[WorkflowCost]


class WorkflowRunSource(Protocol):
    """Exactly the two GitHub reads collection needs, so tests can inject a fake.

    Narrower than `GitHubClient` on purpose: nothing here writes, and a fake that had to implement
    the whole client surface would be mostly stubs that assert they are never called.
    """

    def list_pull_request_runs(
        self, repo: str, since: datetime, until: datetime
    ) -> list[WorkflowRunRef]: ...

    def get_run_jobs(self, repo: str, run_id: int) -> str: ...

    def pull_request_for_sha(self, repo: str, sha: str) -> int | None: ...


def ensure_schema(store: FactStore) -> None:
    with _connect(store) as connection:
        connection.executescript(SCHEMA)


@contextmanager
def _connect(store: FactStore) -> Iterator[sqlite3.Connection]:
    """Own connection to the store's database file.

    `FactStore` exposes reads and its own upserts, not arbitrary writes, and this table belongs to
    this module rather than to the store's schema. Writing through a separate connection to the
    same file keeps that boundary without editing `store.py`.
    """
    connection = sqlite3.connect(store.path)
    try:
        connection.row_factory = sqlite3.Row
        yield connection
        connection.commit()
    finally:
        connection.close()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_jobs(payload: str, repo: str) -> list[JobRun]:
    """Parse a `GET /repos/{repo}/actions/runs/{id}/jobs` response body.

    Jobs without a `completed_at` are still running. They are skipped rather than closed off at
    the current time: a guessed duration would be indistinguishable from a measured one once it is
    in the table, and re-collection would silently change history.
    """
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("jobs payload is not an object")
    entries = document.get("jobs")
    if not isinstance(entries, list):
        raise ValueError("jobs payload has no `jobs` array")

    runs: list[JobRun] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        started_at = _parse_time(_as_optional_str(entry.get("started_at")))
        completed_at = _parse_time(_as_optional_str(entry.get("completed_at")))
        if started_at is None or completed_at is None:
            continue
        runs.append(
            JobRun(
                run_id=int(entry.get("run_id") or 0),
                repo=repo,
                workflow=str(entry.get("workflow_name") or ""),
                job=str(entry.get("name") or ""),
                pr_number=None,
                head_sha=str(entry.get("head_sha") or ""),
                started_at=started_at,
                completed_at=completed_at,
                conclusion=str(entry.get("conclusion") or ""),
            )
        )
    return runs


def _as_optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def record_jobs(store: FactStore, jobs: Sequence[JobRun]) -> None:
    """Upsert on `(repo, run_id, job)`, so re-collecting a window rewrites rather than doubles."""
    ensure_schema(store)
    with _connect(store) as connection:
        connection.executemany(
            """
            INSERT INTO fact_ci_job (repo, run_id, job, workflow, pr_number, head_sha,
                                     started_at, completed_at, conclusion, minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, run_id, job) DO UPDATE SET
                workflow=excluded.workflow,
                pr_number=COALESCE(excluded.pr_number, fact_ci_job.pr_number),
                head_sha=excluded.head_sha,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                conclusion=excluded.conclusion,
                minutes=excluded.minutes
            """,
            [
                (
                    job.repo,
                    job.run_id,
                    job.job,
                    job.workflow,
                    job.pr_number,
                    job.head_sha,
                    job.started_at.isoformat(),
                    job.completed_at.isoformat(),
                    job.conclusion,
                    job.minutes,
                )
                for job in jobs
            ],
        )


def collect(
    github: WorkflowRunSource,
    store: FactStore,
    repo: str,
    since: datetime,
    until: datetime,
    max_runs: int | None = None,
) -> int:
    """Record the jobs of every pull-request workflow run in a window; return rows written.

    The pull request number comes from the run, not from the jobs payload, which does not carry
    it. Where the run does not name one — the Actions API leaves `pull_requests` empty whenever the
    head branch lives in a fork, which on Superset is nearly every contribution — the head commit
    is asked which pull request it belongs to. Without that second read the median would describe
    only the handful of pull requests pushed by committers. Runs neither read can attribute are
    still recorded, with a null number, so the table stays a faithful account of what the runners
    did.

    `max_runs` bounds the walk for repositories that run thousands of jobs a day: each run costs
    an API call, and the figure this feeds is a median, which a bounded sample of the window
    estimates without reading every run.
    """
    ensure_schema(store)
    written = 0
    runs = github.list_pull_request_runs(repo, since, until)
    resolved: dict[str, int | None] = {}
    for run in runs[:max_runs] if max_runs else runs:
        pr_number = run.pr_number
        if pr_number is None and run.head_sha:
            if run.head_sha not in resolved:
                resolved[run.head_sha] = github.pull_request_for_sha(repo, run.head_sha)
            pr_number = resolved[run.head_sha]
        jobs = [
            replace(job, pr_number=pr_number, head_sha=job.head_sha or run.head_sha)
            for job in parse_jobs(github.get_run_jobs(repo, run.run_id), repo)
        ]
        if not jobs:
            continue
        record_jobs(store, jobs)
        written += len(jobs)
    return written


def _period_start(moment: datetime, period: Literal["week", "month"]) -> datetime:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())


def cost_per_pr(
    store: FactStore, repo: str, period: Literal["week", "month"] = "week"
) -> list[CostPoint]:
    """Median compute-minutes a pull request paid in each period, with a per-workflow breakdown.

    A pull request's cost is the sum of every job recorded against it in the period, retries
    included — that is what the change really consumed. The median across pull requests is then
    what a normal change costs, unmoved by the few that were retried repeatedly.
    """
    rows = store.query(
        "SELECT started_at, pr_number, workflow, minutes FROM fact_ci_job"
        " WHERE repo = ? AND pr_number IS NOT NULL",
        (repo,),
    )

    per_period: dict[datetime, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    per_period_workflow: dict[datetime, dict[str, dict[int, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    job_counts: dict[datetime, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in rows:
        start = _period_start(datetime.fromisoformat(str(row["started_at"])), period)
        pr_number = int(row["pr_number"])
        workflow = str(row["workflow"])
        minutes = float(row["minutes"])
        per_period[start][pr_number] += minutes
        per_period_workflow[start][workflow][pr_number] += minutes
        job_counts[start][workflow] += 1

    points: list[CostPoint] = []
    for start in sorted(per_period):
        totals = per_period[start]
        breakdown = [
            WorkflowCost(
                workflow=workflow,
                jobs=job_counts[start][workflow],
                median_minutes=round(median(by_pr.values()), 1),
                total_minutes=round(sum(by_pr.values()), 1),
            )
            for workflow, by_pr in sorted(per_period_workflow[start].items())
        ]
        points.append(
            CostPoint(
                period_start=start,
                repo=repo,
                prs=len(totals),
                median_minutes_per_pr=round(median(totals.values()), 1),
                by_workflow=breakdown,
            )
        )
    return points


def _matches(workflow: str, job: str, selector: str) -> bool:
    """`Workflow` matches a whole workflow; `Workflow/job` matches one job or its shards.

    Shards arrive as `cypress-matrix (1)`, so the job side is a prefix match: naming every shard
    would make the selector depend on the size of a matrix that changes.
    """
    if "/" not in selector:
        return workflow == selector
    wanted_workflow, wanted_job = selector.split("/", 1)
    return workflow == wanted_workflow and job.startswith(wanted_job)


def savings_if_removed(
    store: FactStore, repo: str, workflows: Sequence[str]
) -> tuple[float, float]:
    """What retiring some jobs would return: (minutes off the median pull request, its fraction).

    Computed against the whole recorded history for the repository rather than the latest period,
    so the answer does not swing on one quiet week. The fraction is of the current median, which
    is the number the retirement argument is actually made against.
    """
    rows = store.query(
        "SELECT pr_number, workflow, job, minutes FROM fact_ci_job"
        " WHERE repo = ? AND pr_number IS NOT NULL",
        (repo,),
    )
    totals: dict[int, float] = defaultdict(float)
    removed: dict[int, float] = defaultdict(float)
    for row in rows:
        pr_number = int(row["pr_number"])
        minutes = float(row["minutes"])
        totals[pr_number] += minutes
        if any(_matches(str(row["workflow"]), str(row["job"]), name) for name in workflows):
            removed[pr_number] += minutes

    if not totals:
        return (0.0, 0.0)
    current = median(totals.values())
    saved = median([removed[pr_number] for pr_number in totals])
    if current <= 0:
        return (round(saved, 1), 0.0)
    return (round(saved, 1), round(saved / current, 4))
