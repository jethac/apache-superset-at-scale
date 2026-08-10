"""SQLite fact store.

Every table is natural-keyed and every write is an upsert, so re-running collection over any
window produces identical rows. That property is what makes the baseline trustworthy: the
"before" panel is produced by the same code as the "after" panel, and a reviewer can re-run it.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .github import PullRequestFact
from .models import Authorship, Task
from .policy import CheckResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_event (
    event_id      TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    repo          TEXT NOT NULL,
    number        INTEGER,
    title         TEXT NOT NULL,
    labels        TEXT NOT NULL,
    severity      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    url           TEXT NOT NULL,
    raw_digest    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_task (
    task_id        TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL,
    repo           TEXT NOT NULL,
    target_repo    TEXT,
    stream         TEXT,
    rule_id        TEXT,
    admitted       INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    state          TEXT NOT NULL,
    session_id     TEXT,
    pr_url         TEXT,
    pr_is_draft    INTEGER NOT NULL DEFAULT 0,
    policy_profile TEXT,
    acus_consumed  REAL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_pr (
    pr_url         TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    number         INTEGER NOT NULL,
    author         TEXT NOT NULL,
    cohort         TEXT NOT NULL,
    opened_at      TEXT NOT NULL,
    merged_at      TEXT,
    closed_at      TEXT,
    additions      INTEGER NOT NULL,
    deletions      INTEGER NOT NULL,
    changed_files  INTEGER NOT NULL,
    review_rounds  INTEGER NOT NULL,
    first_push_checks_passed INTEGER
);

CREATE TABLE IF NOT EXISTS fact_policy_check (
    pr_url        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    profile       TEXT NOT NULL,
    check_name    TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    detail        TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    PRIMARY KEY (pr_url, check_name)
);

-- The authorship paragraph is stored verbatim. It is the evidence that a human wrote the pull
-- request, so normalising or trimming it would destroy the thing being evidenced.
CREATE TABLE IF NOT EXISTS fact_authorship (
    task_id       TEXT PRIMARY KEY,
    pr_url        TEXT NOT NULL,
    text          TEXT NOT NULL,
    author        TEXT NOT NULL,
    input_method  TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_daily (
    day           TEXT NOT NULL,
    counter_name  TEXT NOT NULL,
    value         REAL NOT NULL,
    PRIMARY KEY (day, counter_name)
);

CREATE INDEX IF NOT EXISTS idx_task_stream ON fact_task (stream);
CREATE INDEX IF NOT EXISTS idx_pr_cohort ON fact_pr (cohort, opened_at);
"""


class FactStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The webhook service handles requests on a thread pool, so the connection is shared
        # across threads and serialised by an explicit lock rather than by SQLite's owning-thread
        # check.
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def upsert_task(self, task: Task) -> None:
        event = task.event
        with self._tx() as connection:
            connection.execute(
                """
                INSERT INTO fact_event (event_id, event_type, repo, number, title, labels,
                                        severity, created_at, url, raw_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET title=excluded.title, labels=excluded.labels
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    event.repo,
                    event.number,
                    event.title,
                    ",".join(event.labels),
                    event.severity.value,
                    event.created_at.isoformat(),
                    event.url,
                    event.raw_digest,
                ),
            )
            connection.execute(
                """
                INSERT INTO fact_task (task_id, event_id, repo, target_repo, stream, rule_id,
                                       admitted, reason, state, session_id, pr_url, pr_is_draft,
                                       policy_profile, acus_consumed, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state=excluded.state,
                    session_id=COALESCE(excluded.session_id, fact_task.session_id),
                    pr_url=COALESCE(excluded.pr_url, fact_task.pr_url),
                    pr_is_draft=excluded.pr_is_draft,
                    policy_profile=COALESCE(excluded.policy_profile, fact_task.policy_profile),
                    acus_consumed=COALESCE(excluded.acus_consumed, fact_task.acus_consumed),
                    updated_at=excluded.updated_at
                """,
                (
                    task.task_id,
                    event.event_id,
                    event.repo,
                    task.decision.target_repo,
                    task.decision.stream,
                    task.decision.rule_id,
                    int(task.decision.admitted),
                    task.decision.reason,
                    task.state.value,
                    task.session_id,
                    task.pr_url,
                    int(task.pr_is_draft),
                    task.policy_profile,
                    task.acus_consumed,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )

    def task_exists(self, task_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM fact_task WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row is not None

    def upsert_pull_request(self, fact: PullRequestFact, cohort: str) -> None:
        with self._tx() as connection:
            connection.execute(
                """
                INSERT INTO fact_pr (pr_url, repo, number, author, cohort, opened_at, merged_at,
                                     closed_at, additions, deletions, changed_files,
                                     review_rounds, first_push_checks_passed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pr_url) DO UPDATE SET
                    cohort=excluded.cohort,
                    merged_at=excluded.merged_at,
                    closed_at=excluded.closed_at,
                    additions=excluded.additions,
                    deletions=excluded.deletions,
                    changed_files=excluded.changed_files,
                    review_rounds=excluded.review_rounds,
                    first_push_checks_passed=excluded.first_push_checks_passed
                """,
                (
                    fact.pr_url,
                    fact.repo,
                    fact.number,
                    fact.author,
                    cohort,
                    fact.opened_at.isoformat(),
                    fact.merged_at.isoformat() if fact.merged_at else None,
                    fact.closed_at.isoformat() if fact.closed_at else None,
                    fact.additions,
                    fact.deletions,
                    fact.changed_files,
                    fact.review_rounds,
                    None
                    if fact.first_push_checks_passed is None
                    else int(fact.first_push_checks_passed),
                ),
            )

    def record_policy_checks(
        self,
        task_id: str,
        pr_url: str,
        profile: str,
        results: list[CheckResult],
        checked_at: datetime,
    ) -> None:
        with self._tx() as connection:
            connection.executemany(
                """
                INSERT INTO fact_policy_check (pr_url, task_id, profile, check_name, passed,
                                               detail, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pr_url, check_name) DO UPDATE SET
                    passed=excluded.passed,
                    detail=excluded.detail,
                    checked_at=excluded.checked_at
                """,
                [
                    (
                        pr_url,
                        task_id,
                        profile,
                        result.name,
                        int(result.passed),
                        result.detail,
                        checked_at.isoformat(),
                    )
                    for result in results
                ],
            )

    def record_authorship(self, task_id: str, pr_url: str, authorship: Authorship) -> None:
        with self._tx() as connection:
            connection.execute(
                """
                INSERT INTO fact_authorship (task_id, pr_url, text, author, input_method,
                                             recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    text=excluded.text,
                    author=excluded.author,
                    input_method=excluded.input_method,
                    recorded_at=excluded.recorded_at
                """,
                (
                    task_id,
                    pr_url,
                    authorship.text,
                    authorship.author,
                    authorship.input_method,
                    authorship.recorded_at.isoformat(),
                ),
            )

    def authorship_for(self, task_id: str) -> Authorship | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT text, author, input_method, recorded_at FROM fact_authorship"
                " WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return Authorship(
            text=row["text"],
            author=row["author"],
            input_method=row["input_method"],
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
        )

    def set_task_state(
        self, task_id: str, state: str, updated_at: datetime, pr_is_draft: bool
    ) -> None:
        with self._tx() as connection:
            connection.execute(
                "UPDATE fact_task SET state = ?, pr_is_draft = ?, updated_at = ?"
                " WHERE task_id = ?",
                (state, int(pr_is_draft), updated_at.isoformat(), task_id),
            )

    def record_snapshot(self, day: datetime, counter_name: str, value: float) -> None:
        with self._tx() as connection:
            connection.execute(
                """
                INSERT INTO snapshot_daily (day, counter_name, value) VALUES (?, ?, ?)
                ON CONFLICT(day, counter_name) DO UPDATE SET value=excluded.value
                """,
                (day.date().isoformat(), counter_name, value),
            )

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, params).fetchall())

    def close(self) -> None:
        with self._lock:
            self._connection.close()
