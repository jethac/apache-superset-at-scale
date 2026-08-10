"""Turn a Superset clone into a debt time series, one historical commit at a time.

`debt.scan` answers "how much debt is there now", which is one point. A trend line needs points
that already happened, and the only honest way to get them is to measure the code as it stood: the
counts are produced by running today's linter over an old tree, so what moves between points is the
code rather than the instrument.

Two choices follow from what the series is read as. Commits are chosen by interval boundary rather
than by counting commits, because Superset's commit density varies by an order of magnitude across
a year and every-Nth-commit would sample a busy month twenty times and a quiet one once — a shape
of the release calendar masquerading as a shape of the debt. And a commit whose tree has no oxlint
configuration is skipped rather than measured bare: Superset adopted `oxlint.json` part way through
its history, and measuring the earlier commits with oxlint's defaults would reproduce exactly the
677-to-92 phantom drop that `debt` exists to prevent. Skipped commits leave a gap, which is what
absence of a measurement looks like.

Each measurement is stamped with its commit's author date, so `debt.series` orders and compares the
points as the history they describe rather than as the afternoon they were computed in. Comparabil-
ity then falls out of the existing rule: consecutive points share a `ruleset_id` exactly when they
measured the same rules, and the dashboard breaks the line where they do not.

Every checkout happens in a `git worktree` under a temporary directory. The clone a reviewer points
this at is usually one they are working in, and no measurement is worth checking out a year-old
commit over someone's uncommitted work.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .debt import DEFAULT_CONFIG, DebtObservation, ensure_schema, record_run, scan
from .store import FactStore

FRONTEND = "superset-frontend"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Commit:
    """A candidate measurement point: the commit and the moment its work was authored."""

    sha: str
    authored_at: datetime


@dataclass(frozen=True)
class SkippedCommit:
    sha: str
    reason: str


@dataclass(frozen=True)
class BackfillResult:
    """What the run measured and what it could not, so a caller can report both."""

    measured: list[Commit]
    skipped: list[SkippedCommit]


class Scanner(Protocol):
    """The measurement `backfill` performs, narrowed to what it calls.

    Tests substitute a fake linter through this rather than through the module's import, so the
    worktree lifecycle and the skip rules are exercised without a Node toolchain present.
    """

    def __call__(
        self,
        repo_path: Path,
        config: str,
        repo: str | None,
        measured_at: datetime | None,
    ) -> list[DebtObservation]: ...


def history(checkout: Path) -> list[Commit]:
    """Every commit reachable from HEAD, newest author date first.

    Author dates are not monotonic along a git history — a rebased or long-lived branch lands work
    dated before the commits already merged — so the list is sorted rather than trusted in log
    order. Selection asks "what did the tree look like on this date", which is a question about
    dates.
    """
    git = _git()
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [git, "log", "--format=%H %aI"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    commits = [
        Commit(sha=sha, authored_at=datetime.fromisoformat(stamp))
        for sha, _, stamp in (line.partition(" ") for line in completed.stdout.splitlines())
        if sha
    ]
    return sorted(commits, key=lambda commit: commit.authored_at, reverse=True)


def monthly_boundaries(months: int, until: datetime) -> list[datetime]:
    """The first instant of each of the last `months` months, oldest first.

    Calendar months rather than 30-day steps: a reviewer comparing this series against a release or
    a deployment date thinks in months, and a drifting boundary would put two samples in one month
    and none in the next.
    """
    if months < 1:
        raise ValueError("a backfill needs at least one interval")
    anchor = until.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC
    )
    boundaries: list[datetime] = []
    year, month = anchor.year, anchor.month
    for _ in range(months):
        boundaries.append(anchor.replace(year=year, month=month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return sorted(boundaries)


def select_commits(commits: Sequence[Commit], boundaries: Sequence[datetime]) -> list[Commit]:
    """The newest commit at or before each boundary, oldest first and without repeats.

    A boundary with no commit before it contributes nothing, and a quiet stretch that leaves the
    same commit newest at two boundaries contributes one point: measuring an unchanged tree twice
    would draw a flat segment that is an artefact of the sampling, not of the code.
    """
    ordered = sorted(commits, key=lambda commit: commit.authored_at, reverse=True)
    selected: list[Commit] = []
    seen: set[str] = set()
    for boundary in sorted(boundaries):
        for commit in ordered:
            if commit.authored_at <= boundary:
                if commit.sha not in seen:
                    seen.add(commit.sha)
                    selected.append(commit)
                break
    return selected


@contextmanager
def worktree(checkout: Path, sha: str) -> Iterator[Path]:
    """Check `sha` out beside the clone and take it away again, however the body ends.

    `--detach` leaves no branch behind, and the removal is forced because a lint run can leave
    generated files in the tree that would otherwise make the worktree look dirty and unremovable.
    """
    git = _git()
    parent = Path(tempfile.mkdtemp(prefix="scoreboard-backfill-"))
    path = parent / sha[:12]
    try:
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [git, "worktree", "add", "--detach", str(path), sha],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
        )
        yield path
    finally:
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [git, "worktree", "remove", "--force", str(path)],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [git, "worktree", "prune"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )


def backfill(
    store: FactStore,
    checkout: Path,
    repo: str,
    months: int = 12,
    config: str = DEFAULT_CONFIG,
    until: datetime | None = None,
    scanner: Scanner | None = None,
) -> BackfillResult:
    """Measure one commit per month of the last `months` and record each at its author date.

    A commit that cannot be measured is a gap, not a failure: the earliest commits predate
    `oxlint.json` and an intermediate one can fail to lint at all. The run continues and says so,
    because a twelve-month backfill that aborts on the oldest commit produces nothing.
    """
    ensure_schema(store)
    measure = scanner if scanner is not None else _default_scanner
    reference = until if until is not None else datetime.now(UTC)
    commits = select_commits(history(checkout), monthly_boundaries(months, reference))

    measured: list[Commit] = []
    skipped: list[SkippedCommit] = []
    for commit in commits:
        with worktree(checkout, commit.sha) as tree:
            if not (tree / FRONTEND / config).is_file():
                logger.warning(
                    "%s has no %s yet, so it cannot be measured", commit.sha[:12], config
                )
                skipped.append(SkippedCommit(sha=commit.sha, reason=f"no {config}"))
                continue
            try:
                observations = measure(tree, config, repo, commit.authored_at)
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning("%s could not be linted: %s", commit.sha[:12], error)
                skipped.append(SkippedCommit(sha=commit.sha, reason=str(error)))
                continue
        record_run(store, observations)
        measured.append(commit)

    return BackfillResult(measured=measured, skipped=skipped)


def _default_scanner(
    repo_path: Path, config: str, repo: str | None, measured_at: datetime | None
) -> list[DebtObservation]:
    return scan(repo_path, config=config, repo=repo, measured_at=measured_at)


def _git() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH, so history cannot be walked")
    return git
