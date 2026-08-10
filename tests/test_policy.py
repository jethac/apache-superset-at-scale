from __future__ import annotations

from scoreboard import policy
from scoreboard.policy import PolicyConfig, Submission, blocks_ready, evaluate, prompt_section
from tests.conftest import REPO_ROOT

POLICY = PolicyConfig.load(REPO_ROOT / "policy.yaml")

COMPLIANT_BODY = """### SUMMARY

Corrects the ruleset the metrics job measures.

### AI DISCLOSURE

Authored with AI assistance (Devin).
"""


def test_profile_is_selected_by_target_repository() -> None:
    assert POLICY.profile_name_for("jethac/superset") == "asf-superset"
    assert POLICY.profile_name_for("apache/superset") == "asf-superset"
    assert POLICY.profile_name_for("some/unknown-repo") == POLICY.defaults.profile


def test_prompt_carries_the_targets_requirements() -> None:
    section = prompt_section(POLICY.profiles["asf-superset"])
    assert "Generated-by:" in section
    assert "AI DISCLOSURE" in section
    assert "draft" in section
    assert "Do not write it" in section


def test_policy_module_exposes_no_way_to_write_the_authorship_paragraph() -> None:
    """The absent generate button is the control, so its absence is worth a regression test."""
    callables = [
        name for name in dir(policy) if not name.startswith("_") and callable(getattr(policy, name))
    ]
    assert not [
        name
        for name in callables
        if any(verb in name.lower() for verb in ("generate", "suggest", "rewrite", "improve"))
    ]


def test_missing_evidence_blocks_ready() -> None:
    results = evaluate(
        POLICY.profiles["asf-superset"],
        Submission(pr_url="https://github.com/jethac/superset/pull/5"),
    )
    failed = {result.name for result in blocks_ready(results)}
    assert failed == {
        "contribution.generated_by_trailer",
        "contribution.ai_disclosure",
        "contribution.local_test_evidence",
        "contribution.adversarial_review",
        "contribution.human_authorship",
    }


def test_full_evidence_clears_every_blocking_check() -> None:
    results = evaluate(
        POLICY.profiles["asf-superset"],
        Submission(
            pr_url="https://github.com/jethac/superset/pull/5",
            body=COMPLIANT_BODY,
            commit_message="fix(tech-debt): x\n\nGenerated-by: Devin (Cognition)",
            authorship_text="I ran this against the fork and checked the counts by hand.",
            tests_run=True,
            adversarial_review_run=True,
        ),
    )
    assert blocks_ready(results) == []


def test_tone_findings_are_advisory_not_blocking() -> None:
    profile = POLICY.profiles["asf-superset"]
    results = evaluate(
        profile,
        Submission(
            pr_url="https://github.com/jethac/superset/pull/5",
            body="Great point! " + COMPLIANT_BODY,
            commit_message="fix: x\n\nGenerated-by: Devin (Cognition)",
            authorship_text="I checked the numbers myself.",
            tests_run=True,
            adversarial_review_run=True,
        ),
    )
    tone_failures = [result for result in results if result.name.startswith("tone.")]
    assert tone_failures
    assert blocks_ready(results) == []


def test_permissive_profile_requires_nothing() -> None:
    results = evaluate(
        POLICY.profiles["permissive"],
        Submission(pr_url="https://github.com/jethac/apache-superset-at-scale/pull/1"),
    )
    assert blocks_ready(results) == []
