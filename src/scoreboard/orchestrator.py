"""Event → decision → session. The one place that is allowed to spend money.

Ordering is deliberate: dedupe before routing, routing before spend, and the write-boundary
check before the session is created rather than after. Each of those is a control that only
works if nothing downstream can bypass it, so they all live here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .devin import DevinClient, SessionRequest
from .github import assert_writable
from .models import Decision, Event, Task, TaskState, make_task_id
from .scope import ScopeConfig
from .store import FactStore

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """You are working on {target_repo}.

Source of work: {source_repo} {kind} — {title}
{url}

{body}

Requirements:
- Read AGENTS.md / CLAUDE.md in the repository before making changes, and follow them.
- Keep the change minimal and focused on the item above. Do not refactor unrelated code.
- Run the repository's lint and test commands before opening a PR.
- Open the pull request against {target_repo}. Do not push to any other repository.
- If the correct outcome is that no code change is needed, do not open a pull request: report
  that conclusion and the reasoning instead.
"""


def build_prompt(event: Event, decision: Decision) -> str:
    return PROMPT_TEMPLATE.format(
        target_repo=decision.target_repo,
        source_repo=event.repo,
        kind=event.event_type.value.replace("_", " "),
        title=event.title,
        url=event.url,
        body=(event.body or "").strip()[:2000],
    )


@dataclass
class Orchestrator:
    scope: ScopeConfig
    store: FactStore
    devin: DevinClient
    dry_run: bool = True
    allow_upstream_write: bool = False

    def handle(self, event: Event, now: datetime | None = None) -> Task:
        moment = now or datetime.now(UTC)
        task_id = make_task_id(event)

        if self.store.task_exists(task_id):
            task = Task(
                task_id=task_id,
                event=event,
                decision=Decision(admitted=False, reason="duplicate of an existing task"),
                state=TaskState.DEDUPED,
                created_at=moment,
                updated_at=moment,
            )
            self.store.upsert_task(task)
            return task

        decision = self.scope.route(event, moment)
        task = Task(
            task_id=task_id,
            event=event,
            decision=decision,
            state=TaskState.TRIGGERED if decision.admitted else TaskState.FILTERED,
            created_at=moment,
            updated_at=moment,
        )

        if not decision.admitted:
            self.store.upsert_task(task)
            return task

        target_repo = decision.target_repo or self.scope.defaults.target_repo
        assert_writable(target_repo, self.allow_upstream_write)

        if self.dry_run:
            task.state = TaskState.TRIGGERED
            task.decision = decision.model_copy(
                update={"reason": f"{decision.reason} (dry run: no session created)"}
            )
            self.store.upsert_task(task)
            return task

        state = self.devin.create_session(
            SessionRequest(
                prompt=build_prompt(event, decision),
                tags=decision.tags,
                playbook_id=decision.playbook_id,
                max_acu_limit=decision.max_acu_limit,
            )
        )
        task.session_id = state.session_id
        task.pr_url = state.pr_url
        task.acus_consumed = state.acus_consumed
        task.state = _state_for(state.status_detail, state.pr_url, state.structured_output)
        task.updated_at = datetime.now(UTC)
        self.store.upsert_task(task)
        return task


def _state_for(
    status_detail: str | None,
    pr_url: str | None,
    structured_output: dict[str, object] | None,
) -> TaskState:
    """Map a session's reported state onto the task lifecycle.

    `no_action_needed` counts as delivered work. A correctly reasoned decision not to change the
    code is a real outcome, and counting only pull requests would reward opening unnecessary ones.
    """
    outcome = (structured_output or {}).get("outcome")
    if outcome == "failed" or status_detail == "errored":
        return TaskState.ERRORED
    if outcome == "escalated" or status_detail == "waiting_for_user":
        return TaskState.ESCALATED
    if pr_url or outcome in {"pr_opened", "no_action_needed"}:
        return TaskState.WORK_DELIVERED
    return TaskState.SESSION_STARTED
