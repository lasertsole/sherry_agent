"""Source registry and channel routing tests."""

from __future__ import annotations

import pytest

from superagent.core.model import ChannelAddress, Receipt, Channel
from superagent.infra.commands import CommandResult, FakeCommands
from superagent.infra.health import ClaimError, HealthBoard
from superagent.sources.github import GithubSource
from superagent.sources.github_channel import GithubChannel, marker
from superagent.sources.github_trigger import GithubConfig
from superagent.sources.registry import SourceRegistry


def build():
    commands = FakeCommands()
    board = HealthBoard()
    registry = SourceRegistry(commands, board)
    registry.register(GithubSource())
    config = {
        "repository": "acme/widgets",
        "cli": "gh",
        "agent_login": "agent-bot",
        "observe_issues": True,
        "observe_comments": True,
    }
    return registry, commands, board, config


@pytest.mark.asyncio
async def test_register_claims_health_and_rejects_collision() -> None:
    registry, _commands, board, _config = build()
    board.claim("github", "github")  # same owner is idempotent
    with pytest.raises(ClaimError):
        board.claim("github", "someone-else")
    with pytest.raises(ClaimError):
        board.claim("reply", "github")


def test_validate_config_and_scope() -> None:
    registry, _commands, _board, config = build()
    assert registry.validate("github", config) == []
    issues = registry.validate("github", {"repository": "x", "unknown": 1})
    assert issues and issues[0].path == "unknown"
    scope = registry.scope("github")
    assert scope({"kind": "issue"}, config)["row"] == "github/issue"
    assert scope({"kind": "check"}, config) == {"listened": False, "reason": "notObserved"}


@pytest.mark.asyncio
async def test_channel_send_sent_gone_and_throw() -> None:
    registry, commands, board, config = build()
    receipt = Receipt(
        id="msg-1",
        ticket_id="t1",
        channel=ChannelAddress(
            source="github",
            thread="acme/widgets#1",
            address={"repo": "acme/widgets", "number": "1", "kind": "issue"},
        ),
        purpose=Channel.RESULT,
        text="done",
    )
    commands.responses["issue comment"] = CommandResult(0, '{"id": "c1"}')
    assert await registry.send(receipt, config) == "sent"
    assert marker("msg-1") in commands.argv[0][-1]

    commands.responses["issue comment"] = CommandResult(410, "")
    gone = Receipt(**{**receipt.__dict__, "id": "msg-2"})
    assert await registry.send(gone, config) == "gone"

    commands.responses["issue comment"] = RuntimeError("network down")
    failed = Receipt(**{**receipt.__dict__, "id": "msg-3"})
    assert await registry.send(failed, config) == "waiting"


@pytest.mark.asyncio
async def test_channel_idempotency_after_restart() -> None:
    channel = GithubChannel()
    channel.replay(["old note", f"{marker('msg-9')}\nprevious"])
    commands = FakeCommands()
    receipt = Receipt(
        id="msg-9",
        ticket_id="t1",
        channel=ChannelAddress(
            source="github",
            thread="acme/widgets#1",
            address={"repo": "acme/widgets", "number": "1", "kind": "issue"},
        ),
        purpose=Channel.RESULT,
        text="again",
    )
    result = await channel.send(commands, receipt, GithubConfig(repository="acme/widgets"))
    assert result.status == "sent" and result.message_id == ""
    assert commands.calls == []
