"""Derive the Sankey edge list and the headline funnel from the fact store.

The edge list is the entire input to Superset's `sankey_v2` chart, which wants a flat
`source, target, metric` table. `stream` is carried alongside so node colour can encode the work
stream rather than the node name.

Flow is conserved by construction: every task that enters leaves through exactly one edge at each
stage, including the ones that went nowhere. Losses are named nodes, never missing ribbons — a
diagram that can quietly drop its failures is worthless as evidence.

Admitted work is dispatched into a session before it can produce anything, so `In flight` is a
stage rather than an outcome: every admitted task crosses it, and only the ones whose session has
not settled stop there. Drawing it as a sibling of the outcomes would put running work in the same
column as a delivered draft and read as though the two were the same stage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .models import TaskState
from .store import FactStore

NODE_INTAKE = "Intake"
NODE_ADMITTED = "Admitted"
NODE_FILTERED = "Filtered (out of scope)"
NODE_DEDUPED = "Deduped"
NODE_DELIVERED = "Work delivered"
NODE_AWAITING_AUTHORSHIP = "Draft awaiting authorship"
NODE_ESCALATED = "Escalated to human"
NODE_ERRORED = "Errored"
NODE_IN_FLIGHT = "In flight"


@dataclass(frozen=True)
class FlowEdge:
    stream: str
    source: str
    target: str
    task_count: int


_OUTCOMES: dict[TaskState, str] = {
    TaskState.WORK_DELIVERED: NODE_DELIVERED,
    TaskState.DRAFT_AWAITING_AUTHORSHIP: NODE_AWAITING_AUTHORSHIP,
    TaskState.ESCALATED: NODE_ESCALATED,
    TaskState.ERRORED: NODE_ERRORED,
}

_UNSETTLED: frozenset[TaskState] = frozenset({TaskState.SESSION_STARTED, TaskState.TRIGGERED})


def build_edges(store: FactStore) -> list[FlowEdge]:
    rows = store.query(
        "SELECT repo, COALESCE(stream, 'unrouted') AS stream, state, dedupe_hits FROM fact_task"
    )
    counts: Counter[tuple[str, str, str]] = Counter()

    for row in rows:
        stream = str(row["stream"])
        state = TaskState(row["state"])
        source_node = f"{row['repo']}"
        counts[(stream, source_node, NODE_INTAKE)] += 1

        # Re-sightings of the same issue enter intake again and leave as deduped; the work they
        # duplicate keeps its own ribbon.
        hits = int(row["dedupe_hits"])
        if hits:
            counts[(stream, source_node, NODE_INTAKE)] += hits
            counts[(stream, NODE_INTAKE, NODE_DEDUPED)] += hits

        if state is TaskState.FILTERED:
            counts[(stream, NODE_INTAKE, NODE_FILTERED)] += 1
            continue
        if state is TaskState.DEDUPED:
            counts[(stream, NODE_INTAKE, NODE_DEDUPED)] += 1
            continue

        counts[(stream, NODE_INTAKE, NODE_ADMITTED)] += 1
        counts[(stream, NODE_ADMITTED, NODE_IN_FLIGHT)] += 1
        if state not in _UNSETTLED:
            counts[(stream, NODE_IN_FLIGHT, _OUTCOMES[state])] += 1

    return [
        FlowEdge(stream=stream, source=source, target=target, task_count=count)
        for (stream, source, target), count in sorted(counts.items())
    ]


def funnel(store: FactStore) -> dict[str, int]:
    rows = store.query("SELECT state, COUNT(*) AS n FROM fact_task GROUP BY state")
    by_state = {str(row["state"]): int(row["n"]) for row in rows}
    hits = int(store.query("SELECT COALESCE(SUM(dedupe_hits), 0) AS n FROM fact_task")[0]["n"])
    admitted = sum(
        count
        for state, count in by_state.items()
        if state not in {TaskState.FILTERED.value, TaskState.DEDUPED.value}
    )
    return {
        "triggered": sum(by_state.values()) + hits,
        "filtered": by_state.get(TaskState.FILTERED.value, 0),
        "deduped": by_state.get(TaskState.DEDUPED.value, 0) + hits,
        "admitted": admitted,
        "work_delivered": by_state.get(TaskState.WORK_DELIVERED.value, 0),
        "awaiting_authorship": by_state.get(TaskState.DRAFT_AWAITING_AUTHORSHIP.value, 0),
        "escalated": by_state.get(TaskState.ESCALATED.value, 0),
        "errored": by_state.get(TaskState.ERRORED.value, 0),
        "in_flight": by_state.get(TaskState.SESSION_STARTED.value, 0)
        + by_state.get(TaskState.TRIGGERED.value, 0),
    }


def reconciles(counts: dict[str, int]) -> bool:
    """Acceptance check A7: every task is accounted for in exactly one terminal bucket."""
    terminal = (
        counts["filtered"]
        + counts["deduped"]
        + counts["work_delivered"]
        + counts["awaiting_authorship"]
        + counts["escalated"]
        + counts["errored"]
        + counts["in_flight"]
    )
    return terminal == counts["triggered"]
