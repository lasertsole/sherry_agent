"""Shared polling and admission-context tests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from superagent.infra.commands import CommandResult, FakeCommands
from superagent.infra.health import HealthBoard
from superagent.sources.admission_context import AdmissionContext
from superagent.sources.github import GithubSource
from superagent.sources.github_trigger import GithubConfig
from superagent.sources.poll import cursor_at, polled, poll_threads
from superagent.sources.registry import SourceRegistry
from tests.superagent.conftest import make_trigger


def test_cursor_skew_and_horizon() -> None:
    assert cursor_at("2026-11-28T00:00:10+00:00") == "2026-11-28T00:00:05+00:00"
    now = "2026-11-28T00:00:00+00:00"
    assert polled(True, None, None, now) is True
    assert polled(False, "2026-11-27T00:00:00+00:00", None, now) is True
    assert polled(False, "2026-11-01T00:00:00+00:00", None, now) is False
    assert polled(False, None, None, now) is False


@dataclass
class Thread:
    cursor: str | None = None


@pytest.mark.asyncio
async def test_poll_threads_continues_after_one_error() -> None:
    errors: list[str] = []

    async def read_one(thread: Thread, cursor: str | None) -> dict[str, object]:
        if thread.cursor is None:
            raise RuntimeError("bad thread")
        return {"cursor": cursor}

    results = await poll_threads(
        [Thread(), Thread("c2")], read_one, on_error=lambda _t, e: errors.append(e)
    )
    assert results == [{"cursor": "c2"}]
    assert errors == ["bad thread"]


@pytest.mark.asyncio
async def test_admission_context_bounds_window_and_reads_stub() -> None:
    config = GithubConfig(repository="acme/widgets", cli="gh", observe_issues=True)
    source = GithubSource(config.model_dump())
    commands = FakeCommands(
        {
            "issues/42/comments": CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "id": "m1",
                            "user": {"login": "alice"},
                            "created_at": "t1",
                            "body": "first",
                        },
                        {
                            "id": "m2",
                            "user": {"login": "bob"},
                            "created_at": "t2",
                            "body": "second",
                        },
                    ]
                ),
            )
        }
    )
    registry = SourceRegistry(commands, HealthBoard(), config_get=lambda _kind: config.model_dump())
    registry.register(source)
    trigger = make_trigger()
    trigger = trigger.__class__(
        **{**trigger.__dict__, "meta": {"message_id": "m2", "number": "42"}}
    )
    window = await AdmissionContext(registry).build(trigger)
    assert [message.id for message in window.first_five] == ["m1", "m2"]
    assert [message.id for message in window.around] == ["m1", "m2"]
    assert window.other == []


@pytest.mark.asyncio
async def test_admission_context_degrades_on_error() -> None:
    registry = SourceRegistry(FakeCommands({"comments": RuntimeError("offline")}), HealthBoard())
    registry.register(GithubSource())
    trigger = make_trigger()
    window = await AdmissionContext(registry).build(trigger)
    assert window.status == "pollError"
    assert window.all_messages() == []
