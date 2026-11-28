"""GitHub trigger translation, PR following and notification polling."""

from __future__ import annotations

import json

import pytest

from superagent.infra.commands import CommandResult, FakeCommands
from superagent.infra.health import HealthBoard
from superagent.sources.github import GithubSource
from superagent.sources.github_pull import (
    claim_pull,
    end_notice,
    observe_pull,
    apply_pull_event,
    all_follows_closed,
)
from superagent.sources.github_trigger import GithubConfig, github_trigger, is_bot
from superagent.sources.host import SourceContext
from tests.superagent.conftest import make_ticket

CONFIG = GithubConfig(
    repository="acme/widgets",
    cli="gh",
    agent_login="agent-bot",
    observe_issues=True,
    observe_pulls=True,
    observe_comments=True,
    observe_ci=True,
)


def event(**overrides):
    value = {
        "id": "n1",
        "updated_at": "2026-11-28T00:00:00+00:00",
        "type": "issue",
        "body": "please fix @agent-bot",
        "user": {"login": "alice"},
        "issue": {"number": 42},
        "repository": {"full_name": "acme/widgets"},
        "html_url": "https://github.com/acme/widgets/issues/42",
    }
    value.update(overrides)
    return value


def test_trigger_mention_bot_and_ci_cells() -> None:
    trigger = github_trigger(event(), CONFIG)
    assert trigger is not None
    assert trigger.addressed is not None and trigger.addressed.value == "mention"
    assert trigger.human is True
    assert trigger.threads[0].id == "acme/widgets#42"
    assert trigger.meta["kind"] == "issue"

    bot = github_trigger(event(user={"login": "dependabot[bot]"}), CONFIG)
    assert bot is not None and bot.human is False

    ci = github_trigger(event(type="check", body="", user={}), CONFIG)
    assert ci is not None and ci.actor.id == "" and ci.human is False
    assert is_bot("dependabot[bot]") is True

    assert github_trigger(event(user={"login": "agent-bot"}), CONFIG) is None
    assert "42" in trigger.id or trigger.id  # stable non-empty id


def test_trigger_edit_gets_new_id() -> None:
    first = github_trigger(event(), CONFIG)
    second = github_trigger(event(updated_at="2026-11-28T00:01:00+00:00"), CONFIG)
    assert first is not None and second is not None and first.id != second.id


def test_pr_follow_claim_observe_reopen_and_end() -> None:
    ticket = make_ticket()
    own = claim_pull(ticket, 7, "feature")
    observed = observe_pull(ticket, 3)
    ticket.follows.extend([own, observed])
    assert own.own is True and observed.own is False

    changed, ended = apply_pull_event(ticket, {"number": 7, "state": "closed"})
    assert changed is True and ended is False
    changed, ended = apply_pull_event(ticket, {"number": 3, "state": "merged"})
    assert changed is True and ended is True
    assert all_follows_closed(ticket, {"7", "3"}) is True

    apply_pull_event(ticket, {"number": 7, "state": "open"})
    reopened, ended = apply_pull_event(ticket, {"number": 7, "state": "reopened"})
    assert reopened is True and ended is False

    notice = end_notice(ticket, "merged", "2026-11-28T00:00:00+00:00")
    assert notice is not None and len(notice.id) == 16


@pytest.mark.asyncio
async def test_notification_watch_translates_and_tracks_health() -> None:
    commands = FakeCommands(
        {
            "api notifications": CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "id": "n1",
                            "updated_at": "2026-11-28T00:00:00+00:00",
                            "subject": {"type": "Issue", "title": "bug"},
                            "repository": {"full_name": "acme/widgets"},
                        }
                    ]
                ),
            )
        }
    )
    board = HealthBoard()
    states: list[tuple[str, object]] = []
    seen: list[object] = []
    ctx = SourceContext(
        commands=commands,
        board=board,
        receive=lambda trigger: seen.append(trigger) or "stored",
        report=lambda name, state: states.append((name, state.state)),
    )
    source = GithubSource(CONFIG.model_dump())
    triggers = await source.watch(ctx, CONFIG.model_dump(), started="2026-11-28T00:00:05+00:00")
    assert len(triggers) == 1
    assert len(seen) == 1
    assert states == [("github", "ready")]  # successful poll reports ready


@pytest.mark.asyncio
async def test_watch_reports_three_poll_failures() -> None:
    commands = FakeCommands({"api notifications": RuntimeError("offline")})
    states: list[str] = []
    ctx = SourceContext(
        commands=commands,
        board=HealthBoard(),
        report=lambda _name, state: states.append(state.state),
    )
    source = GithubSource()
    for _ in range(3):
        await source.watch(ctx, CONFIG.model_dump(), started="2026-11-28T00:00:00+00:00")
    assert source.poll_failures == 3
    assert states == ["error", "error", "error"]
