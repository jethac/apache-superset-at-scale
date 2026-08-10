"""Normalised domain model shared by intake, routing, orchestration and reporting."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """The kinds of external signal the automation reacts to."""

    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    CHECK_RUN = "check_run"
    CODE_SCANNING_ALERT = "code_scanning_alert"
    DEPENDABOT_ALERT = "dependabot_alert"
    SCHEDULE = "schedule"


class Severity(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.NONE: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Event(BaseModel):
    """A provider payload reduced to the fields routing decisions are allowed to use.

    Keeping the routable surface small and explicit is deliberate: webhook payloads are
    attacker-influenced input, and a rule language that can reach arbitrary payload paths is a
    rule language that can be steered from outside the trust boundary.
    """

    event_id: str
    event_type: EventType
    repo: str
    number: int | None = None
    title: str = ""
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    author_is_bot: bool = False
    severity: Severity = Severity.NONE
    created_at: datetime
    url: str = ""
    raw_digest: str = ""

    def age_days(self, now: datetime | None = None) -> float:
        reference = now or datetime.now(UTC)
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (reference - created).total_seconds() / 86400.0

    def dedupe_key(self) -> str:
        """Stable identity for the underlying work, independent of redelivery.

        Webhook providers retry, and a user can relabel an issue repeatedly. Keying on the
        subject of the event rather than the delivery means a redelivery is suppressed rather
        than spawning a second session against the same issue.
        """
        return f"{self.repo}:{self.event_type.value}:{self.number or self.event_id}"


class Decision(BaseModel):
    """The router's verdict for one event."""

    admitted: bool
    reason: str
    rule_id: str | None = None
    stream: str | None = None
    target_repo: str | None = None
    playbook_id: str | None = None
    max_acu_limit: int | None = None
    tags: list[str] = Field(default_factory=list)


class TaskState(StrEnum):
    """Terminal and in-flight states for a unit of work.

    A task is one triggering event, not one session: retries and child sessions belong to the
    same task.
    """

    TRIGGERED = "triggered"
    FILTERED = "filtered"
    DEDUPED = "deduped"
    SESSION_STARTED = "session_started"
    DRAFT_AWAITING_AUTHORSHIP = "draft_awaiting_authorship"
    WORK_DELIVERED = "work_delivered"
    ESCALATED = "escalated"
    ERRORED = "errored"


class Authorship(BaseModel):
    """The human-written paragraph a draft is waiting on, stored exactly as it was supplied.

    `input_method` is recorded because dictated text arrives as one unpunctuated block and would
    otherwise look like a policy violation, and because how the text came to exist is part of the
    evidence that a human wrote it.
    """

    text: str
    author: str
    input_method: str = "typed"
    recorded_at: datetime


class Task(BaseModel):
    task_id: str
    event: Event
    decision: Decision
    state: TaskState
    session_id: str | None = None
    pr_url: str | None = None
    pr_is_draft: bool = False
    policy_profile: str | None = None
    acus_consumed: float | None = None
    created_at: datetime
    updated_at: datetime


def make_task_id(event: Event) -> str:
    return hashlib.sha256(event.dedupe_key().encode("utf-8")).hexdigest()[:16]


def digest_payload(payload: dict[str, Any]) -> str:
    """Content digest of the raw payload, for audit without storing attacker-controlled text."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
