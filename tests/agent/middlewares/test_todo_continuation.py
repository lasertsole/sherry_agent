"""Unit tests for the todo-continuation enforcer middleware.

Uses the installed langchain 1.3.9 hook contract: ``aafter_agent(state, runtime)``
returning ``None`` (the continuation prompt is delivered through the
fire-and-forget ``maybe_trigger_auto_turn`` path, never a state update).

Hermetic: ``store_sqlite.get_todos_sync`` is monkeypatched and the
``maybe_trigger_auto_turn`` runtime hook is replaced by a spy (or deliberately
left unregistered to prove the no-op degradation); every injected prompt is
asserted to arrive as a ``HumanMessage`` whose text carries the design-doc
marker. No real sleeps, no DB writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.todo_continuation.core as tc
from agent.tools.todolist import stagnation_tracker as st
from agent.tools.todolist.registry import store_sqlite as todo_store
from runtime import hooks

pytestmark = [pytest.mark.unit]

_SID = "sess-tc"
_CONTINUATION_MARKER = "[SYSTEM DIRECTIVE: TODO CONTINUATION]"
_RECOVERY_MARKER = "[SYSTEM DIRECTIVE: RECOVERY MODE]"


def _todo(
    content: str,
    status: str,
    *,
    priority: str = "high",
    flow_id: str | None = None,
    step_id: str | None = None,
) -> dict:
    return {
        "content": content,
        "status": status,
        "priority": priority,
        "flow_id": flow_id,
        "step_id": step_id,
    }


def _state(session_id: str = _SID, error: BaseException | None = None) -> dict:
    state: dict[str, Any] = {"session_id": session_id}
    if error is not None:
        state["error"] = error
    return state


def _patch_todos(monkeypatch: pytest.MonkeyPatch, todos: list[dict]) -> None:
    monkeypatch.setattr(todo_store, "get_todos_sync", lambda session_id: list(todos))


def _step(step_id: str, status: str, task: str = "", deps: list[str] | None = None) -> dict:
    return {
        "step_id": step_id,
        "task": task or f"task-{step_id}",
        "depends_on": list(deps or []),
        "status": status,
    }


def _flow(steps: list[dict], *, flow_id: str = "flow-1", status: str = "running") -> dict:
    return {
        "flow_id": flow_id,
        "status": status,
        "state": {"description": f"{flow_id} work", "steps": list(steps)},
    }


def _patch_flows(monkeypatch: pytest.MonkeyPatch, flows: list[dict]) -> None:
    """Seed the TaskFlow half of the plan (``_active_flows`` reads this lazily)."""
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    monkeypatch.setattr(flow_store, "get_active_flows_sync", lambda session_id: list(flows))


@pytest.fixture(autouse=True)
def _no_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Todo-only by default: every existing case keeps its exact behaviour."""
    _patch_flows(monkeypatch, [])


@pytest.fixture(autouse=True)
def _clean() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


@pytest.fixture()
def spy_auto_turn() -> Iterator[list[tuple[str, HumanMessage]]]:
    """Capture ``maybe_trigger_auto_turn(session_key, injection)`` hook calls."""
    calls: list[tuple[str, HumanMessage]] = []

    async def _spy(session_key: str, injection: HumanMessage) -> object:
        calls.append((session_key, injection))
        return object()

    hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _spy)
    yield calls
    hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)


# ============================================================================
# (a) No todos -> reset + no injection
# ============================================================================


@pytest.mark.asyncio
async def test_no_todos_resets_and_does_not_inject(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [])
    st._stagnation_count[_SID] = 2
    st._last_snapshot[_SID] = "stale"

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert spy_auto_turn == []
    assert _SID not in st._stagnation_count


# ============================================================================
# (b) Incomplete -> continuation prompt injected once + mark_injected recorded
# ============================================================================


