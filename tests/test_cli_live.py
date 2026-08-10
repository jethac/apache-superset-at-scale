"""End-to-end tests for the two subcommands that read the outside world.

Both are wired up in `cli.main` rather than in a library function, so the wiring — which window is
asked for, which bound is applied, which store the rows land in — is only exercised by driving the
command line. The outside world itself is faked; no test here runs oxlint or touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scoreboard import cli, debt
from scoreboard.cicost import JobRun, parse_jobs
from scoreboard.models import WorkflowRunRef
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT

REPO = "apache/superset"
FIXTURE = (REPO_ROOT / "fixtures" / "ci-runs-sample.json").read_text()
JOBS_PER_RUN = len(parse_jobs(FIXTURE, REPO))


@dataclass
class FakeGitHub:
    """The two reads `cicost` makes, plus a record of the window it was asked for."""

    runs: list[WorkflowRunRef] = field(default_factory=list)
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)
    jobs_calls: list[int] = field(default_factory=list)

    def list_pull_request_runs(
        self, repo: str, since: datetime, until: datetime
    ) -> list[WorkflowRunRef]:
        self.windows.append((since, until))
        return list(self.runs)

    def get_run_jobs(self, repo: str, run_id: int) -> str:
        self.jobs_calls.append(run_id)
        return jobs_payload(run_id)


def jobs_payload(run_id: int) -> str:
    """The sample jobs response, re-stamped onto one run, as GitHub would return it."""
    document = json.loads(FIXTURE)
    for job in document["jobs"]:
        job["run_id"] = run_id
    return json.dumps(document)


def use_store(monkeypatch: pytest.MonkeyPatch, store: FactStore) -> None:
    """Point the command line at the fixture's database and at the repository's own config."""
    monkeypatch.setenv("DB_PATH", str(store.path))
    monkeypatch.setenv("SCOPE_PATH", str(REPO_ROOT / "scope.yaml"))
    monkeypatch.setenv("POLICY_PATH", str(REPO_ROOT / "policy.yaml"))
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)


def install_github(monkeypatch: pytest.MonkeyPatch, github: FakeGitHub) -> None:
    def factory(token: str | None = None, api_url: str = "") -> FakeGitHub:
        return github

    monkeypatch.setattr(cli, "HttpGitHubClient", factory)


def observations(measured_at: datetime, counts: dict[str, int]) -> list[debt.DebtObservation]:
    identity = debt.ruleset_id(counts)
    return [
        debt.DebtObservation(
            measured_at=measured_at,
            repo=REPO,
            commit_sha="3b164e42" + "0" * 32,
            config_path=debt.DEFAULT_CONFIG,
            ruleset_id=identity,
            rule=rule,
            count=count,
        )
        for rule, count in sorted(counts.items())
    ]


def test_measure_records_a_run_against_the_checkout_it_was_given(
    store: FactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded run is the evidence for the debt line, so the command must persist all of it."""
    use_store(monkeypatch, store)
    checkout = tmp_path / "superset"
    checkout.mkdir()
    calls: list[tuple[Path, str, str | None]] = []

    def fake_scan(
        repo_path: Path, config: str = debt.DEFAULT_CONFIG, repo: str | None = None
    ) -> list[debt.DebtObservation]:
        calls.append((repo_path, config, repo))
        return observations(
            datetime(2026, 1, 5, tzinfo=UTC),
            {"eslint(no-unused-vars)": 85, "react-hooks(exhaustive-deps)": 381},
        )

    monkeypatch.setattr(debt, "scan", fake_scan)

    assert cli.main(["measure", "--checkout", str(checkout), "--repo", REPO]) == 0

    assert calls == [(checkout, debt.DEFAULT_CONFIG, REPO)]
    rows = store.query("SELECT rule, count FROM fact_debt WHERE repo = ? ORDER BY rule", (REPO,))
    assert [(str(row["rule"]), int(row["count"])) for row in rows] == [
        ("eslint(no-unused-vars)", 85),
        ("react-hooks(exhaustive-deps)", 381),
    ]
    runs = store.query("SELECT total, config_path FROM fact_debt_run WHERE repo = ?", (REPO,))
    assert int(runs[0]["total"]) == 466
    assert str(runs[0]["config_path"]) == debt.DEFAULT_CONFIG


