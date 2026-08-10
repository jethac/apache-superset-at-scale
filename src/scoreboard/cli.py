"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from . import backfill, cicost, debt
from .collector import Collector
from .config import Settings
from .devin import FakeDevinClient, HttpDevinClient
from .flow import build_edges, funnel, reconciles
from .github import HttpGitHubClient
from .normalize import from_github
from .orchestrator import Orchestrator
from .outbox import list_outbox
from .policy import PolicyConfig
from .report import build_report
from .scope import ScopeConfig
from .simulate import render, run_simulation
from .store import FactStore
from .wizard import run_wizard


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scoreboard",
        description="Devin @ apache/superset: event-driven automation and its scoreboard",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="interactive setup wizard for GitHub and Devin credentials")

    serve = subparsers.add_parser("serve", help="run the webhook receiver and report API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    simulate = subparsers.add_parser(
        "simulate", help="run the full workflow offline, no credentials"
    )
    simulate.add_argument("--events", type=int, default=24)
    simulate.add_argument("--seed", type=int, default=7)

    intake = subparsers.add_parser("intake", help="poll GitHub issues and route them")
    intake.add_argument("--repo", action="append", required=True)
    intake.add_argument("--since-days", type=int, default=30)

    subparsers.add_parser("sync", help="poll started Devin sessions and record their outcomes")

    poll = subparsers.add_parser(
        "poll", help="run intake and sync on an interval: the scheduled trigger"
    )
    poll.add_argument("--repo", action="append", required=True)
    poll.add_argument("--since-days", type=int, default=30)
    poll.add_argument("--interval", type=int, default=300, help="seconds between passes")
    poll.add_argument("--passes", type=int, default=0, help="stop after N passes; 0 runs forever")

    collect = subparsers.add_parser("collect", help="collect PR facts for a window")
    collect.add_argument("--repo", required=True)
    collect.add_argument("--since-days", type=int, default=90)

    cicost_parser = subparsers.add_parser(
        "cicost", help="record billed CI job-minutes for pull-request runs"
    )
    cicost_parser.add_argument("--repo", required=True)
    cicost_parser.add_argument("--since-days", type=int, default=30)
    cicost_parser.add_argument(
        "--until-days", type=int, default=0, help="end the window N days ago, for back-sampling"
    )
    cicost_parser.add_argument(
        "--max-runs", type=int, default=40, help="cap runs read per window; 0 reads all of it"
    )

    measure = subparsers.add_parser(
        "measure", help="run oxlint against a checkout and record the violation counts"
    )
    measure.add_argument("--checkout", type=Path, required=True, help="path to a Superset clone")
    measure.add_argument("--repo", default="apache/superset", help="repository the checkout is of")
    measure.add_argument("--config", default=debt.DEFAULT_CONFIG)

    backfill_parser = subparsers.add_parser(
        "backfill", help="measure historical commits of a checkout to produce a debt series"
    )
    backfill_parser.add_argument(
        "--checkout", type=Path, required=True, help="path to a Superset clone"
    )
    backfill_parser.add_argument(
        "--repo", default="apache/superset", help="repository the checkout is of"
    )
    backfill_parser.add_argument(
        "--months", type=int, default=12, help="how many monthly points to measure"
    )
    backfill_parser.add_argument("--config", default=debt.DEFAULT_CONFIG)

    subparsers.add_parser("report", help="print the funnel and the Sankey edge list")

    brief = subparsers.add_parser(
        "brief", help="write the markdown status report for pasting into an issue or an email"
    )
    brief.add_argument(
        "--repo", help="repository whose sessions the brief covers; defaults to the scope target"
    )
    brief.add_argument("--out", type=Path, help="write to FILE instead of stdout")

    subparsers.add_parser(
        "outbox", help="list draft pull requests waiting on a human authorship paragraph"
    )

    replay = subparsers.add_parser("replay", help="route a saved webhook payload from a file")
    replay.add_argument("--event", required=True, help="GitHub event name, e.g. issues")
    replay.add_argument("path", type=Path)

    return parser


