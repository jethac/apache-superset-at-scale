from __future__ import annotations

import pytest

from scoreboard.devin import FakeDevinClient, SessionState, SessionSummary
from scoreboard.flow import (
    NODE_ADMITTED,
    NODE_AWAITING_AUTHORSHIP,
    NODE_DELIVERED,
    NODE_ERRORED,
    NODE_ESCALATED,
    NODE_IN_FLIGHT,
    NODE_QUEUED,
    build_edges,
    funnel,
    reconciles,
)
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
    runner.handle(make_event(repo="apache/superset", labels=["question"], title="How do I deploy?"))
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


def test_admitted_work_reaches_its_outcome_through_in_flight(
    store: FactStore, scope: ScopeConfig
) -> None:
    """`In flight` is a stage every admitted task crosses, not a sibling of the outcomes."""
    runner = orchestrator(store, scope, dry_run=False)
    for number in range(8):
        runner.handle(make_event(number=number, labels=["bug"]))

    edges = build_edges(store)
    admitted = sum(e.task_count for e in edges if e.target == NODE_ADMITTED)
    into_flight = sum(
        e.task_count for e in edges if e.source == NODE_ADMITTED and e.target == NODE_IN_FLIGHT
    )
    assert admitted == into_flight
    assert not [e for e in edges if e.source == NODE_ADMITTED and e.target != NODE_IN_FLIGHT]

    outcomes = {NODE_DELIVERED, NODE_AWAITING_AUTHORSHIP, NODE_ESCALATED, NODE_ERRORED}
    reached = {e.target for e in edges if e.target in outcomes}
    assert reached
    assert all(e.source == NODE_IN_FLIGHT for e in edges if e.target in outcomes)


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


def _capped(scope: ScopeConfig, limit: int) -> ScopeConfig:
    return scope.model_copy(
        update={"defaults": scope.defaults.model_copy(update={"max_concurrent_sessions": limit})}
    )


def test_admitted_work_waits_when_the_fleet_is_at_its_limit(
    store: FactStore, scope: ScopeConfig
) -> None:
    """Admitting work and paying for it are separate acts; the limit is what separates them."""
    runner = orchestrator(store, _capped(scope, 0), dry_run=False)

    task = runner.handle(make_event(labels=["bug"]))

    assert task.state is TaskState.TRIGGERED
    assert task.session_id is None
    assert "queued" in task.decision.reason
    assert runner.devin.sessions == {}  # type: ignore[union-attr]


def test_queued_work_is_dispatched_on_the_next_sighting_once_there_is_room(
    store: FactStore, scope: ScopeConfig
) -> None:
    event = make_event(labels=["bug"])
    orchestrator(store, _capped(scope, 0), dry_run=False).handle(event)

    runner = orchestrator(store, _capped(scope, 5), dry_run=False)
    dispatched = runner.handle(event)

    assert dispatched.session_id is not None
    assert len(runner.devin.sessions) == 1  # type: ignore[union-attr]


class SilentAboutCost(FakeDevinClient):
    """A session the API reports no ACU figure for, as it does while one is still running."""

    def create_session(self, request: object) -> SessionState:  # type: ignore[override]
        return SessionState(
            session_id="s-costless",
            status="running",
            status_detail="working",
            pr_url=None,
            acus_consumed=None,
            structured_output={},
        )

    def get_session(self, session_id: str) -> SessionState:
        return SessionState(
            session_id=session_id,
            status="blocked",
            status_detail="finished",
            pr_url="https://github.com/jethac/superset/pull/99",
            acus_consumed=None,
            structured_output={"outcome": "pr_opened"},
        )


def test_an_unreported_acu_figure_stays_unreported_rather_than_becoming_zero(
    store: FactStore, scope: ScopeConfig
) -> None:
    """Zero ACUs is a claim that the work was free; absent data is not that claim."""
    runner = Orchestrator(scope=scope, store=store, devin=SilentAboutCost(), dry_run=False)
    task = runner.handle(make_event(labels=["bug"]))
    runner.sync()

    rows = store.query("SELECT acus_consumed FROM fact_task WHERE task_id = ?", (task.task_id,))
    assert rows[0]["acus_consumed"] is None


