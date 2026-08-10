"""Tests for the markdown brief: it must agree with the page and admit an empty store.

The debt and CI series are injected exactly as the dashboard tests inject them, so the assertions
here are about rendering only — a brief that recomputed a verdict could pass its own tests while
contradicting the page it summarises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scoreboard.cli import main
from scoreboard.dashboard import dashboard_payload, thesis_payload, throughput_payload
from scoreboard.report import build_report, render_report
from scoreboard.scope import ScopeConfig
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT
from tests.test_dashboard import (
    REPO,
    FakeCostPoint,
    FakeDebtPoint,
    fake_cost_series,
    fake_debt_series,
    seed,
)

STAMP = datetime(2026, 2, 1, 9, 30, tzinfo=UTC)


def no_debt_series(store: FactStore, repo: str) -> list[FakeDebtPoint]:
    return []


def no_cost_series(store: FactStore, repo: str, period: str = "week") -> list[FakeCostPoint]:
    return []


def test_an_empty_store_reports_that_it_is_empty_rather_than_failing() -> None:
    """A brief pasted from a store with nothing in it must read as unproven, not as success."""
    text = build_report(
        FactStore(":memory:"),
        REPO,
        debt_series=no_debt_series,
        cost_series=no_cost_series,
        generated_at=STAMP,
    )

    assert "2026-02-01T09:30:00+00:00" in text
    assert "No Devin session has been started yet." in text
    assert "No draft is waiting on a human paragraph." in text
    assert "No event has entered intake yet." in text
    assert "no data" in text


def test_every_section_renders_from_a_populated_store(store: FactStore, scope: ScopeConfig) -> None:
    seed(store, scope)

    text = build_report(
        store,
        REPO,
        debt_series=fake_debt_series,
        cost_series=fake_cost_series,
        generated_at=STAMP,
    )

    for heading in (
        "## The thesis, in three claims",
        "## Fleet roster",
        "## Authorship outbox",
        "## Intake funnel",
    ):
        assert heading in text
    assert "Technical debt falls" in text
    assert "CI compute per pull request falls" in text
    assert "More issues ship" in text
    assert "https://app.devin.ai/sessions/" in text
    assert "| Triggered |" in text
    assert f"{REPO}#1" in text


def test_the_verdicts_are_the_payloads_own_words(store: FactStore, scope: ScopeConfig) -> None:
    """The brief may not reach a verdict of its own, nor tidy away the one the payload reached."""
    seed(store, scope)
    throughput = throughput_payload(store, REPO, cost_series=fake_cost_series)
    claims = thesis_payload(
        store, REPO, throughput, debt_series=fake_debt_series, cost_series=fake_cost_series
    )

    text = build_report(
        store,
        REPO,
        debt_series=fake_debt_series,
        cost_series=fake_cost_series,
        generated_at=STAMP,
    )

    for claim in claims:
        assert str(claim["status"]) in text
        assert str(claim["detail"]) in text
    assert "677" not in text


def test_a_claim_the_payload_will_not_settle_keeps_its_own_wording() -> None:
    """`not yet comparable` is the honest answer, so it has to survive into the markdown."""
    payload: dict[str, object] = {
        "repo": REPO,
        "measure_repo": "apache/superset",
        "generated_at": STAMP.isoformat(),
        "thesis": [
            {
                "goal": "Technical debt falls",
                "measure": "oxlint violations",
                "status": "not yet comparable",
                "value": 92,
                "unit": "violations",
                "detail": "only one measurement since the rule set last changed",
            }
        ],
    }

    text = render_report(payload)

    assert "not yet comparable" in text
    assert "only one measurement since the rule set last changed" in text


def test_rendering_is_a_pure_function_of_the_payload(store: FactStore, scope: ScopeConfig) -> None:
    """Two renders of one document are byte-identical, so briefs can be diffed across runs."""
    seed(store, scope)
    payload = dashboard_payload(
        store, REPO, debt_series=fake_debt_series, cost_series=fake_cost_series
    )

    assert render_report(payload, generated_at=STAMP) == render_report(payload, generated_at=STAMP)


def test_timestamps_are_normalised_to_utc() -> None:
    payload: dict[str, object] = {
        "repo": REPO,
        "generated_at": "2026-02-01T20:30:00+11:00",
        "thesis": [],
    }

    assert "2026-02-01T09:30:00+00:00" in render_report(payload)


def test_the_command_writes_the_file_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--out` is the whole point of the command: the artefact has to land somewhere quotable."""
    destination = tmp_path / "status.md"
    monkeypatch.setenv("DB_PATH", str(tmp_path / "facts.db"))
    monkeypatch.setenv("SCOPE_PATH", str(REPO_ROOT / "scope.yaml"))
    monkeypatch.setenv("POLICY_PATH", str(REPO_ROOT / "policy.yaml"))

    assert main(["brief", "--repo", REPO, "--out", str(destination)]) == 0

    written = destination.read_text(encoding="utf-8")
    assert written.startswith("# Devin @ apache/superset")
    assert "## Intake funnel" in written
