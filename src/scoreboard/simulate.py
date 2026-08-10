"""Offline simulation of a full deployment window.

Runs the real intake, routing, orchestration and reporting code against fixture events and the
fake clients, so the workflow can be demonstrated end to end with no credentials, no network and
no spend. The only substituted components are the two API clients; the decision logic under test
is the same code that runs in production.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .devin import FakeDevinClient
from .flow import build_edges, funnel, reconciles
from .github import FakeGitHubClient, PullRequestFact
from .models import Event, EventType, Severity, digest_payload
from .orchestrator import Orchestrator
from .outbox import list_outbox
from .policy import PolicyConfig
from .scope import ScopeConfig
from .store import FactStore

FIXTURE_TITLES = [
    ("bug", "Sankey chart drops node colour when a stream merges"),
    ("bug", "Dashboard filter resets after cross-filter is applied"),
    ("bug", "SQL Lab autocomplete throws on empty schema"),
    ("greenfield", "Add chart plugin support for stacked area annotations"),
    ("greenfield", "echarts: expose a Color by control on Sankey"),
    ("security", "Update transitive dependency with known advisory"),
    ("noise", "Question: how do I deploy behind a reverse proxy?"),
    ("noise", "[WIP] experimental branch, do not review"),
]


def _fixture_events(count: int, seed: int) -> list[Event]:
    generator = random.Random(seed)  # noqa: S311 — fixture data, not a security context
    now = datetime.now(UTC)
    events: list[Event] = []
    for index in range(count):
        kind, title = FIXTURE_TITLES[index % len(FIXTURE_TITLES)]
        repo = "apache/superset" if index % 3 == 0 else "jethac/superset"
        labels = {
            "bug": ["#bug"] if repo == "apache/superset" else ["bug"],
            "greenfield": ["enhancement", "viz"],
            "security": ["security"],
            "noise": ["question"] if index % 2 else ["#WIP"],
        }[kind]
        severity = Severity.HIGH if kind == "security" else Severity.NONE
        events.append(
            Event(
                event_id=f"sim-{index:04d}",
                event_type=EventType.ISSUE,
                repo=repo,
                number=1000 + index,
                title=title,
                body=f"Simulated fixture issue for the {kind} stream.",
                labels=labels,
                author=f"contributor-{index % 5}",
                author_is_bot=index % 11 == 0,
                severity=severity,
                created_at=now - timedelta(days=generator.uniform(1, 60)),
                url=f"https://github.com/{repo}/issues/{1000 + index}",
                raw_digest=digest_payload({"sim": index}),
            )
        )
    return events


def _fixture_pull_requests(repo: str, agent_pr_urls: list[str]) -> list[PullRequestFact]:
    now = datetime.now(UTC)
    facts = [
        PullRequestFact(
            pr_url=url,
            repo=repo,
            number=int(url.rsplit("/", 1)[-1]),
            author="devin-service-user",
            is_bot=True,
            opened_at=now - timedelta(days=index % 20),
            merged_at=now - timedelta(days=index % 20) + timedelta(days=2) if index % 3 else None,
            closed_at=None,
            additions=40 + index * 7,
            deletions=12 + index,
            changed_files=1 + index % 4,
            review_rounds=1 + index % 3,
            first_push_checks_passed=index % 4 != 0,
        )
        for index, url in enumerate(agent_pr_urls)
    ]
    facts.extend(
        PullRequestFact(
            pr_url=f"https://github.com/{repo}/pull/{5000 + index}",
            repo=repo,
            number=5000 + index,
            author=f"human-{index % 6}",
            is_bot=False,
            opened_at=now - timedelta(days=index % 30),
            merged_at=now - timedelta(days=index % 30) + timedelta(days=4) if index % 2 else None,
            closed_at=None,
            additions=90 + index * 11,
            deletions=30 + index,
            changed_files=2 + index % 6,
            review_rounds=1 + index % 4,
            first_push_checks_passed=index % 3 != 0,
        )
        for index in range(24)
    )
    return facts


def run_simulation(
    scope_path: Path,
    policy_path: Path,
    db_path: Path,
    event_count: int = 24,
    seed: int = 7,
) -> dict[str, object]:
    if db_path.exists():
        db_path.unlink()

    scope = ScopeConfig.load(scope_path)
    policy = PolicyConfig.load(policy_path)
    store = FactStore(db_path)
    devin = FakeDevinClient(seed=seed)
    orchestrator = Orchestrator(scope=scope, store=store, devin=devin, policy=policy, dry_run=False)

    events = _fixture_events(event_count, seed)
    for event in events:
        orchestrator.handle(event)
    # Replay a slice of the events to exercise the dedupe path, as a webhook redelivery would.
    for event in events[: max(1, event_count // 6)]:
        orchestrator.handle(event)

    agent_pr_urls = [
        str(row["pr_url"])
        for row in store.query("SELECT pr_url FROM fact_task WHERE pr_url IS NOT NULL")
    ]
    github = FakeGitHubClient(
        pull_requests={"jethac/superset": _fixture_pull_requests("jethac/superset", agent_pr_urls)}
    )
    from .collector import Collector

    collector = Collector(github=github, store=store)
    now = datetime.now(UTC)
    collected = collector.collect_pull_requests("jethac/superset", now - timedelta(days=90), now)

    counts = funnel(store)
    edges = build_edges(store)
    # The simulation deliberately leaves the outbox full. Filling it would mean generating the
    # human authorship paragraph, which is the one thing the policy exists to prevent.
    outbox = list_outbox(store)
    result: dict[str, object] = {
        "events": len(events),
        "pull_requests_collected": collected,
        "outbox": [
            {
                "task_id": item.task_id,
                "pr_url": item.pr_url,
                "profile": item.profile,
                "failing_checks": item.failing_checks,
            }
            for item in outbox
        ],
        "funnel": counts,
        "reconciles": reconciles(counts),
        "sankey_edges": [
            {
                "stream": edge.stream,
                "source": edge.source,
                "target": edge.target,
                "tasks": edge.task_count,
            }
            for edge in edges
        ],
    }
    store.close()
    return result


def render(result: dict[str, object]) -> str:
    return json.dumps(result, indent=2, sort_keys=True)
