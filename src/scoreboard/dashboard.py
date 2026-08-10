"""The operator page: debt over time, CI minutes per pull request, flow, funnel, outbox.

The page exists to answer one question honestly — is debt falling, is the CI bill per change
falling, and which work stream did it. The honesty constraint is the reason this module exists at
all rather than a spreadsheet: a debt series measured under a changing rule set is not a series,
it is two series drawn on one axis. Every debt entry therefore carries
`comparable_to_previous` and `ruleset_change` through to the JSON, and the page breaks the line
where they say it must. The Superset spreadsheet this supersedes shows 677 -> 92 as a smooth
slope; most of that fall is rules leaving the tracker, not violations being fixed.

`scoreboard.debt` and `scoreboard.cicost` are written in a parallel session. They are resolved at
call time rather than imported at module import time so that this module, its router and its
tests remain usable before those modules land; the payload builders also take the series function
as an argument, which is what the unit tests inject.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .flow import build_edges, funnel
from .outbox import list_outbox
from .store import FactStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"


class DebtPoint(Protocol):
    """One technical-debt measurement, as produced by `scoreboard.debt.series`."""

    @property
    def measured_at(self) -> datetime: ...
    @property
    def repo(self) -> str: ...
    @property
    def ruleset_id(self) -> str: ...
    @property
    def total(self) -> int: ...
    @property
    def by_rule(self) -> dict[str, int]: ...
    @property
    def comparable_to_previous(self) -> bool: ...
    @property
    def ruleset_change(self) -> str: ...


class WorkflowCost(Protocol):
    """Per-workflow share of the CI minutes spent on one period's pull requests."""

    @property
    def workflow(self) -> str: ...
    @property
    def jobs(self) -> int: ...
    @property
    def median_minutes(self) -> float: ...
    @property
    def total_minutes(self) -> float: ...


class CostPoint(Protocol):
    """CI compute spent per pull request over one period."""

    @property
    def period_start(self) -> datetime: ...
    @property
    def prs(self) -> int: ...
    @property
    def median_minutes_per_pr(self) -> float: ...
    @property
    def by_workflow(self) -> Sequence[WorkflowCost]: ...


class DebtSeriesFn(Protocol):
    def __call__(self, store: FactStore, repo: str) -> Sequence[DebtPoint]: ...


class CostSeriesFn(Protocol):
    def __call__(
        self, store: FactStore, repo: str, period: str = "week"
    ) -> Sequence[CostPoint]: ...


def _load(module_name: str, attribute: str) -> object:
    """Resolve a sibling module's entry point at call time.

    Import failure is deliberately not swallowed: a missing series is a deployment fault and
    should surface as one, not as an empty chart that reads as "debt is zero".
    """
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, attribute)


def default_debt_series() -> DebtSeriesFn:
    return cast(DebtSeriesFn, _load("debt", "series"))


def default_cost_series() -> CostSeriesFn:
    return cast(CostSeriesFn, _load("cicost", "cost_per_pr"))


def _rule_delta(previous: DebtPoint | None, point: DebtPoint) -> tuple[list[str], list[str]]:
    """Rules that entered and left the measured set, so the break marker can name them."""
    if previous is None:
        return [], []
    before = set(previous.by_rule)
    after = set(point.by_rule)
    return sorted(after - before), sorted(before - after)


def debt_payload(
    store: FactStore, repo: str, series: DebtSeriesFn | None = None
) -> list[dict[str, object]]:
    """Debt measurements in measurement order, carrying their own comparability.

    `comparable_to_previous` is passed through untouched. The front end breaks the line on it, so
    smoothing or defaulting it here would silently reintroduce the defect this page calls out.
    """
    points = (series or default_debt_series())(store, repo)
    payload: list[dict[str, object]] = []
    previous: DebtPoint | None = None
    for point in points:
        entered, left = _rule_delta(previous, point)
        payload.append(
            {
                "measured_at": point.measured_at.isoformat(),
                "repo": point.repo,
                "ruleset_id": point.ruleset_id,
                "total": point.total,
                "by_rule": dict(point.by_rule),
                "comparable_to_previous": bool(point.comparable_to_previous),
                "ruleset_change": point.ruleset_change,
                "rules_entered": entered,
                "rules_left": left,
            }
        )
        previous = point
    return payload


def ci_cost_payload(
    store: FactStore, repo: str, period: str = "week", series: CostSeriesFn | None = None
) -> list[dict[str, object]]:
    """CI minutes per pull request per period, with the per-workflow breakdown for the tooltip."""
    points = (series or default_cost_series())(store, repo, period=period)
    return [
        {
            "period_start": point.period_start.isoformat(),
            "prs": point.prs,
            "median_minutes_per_pr": round(point.median_minutes_per_pr, 2),
            "by_workflow": [
                {
                    "workflow": workflow.workflow,
                    "jobs": workflow.jobs,
                    "median_minutes": round(workflow.median_minutes, 2),
                    "total_minutes": round(workflow.total_minutes, 2),
                }
                for workflow in point.by_workflow
            ],
        }
        for point in points
    ]


def flow_payload(store: FactStore) -> list[dict[str, object]]:
    """Sankey edges, stream-tagged so the diagram can colour by work stream."""
    return [
        {
            "stream": edge.stream,
            "source": edge.source,
            "target": edge.target,
            "task_count": edge.task_count,
        }
        for edge in build_edges(store)
    ]


def funnel_payload(store: FactStore) -> dict[str, int]:
    return funnel(store)


def outbox_payload(store: FactStore) -> list[dict[str, object]]:
    """Drafts waiting on a human paragraph, oldest first, each linked to its pull request."""
    return [
        {
            "task_id": item.task_id,
            "pr_url": item.pr_url,
            "title": item.title,
            "waiting_days": round(item.waiting_days, 2),
        }
        for item in list_outbox(store)
    ]


def dashboard_payload(
    store: FactStore,
    repo: str,
    debt_series: DebtSeriesFn | None = None,
    cost_series: CostSeriesFn | None = None,
) -> dict[str, object]:
    """One document per page load: the page makes exactly one request and draws from it."""
    return {
        "repo": repo,
        "debt": debt_payload(store, repo, series=debt_series),
        "ci_cost": ci_cost_payload(store, repo, series=cost_series),
        "flow": flow_payload(store),
        "funnel": funnel_payload(store),
        "outbox": outbox_payload(store),
    }


def build_router(store: FactStore, repo: str) -> APIRouter:
    """Router for the operator page and its data document, for `app.include_router`."""
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    def read_dashboard() -> HTMLResponse:
        """The page itself: one static file, no build step, no external asset."""
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    @router.get("/dashboard/data")
    def read_dashboard_data() -> dict[str, object]:
        return dashboard_payload(store, repo)

    return router
