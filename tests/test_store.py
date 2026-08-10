from __future__ import annotations

import sqlite3
from pathlib import Path

from scoreboard.devin import FakeDevinClient
from scoreboard.models import TaskState
from scoreboard.orchestrator import Orchestrator
from scoreboard.scope import ScopeConfig
from scoreboard.store import FactStore
from tests.conftest import make_event


def _corrupt(path: Path, task_id: str) -> None:
    """Reproduce what the earlier intake wrote: the verdict replaced by `deduped`."""
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE fact_task SET state = ? WHERE task_id = ?",
        (TaskState.DEDUPED.value, task_id),
    )
    connection.commit()
    connection.close()


def test_a_filtered_task_overwritten_by_an_old_intake_is_restored_on_open(
    tmp_path: Path, scope: ScopeConfig
) -> None:
    path = tmp_path / "facts.db"
    store = FactStore(path)
    runner = Orchestrator(scope=scope, store=store, devin=FakeDevinClient(seed=1))
    task = runner.handle(make_event(labels=["question"], number=41))
    assert task.state is TaskState.FILTERED
    _corrupt(path, task.task_id)

    reopened = FactStore(path)
    row = reopened.query("SELECT state FROM fact_task WHERE task_id = ?", (task.task_id,))[0]
    assert row["state"] == TaskState.FILTERED.value


def test_a_task_with_a_session_is_left_for_the_poller_to_reconcile(
    tmp_path: Path, scope: ScopeConfig
) -> None:
    """Only rows with no session and no re-sightings can safely be re-derived from `admitted`."""
    path = tmp_path / "facts.db"
    store = FactStore(path)
    runner = Orchestrator(scope=scope, store=store, devin=FakeDevinClient(seed=1), dry_run=False)
    task = runner.handle(make_event(labels=["bug"], number=42))
    _corrupt(path, task.task_id)

    reopened = FactStore(path)
    row = reopened.query("SELECT state FROM fact_task WHERE task_id = ?", (task.task_id,))[0]
    assert row["state"] == TaskState.DEDUPED.value
