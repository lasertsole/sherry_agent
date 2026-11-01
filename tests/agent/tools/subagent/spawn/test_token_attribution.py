"""Nested-subagent token attribution: every level rolls up to the root turn.

The per-turn budget is a circuit breaker over the main agent plus ALL
descendant subagents. A direct child is attributed to the root session/turn at
spawn; a grandchild must inherit that same attribution instead of restarting
the chain at its immediate parent, and the runtime spawn tool must pass a
nested child's own key (never a double-prefixed one) so depth, containment and
attribution all resolve.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.tools.subagent.spawn.core import _bind_child_run_context
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

ROOT = "root-token-attribution"
CHILD = "agent:main:subagent:token-child"
GRANDCHILD = "agent:main:subagent:token-grandchild"
GREAT_GRANDCHILD = "agent:main:subagent:token-great-grandchild"
_STATE_KEYS = (ROOT, CHILD, GRANDCHILD, GREAT_GRANDCHILD)


def _run(child_key: str, requester_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        child_session_key=child_key,
        requester_session_key=requester_key,
        spawned_by=requester_key,
        thinking=None,
        spawned_cwd=None,
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # The subagent conftest's stub regime may leave pub.func.build_agent_config
    # as a module in a full-directory run; bind a faithful no-op for this test.
    import sys

    import pub.func  # noqa: F401  (ensure the submodule is in sys.modules)

    pub_func = sys.modules["pub.func"]
    monkeypatch.setattr(pub_func, "build_agent_config", lambda *args, **kwargs: {}, raising=False)
    for key in _STATE_KEYS:
        state_register_mem.clear_session(key)
    yield
    for key in _STATE_KEYS:
        state_register_mem.clear_session(key)


def test_every_nesting_level_inherits_the_root_parent_and_turn():
    state_register_mem.set_state(ROOT, StateKey.TOKEN_BUDGET_TURN_ID, "turn-1")

    _bind_child_run_context(_run(CHILD, f"agent:main:session:{ROOT}"))
    child_attr = state_register_mem.get_state(CHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert child_attr == {"session_id": ROOT, "turn_id": "turn-1"}

    _bind_child_run_context(_run(GRANDCHILD, CHILD))
    grandchild_attr = state_register_mem.get_state(GRANDCHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert grandchild_attr == {"session_id": ROOT, "turn_id": "turn-1"}

    _bind_child_run_context(_run(GREAT_GRANDCHILD, GRANDCHILD))
    great_attr = state_register_mem.get_state(GREAT_GRANDCHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert great_attr == {"session_id": ROOT, "turn_id": "turn-1"}


def test_legacy_double_prefixed_requester_still_resolves_to_the_child():
    state_register_mem.set_state(ROOT, StateKey.TOKEN_BUDGET_TURN_ID, "turn-1")
    _bind_child_run_context(_run(CHILD, f"agent:main:session:{ROOT}"))

    _bind_child_run_context(_run(GRANDCHILD, f"agent:main:session:{CHILD}"))

    grandchild_attr = state_register_mem.get_state(GRANDCHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert grandchild_attr == {"session_id": ROOT, "turn_id": "turn-1"}


def test_attribution_is_recorded_without_a_live_turn():
    """Session-level usage is still counted when no turn id exists yet."""
    _bind_child_run_context(_run(CHILD, f"agent:main:session:{ROOT}"))

    attr = state_register_mem.get_state(CHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert attr == {"session_id": ROOT, "turn_id": ""}


def test_descendants_stay_in_the_turn_that_spawned_the_tree():
    """A grandchild inherits its parent's turn even after the root moved on."""
    state_register_mem.set_state(ROOT, StateKey.TOKEN_BUDGET_TURN_ID, "turn-2")
    state_register_mem.set_state(
        CHILD,
        StateKey.TOKEN_BUDGET_PARENT,
        {"session_id": ROOT, "turn_id": "turn-1"},
    )

    _bind_child_run_context(_run(GRANDCHILD, CHILD))

    attr = state_register_mem.get_state(GRANDCHILD, StateKey.TOKEN_BUDGET_PARENT)
    assert attr == {"session_id": ROOT, "turn_id": "turn-1"}


@pytest.mark.asyncio
async def test_nested_spawn_passes_the_child_key_as_requester(monkeypatch):
    import agent.tools.subagent.spawn as spawn_pkg
    from agent.tools.subagent.spawn import privilege
    from agent.tools.subagent.tools import runtime_tools as rt

    captured: dict = {}

    async def _fake_spawn(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="accepted",
            run_id="run-1",
            child_session_key=GRANDCHILD,
            task_name=None,
            note=None,
            error=None,
        )

    monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake_spawn)
    monkeypatch.setattr(privilege, "check_spawn_permission", lambda _sid: (True, ""))

    await rt.sessions_spawn_runtime_tool.ainvoke({"task": "research", "session_id": CHILD})

    assert captured["requester_session_key"] == CHILD


def test_nested_requester_keeps_its_ownership():
    """Ownership must not rewrite a child key into a phantom main session."""
    from agent.tools.subagent.spawn.ownership import resolve_spawn_ownership

    ownership = resolve_spawn_ownership(CHILD)

    assert ownership.controller_session_key == CHILD
    assert ownership.completion_requester_session_key == CHILD
    assert ownership.completion_requester_display_key == CHILD


def test_main_requester_ownership_is_unchanged():
    from agent.tools.subagent.spawn.ownership import resolve_spawn_ownership

    key = f"agent:main:session:{ROOT}"
    ownership = resolve_spawn_ownership(key)

    assert ownership.controller_session_key == key
    assert ownership.completion_requester_session_key == key


@pytest.mark.asyncio
async def test_nested_yield_waits_on_the_childs_own_children(monkeypatch):
    """A nested orchestrator must yield on ITS key, not a prefixed phantom."""
    from agent.tools.subagent.registry import queries
    from agent.tools.subagent.tools import runtime_tools as rt

    seen: list[str] = []

    def _list_runs(key: str):
        seen.append(key)
        return []

    monkeypatch.setattr(queries, "list_runs_for_requester", _list_runs)

    await rt.sessions_yield_runtime_tool.ainvoke({"session_id": CHILD, "timeout_seconds": 0.1})

    assert seen == [CHILD]
