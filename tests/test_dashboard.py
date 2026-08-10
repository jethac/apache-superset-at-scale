"""Tests for the operator page's payload builders and its supply-chain constraint.

`scoreboard.debt` and `scoreboard.cicost` are written in a parallel session, so the debt and CI
series are supplied here as fakes shaped to the agreed contract. The fakes live in the test file
only: stubbing them in `src` would leave a module that silently answers with invented numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scoreboard import dashboard
from scoreboard.dashboard import (
    DASHBOARD_HTML,
    build_router,
    ci_cost_payload,
    dashboard_payload,
    debt_comparable_payload,
    debt_payload,
    fleet_payload,
    flow_payload,
    funnel_payload,
    outbox_payload,
    thesis_payload,
    throughput_payload,
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


def test_the_comparable_series_holds_the_rule_set_fixed_across_every_run() -> None:
    """Totals over the shared rules are smaller than the headline count, and that is the point."""
    comparable = debt_comparable_payload(
        debt_payload(FactStore(":memory:"), REPO, fake_debt_series)
    )

    assert [entry["total"] for entry in comparable] == [400, 350, 92]
    assert {entry["rules"] for entry in comparable} == {1}


def test_a_rule_no_run_shares_leaves_nothing_to_compare() -> None:
    """Counting a rule as zero where it was never measured would invent a fall in debt."""
    points = [
        FakeDebtPoint(DAY, REPO, "a", 10, {"eqeqeq": 10}, True, ""),
        FakeDebtPoint(DAY + timedelta(days=1), REPO, "b", 4, {"no-console": 4}, False, "swapped"),
    ]
    payload = debt_payload(FactStore(":memory:"), REPO, series=lambda store, repo: points)

    assert debt_comparable_payload(payload) == []


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


def test_the_fleet_keeps_finished_sessions_and_puts_running_ones_first(
    store: FactStore, scope: ScopeConfig
) -> None:
    """A reviewer arrives after the run, so a roster of only in-flight work would read as empty."""
    seed(store, scope)

    sessions = fleet_payload(store)

    assert sessions
    assert all(session["session_id"] for session in sessions)
    running = [index for index, s in enumerate(sessions) if s["running"]]
    finished = [index for index, s in enumerate(sessions) if not s["running"]]
    assert not (running and finished) or max(running) < min(finished)
    assert any(not session["running"] for session in sessions)
    for session in sessions:
        assert session["session_url"].startswith("https://app.devin.ai/sessions/")
        assert "devin-" not in session["session_url"]


def test_throughput_excludes_running_sessions_from_the_delivery_rate(
    store: FactStore, scope: ScopeConfig
) -> None:
    """A session still running is not evidence of failure, so it cannot sit in the denominator."""
    seed(store, scope)

    throughput = throughput_payload(store, REPO, cost_series=fake_cost_series)
    delivered = int(cast(int, throughput["delivered"]))
    settled = (
        delivered + int(cast(int, throughput["escalated"])) + int(cast(int, throughput["errored"]))
    )
    daily = cast(list[dict[str, int]], throughput["daily"])

    assert throughput["sessions_started"] == settled + int(cast(int, throughput["in_flight"]))
    assert throughput["delivery_rate"] == pytest.approx(delivered / settled, abs=1e-3)
    assert sum(day["started"] for day in daily) == throughput["sessions_started"]


def test_the_debt_claim_never_compares_across_a_ruleset_change(
    store: FactStore, scope: ScopeConfig
) -> None:
    """The last point is not comparable to its predecessor, so raw totals must not be subtracted.

    677 to 92 would be the headline arithmetic and it is meaningless: two rules left the tracker
    between them. The claim falls back to the one rule every run measured, and says so.
    """
    seed(store, scope)
    throughput = throughput_payload(store, REPO, cost_series=fake_cost_series)

    debt, cost, shipped = thesis_payload(
        store, REPO, throughput, debt_series=fake_debt_series, cost_series=fake_cost_series
    )

    assert debt["from"] == 400
    assert debt["value"] == 92
    assert debt["status"] == "improving"
    assert "1 rules measured by all 3 runs" in str(debt["detail"])
    assert "677" not in str(debt["detail"])
    assert cost["status"] == "improving"
    assert shipped["unit"] == "issues shipped"


def test_the_cost_claim_ignores_a_period_holding_a_single_pull_request(
    store: FactStore, scope: ScopeConfig
) -> None:
    """One PR in a week is that PR's cost, not the week's, and a heavy one fakes a saving."""

    def thin_then_real(store: FactStore, repo: str, period: str = "week") -> list[FakeCostPoint]:
        return [replace(COST_POINTS[0], prs=1, median_minutes_per_pr=48.5), COST_POINTS[1]]

    seed(store, scope)
    throughput = throughput_payload(store, REPO, cost_series=thin_then_real)

    _, cost, _ = thesis_payload(
        store, REPO, throughput, debt_series=fake_debt_series, cost_series=thin_then_real
    )

    assert cost["status"] == "not yet comparable"
    assert "48.5" not in str(cost["detail"])


def test_a_claim_with_no_measurements_reads_as_unproven_rather_than_achieved() -> None:
    def no_debt(store: FactStore, repo: str) -> list[FakeDebtPoint]:
        return []

    def no_cost(store: FactStore, repo: str, period: str = "week") -> list[FakeCostPoint]:
        return []

    store = FactStore(":memory:")
    throughput = throughput_payload(store, REPO, cost_series=no_cost)

    claims = thesis_payload(store, REPO, throughput, debt_series=no_debt, cost_series=no_cost)

    assert [claim["status"] for claim in claims] == ["no data", "no data", "no data"]
    assert throughput["delivery_rate"] is None
    assert throughput["acus_per_delivered_pr"] is None


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
    assert {
        "repo",
        "generated_at",
        "thesis",
        "throughput",
        "debt",
        "ci_cost",
        "flow",
        "fleet",
        "funnel",
        "outbox",
    } <= set(payload)


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


def test_dispatch_graph_branches_one_edge_per_session(store: FactStore, scope: ScopeConfig) -> None:
    """Fan-out has to survive as structure: a session cannot be aggregated into a day's total."""
    seed(store, scope)

    graph = cast(dict[str, list[dict[str, object]]], dashboard.dispatch_graph_payload(store))
    sessions = [node for node in graph["nodes"] if node["kind"] == "session"]
    bursts = [node for node in graph["nodes"] if node["kind"] == "dispatch"]
    outcomes = [node for node in graph["nodes"] if node["kind"] == "outcome"]

    assert sessions and bursts and outcomes
    assert len({node["id"] for node in graph["nodes"]}) == len(graph["nodes"])
    assert len(graph["edges"]) == 2 * len(sessions)
    assert sum(cast(int, node["count"]) for node in bursts) == len(sessions)
    assert sum(cast(int, node["count"]) for node in outcomes) == len(sessions)
    ids = {node["id"] for node in graph["nodes"]}
    assert all({edge["source"], edge["target"]} <= ids for edge in graph["edges"])
    for node in sessions:
        assert cast(str, node["session_url"]).startswith("https://app.devin.ai/sessions/")


def test_debt_and_ci_are_read_from_the_measured_repo_not_the_write_target(
    store: FactStore, scope: ScopeConfig
) -> None:
    """Sessions land on the fork; debt and CI minutes describe the project the fork came from."""
    seen: list[str] = []

    def recording_debt_series(store: FactStore, repo: str) -> list[FakeDebtPoint]:
        seen.append(repo)
        return DEBT_POINTS

    payload = dashboard_payload(
        store,
        REPO,
        debt_series=recording_debt_series,
        cost_series=fake_cost_series,
        measure_repo="apache/superset",
    )

    assert payload["repo"] == REPO
    assert payload["measure_repo"] == "apache/superset"
    assert set(seen) == {"apache/superset"}


def test_the_bare_hostname_lands_on_the_page(store: FactStore) -> None:
    """The link that gets pasted is the hostname, not the hostname plus a path.

    Checked without following the redirect so the target is asserted, not merely the eventual
    200 — a redirect to the wrong place still ends in a page.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(build_router(store, "jethac/superset"))
    response = TestClient(app).get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"
