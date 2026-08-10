"""Tests for the operator page's payload builders and its supply-chain constraint.

`scoreboard.debt` and `scoreboard.cicost` are written in a parallel session, so the debt and CI
series are supplied here as fakes shaped to the agreed contract. The fakes live in the test file
only: stubbing them in `src` would leave a module that silently answers with invented numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scoreboard import dashboard
from scoreboard.dashboard import (
    DASHBOARD_HTML,
    build_router,
    ci_cost_payload,
    dashboard_payload,
    debt_payload,
    flow_payload,
    funnel_payload,
    outbox_payload,
)
from scoreboard.devin import FakeDevinClient
from scoreboard.models import TaskState
from scoreboard.orchestrator import Orchestrator
from scoreboard.policy import PolicyConfig
from scoreboard.scope import ScopeConfig
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT, make_event

POLICY = PolicyConfig.load(REPO_ROOT / "policy.yaml")
REPO = "jethac/superset"
DAY = datetime(2026, 1, 5, tzinfo=UTC)


@dataclass(frozen=True)
class FakeDebtPoint:
    measured_at: datetime
    repo: str
    ruleset_id: str
    total: int
    by_rule: dict[str, int]
    comparable_to_previous: bool
    ruleset_change: str


@dataclass(frozen=True)
class FakeWorkflowCost:
    workflow: str
    jobs: int
    median_minutes: float
    total_minutes: float


@dataclass(frozen=True)
class FakeCostPoint:
    period_start: datetime
    prs: int
    median_minutes_per_pr: float
    by_workflow: list[FakeWorkflowCost] = field(default_factory=list)


DEBT_POINTS = [
    FakeDebtPoint(
        measured_at=DAY,
        repo=REPO,
        ruleset_id="oxlint-full",
        total=677,
        by_rule={"no-unused-vars": 400, "eqeqeq": 200, "no-explicit-any": 77},
        comparable_to_previous=True,
        ruleset_change="",
    ),
    FakeDebtPoint(
        measured_at=DAY + timedelta(days=7),
        repo=REPO,
        ruleset_id="oxlint-full",
        total=612,
        by_rule={"no-unused-vars": 350, "eqeqeq": 190, "no-explicit-any": 72},
        comparable_to_previous=True,
        ruleset_change="",
    ),
    FakeDebtPoint(
        measured_at=DAY + timedelta(days=14),
        repo=REPO,
        ruleset_id="oxlint-default",
        total=92,
        by_rule={"no-unused-vars": 92},
        comparable_to_previous=False,
        ruleset_change="eqeqeq and no-explicit-any left the tracker; uploader omits --config",
    ),
]

COST_POINTS = [
    FakeCostPoint(
        period_start=DAY,
        prs=12,
        median_minutes_per_pr=48.5,
        by_workflow=[FakeWorkflowCost("ci.yml", 24, 30.0, 720.0)],
    ),
    FakeCostPoint(
        period_start=DAY + timedelta(days=7),
        prs=15,
        median_minutes_per_pr=31.256,
        by_workflow=[FakeWorkflowCost("ci.yml", 30, 20.0, 600.0)],
    ),
]


def fake_debt_series(store: FactStore, repo: str) -> list[FakeDebtPoint]:
    return DEBT_POINTS


def fake_cost_series(store: FactStore, repo: str, period: str = "week") -> list[FakeCostPoint]:
    return COST_POINTS


def seed(store: FactStore, scope: ScopeConfig) -> None:
    """Drive real events through the real orchestrator so the flow facts are not hand-written."""
    orchestrator = Orchestrator(
        scope=scope, store=store, devin=FakeDevinClient(seed=3), policy=POLICY, dry_run=False
    )
    for number in range(1, 40):
        orchestrator.handle(make_event(number=number, labels=["bug"]))


def test_debt_payload_carries_comparability_through_to_the_json() -> None:
    payload = debt_payload(FactStore(":memory:"), REPO, series=fake_debt_series)

    assert [entry["comparable_to_previous"] for entry in payload] == [True, True, False]
    break_entry = payload[-1]
    assert break_entry["ruleset_change"]
    assert break_entry["rules_left"] == ["eqeqeq", "no-explicit-any"]
    assert break_entry["rules_entered"] == []
    assert break_entry["total"] == 92


def test_debt_payload_never_infers_comparability_from_the_ruleset_id() -> None:
    """A same-total, same-ruleset point is still reported as the series reported it.

    The front end breaks the line on this flag alone, so the builder must not second-guess it.
    """
    points = [
        FakeDebtPoint(DAY, REPO, "oxlint-full", 100, {"a": 100}, True, ""),
        FakeDebtPoint(DAY, REPO, "oxlint-full", 90, {"a": 90}, False, "counting method changed"),
    ]
    payload = debt_payload(FactStore(":memory:"), REPO, series=lambda store, repo: points)
    assert payload[1]["comparable_to_previous"] is False
    assert payload[1]["ruleset_change"] == "counting method changed"


def test_ci_cost_payload_rounds_and_keeps_the_workflow_breakdown() -> None:
    payload = ci_cost_payload(FactStore(":memory:"), REPO, series=fake_cost_series)

    assert [entry["median_minutes_per_pr"] for entry in payload] == [48.5, 31.26]
    assert payload[0]["by_workflow"] == [
        {"workflow": "ci.yml", "jobs": 24, "median_minutes": 30.0, "total_minutes": 720.0}
    ]


def test_flow_and_funnel_payloads_come_from_the_fact_store(
    store: FactStore, scope: ScopeConfig
) -> None:
    seed(store, scope)

    edges = flow_payload(store)
    counts = funnel_payload(store)

    assert edges
    assert {"stream", "source", "target", "task_count"} == set(edges[0])
    assert counts["triggered"] == sum(
        counts[key]
        for key in (
            "filtered",
            "deduped",
            "work_delivered",
            "awaiting_authorship",
            "escalated",
            "errored",
            "in_flight",
        )
    )


def test_outbox_payload_links_every_row_to_its_pull_request(
    store: FactStore, scope: ScopeConfig
) -> None:
    seed(store, scope)
    rows = store.query(
        "SELECT COUNT(*) AS n FROM fact_task WHERE state = ?",
        (TaskState.DRAFT_AWAITING_AUTHORSHIP.value,),
    )
    if not rows[0]["n"]:
        pytest.skip("the seeded simulation produced no draft awaiting authorship")

    items = outbox_payload(store)
    assert items
    for item in items:
        assert str(item["pr_url"]).startswith("https://github.com/")
        assert isinstance(item["waiting_days"], float)


def test_dashboard_payload_has_every_section(store: FactStore, scope: ScopeConfig) -> None:
    seed(store, scope)
    payload = dashboard_payload(
        store, REPO, debt_series=fake_debt_series, cost_series=fake_cost_series
    )
    assert {"repo", "debt", "ci_cost", "flow", "funnel", "outbox"} <= set(payload)


def test_router_serves_the_page_and_one_data_document(
    store: FactStore, scope: ScopeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, scope)
    monkeypatch.setattr(dashboard, "default_debt_series", lambda: fake_debt_series)
    monkeypatch.setattr(dashboard, "default_cost_series", lambda: fake_cost_series)

    app = FastAPI()
    app.include_router(build_router(store, REPO))
    client = TestClient(app)

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]

    data = client.get("/dashboard/data").json()
    assert data["repo"] == REPO
    assert data["debt"][-1]["comparable_to_previous"] is False
    assert data["ci_cost"][0]["prs"] == 12
    assert data["funnel"]["triggered"] > 0


def test_page_loads_no_third_party_script_or_stylesheet() -> None:
    """Supply-chain control: the page may not pull code from anywhere but this container.

    The container runs with no egress, so a CDN reference would fail closed rather than execute —
    but the reason to forbid it is that a remote script is code we neither pin nor review, and a
    dashboard whose JavaScript can change under it is not evidence of anything.
    """
    html = Path(DASHBOARD_HTML).read_text(encoding="utf-8")

    sources = re.findall(
        r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"']+)["']""",
        html,
        flags=re.IGNORECASE,
    )
    assert [source for source in sources if source.startswith(("http://", "https://", "//"))] == []
    assert "@import" not in html
    assert "cdn" not in html.lower()
