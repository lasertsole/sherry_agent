"""Compression-todo reconciliation (job 6): fork a background agent that aligns the todo list with the discarded conversation slice."""

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from loguru import logger
from config.features import SUMMARIZATION
from runtime import StateKey, state_register_mem
from runtime.lane import LaneType, lane_slot
from ._common import _render_discarded_text
from .background_scaffold import _create_background_review_agent


# Post-compression todo reconciliation. The Summarization middleware fires this
# fire-and-forget after a compaction that actually discarded messages and only
# when the session has a non-empty todo list. The runner never blocks or breaks
# compression (fail-open), and a per-session lock prevents overlapping runs.
#
# The lock is written on the MAIN session on purpose: it is the cross-path
# re-entrancy coordinator, so later compressions of the same session observe it.
# Everything else the fork writes lives under the derived session key below.
_COMPRESSION_TODO_LOCK_KEY = StateKey.COMPRESSION_TODO_UPDATE_LOCK


# Metadata marker admitted by the fork's tool gate. The REAL ``todowrite``
# tool carries it (agent/tools/todolist/tools/__init__.py); ``todoread`` does
# not — the fork may only MUTATE the list, it already receives it in the prompt.
_COMPRESSION_TODO_METADATA_KEY = "todo_update"


# Derived session key for the fork's graph: keeps IterationBudget /
# ToolGuardrails / ToolCallNormalize state keys out of the MAIN session's
# namespace (compression runs inside the main agent's awrap_model_call).
_COMPRESSION_TODO_SESSION_SUFFIX = "::compression-todo"


_COMPRESSION_TODO_PROMPT = (
    "The conversation history was just compressed. The request that follows "
    "carries the discarded conversation slice and the current todo list.\n\n"
    "Reconcile the todo list with the ACTUAL progress evidenced in the "
    "discarded slice by calling `todowrite` — it is the only tool available "
    "to you:\n"
    '1. Mark items that were actually finished as "completed" and items that '
    'were abandoned or superseded as "cancelled".\n'
    '2. Add newly discovered work as "pending" items — never invent work that '
    "the discarded slice does not evidence.\n"
    "3. Write the COMPLETE list back in one `todowrite` call (full "
    "replacement, not a delta).\n"
    "4. For items you keep, preserve their existing field semantics exactly: "
    "priority / category / delegation / plan_ref / flow_id / step_id stay as "
    "they were.\n"
    "5. If nothing actually changed, write the list back unchanged — do not "
    "manufacture changes."
)


# Placeholders are substituted with ``str.replace`` (the injected conversation
# may itself contain braces, so ``str.format`` is unsafe here).
_COMPRESSION_TODO_CONTEXT_TEMPLATE = """## Discarded conversation slice (just compacted away)

<discarded_conversation>
{discarded_text}
</discarded_conversation>

## Current todo list (JSON, ordered)

<current_todos>
{todos}
</current_todos>

Update the session todo list now with `todowrite`, passing the complete \
replacement list."""


# asyncio only keeps weak references to tasks; this module-level set keeps the
# fire-and-forget compression-todo task alive until it completes.
_COMPRESSION_TODO_TASKS: set[asyncio.Task[None]] = set()


def _build_main_session_todowrite(main_session_id: str) -> BaseTool:
    """Bound ``todowrite`` shim that always writes the MAIN session's todos.

    The fork graph runs under a derived session key (engine-state isolation), so
    the state-injected real ``todowrite`` would resolve the wrong session and
    could not see the main session at all. This shim reuses the real tool's
    ``args_schema`` and ``description`` verbatim — zero schema drift: the
    injected ``session_id`` (the DERIVED key) is dropped, every other field is
    forwarded to the same action dispatcher the real tool uses, and a future
    field the dispatcher does not accept fails fast instead of being silently
    dropped. It carries the real tool's ``todo_update`` metadata marker and
    delegates with the main session id captured.
    """
    from agent.tools.todolist.tools import build_todolist_tools
    from langchain_core.tools import StructuredTool

    real_todowrite = next(t for t in build_todolist_tools() if t.name == "todowrite")

    async def _todowrite(**kwargs: Any) -> str:
        kwargs.pop("session_id", None)
        from agent.tools.todolist.tools.todowrite import _run_todo_action

        return await _run_todo_action(session_id=main_session_id, **kwargs)

    shim = StructuredTool.from_function(
        coroutine=_todowrite,
        name=real_todowrite.name,
        description=real_todowrite.description,
        args_schema=real_todowrite.args_schema,
    )
    # ``from_function`` strips the description; restore the real tool's exact text.
    shim.description = real_todowrite.description
    shim.metadata = {"scope": "main_only", _COMPRESSION_TODO_METADATA_KEY: True}
    shim.handle_tool_error = True
    return shim


