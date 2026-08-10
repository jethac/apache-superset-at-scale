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


@dataclass
class Collector:
    github: GitHubClient
    store: FactStore

    def collect_pull_requests(self, repo: str, since: datetime, until: datetime) -> int:
        """Ingest PRs opened in a window, tagging each with its cohort.

        Attribution is by PR URL against tasks already recorded by the orchestrator, so a PR is
        only counted as agent work if this deployment can point at the session that produced it.
        Unattributable PRs fall to the human cohort rather than being claimed.
        """
        attributed = {
            str(row["pr_url"])
            for row in self.store.query("SELECT pr_url FROM fact_task WHERE pr_url IS NOT NULL")
        }
        count = 0
        for fact in self.github.list_pull_requests(repo, since, until):
            cohort = COHORT_AGENT if fact.pr_url in attributed else COHORT_HUMAN
            self.store.upsert_pull_request(fact, cohort)
            count += 1
        return count

    def snapshot_open_issue_count(self, repo: str, day: datetime) -> int:
        issues = self.github.list_issues(repo)
        self.store.record_snapshot(day, f"open_issues:{repo}", float(len(issues)))
        return len(issues)
