"""Which cohort a pull request lands in.

F10 makes agent-versus-contemporaneous-human the comparison that survives scrutiny. It only
survives if the agent is on one side of it. These tests pin the cases where a naive rule puts it
on both: a machine-authored pull request this deployment cannot point at a session for is still
machine-authored, and counting it as human inflates the control with the treatment.
"""

from __future__ import annotations

from scoreboard.collector import (
    COHORT_AGENT,
    COHORT_DEPENDABOT,
    COHORT_HUMAN,
    COHORT_UNATTRIBUTED,
    cohort_for,
)

ATTRIBUTED = {"https://github.com/jethac/superset/pull/7"}


def test_a_pull_request_this_deployment_can_point_at_a_session_for_is_agent() -> None:
    assert (
        cohort_for(
            "https://github.com/jethac/superset/pull/7",
            author="devin-ai-integration[bot]",
            is_bot=True,
            attributed=ATTRIBUTED,
        )
        == COHORT_AGENT
    )


def test_a_person_is_human() -> None:
    assert (
        cohort_for(
            "https://github.com/apache/superset/pull/42930",
            author="some-contributor",
            is_bot=False,
            attributed=ATTRIBUTED,
        )
        == COHORT_HUMAN
    )


def test_dependabot_is_its_own_cohort() -> None:
    """F10 lists it separately, and its volume would swamp whichever bucket it was folded into."""
    assert (
        cohort_for(
            "https://github.com/apache/superset/pull/42931",
            author="dependabot[bot]",
            is_bot=True,
            attributed=set(),
        )
        == COHORT_DEPENDABOT
    )


def test_an_unattributable_bot_is_named_rather_than_counted_as_human() -> None:
    """The case that was wrong: Devin's own account, in the cohort it is the control for.

    Attribution by PR URL fails whenever a session was started outside this deployment — which
    includes every pull request opened before it existed. Those are exactly the ones the baseline
    window is made of.
    """
    assert (
        cohort_for(
            "https://github.com/jethac/superset/pull/43",
            author="devin-ai-integration[bot]",
            is_bot=True,
            attributed=set(),
        )
        == COHORT_UNATTRIBUTED
    )


def test_the_human_cohort_contains_no_bots() -> None:
    """The property the comparison rests on, stated directly."""
    bots = ["devin-ai-integration[bot]", "dependabot[bot]", "github-actions[bot]", "renovate[bot]"]
    assert all(
        cohort_for(f"https://github.com/apache/superset/pull/{i}", a, True, set()) != COHORT_HUMAN
        for i, a in enumerate(bots)
    )
