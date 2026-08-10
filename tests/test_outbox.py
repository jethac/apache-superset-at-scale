from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scoreboard.devin import FakeDevinClient
from scoreboard.github import FakeGitHubClient
from scoreboard.models import TaskState
from scoreboard.orchestrator import Orchestrator
from scoreboard.outbox import (
    AuthorshipRejectedError,
    list_outbox,
    render_body_with_authorship,
    submit_authorship,
)
from scoreboard.policy import PolicyConfig
from scoreboard.scope import ScopeConfig
from scoreboard.store import FactStore
from tests.conftest import REPO_ROOT, make_event

POLICY = PolicyConfig.load(REPO_ROOT / "policy.yaml")
PARAGRAPH = "I reproduced this on my fork and read the diff line by line before submitting it."


def _draft_task(store: FactStore, scope: ScopeConfig) -> str:
    """Drive a real event through the orchestrator until it parks in the outbox."""
    devin = FakeDevinClient(seed=3)
    orchestrator = Orchestrator(scope=scope, store=store, devin=devin, policy=POLICY, dry_run=False)
    for number in range(1, 40):
        task = orchestrator.handle(make_event(number=number, labels=["bug"]))
        if task.state is TaskState.DRAFT_AWAITING_AUTHORSHIP:
            return task.task_id
    raise AssertionError("no draft reached the outbox")


def test_delivered_draft_parks_in_the_outbox(store: FactStore, scope: ScopeConfig) -> None:
    task_id = _draft_task(store, scope)
    items = list_outbox(store)
    assert [item.task_id for item in items] == [task_id]
    assert items[0].profile == "asf-superset"
    assert "contribution.human_authorship" in items[0].failing_checks


def test_empty_text_is_rejected(store: FactStore, scope: ScopeConfig) -> None:
    task_id = _draft_task(store, scope)
    with pytest.raises(AuthorshipRejectedError):
        submit_authorship(store, FakeGitHubClient(), POLICY, task_id, "   ", "jethac")
    assert len(list_outbox(store)) == 1


def test_dictated_text_is_stored_verbatim_and_clears_the_draft(
    store: FactStore, scope: ScopeConfig
) -> None:
    task_id = _draft_task(store, scope)
    github = FakeGitHubClient()
    pr_url = list_outbox(store)[0].pr_url
    number = int(pr_url.rsplit("/", 1)[-1])
    github.drafts.add(("jethac/superset", number))

    submit_authorship(store, github, POLICY, task_id, PARAGRAPH, "jethac", "dictated")

    recorded = store.authorship_for(task_id)
    assert recorded is not None
    assert recorded.text == PARAGRAPH
    assert recorded.author == "jethac"
    assert recorded.input_method == "dictated"
    assert ("jethac/superset", number) not in github.drafts
    assert PARAGRAPH in github.bodies[("jethac/superset", number)]
    assert list_outbox(store) == []


def test_authorship_is_spliced_into_the_existing_section_unmodified() -> None:
    body = (
        "### SUMMARY\n\nA change.\n\n### AUTHOR'S NOTE\n\n_(pending)_\n\n### TESTING\n\nRan it.\n"
    )
    rendered = render_body_with_authorship(body, PARAGRAPH, "jethac", "dictated")
    assert "_(pending)_" not in rendered
    assert PARAGRAPH in rendered
    assert "### TESTING" in rendered
    assert rendered.index(PARAGRAPH) < rendered.index("### TESTING")


def test_authorship_section_is_appended_when_the_template_lacks_one() -> None:
    rendered = render_body_with_authorship("### SUMMARY\n\nA change.\n", PARAGRAPH, "me", "typed")
    assert rendered.rstrip().endswith("— @me (typed)")


def test_unknown_task_is_a_key_error(store: FactStore) -> None:
    with pytest.raises(KeyError):
        submit_authorship(store, FakeGitHubClient(), POLICY, "nope", PARAGRAPH, "jethac")


def test_outbox_records_how_long_the_human_has_been_the_bottleneck(
    store: FactStore, scope: ScopeConfig
) -> None:
    _draft_task(store, scope)
    item = list_outbox(store)[0]
    assert 0 <= item.waiting_days < 1
    assert item.waiting_since <= datetime.now(UTC)
