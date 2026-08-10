from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scoreboard.backfill import (
    Commit,
    Scanner,
    backfill,
    history,
    monthly_boundaries,
    select_commits,
    worktree,
)
from scoreboard.debt import DebtObservation, ruleset_id, series
from scoreboard.store import FactStore

REPO = "apache/superset"
CONFIG = "oxlint.json"
FRONTEND = "superset-frontend"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        ["git", *arguments],  # noqa: S607 - git resolved from PATH, as the module does
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str, authored_at: datetime, *, with_config: bool) -> str:
    frontend = repo / FRONTEND
    frontend.mkdir(exist_ok=True)
    (frontend / "index.ts").write_text(message, encoding="utf-8")
    if with_config:
        (frontend / CONFIG).write_text('{"rules": {}}', encoding="utf-8")
    stamp = authored_at.isoformat()
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
    )
    _git(repo, "add", "-A")
    subprocess.run(  # noqa: S603 - fixed argument list, no shell
        ["git", "commit", "-m", message],  # noqa: S607 - git resolved from PATH
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A three-commit repository whose oldest commit predates the linter configuration."""
    repo = tmp_path / "superset"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "before oxlint", datetime(2026, 5, 4, tzinfo=UTC), with_config=False)
    _commit(repo, "adopt oxlint", datetime(2026, 6, 4, tzinfo=UTC), with_config=True)
    _commit(repo, "more code", datetime(2026, 7, 4, tzinfo=UTC), with_config=True)
    return repo


def counting_scanner(counts: dict[str, int]) -> Scanner:
    """Build a fake linter returning fixed counts, so no Node toolchain is needed."""

    def scanner(
        repo_path: Path, config: str, repo: str | None, measured_at: datetime | None
    ) -> list[DebtObservation]:
        stamp = measured_at if measured_at is not None else NOW
        return [
            DebtObservation(
                measured_at=stamp,
                repo=repo if repo is not None else repo_path.name,
                commit_sha=_git(repo_path, "rev-parse", "HEAD"),
                config_path=config,
                ruleset_id=ruleset_id(counts),
                rule=rule,
                count=count,
            )
            for rule, count in sorted(counts.items())
        ]

    return scanner


def test_monthly_boundaries_are_calendar_month_starts_oldest_first() -> None:
    """Twelve boundaries walk back a year without drifting off the first of the month."""
    boundaries = monthly_boundaries(12, NOW)
    assert len(boundaries) == 12
    assert boundaries[0] == datetime(2025, 9, 1, tzinfo=UTC)
    assert boundaries[-1] == datetime(2026, 8, 1, tzinfo=UTC)
    assert all(boundary.day == 1 for boundary in boundaries)


def test_selection_takes_the_newest_commit_at_or_before_each_boundary() -> None:
    """Commit density must not decide the sample: a busy month contributes one point."""
    commits = [
        Commit(sha="a", authored_at=datetime(2026, 5, 2, tzinfo=UTC)),
        Commit(sha="b", authored_at=datetime(2026, 5, 20, tzinfo=UTC)),
        Commit(sha="c", authored_at=datetime(2026, 5, 29, tzinfo=UTC)),
        Commit(sha="d", authored_at=datetime(2026, 7, 3, tzinfo=UTC)),
    ]
    selected = select_commits(commits, monthly_boundaries(4, NOW))
    assert [commit.sha for commit in selected] == ["c", "d"]


def test_selection_skips_boundaries_that_predate_the_whole_history() -> None:
    """A repository younger than the window yields fewer points, not invented ones."""
    commits = [Commit(sha="a", authored_at=datetime(2026, 7, 3, tzinfo=UTC))]
    assert [commit.sha for commit in select_commits(commits, monthly_boundaries(6, NOW))] == ["a"]


def test_history_is_ordered_by_author_date_newest_first(checkout: Path) -> None:
    """Selection asks a question about dates, so the walk must answer in date order."""
    commits = history(checkout)
    assert [commit.authored_at.date().isoformat() for commit in commits] == [
        "2026-07-04",
        "2026-06-04",
        "2026-05-04",
    ]


def test_a_shallow_clone_is_refused_rather_than_measured(checkout: Path, tmp_path: Path) -> None:
    """A truncated clone would report an empty backfill as success, so it is named instead."""
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", checkout.as_uri(), str(shallow))

    with pytest.raises(RuntimeError, match="shallow"):
        history(shallow)


def test_the_worktree_is_removed_even_when_the_body_raises(checkout: Path) -> None:
    """A reviewer's clone must be left exactly as it was found, failure or not."""
    oldest = history(checkout)[-1]
    with pytest.raises(RuntimeError):
        with worktree(checkout, oldest.sha) as tree:
            assert (tree / FRONTEND / "index.ts").read_text(encoding="utf-8") == "before oxlint"
            escaped = tree
            raise RuntimeError("measurement failed")

    assert not escaped.exists()
    assert _git(checkout, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _git(checkout, "status", "--porcelain") == ""


def test_a_backfill_records_each_commit_at_its_author_date(
    store: FactStore, checkout: Path
) -> None:
    """Points stamped with the backfill's own clock would be one instant, not a series."""
    backfill(
        store,
        checkout,
        repo=REPO,
        months=4,
        until=NOW,
        scanner=counting_scanner({"eslint(no-console)": 7}),
    )

    points = series(store, REPO)
    assert [point.measured_at.date().isoformat() for point in points] == [
        "2026-06-04",
        "2026-07-04",
    ]
    assert [point.total for point in points] == [7, 7]


def test_commits_without_the_linter_configuration_are_skipped_not_measured(
    store: FactStore, checkout: Path
) -> None:
    """Superset adopted oxlint late; measuring earlier trees bare would invent a debt cliff."""
    result = backfill(
        store,
        checkout,
        repo=REPO,
        months=4,
        until=NOW,
        scanner=counting_scanner({"eslint(no-console)": 7}),
    )

    assert [commit.authored_at.date().isoformat() for commit in result.measured] == [
        "2026-06-04",
        "2026-07-04",
    ]
    assert [skipped.reason for skipped in result.skipped] == [f"no {CONFIG}"]


def test_a_failing_lint_run_leaves_a_gap_rather_than_ending_the_backfill(
    store: FactStore, checkout: Path
) -> None:
    """A twelve-month backfill that aborts on one bad commit produces nothing at all."""
    healthy = counting_scanner({"eslint(no-console)": 7})

    def flaky(
        repo_path: Path, config: str, repo: str | None, measured_at: datetime | None
    ) -> list[DebtObservation]:
        if measured_at is not None and measured_at.month == 6:
            raise RuntimeError("oxlint could not resolve its plugins")
        return healthy(repo_path, config, repo, measured_at)

    result = backfill(store, checkout, repo=REPO, months=4, until=NOW, scanner=flaky)

    assert [commit.authored_at.date().isoformat() for commit in result.measured] == ["2026-07-04"]
    assert [skipped.reason for skipped in result.skipped] == [
        f"no {CONFIG}",
        "oxlint could not resolve its plugins",
    ]


def test_a_ruleset_change_mid_backfill_breaks_the_line(store: FactStore, checkout: Path) -> None:
    """The dashboard reads comparable_to_previous, which a backfill must feed truthfully."""
    narrow = counting_scanner({"eslint(no-console)": 7})
    wide = counting_scanner({"eslint(no-console)": 7, "react-hooks(exhaustive-deps)": 3})

    def widening(
        repo_path: Path, config: str, repo: str | None, measured_at: datetime | None
    ) -> list[DebtObservation]:
        chosen = narrow if measured_at is not None and measured_at.month == 6 else wide
        return chosen(repo_path, config, repo, measured_at)

    backfill(store, checkout, repo=REPO, months=4, until=NOW, scanner=widening)

    points = series(store, REPO)
    assert [point.comparable_to_previous for point in points] == [False, False]
    assert "react-hooks(exhaustive-deps)" in points[1].ruleset_change


def test_repeated_boundaries_over_a_quiet_stretch_measure_one_commit_once(
    store: FactStore, checkout: Path
) -> None:
    """A flat segment drawn from re-measuring an unchanged tree would be a sampling artefact."""
    result = backfill(
        store,
        checkout,
        repo=REPO,
        months=4,
        until=NOW + timedelta(days=60),
        scanner=counting_scanner({"eslint(no-console)": 7}),
    )

    assert len({commit.sha for commit in result.measured}) == len(result.measured)
    assert len(series(store, REPO)) == len(result.measured)
