"""Unit tests for the plan-continuation enforcer middleware (todos + TaskFlow flows).

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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

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
    middleware = tc.PlanContinuationEnforcer()

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
    middleware = tc.PlanContinuationEnforcer()

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

    result = await tc.PlanContinuationEnforcer().aafter_agent(
        _state(error=RuntimeError("operation cancelled"))
    )

    assert result is None
    assert spy_auto_turn == []


@pytest.mark.asyncio
async def test_non_abort_error_still_injects(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    result = await tc.PlanContinuationEnforcer().aafter_agent(
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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert spy_auto_turn == []
    assert _SID not in st._stagnation_count


@pytest.mark.asyncio
async def test_all_done_forks_the_plan_extraction(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    """The completion hook is the extraction's ONLY entry point.

    It hangs off this branch on purpose: the plan is complete exactly here, and
    the trigger keeps its own gates (fingerprint, in-flight lock, nudge option),
    so a plan that was already extracted forks nothing.
    """
    import agent.middlewares.summarization.nudges as nudge_mod

    forked: list[str] = []

    async def _record(session_id: str) -> None:
        forked.append(session_id)

    monkeypatch.setattr(nudge_mod, "maybe_trigger_plan_extraction", _record)
    _patch_todos(monkeypatch, [_todo("a", "completed"), _todo("b", "completed")])

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert forked == [_SID]
    assert spy_auto_turn == [], "a finished plan must not arm a continuation turn"


@pytest.mark.asyncio
async def test_an_incomplete_plan_never_forks_the_extraction(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    import agent.middlewares.summarization.nudges as nudge_mod

    forked: list[str] = []
    monkeypatch.setattr(
        nudge_mod, "maybe_trigger_plan_extraction", lambda session_id: forked.append(session_id)
    )
    _patch_todos(monkeypatch, [_todo("a", "completed"), _todo("b", "pending")])

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert forked == []


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
        result = await tc.PlanContinuationEnforcer().aafter_agent(_state())
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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert result is None
    # Nothing injected -> not marked injected -> the next turn retries.
    assert _SID not in st._last_inject_time


# ============================================================================
# (h) Blank session -> no-op
# ============================================================================


@pytest.mark.asyncio
async def test_blank_session_is_noop(monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list) -> None:
    _patch_todos(monkeypatch, [_todo("build api", "pending")])

    result = await tc.PlanContinuationEnforcer().aafter_agent({"session_id": "   "})

    assert result is None
    assert spy_auto_turn == []


@pytest.mark.asyncio
async def test_non_dict_state_is_noop(spy_auto_turn: list) -> None:
    result = await tc.PlanContinuationEnforcer().aafter_agent(None)
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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    assert _CONTINUATION_MARKER in content
    assert "TaskFlow board (open flows)" in content
    assert "flow-1 [running]" in content
    assert "1/3 steps done" in content
    assert "step step-1 (done): task-step-1" in content
    assert "step step-2 (dispatched): task-step-2" in content
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

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert spy_auto_turn == []
    assert _SID not in st._stagnation_count


@pytest.mark.asyncio
async def test_todos_and_flows_share_one_directive(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [_todo("write the design doc", "pending")])
    _patch_flows(monkeypatch, [_flow([_step("step-1", "ready")])])

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    # One directive carries both surfaces: the todo list AND the board.
    assert "write the design doc" in content
    assert "flow-1 [running]" in content
    assert "step step-1 (ready): task-step-1" in content
    assert "1 remaining" in content


@pytest.mark.asyncio
async def test_a_long_flow_is_clipped_in_the_directive(
    monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
) -> None:
    _patch_todos(monkeypatch, [])
    steps = [_step(f"step-{i}", "ready") for i in range(1, 26)]
    _patch_flows(monkeypatch, [_flow(steps)])

    await tc.PlanContinuationEnforcer().aafter_agent(_state())

    content = spy_auto_turn[0][1].content
    assert "step step-20 (ready): task-step-20" in content
    assert "step step-21 (ready)" not in content
    assert "+5 more steps" in content


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

    result = await tc.PlanContinuationEnforcer().aafter_agent(_state())

    assert result is None
    assert len(spy_auto_turn) == 1
    content = spy_auto_turn[0][1].content
    assert "build api" in content
    assert "TaskFlow board" not in content


# ===========================================================================
# todowrite-usage accounting + the tracking nudge (plan: todo_reminder)
# ===========================================================================


class TestTodowriteAccounting:
    def test_turns_advance_and_todowrite_resets(self):
        st.reset("s-usage")
        for _ in range(3):
            st.record_turn("s-usage")
        assert st.turns_since_todowrite("s-usage") == 3

        st.record_todowrite("s-usage")

        assert st.turns_since_todowrite("s-usage") == 0

    def test_reset_clears_the_counter(self):
        st.record_turn("s-usage-reset")
        st.reset("s-usage-reset")

        assert st.turns_since_todowrite("s-usage-reset") == 0

    def test_a_turn_is_detected_by_its_todowrite_tool_call(self):
        from langchain_core.messages import AIMessage

        assert tc._turn_used_todowrite({"messages": [AIMessage(content="")]}) is False
        assert (
            tc._turn_used_todowrite(
                {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[{"name": "todowrite", "args": {}, "id": "c1"}],
                        )
                    ]
                }
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_the_nudge_is_appended_once_the_threshold_is_crossed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """10 turns without todowrite → the continuation directive gains the nudge."""
        sid = "s-nudge"
        _patch_todos(monkeypatch, [_todo("pending work", "pending")])
        st.reset(sid)
        # 9 prior turns + the turn ending now = the 10th turn without todowrite.
        for _ in range(9):
            st.record_turn(sid)

        injected: list[str] = []

        async def _capture(_self, _session_id: str, prompt: str) -> None:
            injected.append(prompt)

        monkeypatch.setattr(tc.PlanContinuationEnforcer, "_inject", _capture)

        await tc.PlanContinuationEnforcer().aafter_agent(_state(sid))

        assert injected, "the continuation directive must fire"
        assert "[REMINDER: TODO TRACKING]" in injected[0]
        assert "10 turns" in injected[0]

    @pytest.mark.asyncio
    async def test_no_nudge_below_the_threshold(self, monkeypatch: pytest.MonkeyPatch):
        sid = "s-nudge-low"
        _patch_todos(monkeypatch, [_todo("pending work", "pending")])
        st.reset(sid)
        for _ in range(3):
            st.record_turn(sid)

        injected: list[str] = []

        async def _capture(_self, _session_id: str, prompt: str) -> None:
            injected.append(prompt)

        monkeypatch.setattr(tc.PlanContinuationEnforcer, "_inject", _capture)

        await tc.PlanContinuationEnforcer().aafter_agent(_state(sid))

        assert injected and "[REMINDER: TODO TRACKING]" not in injected[0]

    @pytest.mark.asyncio
    async def test_a_todowrite_this_turn_resets_the_counter(self, monkeypatch: pytest.MonkeyPatch):
        from langchain_core.messages import AIMessage

        sid = "s-nudge-reset"
        _patch_todos(monkeypatch, [_todo("pending work", "pending")])
        st.reset(sid)
        for _ in range(10):
            st.record_turn(sid)

        injected: list[str] = []

        async def _capture(_self, _session_id: str, prompt: str) -> None:
            injected.append(prompt)

        monkeypatch.setattr(tc.PlanContinuationEnforcer, "_inject", _capture)
        state = _state(sid)
        state["messages"] = [
            AIMessage(content="", tool_calls=[{"name": "todowrite", "args": {}, "id": "c1"}])
        ]

        await tc.PlanContinuationEnforcer().aafter_agent(state)

        assert st.turns_since_todowrite(sid) == 0
        assert injected and "[REMINDER: TODO TRACKING]" not in injected[0]

    @pytest.mark.asyncio
    async def test_recovery_mode_carries_no_nudge(self, monkeypatch: pytest.MonkeyPatch):
        """Recovery already tells the model to run todoread/todowrite — no stacking."""
        sid = "s-nudge-recovery"
        todos = [_todo("pending work", "pending")]
        _patch_todos(monkeypatch, todos)
        st.reset(sid)
        for _ in range(10):
            st.record_turn(sid)
        # Force the stagnation streak to the threshold so recovery takes over.
        rows = [{"content": t_["content"], "status": t_["status"]} for t_ in todos]
        for _ in range(6):
            st.check_stagnation(sid, rows)

        injected: list[str] = []

        async def _capture(_self, _session_id: str, prompt: str) -> None:
            injected.append(prompt)

        monkeypatch.setattr(tc.PlanContinuationEnforcer, "_inject", _capture)

        await tc.PlanContinuationEnforcer().aafter_agent(_state(sid))

        assert injected
        assert "[REMINDER: TODO TRACKING]" not in injected[0], (
            "the recovery directive carries its own todowrite guidance"
        )


# ===========================================================================
# The consecutive-continuation ceiling (turn-level hard cap)
# ===========================================================================

_STOP_MARKER = "[SYSTEM DIRECTIVE: CONTINUATION BUDGET EXHAUSTED]"


def _continuation_turn_state(session_id: str = _SID) -> dict:
    """A turn's state as it looks when the GATE's own directive drove it.

    The injected directive is the last HumanMessage and carries the enforcer's
    origin tag — which is exactly what makes the turn a continuation (and
    therefore what must NOT reset the budget).
    """
    state = _state(session_id)
    state["messages"] = [
        HumanMessage(
            content="[SYSTEM DIRECTIVE: TODO CONTINUATION] keep going",
            metadata={"origin": "todo_continuation", "internal": True},
        )
    ]
    return state


class TestContinuationBudgetState:
    """Pure tracker state: the counter, the latch, and reset()."""

    def test_confirmed_injections_count_and_the_reset_clears(self):
        sid = "s-budget"
        st.reset(sid)
        assert st.continuation_turns(sid) == 0
        for _ in range(3):
            st.mark_injected(sid)

        assert st.continuation_turns(sid) == 3

        st.reset_continuation_turns(sid)

        assert st.continuation_turns(sid) == 0
        assert st.is_stopped(sid) is False

    def test_the_latch_rides_with_the_counter(self):
        sid = "s-budget-latch"
        st.reset(sid)
        st.mark_stopped(sid)
        assert st.is_stopped(sid) is True

        st.reset_continuation_turns(sid)

        assert st.is_stopped(sid) is False

    def test_a_full_reset_clears_the_budget_too(self):
        sid = "s-budget-full"
        st.reset(sid)
        st.mark_injected(sid)
        st.mark_stopped(sid)

        st.reset(sid)

        assert st.continuation_turns(sid) == 0
        assert st.is_stopped(sid) is False

    def test_the_cap_comes_from_the_config(self):
        from config.features import TODOLIST_INFRA

        assert st.continuation_cap() == TODOLIST_INFRA["max_continuation_turns"]
        assert st.continuation_cap() >= 1


class TestContinuationCapEnforcement:
    """The gate's turn-end behaviour around the ceiling."""

    @pytest.mark.asyncio
    async def test_a_confirmed_injection_counts_the_turn(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        _patch_todos(monkeypatch, [_todo("build api", "pending")])

        await tc.PlanContinuationEnforcer().aafter_agent(_state())

        assert len(spy_auto_turn) == 1
        assert st.continuation_turns(_SID) == 1

    @pytest.mark.asyncio
    async def test_an_undelivered_directive_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No trigger registered (no server assembled) → nothing was delivered."""
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

        await tc.PlanContinuationEnforcer().aafter_agent(_state())

        assert st.continuation_turns(_SID) == 0

    @pytest.mark.asyncio
    async def test_a_continuation_turn_keeps_the_count(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        st._continuation_turns[_SID] = 2

        await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())

        assert len(spy_auto_turn) == 1
        assert st.continuation_turns(_SID) == 3

    @pytest.mark.asyncio
    async def test_a_real_user_turn_resets_the_count_and_the_latch(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        st._continuation_turns[_SID] = 5
        st.mark_stopped(_SID)

        # A user turn (no continuation marker): the budget AND the latch clear,
        # so the gate is free to act on the plan again.
        await tc.PlanContinuationEnforcer().aafter_agent(_state())

        assert st.continuation_turns(_SID) == 1, (
            "reset to 0 by the user turn, +1 for this directive"
        )
        assert st.is_stopped(_SID) is False

    @pytest.mark.asyncio
    async def test_a_subagent_completion_turn_resets_the_count(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        """A completion is a NEW event, not a continuation — the budget restarts."""
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        st._continuation_turns[_SID] = 4
        state = _state()
        state["messages"] = [
            HumanMessage(
                content="[subagent:run-1] child finished",
                metadata={"internal": True, "provenance": "subagent_completion"},
            )
        ]

        await tc.PlanContinuationEnforcer().aafter_agent(state)

        assert st.continuation_turns(_SID) == 1

    @pytest.mark.asyncio
    async def test_the_cap_injects_the_stop_directive_and_latches(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        monkeypatch.setattr(st, "continuation_cap", lambda: 3)
        st._continuation_turns[_SID] = 3

        await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())

        assert len(spy_auto_turn) == 1
        content = spy_auto_turn[0][1].content
        assert _STOP_MARKER in content
        assert "3/3 continuation turns" in content
        assert "build api" in content
        assert "stop" in content.lower()
        # Same injector provenance as every other directive.
        assert spy_auto_turn[0][1].metadata.get("origin") == "todo_continuation"
        assert st.is_stopped(_SID) is True
        # The stop directive itself was confirmed delivered → it counts.
        assert st.continuation_turns(_SID) == 4

    @pytest.mark.asyncio
    async def test_the_latched_session_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        st._continuation_turns[_SID] = 4
        st.mark_stopped(_SID)

        await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())

        assert spy_auto_turn == []
        assert st.continuation_turns(_SID) == 4, "silence delivers nothing"

    @pytest.mark.asyncio
    async def test_the_cap_outranks_stagnation_recovery(
        self, monkeypatch: pytest.MonkeyPatch, spy_auto_turn: list
    ) -> None:
        """At the ceiling the stop directive wins; recovery would just extend it."""
        todos = [_todo("build api", "pending")]
        _patch_todos(monkeypatch, todos)
        monkeypatch.setattr(st, "continuation_cap", lambda: 2)
        st._continuation_turns[_SID] = 2
        rows = [{"content": t["content"], "status": t["status"]} for t in todos]
        for _ in range(6):  # saturate the stagnation streak
            st.check_stagnation(_SID, rows)

        await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())

        assert len(spy_auto_turn) == 1
        assert _STOP_MARKER in spy_auto_turn[0][1].content
        assert _RECOVERY_MARKER not in spy_auto_turn[0][1].content

    @pytest.mark.asyncio
    async def test_a_refused_stop_directive_is_retried_not_latched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trigger that raises leaves the latch open — the gate must retry."""
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        monkeypatch.setattr(st, "continuation_cap", lambda: 3)
        st._continuation_turns[_SID] = 3
        calls: list[str] = []

        async def _refuse(session_key: str, injection: HumanMessage) -> object:
            calls.append(session_key)
            raise RuntimeError("session is busy")

        hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _refuse)
        try:
            await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())
        finally:
            hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

        assert calls, "the gate tried to deliver the stop directive"
        assert st.is_stopped(_SID) is False, "a refused stop must stay retryable"
        assert st.continuation_turns(_SID) == 3, "nothing was confirmed delivered"

    @pytest.mark.asyncio
    async def test_a_trigger_that_answers_busy_counts_as_no_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BUSY / ALREADY_PENDING is a refusal, not a delivery (no phantom budget)."""
        _patch_todos(monkeypatch, [_todo("build api", "pending")])
        monkeypatch.setattr(st, "continuation_cap", lambda: 3)
        st._continuation_turns[_SID] = 3

        class _Refused:
            outcome = "already_pending"

        async def _refuse(session_key: str, injection: HumanMessage) -> object:
            return _Refused()

        hooks.register(hooks.MAYBE_TRIGGER_AUTO_TURN, _refuse)
        try:
            await tc.PlanContinuationEnforcer().aafter_agent(_continuation_turn_state())
        finally:
            hooks.unregister(hooks.MAYBE_TRIGGER_AUTO_TURN)

        assert st.is_stopped(_SID) is False, "a refused stop must stay retryable"
        assert st.continuation_turns(_SID) == 3
        assert _SID not in st._last_inject_time, "a refusal must not arm the cooldown"
