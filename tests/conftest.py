from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scoreboard.models import Event, EventType, Severity
from scoreboard.scope import ScopeConfig
from scoreboard.store import FactStore

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scope() -> ScopeConfig:
    return ScopeConfig.load(REPO_ROOT / "scope.yaml")


@pytest.fixture
def store(tmp_path: Path) -> FactStore:
    return FactStore(tmp_path / "facts.db")


def make_event(
    repo: str = "jethac/superset",
    number: int = 1,
    title: str = "Something is broken",
    labels: list[str] | None = None,
    event_type: EventType = EventType.ISSUE,
    severity: Severity = Severity.NONE,
    age_days: float = 30.0,
    author_is_bot: bool = False,
) -> Event:
    return Event(
        event_id=f"test-{repo}-{number}",
        event_type=event_type,
        repo=repo,
        number=number,
        title=title,
        body="body",
        labels=labels if labels is not None else ["bug"],
        author="someone",
        author_is_bot=author_is_bot,
        severity=severity,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        url=f"https://github.com/{repo}/issues/{number}",
    )
