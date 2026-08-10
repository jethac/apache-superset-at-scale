from __future__ import annotations

import pytest

from scoreboard.devin import FakeDevinClient, SessionState
from scoreboard.flow import build_edges, funnel, reconciles
from scoreboard.github import WriteNotPermittedError
from scoreboard.models import Decision, TaskState
from scoreboard.orchestrator import Orchestrator, build_prompt
from scoreboard.scope import Defaults, Match, Route, Rule, ScopeConfig
from scoreboard.store import FactStore
from tests.conftest import make_event


def orchestrator(store: FactStore, scope: ScopeConfig, **kwargs: object) -> Orchestrator:
    return Orchestrator(scope=scope, store=store, devin=FakeDevinClient(seed=3), **kwargs)  # type: ignore[arg-type]


def test_dry_run_records_the_task_without_creating_a_session(
    store: FactStore, scope: ScopeConfig
) -> None:
    task = orchestrator(store, scope, dry_run=True).handle(make_event(labels=["bug"]))
    assert task.state is TaskState.TRIGGERED
    assert task.session_id is None
    assert "dry run" in task.decision.reason


def test_live_run_creates_a_session(store: FactStore, scope: ScopeConfig) -> None:
    task = orchestrator(store, scope, dry_run=False).handle(make_event(labels=["bug"]))
    assert task.session_id is not None
    assert task.state is not TaskState.TRIGGERED


def test_redelivery_is_deduped_rather_than_spawning_a_second_session(
    store: FactStore, scope: ScopeConfig
) -> None:
    runner = orchestrator(store, scope, dry_run=False)
    event = make_event(labels=["bug"])
    first = runner.handle(event)
    second = runner.handle(event)
    assert second.state is TaskState.DEDUPED
    assert second.task_id == first.task_id
    assert len(runner.devin.sessions) == 1  # type: ignore[union-attr]


def test_a_resighting_never_overwrites_the_state_of_the_work_it_duplicates(
    store: FactStore, scope: ScopeConfig
) -> None:
    """Intake sees the same open issue every poll; a running session must survive that."""
    runner = orchestrator(store, scope, dry_run=False)
    event = make_event(labels=["bug"])
    started = runner.handle(event)
    runner.handle(event)
    runner.handle(event)

    row = store.query("SELECT state, session_id, dedupe_hits FROM fact_task")[0]
    assert row["state"] == started.state.value
    assert row["session_id"] == started.session_id
    assert row["dedupe_hits"] == 2
    assert funnel(store)["deduped"] == 2


def test_filtered_event_never_reaches_the_devin_client(
    store: FactStore, scope: ScopeConfig
) -> None:
    runner = orchestrator(store, scope, dry_run=False)
    runner.handle(make_event(labels=["question"], title="How do I deploy?"))
    assert runner.devin.sessions == {}  # type: ignore[union-attr]


def test_upstream_target_is_refused_before_any_spend(store: FactStore) -> None:
    config = ScopeConfig(
        version=1,
        defaults=Defaults(target_repo="apache/superset"),
        rules=[Rule(id="bad", when=Match(labels_any=["bug"]), then=Route(stream="bugfix"))],
    )
    runner = Orchestrator(scope=config, store=store, devin=FakeDevinClient(), dry_run=False)
    with pytest.raises(WriteNotPermittedError):
        runner.handle(make_event(labels=["bug"]))
    assert runner.devin.sessions == {}


def test_prompt_names_the_target_repo_and_forbids_others() -> None:
    decision = Decision(admitted=True, reason="x", target_repo="jethac/superset")
    prompt = build_prompt(make_event(repo="apache/superset"), decision)
    assert "Open the pull request against jethac/superset" in prompt
    assert "Do not push to any other repository" in prompt


def test_no_action_needed_counts_as_delivered_work(store: FactStore, scope: ScopeConfig) -> None:
    class Investigator(FakeDevinClient):
        def create_session(self, request: object) -> SessionState:  # type: ignore[override]
            return SessionState(
                session_id="s1",
                status="blocked",
                status_detail="finished",
                pr_url=None,
                acus_consumed=1.0,
                structured_output={"outcome": "no_action_needed", "summary": "not a bug"},
            )

    runner = Orchestrator(scope=scope, store=store, devin=Investigator(), dry_run=False)
    task = runner.handle(make_event(labels=["bug"]))
    assert task.state is TaskState.WORK_DELIVERED


def test_flow_conserves_every_task(store: FactStore, scope: ScopeConfig) -> None:
    runner = orchestrator(store, scope, dry_run=False)
    for number in range(12):
        labels = ["bug"] if number % 2 else ["question"]
        runner.handle(make_event(number=number, labels=labels))
    counts = funnel(store)
    assert counts["triggered"] == 12
    assert reconciles(counts)


def test_sankey_edges_carry_the_stream(store: FactStore, scope: ScopeConfig) -> None:
    orchestrator(store, scope, dry_run=False).handle(make_event(labels=["bug"]))
    edges = build_edges(store)
    assert edges
    assert {edge.stream for edge in edges} == {"bugfix"}


class WorkingThenFinished(FakeDevinClient):
    """A session that reports no outcome at creation and a pull request on the next poll."""

    def create_session(self, request: object) -> SessionState:  # type: ignore[override]
        return SessionState(
            session_id="s-working",
            status="running",
            status_detail="working",
            pr_url=None,
            acus_consumed=0.4,
            structured_output={},
        )

    def get_session(self, session_id: str) -> SessionState:
        return SessionState(
            session_id=session_id,
            status="blocked",
            status_detail="finished",
            pr_url="https://github.com/jethac/superset/pull/4242",
            acus_consumed=3.1,
            structured_output={"outcome": "pr_opened"},
        )


def test_sync_moves_a_still_working_session_to_its_outcome(
    store: FactStore, scope: ScopeConfig
) -> None:
    runner = Orchestrator(scope=scope, store=store, devin=WorkingThenFinished(), dry_run=False)
    task = runner.handle(make_event(labels=["bug"]))
    assert task.state is TaskState.SESSION_STARTED
    assert funnel(store)["in_flight"] == 1

    moved = runner.sync()

    assert moved == [(task.task_id, TaskState.WORK_DELIVERED)]
    counts = funnel(store)
    assert counts["in_flight"] == 0
    assert counts["work_delivered"] == 1
    assert reconciles(counts)
    assert runner.sync() == []
