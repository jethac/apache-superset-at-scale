"""Webhook intake service.

The endpoint does as little as possible: verify the signature, normalise, route, and record.
Anything slower or riskier belongs behind the CLI, because a webhook handler that can block is a
webhook handler that will be retried, and a handler that is retried must be idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings
from .devin import FakeDevinClient, HttpDevinClient
from .flow import funnel
from .github import HttpGitHubClient
from .normalize import UnsupportedEventError, from_github
from .orchestrator import Orchestrator
from .outbox import AuthorshipRejectedError, list_outbox, submit_authorship
from .policy import PolicyConfig
from .scope import ScopeConfig
from .security import SignatureError, verify_github_signature
from .store import FactStore

logger = logging.getLogger(__name__)


class AuthorshipSubmission(BaseModel):
    """The one thing a human must supply. Stored verbatim; never generated or rewritten."""

    text: str
    author: str
    input_method: str = Field(default="typed", pattern="^(typed|dictated)$")


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(title="Deployment Scoreboard", version="0.1.0")

    scope = ScopeConfig.load(config.scope_path)
    policy = PolicyConfig.load(config.policy_path)
    store = FactStore(config.db_path)
    github = HttpGitHubClient(config.github_token, config.github_api_url)
    devin = (
        HttpDevinClient(config.devin_api_key, config.devin_base_url, org_id=config.devin_org_id)
        if config.devin_api_key
        else FakeDevinClient()
    )
    orchestrator = Orchestrator(
        scope=scope,
        store=store,
        devin=devin,
        policy=policy,
        dry_run=config.dry_run,
        allow_upstream_write=config.allow_upstream_write,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "dry_run": config.dry_run,
            "devin_client": type(devin).__name__,
            "rules": len(scope.rules),
            "policy_profiles": sorted(policy.profiles),
        }

    @app.get("/funnel")
    def read_funnel() -> dict[str, int]:
        return funnel(store)

    @app.get("/outbox")
    def read_outbox() -> list[dict[str, Any]]:
        """Draft pull requests waiting on a human paragraph, oldest first."""
        return [
            {
                "task_id": item.task_id,
                "pr_url": item.pr_url,
                "target_repo": item.target_repo,
                "stream": item.stream,
                "policy_profile": item.profile,
                "title": item.title,
                "issue_url": item.issue_url,
                "waiting_days": round(item.waiting_days, 2),
                "failing_checks": item.failing_checks,
            }
            for item in list_outbox(store)
        ]

    @app.post("/outbox/{task_id}/authorship")
    def post_authorship(task_id: str, submission: AuthorshipSubmission) -> dict[str, Any]:
        try:
            passed = submit_authorship(
                store,
                github,
                policy,
                task_id,
                submission.text,
                submission.author,
                submission.input_method,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AuthorshipRejectedError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"task_id": task_id, "ready_for_review": True, "checks_passed": passed}

    @app.get("/compliance")
    def read_compliance() -> list[dict[str, Any]]:
        """Per-pull-request policy evidence: which checks ran, which passed, and why."""
        rows = store.query(
            "SELECT pr_url, profile, check_name, passed, detail, checked_at"
            " FROM fact_policy_check ORDER BY pr_url, check_name"
        )
        return [
            {
                "pr_url": row["pr_url"],
                "profile": row["profile"],
                "check": row["check_name"],
                "passed": bool(row["passed"]),
                "detail": row["detail"],
                "checked_at": row["checked_at"],
            }
            for row in rows
        ]

    @app.post("/webhook/github")
    async def github_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await request.body()
        try:
            verify_github_signature(config.webhook_secret, body, x_hub_signature_256)
        except SignatureError as error:
            logger.warning("rejected webhook delivery: %s", error)
            raise HTTPException(status_code=401, detail="invalid signature") from error

        if not x_github_event:
            raise HTTPException(status_code=400, detail="missing X-GitHub-Event")

        payload = await request.json()
        try:
            event = from_github(x_github_delivery or "unknown", x_github_event, payload)
        except UnsupportedEventError as error:
            # The detail names payload fields, so it belongs in the operator's log
            # rather than in a response body an arbitrary sender can read.
            logger.info("ignored delivery %s: %s", x_github_delivery, error)
            return {"accepted": False, "reason": "unsupported event"}

        task = orchestrator.handle(event)
        return {
            "accepted": task.decision.admitted,
            "task_id": task.task_id,
            "state": task.state.value,
            "stream": task.decision.stream,
            "reason": task.decision.reason,
            "session_id": task.session_id,
        }

    return app


app = create_app()
