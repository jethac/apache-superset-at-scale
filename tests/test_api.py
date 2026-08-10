from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scoreboard.api import create_app
from scoreboard.config import Settings
from tests.conftest import REPO_ROOT
from tests.test_normalize import ISSUE_PAYLOAD

SECRET = "webhook-secret"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        devin_api_key=None,
        devin_base_url="https://api.devin.ai",
        devin_org_id=None,
        github_token=None,
        github_api_url="https://api.github.com",
        webhook_secret=SECRET,
        scope_path=REPO_ROOT / "scope.yaml",
        policy_path=REPO_ROOT / "policy.yaml",
        db_path=tmp_path / "facts.db",
        dry_run=True,
        allow_upstream_write=False,
    )
    return TestClient(create_app(settings))


def post(client: TestClient, payload: dict[str, object], secret: str = SECRET) -> object:
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-Hub-Signature-256": f"sha256={digest}",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-1",
            "Content-Type": "application/json",
        },
    )


def test_health_reports_dry_run_and_rule_count(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True
    assert body["rules"] > 0


def test_signed_delivery_is_routed(client: TestClient) -> None:
    response = post(client, ISSUE_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["stream"] == "bugfix"


def test_unsigned_delivery_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/webhook/github",
        content=b"{}",
        headers={"X-GitHub-Event": "issues"},
    )
    assert response.status_code == 401


def test_wrongly_signed_delivery_is_rejected(client: TestClient) -> None:
    assert post(client, ISSUE_PAYLOAD, secret="not-the-secret").status_code == 401


def test_funnel_endpoint_counts_the_delivery(client: TestClient) -> None:
    post(client, ISSUE_PAYLOAD)
    counts = client.get("/funnel").json()
    assert counts["triggered"] == 1


def test_outbox_is_empty_until_a_draft_is_delivered(client: TestClient) -> None:
    assert client.get("/outbox").json() == []


def test_health_lists_the_loaded_policy_profiles(client: TestClient) -> None:
    assert "asf-superset" in client.get("/health").json()["policy_profiles"]


def test_authorship_input_method_is_constrained(client: TestClient) -> None:
    response = client.post(
        "/outbox/whatever/authorship",
        json={"text": "words", "author": "jethac", "input_method": "generated"},
    )
    assert response.status_code == 422


def test_unknown_task_authorship_is_not_found(client: TestClient) -> None:
    response = client.post(
        "/outbox/whatever/authorship",
        json={"text": "words", "author": "jethac", "input_method": "dictated"},
    )
    assert response.status_code == 404


def test_dashboard_is_mounted_and_serves_its_data(client: TestClient) -> None:
    assert client.get("/dashboard").status_code == 200
    payload = client.get("/dashboard/data").json()
    assert set(payload) >= {"repo", "debt", "ci_cost", "flow", "funnel", "outbox"}