def _intake(
    github: HttpGitHubClient, orchestrator: Orchestrator, repos: list[str], since_days: int
) -> None:
    since = datetime.now(UTC) - timedelta(days=since_days)
    for repo in repos:
        for event in github.list_issues(repo, since):
            task = orchestrator.handle(event)
            logging.info(
                "%s#%s -> %s (%s)", repo, event.number, task.state.value, task.decision.reason
            )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _build_parser().parse_args(argv)
    settings = Settings.from_env()

    if args.command == "init":
        return run_wizard(settings.scope_path)

    if args.command == "serve":
        uvicorn.run("scoreboard.api:app", host=args.host, port=args.port, log_level="info")
        return 0

    if args.command == "simulate":
        result = run_simulation(
            settings.scope_path,
            settings.policy_path,
            settings.db_path,
            event_count=args.events,
            seed=args.seed,
        )
        print(render(result))
        return 0 if result["reconciles"] else 1

    scope = ScopeConfig.load(settings.scope_path)
    policy = PolicyConfig.load(settings.policy_path)
    store = FactStore(settings.db_path)

    if args.command == "outbox":
        items = list_outbox(store)
        print(
            json.dumps(
                [
                    {
                        "task_id": item.task_id,
                        "pr_url": item.pr_url,
                        "profile": item.profile,
                        "waiting_days": round(item.waiting_days, 2),
                        "failing_checks": item.failing_checks,
                    }
                    for item in items
                ],
                indent=2,
            )
        )
        return 0

    if args.command == "report":
        counts = funnel(store)
        print(
            json.dumps(
                {
                    "funnel": counts,
                    "reconciles": reconciles(counts),
                    "sankey_edges": [edge.__dict__ for edge in build_edges(store)],
                },
                indent=2,
            )
        )
        return 0

    if args.command == "brief":
        # The trend tables belong to their own modules, so a store written before they landed
        # still produces a brief that says "no data" rather than failing on a missing table.
        debt.ensure_schema(store)
        cicost.ensure_schema(store)
        markdown = build_report(
            store,
            args.repo or scope.defaults.target_repo,
            measure_repo=settings.measure_repo,
        )
        if args.out:
            Path(args.out).write_text(markdown, encoding="utf-8")
            logging.info("wrote %s", args.out)
        else:
            print(markdown, end="")
        return 0

    devin = (
        HttpDevinClient(
            settings.devin_api_key, settings.devin_base_url, org_id=settings.devin_org_id
        )
        if settings.devin_api_key
        else FakeDevinClient()
    )
    if not settings.devin_api_key:
        logging.warning("DEVIN_API_KEY not set: using the offline fake client")
    orchestrator = Orchestrator(
        scope=scope,
        store=store,
        devin=devin,
        policy=policy,
        dry_run=settings.dry_run,
        allow_upstream_write=settings.allow_upstream_write,
    )

    if args.command == "sync":
        moved = orchestrator.sync()
        for task_id, state in moved:
            logging.info("%s -> %s", task_id, state.value)
        logging.info("%d session(s) reached an outcome", len(moved))
        return 0

    if args.command == "replay":
        payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
        task = orchestrator.handle(from_github("replay", args.event, payload))
        print(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "state": task.state.value,
                    "stream": task.decision.stream,
                    "reason": task.decision.reason,
                },
                indent=2,
            )
        )
        return 0

    github = HttpGitHubClient(settings.github_token, settings.github_api_url)

    if args.command == "intake":
        _intake(github, orchestrator, args.repo, args.since_days)
        return 0

    if args.command == "poll":
        passes = 0
        while args.passes == 0 or passes < args.passes:
            _intake(github, orchestrator, args.repo, args.since_days)
            for task_id, state in orchestrator.sync():
                logging.info("%s -> %s", task_id, state.value)
            passes += 1
            if args.passes and passes >= args.passes:
                break
            time.sleep(args.interval)
        return 0

    if args.command == "measure":
        observations = debt.scan(args.checkout, config=args.config, repo=args.repo)
        debt.ensure_schema(store)
        debt.record_run(store, observations)
        logging.info(
            "recorded %d violations across %d rules for %s",
            sum(observation.count for observation in observations),
            len(observations),
            args.repo,
        )
        return 0

    if args.command == "backfill":
        backfilled = backfill.backfill(
            store, args.checkout, repo=args.repo, months=args.months, config=args.config
        )
        logging.info(
            "measured %d commit(s), skipped %d",
            len(backfilled.measured),
            len(backfilled.skipped),
        )
        return 0

    if args.command == "cicost":
        now = datetime.now(UTC)
        written = cicost.collect(
            github,
            store,
            args.repo,
            now - timedelta(days=args.since_days),
            now - timedelta(days=args.until_days),
            max_runs=args.max_runs or None,
        )
        logging.info("recorded %d CI job(s) from %s", written, args.repo)
        return 0

    if args.command == "collect":
        now = datetime.now(UTC)
        count = Collector(github=github, store=store).collect_pull_requests(
            args.repo, now - timedelta(days=args.since_days), now
        )
        logging.info("collected %d pull requests from %s", count, args.repo)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
