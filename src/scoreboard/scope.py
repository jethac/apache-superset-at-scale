"""Scope rules: decide whether an event is in scope, and which stream it belongs to.

The rule set is data, not code, so the boundary between "what we react to" and "how we react"
can be reviewed by someone who does not read Python. Rules are ordered and first match wins;
an event matching no rule is filtered rather than admitted, so widening scope is always an
explicit act.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import SEVERITY_ORDER, Decision, Event, EventType, Severity


class Match(BaseModel):
    """Conditions on an event. All present conditions must hold (AND)."""

    repo: list[str] = Field(default_factory=list)
    event_type: list[EventType] = Field(default_factory=list)
    labels_any: list[str] = Field(default_factory=list)
    labels_none: list[str] = Field(default_factory=list)
    title_regex: str | None = None
    severity_min: Severity | None = None
    age_days_min: float | None = None
    age_days_max: float | None = None
    exclude_bots: bool = False

    @field_validator("title_regex")
    @classmethod
    def _validate_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid title_regex: {error}") from error
        return value

    def matches(self, event: Event, now: datetime | None = None) -> bool:
        if self.repo and event.repo not in self.repo:
            return False
        if self.event_type and event.event_type not in self.event_type:
            return False
        labels = {label.lower() for label in event.labels}
        if self.labels_any and not labels & {label.lower() for label in self.labels_any}:
            return False
        if self.labels_none and labels & {label.lower() for label in self.labels_none}:
            return False
        if self.title_regex and not re.search(self.title_regex, event.title, re.IGNORECASE):
            return False
        if self.severity_min is not None and (
            SEVERITY_ORDER[event.severity] < SEVERITY_ORDER[self.severity_min]
        ):
            return False
        age = event.age_days(now)
        if self.age_days_min is not None and age < self.age_days_min:
            return False
        if self.age_days_max is not None and age > self.age_days_max:
            return False
        if self.exclude_bots and event.author_is_bot:
            return False
        return True


class Route(BaseModel):
    """What to do with an event that matched."""

    stream: str
    target_repo: str | None = None
    playbook_id: str | None = None
    max_acu_limit: int | None = None
    tags: list[str] = Field(default_factory=list)


class Rule(BaseModel):
    id: str
    description: str = ""
    enabled: bool = True
    when: Match
    then: Route


class Defaults(BaseModel):
    target_repo: str
    tags: list[str] = Field(default_factory=list)
    max_acu_limit: int | None = None
    max_concurrent_sessions: int | None = None
    adopt_session_tags: list[str] = Field(default_factory=list)


class ScopeConfig(BaseModel):
    version: int
    defaults: Defaults
    rules: list[Rule]

    @classmethod
    def load(cls, path: str | Path) -> ScopeConfig:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def route(self, event: Event, now: datetime | None = None) -> Decision:
        for rule in self.rules:
            if not rule.enabled:
                continue
            if not rule.when.matches(event, now):
                continue
            target = rule.then.target_repo or self.defaults.target_repo
            tags = sorted(
                {
                    *self.defaults.tags,
                    *rule.then.tags,
                    f"fde:stream={rule.then.stream}",
                    f"fde:trigger={event.event_type.value}",
                    f"fde:source-repo={event.repo}",
                }
            )
            return Decision(
                admitted=True,
                reason=f"matched rule {rule.id}",
                rule_id=rule.id,
                stream=rule.then.stream,
                target_repo=target,
                playbook_id=rule.then.playbook_id,
                max_acu_limit=rule.then.max_acu_limit or self.defaults.max_acu_limit,
                tags=tags,
            )
        return Decision(admitted=False, reason="no matching rule")