def test_work_waiting_for_capacity_is_queued_rather_than_counted_as_a_running_session(
    store: FactStore, scope: ScopeConfig
) -> None:
    """A queue is not a fleet: work with no session must not inflate what is in flight."""
    runner = orchestrator(store, _capped(scope, 0), dry_run=False)
    runner.handle(make_event(labels=["bug"]))

    counts = funnel(store)
    assert counts["queued"] == 1
    assert counts["in_flight"] == 0
    assert reconciles(counts)

    edges = build_edges(store)
    assert [e.target for e in edges if e.source == NODE_ADMITTED] == [NODE_QUEUED]


def test_an_issue_filtered_under_older_rules_is_admitted_once_the_rules_widen(
    store: FactStore, scope: ScopeConfig
) -> None:
    """Rules change; work that never started is routed again rather than kept on a stale verdict."""
    narrow = ScopeConfig(
        version=1,
        defaults=Defaults(target_repo="jethac/superset"),
        rules=[Rule(id="narrow", when=Match(labels_any=["bug"]), then=Route(stream="bugfix"))],
    )
    event = make_event(labels=[], number=6)
    filtered = orchestrator(store, narrow, dry_run=True).handle(event)
    assert filtered.state is TaskState.FILTERED

    readmitted = orchestrator(store, scope, dry_run=True).handle(event)

    assert readmitted.decision.admitted
    assert readmitted.state is TaskState.TRIGGERED
    row = store.query(
        "SELECT state, admitted FROM fact_task WHERE task_id = ?", (readmitted.task_id,)
    )[0]
    assert row["admitted"] == 1
    assert row["state"] == TaskState.TRIGGERED.value


def _adopting(scope: ScopeConfig) -> ScopeConfig:
    return scope.model_copy(
        update={
            "defaults": scope.defaults.model_copy(
                update={"adopt_session_tags": ["fde:initiative=superset-scoreboard"]}
            )
        }
    )


def _foreign(session_id: str, tags: list[str]) -> SessionSummary:
    return SessionSummary(
        session_id=session_id,
        title="Another Devin, same backlog",
        tags=tags,
        status="running",
        created_at=None,
    )


def test_a_session_started_elsewhere_joins_the_fleet(store: FactStore, scope: ScopeConfig) -> None:
    """The reviewer counts Devins in the Devin app; the roster has to agree with them."""
    devin = FakeDevinClient(seed=3)
    devin.foreign_sessions.append(
        _foreign(
            "devin-foreign",
            ["fde:initiative=superset-scoreboard", "fde:stream=techdebt"],
        )
    )
    runner = Orchestrator(scope=_adopting(scope), store=store, devin=devin, dry_run=False)

    assert runner.adopt() == ["devin-foreign"]
    row = store.query("SELECT state, session_id, stream FROM fact_task")[0]
    assert row["session_id"] == "devin-foreign"
    assert row["state"] == TaskState.SESSION_STARTED.value
    assert row["stream"] == "techdebt"
    assert store.count_sessions_in_flight() == 1


def test_adopting_twice_records_one_session(store: FactStore, scope: ScopeConfig) -> None:
    devin = FakeDevinClient(seed=3)
    devin.foreign_sessions.append(_foreign("devin-foreign", ["fde:initiative=superset-scoreboard"]))
    runner = Orchestrator(scope=_adopting(scope), store=store, devin=devin, dry_run=False)

    runner.adopt()

    assert runner.adopt() == []
    assert len(store.query("SELECT task_id FROM fact_task")) == 1


def test_a_session_outside_the_initiative_is_left_alone(
    store: FactStore, scope: ScopeConfig
) -> None:
    """The key can see the whole organisation; only this deployment's work is this deployment's."""
    devin = FakeDevinClient(seed=3)
    devin.foreign_sessions.append(_foreign("devin-unrelated", ["someone-elses-project"]))
    runner = Orchestrator(scope=_adopting(scope), store=store, devin=devin, dry_run=False)

    assert runner.adopt() == []
    assert store.query("SELECT task_id FROM fact_task") == []


def test_nothing_is_adopted_when_no_tag_claims_ownership(
    store: FactStore, scope: ScopeConfig
) -> None:
    devin = FakeDevinClient(seed=3)
    devin.foreign_sessions.append(_foreign("devin-foreign", ["fde:initiative=superset-scoreboard"]))
    unclaimed = scope.model_copy(
        update={"defaults": scope.defaults.model_copy(update={"adopt_session_tags": []})}
    )
    runner = Orchestrator(scope=unclaimed, store=store, devin=devin, dry_run=False)

    assert runner.adopt() == []
