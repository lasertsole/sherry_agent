"""Unit tests for the E3 todo-continuation enforcer middleware.

Uses the installed langchain 1.3.9 hook contract: ``aafter_agent(state, runtime)``
returning ``None`` (the continuation prompt is delivered through the
fire-and-forget ``maybe_trigger_auto_turn`` path, never a state update).

Hermetic: ``get_todos_sync`` is monkeypatched and ``maybe_trigger_auto_turn`` is
spied; every injected prompt is asserted to arrive as a ``HumanMessage`` whose
text carries the design-doc marker. No real sleeps, no DB writes.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

import agent.middlewares.todo_continuation as tc
from agent.tools.todolist import stagnation_tracker as st

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
    monkeypatch.setattr(tc, "get_todos_sync", lambda session_id: list(todos))


@pytest.fixture(autouse=True)
def _clean() -> None:
    st.reset(_SID)
    yield
    st.reset(_SID)


@pytest.fixture()
def spy_auto_turn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, HumanMessage]]:
    """Capture ``maybe_trigger_auto_turn(session_key, injection)`` calls."""
    calls: list[tuple[str, HumanMessage]] = []

    async def _spy(session_key: str, injection: HumanMessage) -> object:
        calls.append((session_key, injection))
        return object()

    import server.service.auto_turn as auto_turn

    monkeypatch.setattr(auto_turn, "maybe_trigger_auto_turn", _spy)
    return calls


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

    import server.service.auto_turn as auto_turn

    monkeypatch.setattr(auto_turn, "maybe_trigger_auto_turn", _boom)

    result = await tc.TodoContinuationEnforcer().aafter_agent(_state())

    assert result is None
    # Fail-open: a failed injection must not mark the session as injected, so
    # the next turn retries instead of being locked out by a phantom cooldown.
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
