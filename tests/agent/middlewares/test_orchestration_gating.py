"""Spike: the Daily preset's tasks-off gating (TaskIntent / reminders / continuation).

The helpers read ``enabled_tool_names`` from the mem state register (the same
source the tool-selection middleware uses); these tests pin the *exact* tool
sets so a session with only ``knowledge`` does not count as orchestration-ready.
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from agent.middlewares.reminder_catalog.sources import _tasks_enabled, _tasks_gated
from agent.middlewares.task_intent.core import TaskIntentMiddleware, _task_steering_enabled
from agent.middlewares.todo_continuation.core import _continuation_enabled
from runtime import StateKey, state_register_mem

DAILY_TOOLS = ["read_file", "terminal", "message_search", "memory", "web_search"]
PARTIAL_TASKS = ["knowledge", "todoread"]
FULL_TOOLS = [
    *DAILY_TOOLS,
    "taskflow_flow",
    "taskflow_step",
    "taskflow_inspect",
    "todowrite",
    "todoread",
    "knowledge",
    "sessions_spawn",
    "sessions_yield",
    "sessions_send",
    "sessions_kill",
    "sessions_steer",
    "agents_list",
    "subagents_list",
    "sessions_messages",
]


def _configure(session_id: str, tools: list[str]) -> str:
    state_register_mem.set_state(session_id, StateKey.AGENT_CONFIG, {"tools": tools})
    return session_id


def test_tasks_and_steering_need_the_full_tool_sets() -> None:
    daily = _configure("spike-gate-daily", DAILY_TOOLS)
    partial = _configure("spike-gate-partial", PARTIAL_TASKS)
    full = _configure("spike-gate-full", FULL_TOOLS)

    assert _tasks_enabled(daily) is False
    assert _tasks_enabled(partial) is False
    assert _tasks_enabled(full) is True
    assert _tasks_enabled("spike-gate-unconfigured") is True  # None = all tools

    assert _task_steering_enabled(daily) is False
    assert _task_steering_enabled(partial) is False
    assert _task_steering_enabled(full) is True
    assert _continuation_enabled(daily) is False
    assert _continuation_enabled(partial) is False
    assert _continuation_enabled(full) is True


def test_task_intent_skips_only_the_steering_for_tasks_off() -> None:
    daily = _configure("spike-gate-intent-daily", DAILY_TOOLS)
    full = _configure("spike-gate-intent-full", FULL_TOOLS)
    middleware = TaskIntentMiddleware()

    async def run(session_id: str):
        return await middleware.abefore_model(
            {
                "messages": [HumanMessage(content="implement the login feature")],
                "session_id": session_id,
            }
        )

    assert asyncio.run(run(daily)) is None
    steering = asyncio.run(run(full))
    assert steering is not None
    assert "TASK INTENT DETECTED" in steering["messages"][0].content


def test_reminder_wrapper_is_scoped_to_its_builder() -> None:
    daily = _configure("spike-gate-reminder-daily", DAILY_TOOLS)
    full = _configure("spike-gate-reminder-full", FULL_TOOLS)
    gated = _tasks_gated(lambda _session_id: "TODO")

    assert gated(daily, {}) is None
    assert gated(full, {}) == "TODO"