async def update_todos_from_compaction(session_id: str, discarded_messages: Sequence[Any]) -> None:
    """Reconcile the session todo list with a just-discarded compaction slice.

    Runs a dedicated background-review agent whose system prompt is
    :data:`_COMPRESSION_TODO_PROMPT` and whose tool set is exactly the
    metadata-marked ``todowrite`` shim bound to the main session.

    Engine-state isolation: the fork graph runs under the derived session key
    ``f"{session_id}::compression-todo"`` so IterationBudget / ToolGuardrails /
    ToolCallNormalize can never write the MAIN session's state keys while the
    main agent's ``awrap_model_call`` is in flight. The one deliberate exception
    is ``_COMPRESSION_TODO_LOCK_KEY`` (written on the main session as the
    re-entrancy coordinator — see its definition). The fork result messages are
    only logged: nothing from ``res["messages"]`` reaches the main graph or its
    checkpointer, and the fork itself has no checkpointer.

    Fail-open by construction: a missing todo list, an unreadable slice, or any
    agent error is logged and swallowed. The per-session lock is always
    released, and the derived session's middleware state is cleared in
    ``finally`` so per-compression engine state does not accumulate in
    ``state_register_mem``.
    """
    derived_session_id = f"{session_id}{_COMPRESSION_TODO_SESSION_SUFFIX}"
    state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, True)
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
        if not todos:
            logger.debug("compression todo update: no todos for session {}", session_id)
            return

        discarded_text = _render_discarded_text(discarded_messages)
        if not discarded_text.strip():
            logger.debug("compression todo update: empty slice for session {}", session_id)
            return

        context = _COMPRESSION_TODO_CONTEXT_TEMPLATE.replace(
            "{discarded_text}", discarded_text
        ).replace("{todos}", json.dumps(todos, ensure_ascii=False, indent=2))
        async with lane_slot(LaneType.BACKGROUND_REVIEW):
            agent = await _create_background_review_agent(
                _COMPRESSION_TODO_PROMPT,
                allowed_metadata_key=_COMPRESSION_TODO_METADATA_KEY,
                tools=[_build_main_session_todowrite(session_id)],
            )
            res = await agent.ainvoke(
                input={
                    "session_id": derived_session_id,
                    "messages": [HumanMessage(content=context)],
                }
            )
        logger.debug("compression todo update res is {}", res["messages"][-1])
    except Exception:
        logger.exception("compression todo update failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False)
        try:
            state_register_mem.clear_session(derived_session_id)
        except Exception as exc:
            logger.debug(
                "compression todo update: failed to clear derived state for {}: {}",
                derived_session_id,
                exc,
            )


def schedule_compression_todo_update(session_id: str, discarded_messages: Sequence[Any]) -> bool:
    """Gate and fire-and-forget :func:`update_todos_from_compaction`.

    Schedules only when ALL hold: the feature switch is on, the cut actually
    discarded messages, the session has a non-empty todo list, and no update is
    already in flight for this session. Returns True when a task was created.

    Never blocks and never raises: with no running event loop (the sync
    compression path) it logs at debug level and skips; a task failure cannot
    reach the compression result.
    """
    if not SUMMARIZATION["compression_todo_update_enabled"]:
        return False
    if not discarded_messages:
        return False
    if state_register_mem.get_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False):
        return False
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        if not get_todos_sync(session_id):
            return False
    except Exception:
        logger.exception("compression todo update: failed to read todos for {}", session_id)
        return False

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "compression todo update: no running event loop, skipping for session {}",
            session_id,
        )
        return False

    state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, True)
    coro = update_todos_from_compaction(session_id, list(discarded_messages))
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        state_register_mem.set_state(session_id, _COMPRESSION_TODO_LOCK_KEY, False)
        logger.debug(
            "compression todo update: event loop unavailable, skipping for session {}",
            session_id,
        )
        return False
    _COMPRESSION_TODO_TASKS.add(task)
    task.add_done_callback(_COMPRESSION_TODO_TASKS.discard)
    logger.debug("compression todo update scheduled for session {}", session_id)
    return True
