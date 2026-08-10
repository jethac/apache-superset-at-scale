"""Devin API client.

Authentication is a bearer API key (service user key, or a personal access token) — not SSO,
which governs webapp login rather than API calls.

The client is defined as a Protocol with a real HTTP implementation and an in-memory fake. The
fake is what makes the whole workflow runnable offline with no credentials and no spend, which
matters more than it sounds: a demo that cannot be run by the person evaluating it is a claim,
not a demonstration.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

SESSION_STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["pr_opened", "no_action_needed", "escalated", "failed"],
        },
        "summary": {"type": "string"},
        "pr_url": {"type": ["string", "null"]},
    },
    "required": ["outcome", "summary"],
}


@dataclass(frozen=True)
class SessionRequest:
    prompt: str
    tags: list[str]
    playbook_id: str | None = None
    max_acu_limit: int | None = None
    idempotent: bool = True


@dataclass(frozen=True)
class SessionState:
    session_id: str
    status: str
    status_detail: str | None
    pr_url: str | None
    acus_consumed: float | None
    structured_output: dict[str, Any] | None
    url: str | None = None


@dataclass(frozen=True)
class Repository:
    """A repository Devin itself can reach, as reported by the Devin API.

    This is distinct from what a GitHub token can see. Devin's access is granted by the org's git
    integration, so a repo can be perfectly visible to the collector and still unclonable by a
    session — a failure that otherwise only surfaces once a session is already running.
    """

    repo_path: str
    host: str
    indexed: bool


class DevinClient(Protocol):
    def create_session(self, request: SessionRequest) -> SessionState: ...

    def get_session(self, session_id: str) -> SessionState: ...

    def list_repositories(self) -> list[Repository]: ...


def _state_from_payload(payload: dict[str, Any]) -> SessionState:
    pull_requests = payload.get("pull_requests") or []
    pr_url = None
    if pull_requests and isinstance(pull_requests[0], dict):
        pr_url = pull_requests[0].get("url")
    elif isinstance(payload.get("pull_request"), dict):
        pr_url = (payload["pull_request"] or {}).get("url")
    return SessionState(
        session_id=str(payload.get("session_id") or payload.get("id") or ""),
        status=str(payload.get("status_enum") or payload.get("status") or "unknown"),
        status_detail=payload.get("status_detail"),
        pr_url=pr_url,
        acus_consumed=payload.get("acus_consumed"),
        structured_output=payload.get("structured_output"),
        url=payload.get("url"),
    )


class HttpDevinClient:
    """Real client against the Devin REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.devin.ai",
        timeout: float = 30.0,
        org_id: str | None = None,
    ):
        if not api_key:
            raise ValueError("Devin API key is required")
        self._org_id = org_id
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def create_session(self, request: SessionRequest) -> SessionState:
        body: dict[str, Any] = {
            "prompt": request.prompt,
            "tags": request.tags,
            "idempotent": request.idempotent,
            "structured_output_schema": SESSION_STRUCTURED_OUTPUT_SCHEMA,
        }
        if request.playbook_id:
            body["playbook_id"] = request.playbook_id
        if request.max_acu_limit:
            body["max_acu_limit"] = request.max_acu_limit
        response = self._client.post("/v1/sessions", json=body)
        response.raise_for_status()
        return _state_from_payload(response.json())

    def get_session(self, session_id: str) -> SessionState:
        response = self._client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        return _state_from_payload(response.json())

    def list_repositories(self) -> list[Repository]:
        """Repositories the Devin organisation can reach. Requires an organisation ID."""
        if not self._org_id:
            raise ValueError("DEVIN_ORG_ID is required to list repositories")
        repositories: list[Repository] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"first": 100}
            if cursor:
                params["after"] = cursor
            response = self._client.get(
                f"/v3beta1/organizations/{self._org_id}/repositories", params=params
            )
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("items") or []:
                status = (item.get("indexing_status") or {}).get("indexing_enabled")
                repositories.append(
                    Repository(
                        repo_path=str(item.get("repo_path") or ""),
                        host=str(item.get("git_connection_host") or ""),
                        indexed=bool(status),
                    )
                )
            if not payload.get("has_next_page"):
                return repositories
            cursor = payload.get("end_cursor")
            if not cursor:
                return repositories

    def close(self) -> None:
        self._client.close()


@dataclass
class FakeDevinClient:
    """Deterministic in-memory stand-in used by the simulator and the tests.

    Outcomes are drawn from a seeded distribution rather than always succeeding. A simulator in
    which every session opens a merged PR produces a dashboard that cannot show its own failure
    modes, which defeats the point of the reporting layer.
    """

    seed: int = 1
    outcomes: tuple[tuple[str, float], ...] = (
        ("pr_opened", 0.68),
        ("no_action_needed", 0.12),
        ("escalated", 0.12),
        ("failed", 0.08),
    )
    sessions: dict[str, SessionState] = field(default_factory=dict)
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _random: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._random = random.Random(self.seed)  # noqa: S311 — simulation only

    def _pick_outcome(self) -> str:
        population = [name for name, _ in self.outcomes]
        weights = [weight for _, weight in self.outcomes]
        # Simulation only: this randomness shapes a demo distribution, not a secret.
        return self._random.choices(population, weights=weights, k=1)[0]  # noqa: S311

    def create_session(self, request: SessionRequest) -> SessionState:
        index = next(self._counter)
        session_id = f"devin-sim-{index:04d}"
        outcome = self._pick_outcome()
        pr_url = None
        status, status_detail = "blocked", "finished"
        if outcome == "pr_opened":
            pr_url = f"https://github.com/jethac/superset/pull/{9000 + index}"
        elif outcome == "escalated":
            status_detail = "waiting_for_user"
        elif outcome == "failed":
            status, status_detail = "expired", "errored"
        state = SessionState(
            session_id=session_id,
            status=status,
            status_detail=status_detail,
            pr_url=pr_url,
            acus_consumed=round(self._random.uniform(0.8, 6.5), 2),
            structured_output={
                "outcome": outcome,
                "summary": f"simulated {outcome}",
                "pr_url": pr_url,
            },
            url=f"https://app.devin.ai/sessions/{session_id}",
        )
        self.sessions[session_id] = state
        return state

    def get_session(self, session_id: str) -> SessionState:
        return self.sessions[session_id]

    def list_repositories(self) -> list[Repository]:
        return [
            Repository(repo_path="jethac/superset", host="github.com", indexed=True),
            Repository(
                repo_path="jethac/apache-superset-at-scale", host="github.com", indexed=True
            ),
        ]
