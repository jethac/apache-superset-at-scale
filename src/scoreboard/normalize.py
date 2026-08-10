"""Reduce provider webhook payloads to the normalised `Event` model.

Normalisation happens before routing so that the rule engine never touches raw payload
structure. Unknown or unsupported payloads raise rather than producing a partially-populated
event: an event we cannot understand must not be silently routed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import Event, EventType, Severity, digest_payload

_SEVERITY_ALIASES: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "error": Severity.HIGH,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "moderate": Severity.MEDIUM,
    "warning": Severity.MEDIUM,
    "low": Severity.LOW,
    "note": Severity.LOW,
}


ROUTED_ISSUE_ACTIONS = frozenset({"opened", "reopened", "labeled"})


class UnsupportedEventError(ValueError):
    """Raised when a payload does not correspond to an event type we route."""


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _severity(value: str | None) -> Severity:
    return _SEVERITY_ALIASES.get((value or "").lower(), Severity.NONE)


def _repo(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        raise UnsupportedEventError("payload has no usable repository.full_name")
    return full_name


def from_github(delivery_id: str, event_name: str, payload: dict[str, Any]) -> Event:
    """Translate a GitHub webhook delivery into an `Event`."""
    repo = _repo(payload)
    digest = digest_payload(payload)

    if event_name == "issues":
        action = str(payload.get("action") or "")
        if action and action not in ROUTED_ISSUE_ACTIONS:
            raise UnsupportedEventError(f"issue action not routed: {action}")
        issue = payload.get("issue") or {}
        user = issue.get("user") or {}
        return Event(
            event_id=delivery_id,
            event_type=EventType.ISSUE,
            repo=repo,
            number=issue.get("number"),
            title=str(issue.get("title") or ""),
            body=str(issue.get("body") or "")[:4000],
            labels=[str(label.get("name", "")) for label in issue.get("labels") or []],
            author=str(user.get("login") or ""),
            author_is_bot=str(user.get("type") or "").lower() == "bot",
            created_at=_parse_time(issue.get("created_at")),
            url=str(issue.get("html_url") or ""),
            raw_digest=digest,
        )

    if event_name == "check_run":
        check_run = payload.get("check_run") or {}
        if check_run.get("conclusion") not in {"failure", "timed_out"}:
            raise UnsupportedEventError("check_run did not fail")
        return Event(
            event_id=delivery_id,
            event_type=EventType.CHECK_RUN,
            repo=repo,
            title=str(check_run.get("name") or ""),
            severity=Severity.MEDIUM,
            created_at=_parse_time(check_run.get("started_at")),
            url=str(check_run.get("html_url") or ""),
            raw_digest=digest,
        )

    if event_name == "code_scanning_alert":
        alert = payload.get("alert") or {}
        rule = alert.get("rule") or {}
        return Event(
            event_id=delivery_id,
            event_type=EventType.CODE_SCANNING_ALERT,
            repo=repo,
            number=alert.get("number"),
            title=str(rule.get("description") or rule.get("id") or ""),
            severity=_severity(rule.get("security_severity_level") or rule.get("severity")),
            created_at=_parse_time(alert.get("created_at")),
            url=str(alert.get("html_url") or ""),
            raw_digest=digest,
        )

    if event_name == "dependabot_alert":
        alert = payload.get("alert") or {}
        advisory = alert.get("security_advisory") or {}
        return Event(
            event_id=delivery_id,
            event_type=EventType.DEPENDABOT_ALERT,
            repo=repo,
            number=alert.get("number"),
            title=str(advisory.get("summary") or ""),
            severity=_severity(advisory.get("severity")),
            created_at=_parse_time(alert.get("created_at")),
            url=str(alert.get("html_url") or ""),
            raw_digest=digest,
        )

    raise UnsupportedEventError(f"unsupported GitHub event: {event_name}")
