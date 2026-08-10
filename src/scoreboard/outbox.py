"""The authorship outbox: draft pull requests waiting on a human paragraph.

The agent never blocks. When a target project requires the pull request to be written in a
human's voice, the session opens a draft, the draft lands here, and the agent moves to the next
task. A human clears the queue in batches, which puts their latency outside the agent's critical
path and makes it measurable — the age of this queue is the operator's contribution to lead time,
not the deployment's.

There is no facility for generating, improving or rewriting the paragraph, and there should not
be. The rule being implemented is that a human wrote it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .github import GitHubClient, parse_pr_url
from .models import Authorship, TaskState
from .policy import PolicyConfig, Submission, blocks_ready, evaluate
from .store import FactStore

AUTHOR_NOTE_HEADING = "### AUTHOR'S NOTE"
PENDING_MARKER = "_(pending)_"


class AuthorshipRejectedError(ValueError):
    """Raised when submitted authorship text does not satisfy the target project's policy."""


@dataclass(frozen=True)
class OutboxItem:
    task_id: str
    pr_url: str
    target_repo: str
    stream: str | None
    profile: str | None
    title: str
    issue_url: str
    waiting_since: datetime
    failing_checks: list[str]

    @property
    def waiting_days(self) -> float:
        return (datetime.now(UTC) - self.waiting_since).total_seconds() / 86400.0


def list_outbox(store: FactStore) -> list[OutboxItem]:
    rows = store.query(
        """
        SELECT t.task_id, t.pr_url, t.target_repo, t.stream, t.policy_profile, t.updated_at,
               e.title, e.url
        FROM fact_task t JOIN fact_event e ON e.event_id = t.event_id
        WHERE t.state = ? AND t.pr_url IS NOT NULL
        ORDER BY t.updated_at
        """,
        (TaskState.DRAFT_AWAITING_AUTHORSHIP.value,),
    )
    items: list[OutboxItem] = []
    for row in rows:
        failing = store.query(
            "SELECT check_name FROM fact_policy_check WHERE pr_url = ? AND passed = 0",
            (row["pr_url"],),
        )
        items.append(
            OutboxItem(
                task_id=row["task_id"],
                pr_url=row["pr_url"],
                target_repo=row["target_repo"] or "",
                stream=row["stream"],
                profile=row["policy_profile"],
                title=row["title"],
                issue_url=row["url"],
                waiting_since=datetime.fromisoformat(row["updated_at"]),
                failing_checks=[str(item["check_name"]) for item in failing],
            )
        )
    return items


def render_body_with_authorship(body: str, text: str, author: str, input_method: str) -> str:
    """Splice the human paragraph into the pull request body, verbatim.

    The text is inserted under the existing `AUTHOR'S NOTE` heading when the template has one,
    and appended under a new heading when it does not. It is never reflowed or edited.
    """
    attribution = f"\n\n— @{author} ({input_method})"
    block = f"{AUTHOR_NOTE_HEADING}\n\n{text.strip()}{attribution}"

    if AUTHOR_NOTE_HEADING not in body:
        return f"{body.rstrip()}\n\n{block}\n"

    head, _, tail = body.partition(AUTHOR_NOTE_HEADING)
    # Keep everything from the next heading onwards; replace only this section's contents.
    remainder = ""
    for line in tail.splitlines(keepends=True):
        if line.startswith("### "):
            remainder = tail[tail.index(line) :]
            break
    return f"{head.rstrip()}\n\n{block}\n\n{remainder.lstrip()}".rstrip() + "\n"


def submit_authorship(
    store: FactStore,
    github: GitHubClient,
    policy: PolicyConfig,
    task_id: str,
    text: str,
    author: str,
    input_method: str = "typed",
) -> list[str]:
    """Record the paragraph, patch the pull request body, and mark the draft ready.

    Returns the names of the checks that now pass. Raises `AuthorshipRejectedError` if the text is
    empty or still fails a blocking check, which is the only validation performed — a length floor
    beyond the policy's sentence minimum would just teach the operator to pad.
    """
    rows = store.query(
        "SELECT pr_url, target_repo, policy_profile FROM fact_task WHERE task_id = ?",
        (task_id,),
    )
    if not rows:
        raise KeyError(f"unknown task {task_id}")
    row = rows[0]
    pr_url = row["pr_url"]
    if not pr_url:
        raise AuthorshipRejectedError(f"task {task_id} has no pull request")

    if not text.strip():
        raise AuthorshipRejectedError("authorship text is empty")

    repo, number = parse_pr_url(pr_url)
    profile = policy.for_repo(row["target_repo"] or repo)
    body = github.get_pull_request_body(repo, number)

    results = evaluate(
        profile,
        Submission(
            pr_url=pr_url,
            body=body,
            commit_message="",
            authorship_text=text,
            tests_run=True,
            adversarial_review_run=True,
        ),
    )
    authorship_failed = [
        result for result in blocks_ready(results) if result.name == "contribution.human_authorship"
    ]
    if authorship_failed:
        raise AuthorshipRejectedError(authorship_failed[0].detail)

    now = datetime.now(UTC)
    authorship = Authorship(text=text, author=author, input_method=input_method, recorded_at=now)
    store.record_authorship(task_id, pr_url, authorship)

    github.update_pull_request_body(
        repo, number, render_body_with_authorship(body, text, author, input_method)
    )
    github.mark_ready_for_review(repo, number)

    store.record_policy_checks(task_id, pr_url, row["policy_profile"] or "", results, now)
    store.set_task_state(task_id, TaskState.WORK_DELIVERED.value, now, pr_is_draft=False)
    return [result.name for result in results if result.passed]
