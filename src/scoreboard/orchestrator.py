"""Event → decision → session. The one place that is allowed to spend money.

Ordering is deliberate: dedupe before routing, routing before spend, and the write-boundary
check before the session is created rather than after. Each of those is a control that only
works if nothing downstream can bypass it, so they all live here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .devin import DevinClient, SessionRequest, SessionSummary
from .github import assert_writable
from .models import Decision, Event, EventType, Task, TaskState, make_task_id
from .policy import PolicyConfig, Profile, Submission, evaluate, prompt_section
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

{policy}
"""


def build_prompt(event: Event, decision: Decision, profile: Profile | None = None) -> str:
    return PROMPT_TEMPLATE.format(
        target_repo=decision.target_repo,
        source_repo=event.repo,
        kind=event.event_type.value.replace("_", " "),
        title=event.title,
        url=event.url,
        body=(event.body or "").strip()[:2000],
        policy=prompt_section(profile) if profile else "",
    )


@dataclass
class Orchestrator:
    scope: ScopeConfig
    store: FactStore
    devin: DevinClient
    policy: PolicyConfig | None = None
    dry_run: bool = True
    allow_upstream_write: bool = False

    def handle(self, event: Event, now: datetime | None = None) -> Task:
        moment = now or datetime.now(UTC)
        task_id = make_task_id(event)

        if self.store.task_exists(task_id):
            self.store.record_duplicate_sighting(task_id)
            # Only work that already has a session is a settled duplicate. Anything else is
            # re-routed against the current rules from the event just read, so a rule widened
            # after an issue was first seen picks that issue up, and work admitted while the
            # fleet was full is dispatched once there is room.
            if self.store.task_has_started(task_id):
                return Task(
                    task_id=task_id,
                    event=event,
                    decision=Decision(admitted=False, reason="duplicate of an existing task"),
                    state=TaskState.DEDUPED,
                    created_at=moment,
                    updated_at=moment,
                )

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
        profile = self.policy.for_repo(target_repo) if self.policy else None
        task.policy_profile = self.policy.profile_name_for(target_repo) if self.policy else None

        if self.dry_run:
            task.state = TaskState.TRIGGERED
            task.decision = decision.model_copy(
                update={"reason": f"{decision.reason} (dry run: no session created)"}
            )
            self.store.upsert_task(task)
            return task

        if not self._has_dispatch_capacity():
            task.decision = decision.model_copy(
                update={"reason": f"{decision.reason} (queued: the fleet is at capacity)"}
            )
            self.store.upsert_task(task)
            return task

        state = self.devin.create_session(
            SessionRequest(
                prompt=build_prompt(event, decision, profile),
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

        if task.pr_url and profile is not None:
            task.state = self._apply_policy(task, profile, state.structured_output)

        self.store.upsert_task(task)
        return task

    def _has_dispatch_capacity(self) -> bool:
        """Whether another session may start, given how many are already running.

        Admitting work and paying for it are separate acts. Without this, a backlog filed in one
        afternoon becomes that many concurrent sessions and that many ACUs in the same minute,
        which is neither reviewable by a human nor recoverable if the routing was wrong. Work over
        the limit stays admitted and waits; intake's next pass dispatches it.
        """
        limit = self.scope.defaults.max_concurrent_sessions
        if limit is None:
            return True
        return self.store.count_sessions_in_flight() < limit

    def adopt(self, now: datetime | None = None) -> list[str]:
        """Record sessions this deployment did not start but is accountable for.

        The fleet is not only what this process dispatched: a human, or another Devin working the
        same backlog, starts sessions that spend the same ACUs against the same repository. A
        roster that omits them reports a smaller fleet than the Devin app shows, and the first
        person to notice is the reviewer. Ownership is claimed by tag, so adoption stays an
        explicit configuration decision rather than a guess made from titles.
        """
        wanted = set(self.scope.defaults.adopt_session_tags)
        if not wanted:
            return []
        moment = now or datetime.now(UTC)
        known = self.store.known_session_ids()
        adopted: list[str] = []

        for summary in self.devin.list_sessions():
            if summary.session_id in known or not wanted.intersection(summary.tags):
                continue
            task = _adopted_task(summary, self.scope.defaults.target_repo, moment)
            self.store.upsert_task(task)
            logger.info("adopted session %s (%s)", summary.session_id, summary.title)
            adopted.append(summary.session_id)

        return adopted

    def sync(self, now: datetime | None = None) -> list[tuple[str, TaskState]]:
        """Poll every started session and move the ones that have reached an outcome.

        Starting a session is the cheap half of managing one. Without this the funnel would
        report whatever was true at creation time and `in_flight` would only ever grow, so the
        poller is what makes the reported outcome a fact about the session rather than about the
        moment it was launched. Run it on a schedule alongside `intake`.
        """
        moment = now or datetime.now(UTC)
        moved: list[tuple[str, TaskState]] = []

        for row in self.store.tasks_awaiting_session_outcome():
            session_id = str(row["session_id"])
            state = self.devin.get_session(session_id)
            resolved = _state_for(state.status_detail, state.pr_url, state.structured_output)
            if resolved is TaskState.SESSION_STARTED:
                continue

            target_repo = str(row["target_repo"] or self.scope.defaults.target_repo)
            profile = self.policy.for_repo(target_repo) if self.policy else None
            is_draft = False
            if state.pr_url and profile is not None:
                results = evaluate(
                    profile,
                    _submission_from(state.pr_url, state.structured_output),
                )
                self.store.record_policy_checks(
                    str(row["task_id"]),
                    state.pr_url,
                    str(row["policy_profile"] or ""),
                    results,
                    moment,
                )
                if profile.contribution.require_human_authorship:
                    resolved = TaskState.DRAFT_AWAITING_AUTHORSHIP
                    is_draft = profile.contribution.open_as_draft

            self.store.record_session_outcome(
                task_id=str(row["task_id"]),
                state=resolved.value,
                pr_url=state.pr_url,
                pr_is_draft=is_draft,
                acus_consumed=state.acus_consumed or 0.0,
                updated_at=moment,
            )
            logger.info("%s -> %s (session %s)", row["task_id"], resolved.value, session_id)
            moved.append((str(row["task_id"]), resolved))

        return moved

    def _apply_policy(
        self,
        task: Task,
        profile: Profile,
        structured_output: dict[str, object] | None,
    ) -> TaskState:
        """Check the delivered pull request against the target's policy and record the evidence.

        A draft that still needs the human authorship paragraph is not delivered work. It gets its
        own state so the funnel shows the queue rather than counting it as finished, and so the
        age of that queue is measurable.
        """
        results = evaluate(profile, _submission_from(task.pr_url or "", structured_output))
        self.store.record_policy_checks(
            task.task_id,
            task.pr_url or "",
            task.policy_profile or "",
            results,
            task.updated_at,
        )
        if profile.contribution.require_human_authorship:
            task.pr_is_draft = profile.contribution.open_as_draft
            return TaskState.DRAFT_AWAITING_AUTHORSHIP
        return task.state


def _tag_value(tags: list[str], prefix: str) -> str | None:
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _adopted_task(summary: SessionSummary, target_repo: str, moment: datetime) -> Task:
    """A task standing for a session started elsewhere, described by what the session carries.

    There is no triggering event to point at, so the session itself is the event: its own tags
    say which repository and trigger it came from where the dispatcher set them, and the reason
    records that the row was adopted rather than routed, so the funnel cannot pass it off as
    admitted work.
    """
    repo = _tag_value(summary.tags, "fde:source-repo=") or target_repo
    trigger = _tag_value(summary.tags, "fde:trigger=")
    event = Event(
        event_id=summary.session_id,
        event_type=EventType(trigger) if trigger in set(EventType) else EventType.SCHEDULE,
        repo=repo,
        title=summary.title,
        created_at=summary.created_at or moment,
        url=f"https://app.devin.ai/sessions/{summary.session_id}",
    )
    return Task(
        task_id=make_task_id(event),
        event=event,
        decision=Decision(
            admitted=True,
            reason="adopted: session started outside this deployment",
            stream=_tag_value(summary.tags, "fde:stream="),
            target_repo=target_repo,
            tags=summary.tags,
        ),
        state=TaskState.SESSION_STARTED,
        session_id=summary.session_id,
        created_at=event.created_at,
        updated_at=moment,
    )


def _submission_from(pr_url: str, structured_output: dict[str, object] | None) -> Submission:
    """Read what the session reported into the shape the policy grader checks.

    `authorship_text` is always absent here: the paragraph is the one field the automation is
    forbidden to supply, so a session can never satisfy that check on a human's behalf.
    """
    output = structured_output or {}
    return Submission(
        pr_url=pr_url,
        body=str(output.get("pr_body") or ""),
        commit_message=str(output.get("commit_message") or ""),
        authorship_text=None,
        tests_run=bool(output.get("tests_run")),
        adversarial_review_run=bool(output.get("adversarial_review_run")),
    )


def _state_for(
    status_detail: str | None,
    pr_url: str | None,
    structured_output: dict[str, object] | None,
) -> TaskState:
    """Map a session's reported state onto the task lifecycle.

    `no_action_needed` counts as delivered work. A correctly reasoned decision not to change the
    code is a real outcome, and counting only pull requests would reward opening unnecessary ones.

    Delivery is read before `waiting_for_user`, which a session enters as soon as it finishes and
    reports back. Taking that status first would file every completed session as an escalation and
    the delivery rate would read zero with the pull requests sitting on GitHub.
    """
    outcome = (structured_output or {}).get("outcome")
    if outcome == "failed" or status_detail == "errored":
        return TaskState.ERRORED
    if pr_url or outcome in {"pr_opened", "no_action_needed"}:
        return TaskState.WORK_DELIVERED
    if outcome == "escalated" or status_detail == "waiting_for_user":
        return TaskState.ESCALATED
    return TaskState.SESSION_STARTED
