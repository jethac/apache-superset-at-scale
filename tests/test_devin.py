"""Parsing of what the Devin API says about sessions this deployment did not create.

Adoption is only as trustworthy as the listing it reads, and the listing is the one payload the
orchestrator cannot shape: the tags on a session started elsewhere are whatever that dispatcher
set, and fields the API omits have to survive rather than raise.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from scoreboard.devin import HttpDevinClient


def _client(payload: dict[str, object]) -> HttpDevinClient:
    client = HttpDevinClient("apk_user_test", base_url="https://api.devin.ai")
    client.close()
    client._client = httpx.Client(  # noqa: SLF001 - swapping the transport is the whole point
        base_url="https://api.devin.ai",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    return client


def test_a_listed_session_carries_its_tags_and_start_time() -> None:
    listed = _client(
        {
            "sessions": [
                {
                    "session_id": "devin-abc",
                    "title": "Retire the last Cypress specs",
                    "tags": ["fde:initiative=superset-scoreboard", "fde:stream=techdebt"],
                    "status_enum": "running",
                    "created_at": "2026-08-10T04:08:00Z",
                }
            ]
        }
    ).list_sessions()

    assert len(listed) == 1
    assert listed[0].session_id == "devin-abc"
    assert listed[0].tags == ["fde:initiative=superset-scoreboard", "fde:stream=techdebt"]
    assert listed[0].status == "running"
    assert listed[0].created_at == datetime(2026, 8, 10, 4, 8, tzinfo=UTC)


def test_a_sparse_session_still_parses() -> None:
    listed = _client({"sessions": [{"session_id": "devin-sparse"}]}).list_sessions()

    assert listed[0].tags == []
    assert listed[0].created_at is None
    assert listed[0].status == "unknown"


def test_an_empty_listing_is_not_an_error() -> None:
    assert _client({}).list_sessions() == []
