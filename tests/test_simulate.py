from __future__ import annotations

from pathlib import Path

from scoreboard.simulate import run_simulation
from tests.conftest import REPO_ROOT


def test_simulation_runs_offline_and_reconciles(tmp_path: Path) -> None:
    result = run_simulation(
        REPO_ROOT / "scope.yaml",
        REPO_ROOT / "policy.yaml",
        tmp_path / "sim.db",
        event_count=24,
        seed=7,
    )
    assert result["reconciles"] is True
    assert result["events"] == 24


def test_simulation_exercises_every_outcome(tmp_path: Path) -> None:
    """A demo that only ever succeeds cannot show the reporting layer working."""
    counts = run_simulation(
        REPO_ROOT / "scope.yaml", REPO_ROOT / "policy.yaml", tmp_path / "sim.db", event_count=36
    )["funnel"]
    assert isinstance(counts, dict)
    # Nothing reaches `work_delivered` offline: the fork's policy profile requires a human
    # paragraph, so delivered PRs park in the outbox until a person writes one.
    for bucket in ("filtered", "deduped", "awaiting_authorship"):
        assert counts[bucket] > 0, f"{bucket} path never exercised"


def test_simulation_is_deterministic(tmp_path: Path) -> None:
    first = run_simulation(
        REPO_ROOT / "scope.yaml", REPO_ROOT / "policy.yaml", tmp_path / "a.db", seed=11
    )
    second = run_simulation(
        REPO_ROOT / "scope.yaml", REPO_ROOT / "policy.yaml", tmp_path / "b.db", seed=11
    )
    assert first["funnel"] == second["funnel"]
    assert first["sankey_edges"] == second["sankey_edges"]
