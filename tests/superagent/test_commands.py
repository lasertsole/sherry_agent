"""CLI transport and worker environment tests."""

from __future__ import annotations

import pytest

from superagent.core.model import Ticket
from superagent.infra.commands import CommandResult, FakeCommands, redact_env
from superagent.infra.environment import (
    execution_environment,
    unmanaged_files,
    worker_env,
    write_worker_environment,
)
from superagent.infra.paths import state_paths
from superagent.infra.text import chat_limit, fill, word_count
from tests.superagent.conftest import make_ticket


@pytest.mark.asyncio
async def test_commands_records_calls_and_parses_json() -> None:
    commands = FakeCommands({"checks": CommandResult(0, '{"a": 1}')})
    result = await commands.run(["gh", "pr", "checks", "1"], cwd="/tmp")
    assert result.ok is True and result.json() == {"a": 1}
    assert commands.argv == [["gh", "pr", "checks", "1"]]
    assert commands.calls[0][1]["cwd"] == "/tmp"


def test_text_helpers() -> None:
    assert word_count("hello world") == 2
    assert chat_limit("one two three", 7) == ("one two", True)
    assert chat_limit("short", 10) == ("short", False)
    assert fill("{name} / {missing}", {"name": "x"}) == "x / {missing}"


def test_worker_environment_has_no_credentials_and_source_vars() -> None:
    ticket: Ticket = make_ticket()
    ticket.execution["directory"] = "/work/t1"
    env = execution_environment(
        ticket,
        variables={"GITHUB_REPOSITORY": "acme/widgets", "GITHUB_NUMBER": "42"},
    ).as_dict()
    assert env["DSH_SUPERAGENT_TICKET"] == "ticket-1"
    assert env["DSH_SUPERAGENT_GITHUB_REPOSITORY"] == "acme/widgets"
    assert env["DSH_SUPERAGENT_GITHUB_NUMBER"] == "42"
    assert not any("TOKEN" in key for key in env)
    assert redact_env({"API_TOKEN": "x", "NAME": "y"}) == {"NAME": "y"}


def test_worker_paths_and_unmanaged_files(tmp_path) -> None:
    paths = state_paths(tmp_path)
    paths.ensure()
    managed = write_worker_environment(paths, gh_cli="/usr/bin/gh", blocked_commands=("kubectl",))
    assert "worker-bin/gh" in managed
    assert "worker-gitconfig/config" in managed
    assert unmanaged_files(paths, managed) == []
    (paths.worker_bin / "rogue").write_text("x", encoding="utf-8")
    assert unmanaged_files(paths, managed) == ["worker-bin/rogue"]
    env = worker_env(make_ticket(), paths=paths)
    assert env["PATH"].startswith(str(paths.worker_bin))
    assert env["GIT_CONFIG_GLOBAL"] == str(paths.worker_gitconfig / "config")
