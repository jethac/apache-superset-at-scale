"""Collect repository facts into the fact store.

The collector is the part that makes the "before" column possible. It reads GitHub over an
arbitrary window, so the baseline period — which predates the deployment and therefore has no
Devin data at all — is produced by exactly the same code as the post-deployment period. Devin
data is joined on afterwards as an attribution overlay, never as a source of the headline number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .github import GitHubClient
from .store import FactStore

COHORT_AGENT = "agent"
COHORT_HUMAN = "human"
COHORT_DEPENDABOT = "dependabot"
COHORT_UNATTRIBUTED = "unattributed"

DEPENDABOT_LOGINS = frozenset({"dependabot[bot]", "dependabot-preview[bot]"})


def cohort_for(pr_url: str, author: str, is_bot: bool, attributed: set[str]) -> str:
    """Which cohort a pull request belongs to.

    Attribution by PR URL is the only thing that proves *this* deployment produced a change, so
    `agent` still requires it. What changed is where the rest goes. Sending everything
    unattributable to `human` put machine-authored pull requests — Devin's own integration
    account among them — into the cohort the comparison is measured against, which inflates the
    baseline with the very work it is the control for. F10 calls agent-versus-contemporaneous-human
    the comparison that survives scrutiny; it does not survive the agent being on both sides.

    So a bot we cannot attribute lands in `unattributed`, which is F5's rule applied to pull
    requests: work that cannot be attributed is named, not filed somewhere convenient. Dependabot
    is split out because F10 lists it as its own cohort and its volume would swamp either bucket.

    If you would rather claim every Devin-authored pull request as `agent` regardless of whether a
    session can be pointed at, change the `COHORT_UNATTRIBUTED` below. That is a defensible
    reading, but it asserts provenance from a username, and a skeptic can rename a bot.
    """
    if pr_url in attributed:
        return COHORT_AGENT
    if author in DEPENDABOT_LOGINS:
        return COHORT_DEPENDABOT
    if is_bot:
        return COHORT_UNATTRIBUTED
    return COHORT_HUMAN


@dataclass
class Collector:
    github: GitHubClient
    store: FactStore

    def collect_pull_requests(self, repo: str, since: datetime, until: datetime) -> int:
        """Ingest PRs opened in a window, tagging each with its cohort.

        Attribution is by PR URL against tasks already recorded by the orchestrator, so a PR is
        only counted as agent work if this deployment can point at the session that produced it.
        Everything else is sorted by authorship rather than swept into `human`; see `cohort_for`.
        """
        attributed = {
            str(row["pr_url"])
            for row in self.store.query("SELECT pr_url FROM fact_task WHERE pr_url IS NOT NULL")
        }
        count = 0
        for fact in self.github.list_pull_requests(repo, since, until):
            cohort = cohort_for(fact.pr_url, fact.author, fact.is_bot, attributed)
            self.store.upsert_pull_request(fact, cohort)
            count += 1
        return count

    def snapshot_open_issue_count(self, repo: str, day: datetime) -> int:
        issues = self.github.list_issues(repo)
        self.store.record_snapshot(day, f"open_issues:{repo}", float(len(issues)))
        return len(issues)