@pytest.mark.asyncio
async def test_incomplete_injects_continuation_once(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(
        monkeypatch,
        [
            _todo("build api", "pending"),
            _todo("write tests", "in_progress", flow_id="f1", step_id="s2"),
            _todo("ship it", "completed"),
        ],
    )

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert len(spy_auto_turn) == 1
    session_key, injection = spy_auto_turn[0]
    assert session_key == f"agent:main:session:{_SID}"
    assert isinstance(injection, HumanMessage)
    assert _CONTINUATION_MARKER in injection.content
    assert "build api" in injection.content
    assert "write tests" in injection.content
    # Status block reports the remaining count and flow/step association.
    assert "2 remaining" in injection.content
    assert "flow f1 / s2" in injection.content
    # Injector provenance: the chat shows these directives as a neutral system
    # card, never as a message the user sent.
    assert injection.metadata.get("origin") == "todo_continuation"
    # mark_injected recorded -> the session is now inside the cooldown window.
    assert st.is_in_cooldown(_SID) is True


# ============================================================================
# (c) Cooldown suppresses the second injection
# ============================================================================


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_injection(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])
    middleware = tc.TodoContinuationEnforcer()

    await middleware.aafter_agent(_state())
    await middleware.aafter_agent(_state())

    assert len(spy_auto_turn) == 1


# ============================================================================
# (d) Stagnation -> recovery prompt, attempts capped
# ============================================================================


@pytest.mark.asyncio
async def test_stagnation_triggers_recovery_prompt_capped(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])
    middleware = tc.TodoContinuationEnforcer()

    for _ in range(12):
        await middleware.aafter_agent(_state())

    recoveries = [c for c in spy_auto_turn if _RECOVERY_MARKER in c[1].content]
    continuations = [c for c in spy_auto_turn if _CONTINUATION_MARKER in c[1].content]

    assert len(continuations) == 1
    assert len(recoveries) == st._MAX_RECOVERY_ATTEMPTS
    assert st._recovery_attempts[_SID] == st._MAX_RECOVERY_ATTEMPTS
    # Recovery prompts still carry the design-doc status block.
    assert "build api" in recoveries[0][1].content


# ============================================================================
# (e) Abort error -> no injection
# ============================================================================


