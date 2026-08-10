from __future__ import annotations

from typing import Any

import pytest

from scoreboard.models import EventType, Severity
from scoreboard.normalize import UnsupportedEventError, from_github

ISSUE_PAYLOAD: dict[str, Any] = {
    "action": "opened",
    "repository": {"full_name": "apache/superset"},
    "issue": {
        "number": 42,
        "title": "Sankey colour is lost when streams merge",
        "body": "steps to reproduce",
        "labels": [{"name": "#bug"}],
        "user": {"login": "someone", "type": "User"},
        "created_at": "2024-01-01T00:00:00Z",
        "html_url": "https://github.com/apache/superset/issues/42",
    },
}


def test_issue_payload_is_normalised() -> None:
    event = from_github("d1", "issues", ISSUE_PAYLOAD)
    assert event.event_type is EventType.ISSUE
    assert event.repo == "apache/superset"
    assert event.number == 42
    assert event.labels == ["#bug"]
    assert event.raw_digest


def test_dedupe_key_is_stable_across_redeliveries() -> None:
    first = from_github("delivery-1", "issues", ISSUE_PAYLOAD)
    second = from_github("delivery-2", "issues", ISSUE_PAYLOAD)
    assert first.dedupe_key() == second.dedupe_key()


def test_unknown_event_is_rejected_rather_than_partially_populated() -> None:
    with pytest.raises(UnsupportedEventError):
        from_github("d1", "star", {"repository": {"full_name": "jethac/superset"}})


def test_payload_without_repository_is_rejected() -> None:
    with pytest.raises((UnsupportedEventError, ValueError)):
        from_github("d1", "issues", {"issue": ISSUE_PAYLOAD["issue"]})


def test_successful_check_run_is_not_an_event() -> None:
    payload = {
        "repository": {"full_name": "jethac/superset"},
        "check_run": {
            "id": 1,
            "name": "tests",
            "conclusion": "success",
            "started_at": "2024-01-01T00:00:00Z",
            "html_url": "https://github.com/jethac/superset/runs/1",
        },
    }
    with pytest.raises(UnsupportedEventError):
        from_github("d1", "check_run", payload)


def test_dependabot_alert_severity_is_mapped() -> None:
    payload = {
        "repository": {"full_name": "jethac/superset"},
        "alert": {
            "number": 7,
            "security_advisory": {"summary": "vulnerable dependency", "severity": "critical"},
            "created_at": "2024-01-01T00:00:00Z",
            "html_url": "https://github.com/jethac/superset/security/dependabot/7",
        },
    }
    event = from_github("d1", "dependabot_alert", payload)
    assert event.severity is Severity.CRITICAL
