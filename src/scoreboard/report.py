"""The pasteable artefact: the dashboard's document rendered as a markdown status report.

The operator page answers a browser. An issue comment, a pull request description and a status
email do not run JavaScript, and that is where a review of this deployment actually happens — so
the same facts have to leave the container as text a reviewer can quote. Nothing here decides
anything: every number is read out of `dashboard_payload`, verdicts included, so the page and the
report cannot disagree about whether debt is falling. A claim the payload refuses to settle is
printed with the payload's own wording rather than resolved into a number here; inventing one
would be the defect the payload exists to prevent, committed one layer further out.

`render_report` is a pure function of the payload, which is what makes the output diffable: the
only wall-clock reading is the payload's `generated_at`, and it can be overridden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from .dashboard import CostSeriesFn, DebtSeriesFn, dashboard_payload
from .store import FactStore

MISSING = "—"


def _utc_iso(value: object) -> str:
    """Timestamps as UTC ISO-8601, whatever offset the fact store recorded them in.

    Rows written by different collectors carry different offsets, and a report that mixes them
    invites a reader to compare two times that are not on the same clock.
    """
    if value is None:
        return MISSING
    text = str(value)
    if not text:
        return MISSING
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _cell(value: object) -> str:
    """A table cell that admits a gap rather than printing a plausible zero."""
    if value is None or value == "":
        return MISSING
    return str(value)


def _link(url: object, label: str) -> str:
    if not url:
        return MISSING
    return f"[{label}]({url})"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _claim_value(claim: Mapping[str, object]) -> str:
    value = claim.get("value")
    if value is None:
        return MISSING
    unit = claim.get("unit")
    return f"{value} {unit}" if unit else str(value)


def _thesis_section(claims: Sequence[Mapping[str, object]]) -> list[str]:
    """The three claims with the verdicts the payload reached, `not yet comparable` included."""
    lines = ["## The thesis, in three claims", ""]
    if not claims:
        return lines + ["No claim has any measurement behind it yet.", ""]
    rows = [
        [
            _cell(claim.get("goal")),
            _cell(claim.get("status")),
            _claim_value(claim),
            _cell(claim.get("from")),
            _utc_iso(claim.get("since")),
            _cell(claim.get("detail")),
        ]
        for claim in claims
    ]
    lines.extend(_table(["Claim", "Verdict", "Now", "From", "Since", "Evidence"], rows))
    lines.append("")
    return lines


def _issue_labels(graph: Mapping[str, object]) -> dict[str, str]:
    """Session id to `repo#number`, so the roster can name the issue each session was given.

    The fleet rows carry the repository but not the issue; the dispatch graph already joins the
    event, so the label is taken from there rather than by querying the store again.
    """
    nodes = cast(Sequence[Mapping[str, object]], graph.get("nodes", []))
    labels: dict[str, str] = {}
    for node in nodes:
        if node.get("kind") != "session":
            continue
        session_id = str(node.get("id", "")).removeprefix("session:")
        labels[session_id] = str(node.get("label", ""))
    return labels


def _fleet_section(
    sessions: Sequence[Mapping[str, object]], graph: Mapping[str, object]
) -> list[str]:
    lines = ["## Fleet roster", ""]
    if not sessions:
        return lines + ["No Devin session has been started yet.", ""]
    labels = _issue_labels(graph)
    rows = [
        [
            _link(session.get("session_url"), str(session.get("session_id", ""))),
            _cell(labels.get(str(session.get("session_id", "")))),
            _cell(session.get("state")),
            _link(session.get("pr_url"), "PR"),
            _cell(session.get("acus_consumed")),
            _utc_iso(session.get("updated_at")),
        ]
        for session in sessions
    ]
    lines.extend(_table(["Session", "Issue", "State", "Pull request", "ACUs", "Updated"], rows))
    lines.append("")
    return lines


def _outbox_section(items: Sequence[Mapping[str, object]]) -> list[str]:
    """Drafts and the checks holding them: the queue is human latency, not an agent failure."""
    lines = ["## Authorship outbox", ""]
    if not items:
        return lines + ["No draft is waiting on a human paragraph.", ""]
    rows = [
        [
            _cell(item.get("title")),
            _link(item.get("pr_url"), "PR"),
            _cell(item.get("profile")),
            _cell(item.get("waiting_days")),
            ", ".join(str(check) for check in cast(Sequence[object], item.get("blocked_by", [])))
            or MISSING,
        ]
        for item in items
    ]
    lines.extend(_table(["Draft", "Pull request", "Profile", "Waiting (days)", "Blocked by"], rows))
    lines.append("")
    return lines


FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("triggered", "Triggered"),
    ("filtered", "Filtered (out of scope)"),
    ("deduped", "Deduped"),
    ("admitted", "Admitted"),
    ("in_flight", "In flight"),
    ("work_delivered", "Work delivered"),
    ("awaiting_authorship", "Draft awaiting authorship"),
    ("escalated", "Escalated to human"),
    ("errored", "Errored"),
)


def _funnel_section(counts: Mapping[str, int]) -> list[str]:
    lines = ["## Intake funnel", ""]
    if not counts or not counts.get("triggered"):
        return lines + ["No event has entered intake yet.", ""]
    rows = [[label, str(counts.get(key, 0))] for key, label in FUNNEL_STAGES]
    lines.extend(_table(["Stage", "Tasks"], rows))
    lines.append("")
    return lines


def render_report(payload: Mapping[str, object], generated_at: datetime | None = None) -> str:
    """Render one dashboard document as markdown, in the order a reviewer reads it.

    `generated_at` overrides the payload's timestamp so a caller — a test, or a scheduled job
    stamping its own run time — controls the only line that is not derived from the facts.
    """
    stamp = generated_at.astimezone(UTC).isoformat() if generated_at else None
    lines = [
        "# Devin @ apache/superset — status report",
        "",
        f"- Generated: {stamp or _utc_iso(payload.get('generated_at'))}",
        f"- Sessions and pull requests: {_cell(payload.get('repo'))}",
        f"- Debt and CI minutes measured on: {_cell(payload.get('measure_repo'))}",
        "",
    ]
    lines += _thesis_section(cast(Sequence[Mapping[str, object]], payload.get("thesis", [])))
    lines += _fleet_section(
        cast(Sequence[Mapping[str, object]], payload.get("fleet", [])),
        cast(Mapping[str, object], payload.get("dispatch_graph", {})),
    )
    lines += _outbox_section(cast(Sequence[Mapping[str, object]], payload.get("outbox", [])))
    lines += _funnel_section(cast(Mapping[str, int], payload.get("funnel", {})))
    return "\n".join(lines).rstrip() + "\n"


def build_report(
    store: FactStore,
    repo: str,
    measure_repo: str | None = None,
    debt_series: DebtSeriesFn | None = None,
    cost_series: CostSeriesFn | None = None,
    generated_at: datetime | None = None,
) -> str:
    """The report for one fact store, built from the page's own document."""
    payload = dashboard_payload(
        store,
        repo,
        debt_series=debt_series,
        cost_series=cost_series,
        measure_repo=measure_repo,
    )
    return render_report(payload, generated_at=generated_at)
