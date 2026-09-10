"""E3 todo-continuation enforcer — a turn-end ``after_agent`` middleware.

When a turn ends with incomplete todos, the enforcer injects a continuation
directive that pulls the model back to work. Delivery reuses the existing
fire-and-forget infrastructure in ``server/service/auto_turn.py``: the prompt is
handed to ``maybe_trigger_auto_turn`` as a ``HumanMessage`` and injected on the
next idle turn — the middleware itself never mutates state.

Tailored from omo ``todo-continuation-enforcer``:

- idle detection → ``auto_turn`` + ``detect_state`` (already present);
- 2.5s countdown → ``auto_turn`` fire-and-forget (already present);
- stagnation/backoff/abort/recovery → ``agent/tools/todolist/stagnation_tracker``;
- user-takeover / assistant-activity cancellation → ``auto_turn`` internals.

Hook contract (installed ``langchain 1.3.9``): ``aafter_agent(state, runtime)``
returns ``None``; it is NOT a state update. Registered FIRST in the middleware
list because after_agent hooks run in REVERSE list order — first registered runs
LAST (truly at the end of the turn).

Hard rules honored: blank/missing session → no-op; abort-class turn error → no
continuation; any internal failure is swallowed (log + return None) so the
enforcer can never break a turn.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from loguru import logger

from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from agent.tools.todolist.stagnation_tracker import (
    check_stagnation,
    enter_recovery,
    is_abort_error,
    is_in_cooldown,
    mark_injected,
    reset,
    should_enter_recovery,
)

__all__ = ["TodoContinuationEnforcer"]

# Continuation directive (verbatim from TODO/TODOLIST_PLAN.md §9 E3).
_CONTINUATION_PROMPT = """[SYSTEM DIRECTIVE: TODO CONTINUATION]

Incomplete tasks remain in your todo list. Continue working on the next pending task.

- Proceed without asking for permission
- Mark each task complete when finished
- Do not stop until all tasks are done
- If you believe all work is complete, the system is questioning your completion claim.
  Critically re-examine each todo item, verify the work was actually done, and update accordingly.

{todo_status}"""

# Recovery directive (verbatim from TODO/TODOLIST_PLAN.md §9 E3).
_RECOVERY_PROMPT = """[SYSTEM DIRECTIVE: RECOVERY MODE]

Stagnation detected — the todo list has not changed across multiple continuation attempts.

Recovery actions:
1. Run todoread to see the exact current state
2. For each incomplete task, ask: "Is this actually done but unmarked, or genuinely incomplete?"
3. If done: mark completed via todowrite (verify first — Sisyphus contract)
4. If incomplete: re-plan the task — break it down differently or delegate to a subagent
5. If blocked: mark cancelled and note the blocker

Do NOT repeat the same actions that led to stagnation.

{todo_status}"""

_INCOMPLETE_STATUSES = ("pending", "in_progress")

# The turn error is recorded by the runtime under one of these optional state
# keys; none exist in the base AgentState schema, so absence is the normal case.
_ERROR_STATE_KEYS = ("error", "turn_error", "exception")


def _build_status_block(todos: list[dict]) -> str:
    """Render the design-doc status block: counts + per-todo icons."""
    done = sum(1 for t in todos if t["status"] in ("completed", "cancelled"))
    total = len(todos)
    remaining = [t for t in todos if t["status"] in _INCOMPLETE_STATUSES]
    lines = [f"[Status: {done}/{total} completed, {len(remaining)} remaining]"]
    lines.append("Remaining tasks:")
    for todo in remaining:
        icon = {"pending": "○", "in_progress": "◐"}.get(todo["status"], "○")
        step_ref = ""
        if todo.get("flow_id") and todo.get("step_id"):
            step_ref = f" (flow {todo['flow_id']} / {todo['step_id']})"
        lines.append(f"- [{icon}] {todo['content']} ({todo['priority']}){step_ref}")
    return "\n".join(lines)


def _turn_error(state: Any) -> BaseException | None:
    """Extract a turn-end exception from optional state keys, if recorded."""
    if not isinstance(state, dict):
        return None
    for key in _ERROR_STATE_KEYS:
        value = state.get(key)
        if isinstance(value, BaseException):
            return value
    return None


class TodoContinuationEnforcer(AgentMiddleware):
    """Turn-end enforcer: inject a continuation prompt when todos remain.

    Registered in ``agent/core.py`` FIRST in the middleware list, so it runs
    LAST at turn end (after_agent hooks execute in reverse list order).
    """

    async def aafter_agent(self, state, runtime=None) -> None:
        """Inspect the finished turn; fire-and-forget a continuation if needed."""
        try:
            if not isinstance(state, dict):
                return None
            session_id = str(state.get("session_id") or "")
            if not session_id.strip():
                logger.debug("todo continuation: no session_id in state; skipping")
                return None

            # Abort-class failures (user cancel / timeout) must never continue.
            error = _turn_error(state)
            if error is not None and is_abort_error(error):
                logger.info("todo continuation: abort error for session {}; skipping", session_id)
                return None

            todos = get_todos_sync(session_id)
            if not todos:
                reset(session_id)
                return None

            incomplete = [t for t in todos if t["status"] in _INCOMPLETE_STATUSES]
            if not incomplete:
                reset(session_id)
                return None

            # Stagnation: unchanged across N attempts → recovery or give up.
            if check_stagnation(session_id, todos):
                if should_enter_recovery(session_id):
                    enter_recovery(session_id)
                    logger.info(
                        "todo continuation: recovery mode for session {} ({} remaining)",
                        session_id,
                        len(incomplete),
                    )
                    await self._inject(
                        session_id, _RECOVERY_PROMPT.format(todo_status=_build_status_block(todos))
                    )
                return None

            if is_in_cooldown(session_id):
                logger.debug("todo continuation: session {} in cooldown; skipping", session_id)
                return None

            logger.info(
                "todo continuation: injecting continuation for session {} ({} remaining)",
                session_id,
                len(incomplete),
            )
            await self._inject(
                session_id, _CONTINUATION_PROMPT.format(todo_status=_build_status_block(todos))
            )
            return None
        except Exception:
            logger.exception("todo continuation: failed; continuing without injection")
            return None

    async def _inject(self, session_id: str, prompt: str) -> None:
        """Fire-and-forget the prompt; record the injection only on success."""
        try:
            from server.service.auto_turn import maybe_trigger_auto_turn

            session_key = f"agent:main:session:{session_id}"
            await maybe_trigger_auto_turn(session_key, HumanMessage(content=prompt))
            mark_injected(session_id)
        except Exception:
            logger.exception(
                "todo continuation: auto_turn injection failed for session {}; "
                "leaving the session retryable",
                session_id,
            )