def test_measure_passes_the_named_configuration_through(
    store: FactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measuring a different rule set than the one asked for is the bug this repo exists for."""
    use_store(monkeypatch, store)
    configs: list[str] = []

    def fake_scan(
        repo_path: Path, config: str = debt.DEFAULT_CONFIG, repo: str | None = None
    ) -> list[debt.DebtObservation]:
        configs.append(config)
        return observations(datetime(2026, 1, 6, tzinfo=UTC), {"eqeqeq": 3})

    monkeypatch.setattr(debt, "scan", fake_scan)

    assert cli.main(["measure", "--checkout", str(tmp_path), "--config", ".oxlintrc.json"]) == 0
    assert configs == [".oxlintrc.json"]


def test_cicost_writes_a_job_row_for_every_job_of_every_run(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard's cost line is a median over these rows; a missing run moves the number."""
    use_store(monkeypatch, store)
    github = FakeGitHub(
        runs=[
            WorkflowRunRef(run_id=500001, pr_number=42, head_sha="a" * 40),
            WorkflowRunRef(run_id=500002, pr_number=None, head_sha="b" * 40),
        ]
    )
    install_github(monkeypatch, github)

    assert cli.main(["cicost", "--repo", REPO]) == 0

    assert github.jobs_calls == [500001, 500002]
    rows = store.query("SELECT COUNT(*) AS n FROM fact_ci_job WHERE repo = ?", (REPO,))
    assert int(rows[0]["n"]) == 2 * JOBS_PER_RUN
    attributed = store.query(
        "SELECT DISTINCT pr_number FROM fact_ci_job WHERE pr_number IS NOT NULL"
    )
    assert [int(row["pr_number"]) for row in attributed] == [42]


def test_cicost_reads_no_more_runs_than_max_runs_allows(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each run costs an API call, so the bound is what keeps a busy repository affordable."""
    use_store(monkeypatch, store)
    github = FakeGitHub(
        runs=[
            WorkflowRunRef(run_id=500000 + index, pr_number=index, head_sha="c" * 40)
            for index in range(1, 6)
        ]
    )
    install_github(monkeypatch, github)

    assert cli.main(["cicost", "--repo", REPO, "--max-runs", "2"]) == 0

    assert github.jobs_calls == [500001, 500002]
    rows = store.query("SELECT COUNT(*) AS n FROM fact_ci_job")
    assert int(rows[0]["n"]) == 2 * JOBS_PER_RUN


def test_cicost_ends_the_window_where_until_days_says(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-sampling an older window is how a before-and-after comparison is made at all."""
    use_store(monkeypatch, store)
    github = FakeGitHub()
    install_github(monkeypatch, github)

    assert cli.main(["cicost", "--repo", REPO, "--since-days", "14", "--until-days", "7"]) == 0

    since, until = github.windows[0]
    now = datetime.now(UTC)
    assert abs((now - until) - timedelta(days=7)) < timedelta(minutes=1)
    assert abs((until - since) - timedelta(days=7)) < timedelta(minutes=1)


def test_cicost_on_a_window_with_no_runs_writes_nothing_and_succeeds(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quiet window is a fact about the repository, not a failure of the collector."""
    use_store(monkeypatch, store)
    install_github(monkeypatch, FakeGitHub())

    assert cli.main(["cicost", "--repo", REPO]) == 0
    rows = store.query("SELECT COUNT(*) AS n FROM fact_ci_job")
    assert int(rows[0]["n"]) == 0


def test_a_run_whose_jobs_are_all_unfinished_is_not_recorded(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guessed duration would be indistinguishable from a measured one once it is in the table."""
    use_store(monkeypatch, store)

    @dataclass
    class RunningGitHub(FakeGitHub):
        def get_run_jobs(self, repo: str, run_id: int) -> str:
            self.jobs_calls.append(run_id)
            return (
                '{"total_count": 1, "jobs": [{"run_id": 500001, "workflow_name": "CI",'
                ' "name": "build", "status": "in_progress", "started_at":'
                ' "2026-01-14T11:03:00Z", "completed_at": null}]}'
            )

    github = RunningGitHub(runs=[WorkflowRunRef(run_id=500001, pr_number=7, head_sha="d" * 40)])
    install_github(monkeypatch, github)

    assert cli.main(["cicost", "--repo", REPO]) == 0
    rows = store.query("SELECT COUNT(*) AS n FROM fact_ci_job")
    assert int(rows[0]["n"]) == 0


def test_the_jobs_of_a_run_carry_the_runs_pull_request_number(
    store: FactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The jobs payload names no pull request, so the attribution can only come from the run."""
    use_store(monkeypatch, store)
    run = WorkflowRunRef(run_id=500003, pr_number=99, head_sha="e" * 40)
    install_github(monkeypatch, FakeGitHub(runs=[run]))

    assert cli.main(["cicost", "--repo", REPO]) == 0

    rows = store.query("SELECT job, pr_number, minutes FROM fact_ci_job ORDER BY job")
    expected: list[JobRun] = parse_jobs(FIXTURE, REPO)
    assert {int(row["pr_number"]) for row in rows} == {99}
    assert sum(float(row["minutes"]) for row in rows) == pytest.approx(
        sum(job.minutes for job in expected), abs=0.05
    )
