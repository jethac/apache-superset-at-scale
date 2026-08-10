"""Webhook intake service.

The endpoint does as little as possible: verify the signature, normalise, route, and record.
Anything slower or riskier belongs behind the CLI, because a webhook handler that can block is a
webhook handler that will be retried, and a handler that is retried must be idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .config import Settings
from .devin import FakeDevinClient, HttpDevinClient
from .flow import funnel
from .normalize import UnsupportedEventError, from_github
from .orchestrator import Orchestrator
from .scope import ScopeConfig
from .security import SignatureError, verify_github_signature
from .store import FactStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    app = FastAPI(title="Deployment Scoreboard", version="0.1.0")

    scope = ScopeConfig.load(config.scope_path)
    store = FactStore(config.db_path)
    devin = (
        HttpDevinClient(config.devin_api_key, config.devin_base_url, org_id=config.devin_org_id)
        if config.devin_api_key
        else FakeDevinClient()
    )
    orchestrator = Orchestrator(
        scope=scope,
        store=store,
        devin=devin,
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
        }

    @app.get("/funnel")
    def read_funnel() -> dict[str, int]:
        return funnel(store)

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
            return {"accepted": False, "reason": str(error)}

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
