"""In-memory repository and nested rollback tests."""

from __future__ import annotations

import pytest

from superagent.core.memory import MemoryRepositories
from superagent.core.model import AssignmentStatus
from tests.superagent.conftest import make_ticket, make_trigger


def test_memory_repos_round_trip_and_order() -> None:
    """Saves preserve insertion order in every repository."""
    repos = MemoryRepositories()
    first = make_trigger("trig-1")
    second = make_trigger("trig-2")
    repos.triggers.save(first)
    repos.triggers.save(second)
    assert [item.id for item in repos.triggers.list()] == ["trig-1", "trig-2"]
    assert repos.triggers.get("trig-1") == first

    ticket = make_ticket()
    repos.tickets.save(ticket)
    assert repos.tickets.list_open() == [ticket]
    assert repos.tickets.list_active() == [ticket]


def test_memory_rollback_restores_deep_state() -> None:
    """A failed transaction restores nested objects, not just ids."""
    repos = MemoryRepositories()
    ticket = make_ticket()
    repos.tickets.save(ticket)

    with pytest.raises(RuntimeError):
        with repos.atomic:
            changed = repos.tickets.get(ticket.id)
            assert changed is not None
            changed.assignments[0].status = AssignmentStatus.RUNNING
            repos.tickets.save(changed)
            raise RuntimeError("rollback")

    restored = repos.tickets.get(ticket.id)
    assert restored is not None
    assert restored.assignments[0].status == AssignmentStatus.PENDING


def test_memory_nested_rollback_and_after_commit() -> None:
    """Inner rollback restores one frame; outer commit still fires callbacks."""
    repos = MemoryRepositories()
    calls: list[str] = []
    repos.triggers.save(make_trigger("trig-1"))

    with repos.atomic:
        repos.atomic.after_commit(lambda: calls.append("outer"))
        with pytest.raises(ValueError):
            with repos.atomic:
                repos.atomic.after_commit(lambda: calls.append("discarded"))
                repos.triggers.save(make_trigger("trig-2"))
                raise ValueError("inner rollback")
        assert repos.triggers.exists("trig-2") is False

        with repos.atomic:
            repos.atomic.after_commit(lambda: calls.append("inner-ok"))
            repos.triggers.save(make_trigger("trig-3"))

    assert repos.triggers.exists("trig-3") is True
    assert calls == ["outer", "inner-ok"]
