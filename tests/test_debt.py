from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scoreboard.debt import (
    DebtObservation,
    ingest_csv,
    parse_oxlint_json,
    record_run,
    ruleset_id,
    series,
    series_on_fixed_ruleset,
)
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT

SAMPLE = REPO_ROOT / "fixtures" / "oxlint-sample.json"
HISTORY = REPO_ROOT / "fixtures" / "debt-history.csv"
REPO = "apache/superset"

# The bare command reports 92 diagnostics, 85 of them a rule oxlint.json switches off; the same
# checkout under --config oxlint.json reports 1470. Both numbers are used verbatim below so a
# failure reads as a claim about Superset rather than about arithmetic.
BARE_TOTAL = 92
CONFIGURED_TOTAL = 1470
BARE = {
    "eslint(no-unused-vars)": 85,
    "eslint(no-empty)": 3,
    "eslint(no-prototype-builtins)": 2,
    "eslint(no-async-promise-executor)": 1,
    "eslint(no-case-declarations)": 1,
}
CONFIGURED = {
    "eslint(prefer-destructuring)": 567,
    "react-hooks(exhaustive-deps)": 381,
    "react(no-unstable-nested-components)": 151,
    "eslint(no-console)": 135,
    "react(jsx-key)": 80,
    "jest(no-conditional-expect)": 68,
    "react-hooks(rules-of-hooks)": 47,
    "typescript(no-explicit-any)": 25,
    "eslint(no-empty)": 16,
}


def observations(
    counts: dict[str, int], measured_at: datetime, config_path: str, commit_sha: str = "3b164e42"
) -> list[DebtObservation]:
    identity = ruleset_id(counts)
    return [
        DebtObservation(
            measured_at=measured_at,
            repo=REPO,
            commit_sha=commit_sha,
            config_path=config_path,
            ruleset_id=identity,
            rule=rule,
            count=count,
        )
        for rule, count in counts.items()
    ]


def _change_parts(change: str) -> tuple[list[str], list[str]]:
    sections = {
        part.split(" ", 1)[0]: part.split(" ", 1)[1].split(", ") for part in change.split("; ")
    }
    return sections.get("added", []), sections.get("removed", [])


def test_parses_every_shape_oxlint_has_emitted() -> None:
    counts = parse_oxlint_json(SAMPLE.read_text(encoding="utf-8"))
    assert counts == {
        "eslint(no-unused-vars)": 3,
        "react-hooks(exhaustive-deps)": 2,
        "eslint(prefer-destructuring)": 1,
        "eslint(no-console)": 1,
        "no-debugger": 1,
    }


def test_parses_a_bare_diagnostics_list() -> None:
    payload = '[{"code": "no-unused-vars", "plugin": "eslint"}, {"code": "eslint(jsx-key)"}]'
    assert parse_oxlint_json(payload) == {"eslint(no-unused-vars)": 1, "eslint(jsx-key)": 1}


def test_ruleset_id_is_stable_under_reordering_and_sensitive_to_membership() -> None:
    rules = list(CONFIGURED)
    assert ruleset_id(rules) == ruleset_id(reversed(rules))
    assert ruleset_id(rules) == ruleset_id([*rules, *rules])
    assert ruleset_id(rules) != ruleset_id(rules[:-1])
    assert len(ruleset_id(rules)) == 12


def test_a_narrower_ruleset_breaks_the_line_and_names_what_left(store: FactStore) -> None:
    record_run(store, observations(CONFIGURED, datetime(2026, 5, 12, tzinfo=UTC), "oxlint.json"))
    record_run(store, observations(BARE, datetime(2026, 5, 13, tzinfo=UTC), ""))

    points = series(store, REPO)
    assert [point.total for point in points] == [CONFIGURED_TOTAL, BARE_TOTAL]
    assert points[0].comparable_to_previous is False
    assert points[1].comparable_to_previous is False
    added, removed = _change_parts(points[1].ruleset_change)
    assert "react-hooks(exhaustive-deps)" in removed
    assert "eslint(prefer-destructuring)" in removed
    assert "eslint(no-unused-vars)" in added


def test_an_unchanged_ruleset_stays_comparable(store: FactStore) -> None:
    record_run(store, observations(BARE, datetime(2026, 5, 13, tzinfo=UTC), ""))
    record_run(store, observations(BARE, datetime(2026, 5, 14, tzinfo=UTC), ""))

    points = series(store, REPO)
    assert points[1].comparable_to_previous is True
    assert points[1].ruleset_change == ""


def test_fixed_ruleset_omits_rather_than_zero_fills_an_unmeasured_rule(store: FactStore) -> None:
    record_run(store, observations(CONFIGURED, datetime(2026, 5, 12, tzinfo=UTC), "oxlint.json"))
    record_run(store, observations(BARE, datetime(2026, 5, 13, tzinfo=UTC), ""))
    record_run(store, observations(CONFIGURED, datetime(2026, 8, 10, tzinfo=UTC), "oxlint.json"))

    points = series_on_fixed_ruleset(
        store, REPO, ["react-hooks(exhaustive-deps)", "eslint(no-console)"]
    )
    assert [point.measured_at.date().isoformat() for point in points] == [
        "2026-05-12",
        "2026-08-10",
    ]
    assert all(point.total == 381 + 135 for point in points)
    assert all(point.comparable_to_previous for point in points[1:])
    assert not any(0 in point.by_rule.values() for point in points)


def test_history_ingest_is_idempotent_and_preserves_the_discontinuity(store: FactStore) -> None:
    first = ingest_csv(store, HISTORY)
    second = ingest_csv(store, Path(HISTORY))
    assert first == second == 29

    points = series(store, REPO)
    assert [point.total for point in points] == [677, BARE_TOTAL, CONFIGURED_TOTAL]
    assert [point.comparable_to_previous for point in points] == [False, False, False]
    assert "react-hooks(exhaustive-deps)" in _change_parts(points[1].ruleset_change)[1]
    assert points[1].by_rule["eslint(no-unused-vars)"] == 85
    assert points[2].by_rule["react-hooks(exhaustive-deps)"] == 381


def test_re_recording_a_run_updates_rows_instead_of_duplicating_them(store: FactStore) -> None:
    measured_at = datetime(2026, 8, 10, tzinfo=UTC)
    record_run(store, observations(CONFIGURED, measured_at, "oxlint.json"))
    record_run(store, observations(CONFIGURED, measured_at, "oxlint.json"))

    rules = store.query("SELECT COUNT(*) AS n FROM fact_debt WHERE repo = ?", (REPO,))
    runs = store.query("SELECT total FROM fact_debt_run WHERE repo = ?", (REPO,))
    assert int(rules[0]["n"]) == len(CONFIGURED)
    assert [int(row["total"]) for row in runs] == [CONFIGURED_TOTAL]

    corrected = dict(CONFIGURED)
    corrected["eslint(no-console)"] = 130
    record_run(store, observations(corrected, measured_at, "oxlint.json"))
    runs = store.query("SELECT total FROM fact_debt_run WHERE repo = ?", (REPO,))
    assert [int(row["total"]) for row in runs] == [CONFIGURED_TOTAL - 5]
