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
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

from .flow import build_edges, funnel
from .outbox import list_outbox
from .store import FactStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"
LOZENGE_CSS = STATIC_DIR / "lozenge.min.css"


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


def debt_comparable_payload(points: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """The same measurements restricted to the rules every one of them measured.

    Rules enter and leave the configured set as the project edits `oxlint.json`, so consecutive
    raw totals are rarely comparable and a series of honest points can still refuse to answer
    "is debt falling". Holding the rule set fixed at the intersection answers it without pretending
    the instrument never changed: the totals are smaller than the headline count because they
    describe fewer rules, and a rule absent from any run is dropped rather than counted as zero.
    """
    by_rule_sets = [set(cast(dict[str, int], point["by_rule"])) for point in points]
    if not by_rule_sets:
        return []
    shared = set.intersection(*by_rule_sets)
    if not shared:
        return []
    return [
        {
            "measured_at": point["measured_at"],
            "total": sum(
                count
                for rule, count in cast(dict[str, int], point["by_rule"]).items()
                if rule in shared
            ),
            "rules": len(shared),
        }
        for point in points
    ]


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


SESSION_URL = "https://app.devin.ai/sessions/"


def fleet_payload(store: FactStore) -> list[dict[str, object]]:
    """Every Devin session this deployment has started, running ones first.

    A reviewer opens this page long after the run, so the fleet cannot be only what is in flight:
    a page showing an empty roster because the work already finished would be indistinguishable
    from one showing an automation that never started anything. Finished sessions therefore stay
    on the roster with their outcome, and each row links back to the session in the Devin app so
    the transcript is one click away rather than a claim made here.
    """
    rows = store.query(
        "SELECT task_id, session_id, repo, target_repo, stream, state, pr_url, acus_consumed,"
        " created_at, updated_at FROM fact_task WHERE session_id IS NOT NULL"
        " ORDER BY (state = 'session_started') DESC, updated_at DESC"
    )
    return [
        {
            "task_id": str(row["task_id"]),
            "session_id": str(row["session_id"]),
            "session_url": SESSION_URL + str(row["session_id"]).removeprefix("devin-"),
            "repo": str(row["repo"]),
            "target_repo": str(row["target_repo"]),
            "stream": str(row["stream"] or "unrouted"),
            "state": str(row["state"]),
            "running": str(row["state"]) == "session_started",
            "pr_url": row["pr_url"],
            "acus_consumed": row["acus_consumed"],
            "started_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


SETTLED_STATES = ("work_delivered", "draft_awaiting_authorship", "escalated", "errored")
DELIVERED_STATES = ("work_delivered", "draft_awaiting_authorship")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def throughput_payload(
    store: FactStore,
    repo: str,
    cost_series: CostSeriesFn | None = None,
    measure_repo: str | None = None,
) -> dict[str, object]:
    """Is it working, and at what rate — answered from the same facts the flow is drawn from.

    Two numbers a reviewer actually argues about live here. The first is the delivery rate: of the
    sessions that have settled, how many produced a pull request rather than an escalation or an
    error. Sessions still running are excluded from the denominator rather than counted as
    failures, because a running session is not yet evidence either way. The second is cost: ACUs
    spent per delivered pull request, set against the CI minutes each pull request costs the
    project. Both are reported as `None` when the facts do not support them, since a plausible
    zero is worse than an admitted gap.
    """
    rows = store.query(
        "SELECT state, pr_url, acus_consumed, created_at, updated_at FROM fact_task"
        " WHERE session_id IS NOT NULL"
    )
    states = [str(row["state"]) for row in rows]
    started = len(rows)
    settled = [row for row in rows if str(row["state"]) in SETTLED_STATES]
    delivered = [row for row in rows if str(row["state"]) in DELIVERED_STATES]
    acus = [float(row["acus_consumed"]) for row in rows if row["acus_consumed"] is not None]
    hours = [
        (
            datetime.fromisoformat(str(row["updated_at"]))
            - datetime.fromisoformat(str(row["created_at"]))
        ).total_seconds()
        / 3600
        for row in delivered
    ]
    cost_points = ci_cost_payload(store, measure_repo or repo, series=cost_series)
    minutes_per_pr = cost_points[-1]["median_minutes_per_pr"] if cost_points else None
    prs = len([row for row in rows if row["pr_url"]])
    median_hours = _median(hours)

    return {
        "sessions_started": started,
        "in_flight": states.count("session_started"),
        "delivered": len(delivered),
        "escalated": states.count("escalated"),
        "errored": states.count("errored"),
        "pull_requests": prs,
        "delivery_rate": round(len(delivered) / len(settled), 3) if settled else None,
        "median_hours_to_delivery": round(median_hours, 2) if median_hours is not None else None,
        "acus_total": round(sum(acus), 2) if acus else None,
        "acus_per_delivered_pr": (
            round(sum(acus) / len(delivered), 2) if acus and delivered else None
        ),
        "ci_minutes_per_pr": minutes_per_pr,
        "ci_minutes_committed": (
            round(prs * float(cast(float, minutes_per_pr)), 1) if minutes_per_pr and prs else None
        ),
        "daily": _daily_throughput(rows),
    }


def _daily_throughput(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    """Sessions started and pull requests delivered per day, oldest first."""
    started: dict[str, int] = {}
    delivered: dict[str, int] = {}
    for row in rows:
        started[str(row["created_at"])[:10]] = started.get(str(row["created_at"])[:10], 0) + 1
        if str(row["state"]) in DELIVERED_STATES:
            day = str(row["updated_at"])[:10]
            delivered[day] = delivered.get(day, 0) + 1
    return [
        {"day": day, "started": started.get(day, 0), "delivered": delivered.get(day, 0)}
        for day in sorted(set(started) | set(delivered))
    ]


OUTCOME_NODES = {
    "session_started": ("In flight", "running"),
    "work_delivered": ("Pull request delivered", "delivered"),
    "draft_awaiting_authorship": ("Draft awaiting authorship", "delivered"),
    "escalated": ("Escalated to human", "escalated"),
    "errored": ("Errored", "errored"),
}


def dispatch_graph_payload(store: FactStore) -> dict[str, object]:
    """Dispatches and their outcomes as a directed graph, one edge per Devin session.

    A bar chart of sessions per day answers "how many" and hides the thing worth seeing, which is
    that one trigger fans out to many Devins and those Devins do not all end the same way. As a
    graph, a fan-out is literally a branch: the burst node a reviewer can count, an edge per
    session carrying its own identity, and a terminal node naming what became of it. Nothing is
    aggregated away, so a session that errored cannot hide inside a day's total.
    """
    rows = store.query(
        "SELECT t.session_id, t.repo, e.number, t.stream, t.state, t.pr_url,"
        " t.created_at, t.updated_at"
        " FROM fact_task t LEFT JOIN fact_event e ON e.event_id = t.event_id"
        " WHERE t.session_id IS NOT NULL ORDER BY t.created_at"
    )
    bursts: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        bursts.setdefault(str(row["created_at"])[:10], []).append(row)

    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    for day, batch in sorted(bursts.items()):
        burst_id = f"burst:{day}"
        nodes.append(
            {
                "id": burst_id,
                "kind": "dispatch",
                "label": day,
                "detail": f"{len(batch)} session{'s' if len(batch) != 1 else ''} dispatched",
                "count": len(batch),
                "at": day,
            }
        )
        for row in batch:
            session_id = str(row["session_id"])
            label, outcome = OUTCOME_NODES.get(str(row["state"]), ("Unknown", "running"))
            outcome_id = f"outcome:{day}:{outcome}"
            nodes.append(
                {
                    "id": f"session:{session_id}",
                    "kind": "session",
                    "label": f"{row['repo']}#{row['number']}"
                    if row["number"]
                    else str(row["repo"]),
                    "detail": str(row["stream"] or "unrouted"),
                    "session_url": SESSION_URL + session_id.removeprefix("devin-"),
                    "pr_url": row["pr_url"],
                    "outcome": outcome,
                    "at": str(row["created_at"]),
                }
            )
            if not any(node["id"] == outcome_id for node in nodes):
                nodes.append(
                    {
                        "id": outcome_id,
                        "kind": "outcome",
                        "label": label,
                        "outcome": outcome,
                        "count": 0,
                        "at": str(row["updated_at"]),
                    }
                )
            for node in nodes:
                if node["id"] == outcome_id:
                    node["count"] = int(cast(int, node["count"])) + 1
            edges.append({"source": burst_id, "target": f"session:{session_id}"})
            edges.append(
                {
                    "source": f"session:{session_id}",
                    "target": outcome_id,
                    "outcome": outcome,
                }
            )
    return {"nodes": nodes, "edges": edges}


def _verdict(first: float, last: float, want_down: bool) -> str:
    """`improving`, `worsening` or `flat` — never a bare arrow, which reads as opinion."""
    if first == last:
        return "flat"
    fell = last < first
    return "improving" if fell is want_down else "worsening"


def _debt_claim(points: Sequence[dict[str, object]]) -> dict[str, object]:
    """Debt is only compared across points the measurement itself calls comparable.

    The series contains a rule-set change, so the first and last totals are measurements of
    different things. Taking the newest run of comparable points keeps the claim true; a
    first-to-last delta across the break would report rule removals as debt paid, which is the
    exact defect this deployment was built to stop repeating.
    """
    if not points:
        return {"status": "no data", "detail": "no oxlint measurements ingested yet"}
    run: list[dict[str, object]] = []
    for point in points:
        if point["comparable_to_previous"] is False:
            run = []
        run.append(point)
    if len(run) < 2:
        return _fixed_ruleset_claim(points)
    first = float(cast(float, run[0]["total"]))
    last = float(cast(float, run[-1]["total"]))
    return {
        "status": _verdict(first, last, want_down=True),
        "value": last,
        "unit": "violations",
        "from": first,
        "since": run[0]["measured_at"],
        "detail": (
            f"{int(first)} to {int(last)} across {len(run)} comparable measurements of the "
            "project's configured rule set"
        ),
    }


def _fixed_ruleset_claim(points: Sequence[dict[str, object]]) -> dict[str, object]:
    """The like-for-like answer when no two consecutive raw totals measured the same rules.

    Superset edits its rule set often enough that the raw series is a string of one-point runs, and
    a card that only ever says "not yet comparable" answers the reviewer's question with a shrug.
    Restricting to the rules common to every run is a real comparison, so long as the card says
    that is what it is.
    """
    comparable = debt_comparable_payload(points)
    if len(comparable) < 2:
        return {
            "status": "not yet comparable",
            "value": points[-1]["total"],
            "unit": "violations",
            "detail": (
                "only one measurement since the rule set last changed, so there is nothing "
                "honest to compare it against yet"
            ),
        }
    first = float(cast(float, comparable[0]["total"]))
    last = float(cast(float, comparable[-1]["total"]))
    rules = int(cast(int, comparable[-1]["rules"]))
    return {
        "status": _verdict(first, last, want_down=True),
        "value": last,
        "unit": "violations",
        "from": first,
        "since": comparable[0]["measured_at"],
        "detail": (
            f"{int(first)} to {int(last)} on the {rules} rules measured by all "
            f"{len(comparable)} runs; the configured set changed in between, so the headline "
            f"count of {points[-1]['total']} is not comparable across them"
        ),
    }


def _cost_claim(points: Sequence[dict[str, object]]) -> dict[str, object]:
    if len(points) < 2:
        return {"status": "no data", "detail": "not enough CI periods to show a direction yet"}
    first = float(cast(float, points[0]["median_minutes_per_pr"]))
    last = float(cast(float, points[-1]["median_minutes_per_pr"]))
    return {
        "status": _verdict(first, last, want_down=True),
        "value": last,
        "unit": "median minutes per PR",
        "from": first,
        "since": points[0]["period_start"],
        "detail": f"{first} to {last} median job-minutes billed per pull request",
    }


def _shipped_claim(daily: Sequence[dict[str, object]], delivered: int) -> dict[str, object]:
    """Shipping rate compares the two halves of the run rather than day to day.

    A day-over-day comparison of a fleet that dispatches in bursts is noise, and reporting noise as
    a trend is how a dashboard stops being believed.
    """
    if not delivered:
        return {
            "status": "no data",
            "value": 0,
            "unit": "issues shipped",
            "detail": "no pull request has been delivered yet",
        }
    half = len(daily) // 2
    early = sum(int(cast(int, day["delivered"])) for day in daily[:half])
    late = sum(int(cast(int, day["delivered"])) for day in daily[half:])
    status = "improving" if late > early else _verdict(early, late, want_down=False)
    return {
        "status": status if half else "too early",
        "value": delivered,
        "unit": "issues shipped",
        "from": early,
        "since": daily[0]["day"] if daily else None,
        "detail": f"{delivered} pull requests delivered across {len(daily)} days of dispatch",
    }


def thesis_payload(
    store: FactStore,
    repo: str,
    throughput: dict[str, object],
    debt_series: DebtSeriesFn | None = None,
    cost_series: CostSeriesFn | None = None,
    measure_repo: str | None = None,
) -> list[dict[str, object]]:
    """The three claims the deployment is making, each with the evidence that settles it.

    The page leads with these because a reviewer's first question is not "what does the fleet do"
    but "is any of this working". Each claim carries its own status, including `no data`, so a
    claim with nothing behind it looks unproven rather than achieved.
    """
    daily = cast(Sequence[dict[str, object]], throughput["daily"])
    measured = measure_repo or repo
    return [
        {
            "goal": "Technical debt falls",
            "measure": "oxlint violations under the project's configured rule set",
            **_debt_claim(debt_payload(store, measured, series=debt_series)),
        },
        {
            "goal": "CI compute per pull request falls",
            "measure": "median billed job-minutes per pull request",
            **_cost_claim(ci_cost_payload(store, measured, series=cost_series)),
        },
        {
            "goal": "More issues ship",
            "measure": "pull requests delivered by the fleet",
            **_shipped_claim(daily, int(cast(int, throughput["delivered"]))),
        },
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
    """Drafts waiting on a human paragraph, oldest first, each linked to its pull request.

    The failing checks travel with the row so the page can name the rule holding the draft back
    rather than leaving a reviewer to read "awaiting authorship" as a stalled agent.
    """
    return [
        {
            "task_id": item.task_id,
            "pr_url": item.pr_url,
            "title": item.title,
            "profile": item.profile,
            "target_repo": item.target_repo,
            "blocked_by": item.failing_checks,
            "waiting_days": round(item.waiting_days, 2),
        }
        for item in list_outbox(store)
    ]


def dashboard_payload(
    store: FactStore,
    repo: str,
    debt_series: DebtSeriesFn | None = None,
    cost_series: CostSeriesFn | None = None,
    measure_repo: str | None = None,
) -> dict[str, object]:
    """One document per page load: the page makes exactly one request and draws from it.

    Two repositories, deliberately. Sessions and pull requests are the fork's, because that is
    where the fleet is permitted to write; debt and CI minutes are the upstream project's, because
    that is the codebase whose health the thesis is about. Measuring the fork's own CI would
    describe this deployment rather than the problem it exists to shrink.
    """
    measured = measure_repo or repo
    throughput = throughput_payload(store, repo, cost_series=cost_series, measure_repo=measured)
    debt = debt_payload(store, measured, series=debt_series)
    return {
        "repo": repo,
        "measure_repo": measured,
        "generated_at": datetime.now(UTC).isoformat(),
        "thesis": thesis_payload(
            store,
            repo,
            throughput,
            debt_series=debt_series,
            cost_series=cost_series,
            measure_repo=measured,
        ),
        "debt": debt,
        "debt_comparable": debt_comparable_payload(debt),
        "ci_cost": ci_cost_payload(store, measured, series=cost_series),
        "throughput": throughput,
        "flow": flow_payload(store),
        "fleet": fleet_payload(store),
        "dispatch_graph": dispatch_graph_payload(store),
        "funnel": funnel_payload(store),
        "outbox": outbox_payload(store),
    }


def build_router(store: FactStore, repo: str, measure_repo: str | None = None) -> APIRouter:
    """Router for the operator page and its data document, for `app.include_router`."""
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    def read_dashboard() -> HTMLResponse:
        """The page itself: one static file, no build step, no external asset."""
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    @router.get("/dashboard/lozenge.min.css")
    def read_stylesheet() -> Response:
        """The design system, vendored and served from this container rather than from a CDN."""
        return Response(
            LOZENGE_CSS.read_text(encoding="utf-8"),
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/dashboard/data")
    def read_dashboard_data() -> dict[str, object]:
        return dashboard_payload(store, repo, measure_repo=measure_repo)

    return router
