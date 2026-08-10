from __future__ import annotations

import pytest
from pydantic import ValidationError

from scoreboard.models import EventType, Severity
from scoreboard.scope import Defaults, Match, Route, Rule, ScopeConfig
from tests.conftest import make_event


def test_fork_bug_issue_is_admitted(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(labels=["bug"]))
    assert decision.admitted
    assert decision.rule_id == "fork-bug-issue"
    assert decision.stream == "bugfix"
    assert decision.target_repo == "jethac/superset"


def test_unmatched_event_is_filtered_not_admitted(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(labels=["question"], title="How do I deploy?"))
    assert not decision.admitted
    assert decision.reason == "no matching rule"
    assert decision.stream is None


def test_upstream_issue_routes_work_to_the_fork(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(repo="apache/superset", labels=["#bug"], age_days=30))
    assert decision.admitted
    assert decision.target_repo == "jethac/superset", "upstream must never be a write target"
    assert "fde:source-repo=apache/superset" in decision.tags


def test_upstream_issue_below_age_floor_is_filtered(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(repo="apache/superset", labels=["#bug"], age_days=2))
    assert not decision.admitted


def test_excluded_label_blocks_an_otherwise_matching_upstream_issue(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(repo="apache/superset", labels=["#bug", "#WIP"], age_days=30))
    assert not decision.admitted


def test_bot_authored_issue_is_filtered(scope: ScopeConfig) -> None:
    decision = scope.route(make_event(labels=["bug"], author_is_bot=True))
    assert not decision.admitted


def test_severity_floor_admits_high_and_rejects_low(scope: ScopeConfig) -> None:
    high = scope.route(
        make_event(event_type=EventType.DEPENDABOT_ALERT, severity=Severity.CRITICAL, labels=[])
    )
    low = scope.route(
        make_event(event_type=EventType.DEPENDABOT_ALERT, severity=Severity.LOW, labels=[])
    )
    assert high.admitted and high.stream == "security"
    assert not low.admitted


def test_first_matching_rule_wins() -> None:
    config = ScopeConfig(
        version=1,
        defaults=Defaults(target_repo="jethac/superset"),
        rules=[
            Rule(id="first", when=Match(labels_any=["bug"]), then=Route(stream="a")),
            Rule(id="second", when=Match(labels_any=["bug"]), then=Route(stream="b")),
        ],
    )
    assert config.route(make_event(labels=["bug"])).rule_id == "first"


def test_disabled_rule_is_skipped() -> None:
    config = ScopeConfig(
        version=1,
        defaults=Defaults(target_repo="jethac/superset"),
        rules=[
            Rule(id="off", enabled=False, when=Match(labels_any=["bug"]), then=Route(stream="a")),
        ],
    )
    assert not config.route(make_event(labels=["bug"])).admitted


def test_invalid_regex_is_rejected_at_load_time() -> None:
    with pytest.raises(ValidationError):
        Match(title_regex="(unclosed")


def test_shipped_rules_never_target_upstream(scope: ScopeConfig) -> None:
    targets = {rule.then.target_repo or scope.defaults.target_repo for rule in scope.rules}
    assert "apache/superset" not in targets
