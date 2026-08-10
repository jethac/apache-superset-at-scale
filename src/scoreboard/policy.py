"""Target-project contribution policy: tone, disclosure, and human authorship.

Projects publish rules about AI-assisted contributions, and Apache Superset enforces one of them
socially — a pull request that reads as entirely machine-written is tagged
`lacks-human-authorship` and closed. A deployment that opens pull requests into such a project has
to know the rules before it writes, not after.

The policy is applied at two points:

- **Intake.** `prompt_section` turns the profile into instructions carried in the session prompt,
  so the agent writes to the target's standards rather than being corrected afterwards.
- **Submit.** `evaluate` checks the resulting pull request body against the same profile. The
  checks are recorded per pull request, so compliance is evidence rather than an assertion.

The one thing deliberately absent is any way to produce the human authorship paragraph. A
generate button here would satisfy the check while defeating the rule it implements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

GENERATED_BY_PATTERN = re.compile(r"^Generated-by:\s*\S", re.IGNORECASE | re.MULTILINE)
SENTENCE_END = re.compile(r"[.!?](\s|$)")


class Tone(BaseModel):
    """Style constraints. Advisory in the prompt, checked as substrings on submission."""

    guidance: list[str] = Field(default_factory=list)
    banned_openers: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)


class Contribution(BaseModel):
    open_as_draft: bool = False
    require_generated_by_trailer: bool = False
    require_ai_disclosure: bool = False
    require_human_authorship: bool = False
    require_local_test_evidence: bool = False
    require_adversarial_review: bool = False
    pr_template: str | None = None
    authorship_min_sentences: int = 1


class Signoff(BaseModel):
    default_author: str | None = None


class Profile(BaseModel):
    description: str = ""
    tone: Tone = Field(default_factory=Tone)
    contribution: Contribution = Field(default_factory=Contribution)
    signoff: Signoff = Field(default_factory=Signoff)


class PolicyDefaults(BaseModel):
    profile: str


class PolicyConfig(BaseModel):
    version: int
    defaults: PolicyDefaults
    profiles: dict[str, Profile]
    repositories: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> PolicyConfig:
        raw: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        config = cls.model_validate(raw)
        unknown = {name for name in [*config.repositories.values(), config.defaults.profile]} - set(
            config.profiles
        )
        if unknown:
            raise ValueError(f"policy references undefined profiles: {sorted(unknown)}")
        return config

    def profile_name_for(self, repo: str) -> str:
        return self.repositories.get(repo, self.defaults.profile)

    def for_repo(self, repo: str) -> Profile:
        return self.profiles[self.profile_name_for(repo)]


@dataclass(frozen=True)
class Submission:
    """What is known about a pull request at the moment it is checked."""

    pr_url: str
    body: str = ""
    commit_message: str = ""
    authorship_text: str | None = None
    tests_run: bool = False
    adversarial_review_run: bool = False


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    @property
    def blocking(self) -> bool:
        """Tone findings are advisory; everything else gates the draft → ready transition."""
        return not self.name.startswith("tone.")


def prompt_section(profile: Profile) -> str:
    """Policy rendered as prompt text, so the agent writes compliant output the first time."""
    lines: list[str] = ["Contribution policy for the target repository:"]
    lines.extend(f"- {item}" for item in profile.tone.guidance)

    avoid = [*profile.tone.banned_openers, *profile.tone.banned_phrases]
    if avoid:
        lines.append(f"- Do not use these phrases: {', '.join(repr(item) for item in avoid)}.")

    contribution = profile.contribution
    if contribution.pr_template:
        lines.append(
            f"- Use the repository's {contribution.pr_template} and fill in every section."
        )
    if contribution.require_generated_by_trailer:
        lines.append(
            "- End the commit message with a `Generated-by:` trailer naming the tool, per the "
            "ASF generative tooling guidance."
        )
    if contribution.require_ai_disclosure:
        lines.append(
            "- Include an `### AI DISCLOSURE` section in the pull request body stating that the "
            "change was authored with AI assistance."
        )
    if contribution.require_local_test_evidence:
        lines.append(
            "- Run the repository's lint and test commands locally and quote the exact commands "
            "and their output in the pull request body."
        )
    if contribution.require_adversarial_review:
        lines.append(
            "- Review your own diff adversarially before opening the pull request and record "
            "what you looked for and what you found."
        )
    if contribution.require_human_authorship:
        lines.append(
            "- Open the pull request as a draft with an empty `### AUTHOR'S NOTE` section. A "
            "human fills that section in their own voice before the pull request is marked "
            "ready. Do not write it, and do not suggest wording for it."
        )
    return "\n".join(lines)


def _sentence_count(text: str) -> int:
    return len([part for part in SENTENCE_END.split(text.strip()) if part.strip()])


def evaluate(profile: Profile, submission: Submission) -> list[CheckResult]:
    """Check one pull request against a profile. Order is stable so the panel does not jitter."""
    contribution = profile.contribution
    body = submission.body
    lowered = body.lower()
    results: list[CheckResult] = []

    if contribution.require_generated_by_trailer:
        found = bool(GENERATED_BY_PATTERN.search(submission.commit_message))
        results.append(
            CheckResult(
                "contribution.generated_by_trailer",
                found,
                "present in commit message" if found else "no `Generated-by:` trailer",
            )
        )

    if contribution.require_ai_disclosure:
        found = "ai disclosure" in lowered or "ai-assisted" in lowered
        results.append(
            CheckResult(
                "contribution.ai_disclosure",
                found,
                "disclosure section present" if found else "no AI disclosure in the body",
            )
        )

    if contribution.require_local_test_evidence:
        results.append(
            CheckResult(
                "contribution.local_test_evidence",
                submission.tests_run,
                "tests run locally" if submission.tests_run else "no record of a local test run",
            )
        )

    if contribution.require_adversarial_review:
        ran = submission.adversarial_review_run
        results.append(
            CheckResult(
                "contribution.adversarial_review",
                ran,
                "self-review recorded" if ran else "no adversarial self-review recorded",
            )
        )

    if contribution.require_human_authorship:
        text = (submission.authorship_text or "").strip()
        sentences = _sentence_count(text)
        needed = contribution.authorship_min_sentences
        passed = bool(text) and sentences >= needed
        detail = (
            f"{sentences} sentence(s) supplied by a human"
            if passed
            else "awaiting a human-authored paragraph"
        )
        results.append(CheckResult("contribution.human_authorship", passed, detail))

    for opener in profile.tone.banned_openers:
        if lowered.lstrip().startswith(opener.lower()):
            results.append(CheckResult("tone.banned_opener", False, f"body opens with {opener!r}"))
    hits = [phrase for phrase in profile.tone.banned_phrases if phrase.lower() in lowered]
    if hits:
        results.append(CheckResult("tone.banned_phrase", False, f"body contains {', '.join(hits)}"))

    return results


def blocks_ready(results: list[CheckResult]) -> list[CheckResult]:
    """The subset that must pass before a draft may be marked ready for review."""
    return [result for result in results if result.blocking and not result.passed]
