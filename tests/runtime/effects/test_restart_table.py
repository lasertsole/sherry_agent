"""Restart terminal declarations: registration, idempotent delegation, fail-open."""

from __future__ import annotations

import pytest

from server.service.effects import restart_table
from server.service.effects.restart_table import (
    RestartTerminal,
    all_terminals,
    apply_all_terminals,
    apply_queue_terminals,
    apply_subagent_terminals,
)

pytestmark = [pytest.mark.unit]


def test_the_default_table_declares_both_state_machines() -> None:
    names = {spec.name for spec in all_terminals()}
    assert names == {"subagent_runs", "user_input_queue"}


@pytest.mark.asyncio
async def test_apply_all_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok() -> int:
        return 5

    async def boom() -> int:
        raise RuntimeError("broken state machine")

    monkeypatch.setattr(
        restart_table,
        "_RESTART_TERMINALS",
        [
            RestartTerminal("ok", ok, frozenset(), "DONE"),
            RestartTerminal("boom", boom, frozenset(), "DONE"),
        ],
    )

    assert await apply_all_terminals() == {"ok": 5}


@pytest.mark.asyncio
async def test_subagent_apply_delegates_to_restore_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    async def fake_restore() -> None:
        called["n"] += 1

    import agent.tools.subagent.registry.state as state_mod

    monkeypatch.setattr(state_mod, "restore_runs_from_disk", fake_restore)

    assert await apply_subagent_terminals() is None
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_queue_apply_delegates_to_boot_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    async def fake_requeue() -> int:
        called["n"] += 1
        return 3

    import server.service.boot_recovery as boot_mod

    monkeypatch.setattr(boot_mod, "requeue_claimed_inputs", fake_requeue)

    assert await apply_queue_terminals() == 3
    assert called["n"] == 1


def test_register_restart_terminal_adds_an_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart_table, "_RESTART_TERMINALS", [])

    async def apply() -> None:
        return None

    spec = RestartTerminal("future", apply, frozenset({"OPEN"}), "CLOSED")
    restart_table.register_restart_terminal(spec)

    assert all_terminals() == [spec]