@pytest.mark.asyncio
async def test_abort_error_skips_injection(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    result = await tc.TodoContinuationEnforcer().aafter_agent(
        _state(error=RuntimeError("operation cancelled"))
    )

    assert result is None
    assert spy_auto_turn == []


@pytest.mark.asyncio
async def test_non_abort_error_still_injects(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    result = await tc.TodoContinuationEnforcer().aafter_agent(
        _state(error=RuntimeError("invalid JSON from model"))
    )

    assert result is None
    assert len(spy_auto_turn) == 1


# ============================================================================
# (f) All done -> reset + no injection
# ============================================================================


@pytest.mark.asyncio
async def test_all_done_resets_and_does_not_inject(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(
        monkeypatch,
        [_todo("a", "completed"), _todo("b", "cancelled")],
    )
    st._stagnation_count[_SID] = 2

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert spy_auto_turn == []
    assert _SID not in st._stagnation_count


# ============================================================================
# (g) auto_turn raising -> swallowed (fail-open)
# ============================================================================


@pytest.mark.asyncio
async def test_auto_turn_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    async def _boom(session_key: str, injection: HumanMessage) -> object:
        raise RuntimeError("auto turn exploded")

    hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _boom)
    try:
        result = await tc.TodoContinuationEnforcer().aafter_agent(_state())
    finally:
        hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

    assert result is None
    # Fail-open: a failed injection must not mark the session as injected, so
    # the next turn retries instead of being locked out by a phantom cooldown.
    assert _SID not in st._last_inject_time


@pytest.mark.asyncio
async def test_missing_hook_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No server assembled: the trigger hook resolves to None -> silent no-op."""
    _patch_todos(monkeypatch, [_todo("build api", "pending")])
    hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    # Nothing injected -> not marked injected -> the next turn retries.
    assert _SID not in st._last_inject_time


# ============================================================================
# (h) Blank session -> no-op
# ============================================================================


@pytest.mark.asyncio
async def test_blank_session_is_noop(monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    result = await tc.TodoContinuationEnforcer().aafter_agent({"session_id": "   "})

    assert result is None
    assert spy_auto_turn == []


@pytest.mark.asyncio
async def test_non_dict_state_is_noop(spy_auto_turn: list) -> None:
    result = await tc.TodoContinuationEnforcer().aafter_agent(None)
    assert result is None
    assert spy_auto_turn == []


# ============================================================================
# (f) The TaskFlow half of the plan: an open board gates the turn too
# ============================================================================


@pytest.mark.asyncio
async def test_open_flow_without_todos_still_gates_the_turn(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    """The reported gap: a plan that lives only on the board was never gated."""
    _patch_todos(monkeypatch, [])
    _patch_flows(
        monkeypatch,
        [
            _flow(
                [
                    _step("step-1", "done"),
                    _step("step-2", "dispatched"),
                    _step("step-3", "blocked", deps=["step-2"]),
                ]
            )
        ],
    )

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    assert _CONTINUATION_MARKER in content
    assert "TaskFlow board (open flows)" in content
    assert "flow-1 [running]" in content
    assert "1/3 steps done" in content
    assert "step-2 [dispatched]" in content
    assert "wave 1" in content and "wave 2" in content
    # The board's next actions are spelled out, not left to guesswork.
    assert "taskflow_wait_all" in content
    assert "taskflow_finish" in content


@pytest.mark.asyncio
async def test_a_settled_but_open_flow_asks_for_closure(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    """Every step green is NOT completion: the flow itself has to be closed."""
    _patch_todos(monkeypatch, [])
    _patch_flows(monkeypatch, [_flow([_step("step-1", "done"), _step("step-2", "done")])])

    await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    assert "still open" in content
    assert "taskflow_finish" in content


@pytest.mark.asyncio
async def test_a_finished_plan_never_nudges(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    """No todos and no non-terminal flow: the gate stays silent (and resets)."""
    _patch_todos(monkeypatch, [])
    _patch_flows(monkeypatch, [])
    st._stagnation_count[_SID] = 3
    st._last_snapshot[_SID] = "stale"

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert spy_auto_turn == []
    assert _SID not in st._stagnation_count


@pytest.mark.asyncio
async def test_todos_and_flows_share_one_directive(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("write the design doc", "pending")])
    _patch_flows(monkeypatch, [_flow([_step("step-1", "ready")])])

    await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    # One directive carries both surfaces: the todo list AND the board.
    assert "write the design doc" in content
    assert "flow-1 [running]" in content
    assert "step-1 [ready]" in content
    assert "1 remaining" in content


@pytest.mark.asyncio
async def test_a_long_flow_is_clipped_in_the_directive(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [])
    steps = [_step(f"step-{i}", "ready") for i in range(1, 26)]
    _patch_flows(monkeypatch, [_flow(steps)])

    await tc.TodoContinuationEnforcer().aafter_agent(_state())

    content = spy_auto_turn[0][1].content
    assert "step-20 [ready]" in content
    assert "step-21 [ready]" not in content
    assert "+5 more" in content


@pytest.mark.asyncio
async def test_a_broken_taskflow_read_degrades_to_the_todo_half(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    """A store error must not break the turn — the gate keeps its todo half."""
    _patch_todos(monkeypatch, [_todo("build api", "pending")])
    from agent.tools.taskflow.registry import store_sqlite as flow_store

    def _boom(session_id: str) -> list[dict]:
        raise RuntimeError("taskflow db is gone")

    monkeypatch.setattr(flow_store, "get_active_flows_sync", _boom)

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    assert "build api" in content
    assert "TaskFlow board" not in content
